"""Full-data, time-ordered contextual model for synthetic bank transfers.

The model predicts the AI Hub synthetic 이상거래여부 label.  It is not a real
fraud decision system.  Row-level matrices and account identifiers remain in
the ignored data/ directory; reports contain aggregate measures only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import sklearn
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score
from model_artifact import TRUSTED_TYPES

from bank_context_features import (
    CATEGORICAL_FIELDS, CATEGORICAL_INDICES, FEATURE_NAMES, HISTORY_DAYS,
    SCHEMA_VERSION, LifetimeState, feature_snapshot, history_base, normalize_transaction,
    summary_from_base, vector,
)
from train_card_baseline import DATA, ROOT, metrics, period


CATEGORY = "전자금융공동망"
FIT_END = "20230930"
TUNE_END = "20231231"
EVAL_YEAR = "2024"
CACHE = DATA / ".bank-context-cache"
ARTIFACT_PATH = ROOT / "models" / "bank_contextual_v1.skops"
REPORT_PATH = ROOT / "reports" / "bank_contextual_v1.json"
CANDIDATES = [
    {"max_leaf_nodes": 31, "min_samples_leaf": 40, "l2_regularization": 1.0, "max_iter": 150},
    {"max_leaf_nodes": 15, "min_samples_leaf": 100, "l2_regularization": 5.0, "max_iter": 150},
    {"max_leaf_nodes": 63, "min_samples_leaf": 80, "l2_regularization": 10.0, "max_iter": 150},
    {"max_leaf_nodes": 63, "min_samples_leaf": 100, "l2_regularization": 25.0, "max_iter": 300},
    {"max_leaf_nodes": 31, "min_samples_leaf": 150, "l2_regularization": 25.0, "max_iter": 300},
    {"max_leaf_nodes": 63, "min_samples_leaf": 200, "l2_regularization": 50.0, "max_iter": 400},
    {"max_leaf_nodes": 31, "min_samples_leaf": 100, "l2_regularization": 10.0, "max_iter": 400, "learning_rate": 0.05},
    {"max_leaf_nodes": 127, "min_samples_leaf": 100, "l2_regularization": 25.0, "max_iter": 300},
]
SQL_COLUMNS = (
    "event_date", "split", "sender", "recipient", "sender_bank", "recipient_bank",
    "fund", "amount", "hour", "channel", "label", "anomaly_type", "quarter",
)


def files() -> list[tuple[str, Path]]:
    selected = []
    for split in ("training", "validation"):
        for path in sorted((DATA / split / CATEGORY).glob("*.csv")):
            if (2021, 3) <= period(path) <= (2024, 4):
                selected.append((split, path))
    if len(selected) != 28:
        raise FileNotFoundError("Expected 14 training and 14 validation quarterly bank CSVs")
    return selected


def source_fingerprint(paths: list[tuple[str, Path]]) -> list[dict]:
    # File size and content digest make the derived cache reproducible without
    # exposing any licensed source rows in a public report.
    manifest = []
    for split, path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest.append({"split": split, "file": path.name, "bytes": path.stat().st_size,
                         "sha256": digest.hexdigest()})
    return manifest


def prepare_stage(path: Path, sources: list[tuple[str, Path]]) -> dict:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("""CREATE TABLE events (
        event_date TEXT NOT NULL, split TEXT NOT NULL, sender TEXT NOT NULL,
        recipient TEXT NOT NULL, sender_bank TEXT NOT NULL, recipient_bank TEXT NOT NULL,
        fund TEXT NOT NULL, amount REAL NOT NULL, hour INTEGER NOT NULL,
        channel TEXT NOT NULL, label INTEGER NOT NULL, anomaly_type TEXT NOT NULL,
        quarter TEXT NOT NULL
    )""")
    counts = defaultdict(lambda: {"rows": 0, "positives": 0})
    for split, path in sources:
        quarter = f"{period(path)[0]}Q{period(path)[1]}"
        batch = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            for row in reader:
                event = normalize_transaction(row, exact_fields=False)
                label = row["이상거래여부"]
                if label not in ("0", "1"):
                    raise ValueError(f"Invalid label in {path.name}")
                batch.append((
                    event["거래일자"], split, event["출금계좌일련번호"], event["입금계좌일련번호"],
                    event["출금금융회사일련번호"], event["입금금융회사일련번호"],
                    event["자금구분"], event["거래금액"], event["거래시간대"], event["매체구분"],
                    int(label), row["이상거래유형"], quarter,
                ))
                counts[f"{split}/{quarter}"]["rows"] += 1
                counts[f"{split}/{quarter}"]["positives"] += int(label)
                if len(batch) >= 10_000:
                    connection.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                    batch.clear()
            if batch:
                connection.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        connection.commit()
    connection.execute("CREATE INDEX idx_events_date ON events(event_date)")
    connection.commit()
    connection.close()
    return dict(counts)


def target_split(row: dict) -> str | None:
    date = row["event_date"]
    if row["split"] == "training" and date <= FIT_END:
        return "fit"
    if row["split"] == "training" and FIT_END < date <= TUNE_END:
        return "tune"
    if row["split"] == "validation" and date.startswith(EVAL_YEAR):
        return "validation"
    return None


def row_event(row: dict) -> dict:
    return {
        "출금계좌일련번호": row["sender"], "입금계좌일련번호": row["recipient"],
        "출금금융회사일련번호": row["sender_bank"],
        "입금금융회사일련번호": row["recipient_bank"],
        "자금구분": row["fund"], "거래금액": row["amount"],
        "거래시간대": row["hour"], "매체구분": row["channel"],
        "거래일자": row["event_date"],
        "date": datetime.strptime(row["event_date"], "%Y%m%d").date(),
    }


def matrix_paths(cache: Path, name: str) -> tuple[Path, Path]:
    return cache / f"x_{name}.npy", cache / f"y_{name}.npy"


def build_matrices(stage: Path, cache: Path) -> tuple[dict, dict, dict]:
    connection = sqlite3.connect(stage)
    connection.row_factory = sqlite3.Row
    counts = {
        "fit": connection.execute("SELECT COUNT(*) FROM events WHERE split='training' AND event_date<=?", (FIT_END,)).fetchone()[0],
        "tune": connection.execute("SELECT COUNT(*) FROM events WHERE split='training' AND event_date>? AND event_date<=?", (FIT_END, TUNE_END)).fetchone()[0],
        "validation": connection.execute("SELECT COUNT(*) FROM events WHERE split='validation' AND event_date LIKE '2024%'").fetchone()[0],
    }
    arrays = {}
    for name, count in counts.items():
        xp, yp = matrix_paths(cache, name)
        arrays[name] = (
            np.lib.format.open_memmap(xp, mode="w+", dtype=np.float32, shape=(count, len(FEATURE_NAMES))),
            np.lib.format.open_memmap(yp, mode="w+", dtype=np.uint8, shape=(count,)),
        )
    type_array = np.lib.format.open_memmap(cache / "type_validation.npy", mode="w+", dtype="S16", shape=(counts["validation"],))
    quarter_array = np.lib.format.open_memmap(cache / "quarter_validation.npy", mode="w+", dtype="S8", shape=(counts["validation"],))
    maps = {name: {} for name in CATEGORICAL_FIELDS}
    indices = dict.fromkeys(counts, 0)
    histories = defaultdict(deque)
    lifetimes = defaultdict(LifetimeState)
    max_history = 0
    max_lifetime = 0
    cursor = connection.execute("SELECT * FROM events ORDER BY event_date, rowid")
    for date_key, group in itertools.groupby(cursor, key=lambda r: r["event_date"]):
        daily = [dict(row) for row in group]
        day = datetime.strptime(date_key, "%Y%m%d").date()
        lower = day - timedelta(days=HISTORY_DAYS)
        # Bound memory even for accounts that never appear again.
        for old_sender in list(histories):
            old_history = histories[old_sender]
            while old_history and old_history[0]["date"] < lower:
                old_history.popleft()
            if not old_history:
                del histories[old_sender]
        grouped = defaultdict(list)
        for row in daily:
            grouped[row["sender"]].append(row)
        for sender, sender_rows in grouped.items():
            history = histories[sender]
            tx0 = row_event(sender_rows[0])
            base = history_base(tx0, history)
            max_history = max(max_history, base["count_90"])
            max_lifetime = max(max_lifetime, lifetimes[sender].count)
            for row in sender_rows:
                name = target_split(row)
                if name is None:
                    continue
                tx = row_event(row)
                snapshot = feature_snapshot(
                    tx, summary_from_base(tx, base), maps, fit=name == "fit",
                    prior_summary=lifetimes[sender].for_transaction(tx),
                )
                idx = indices[name]
                x, y = arrays[name]
                x[idx] = vector(snapshot)
                y[idx] = row["label"]
                if name == "validation":
                    type_array[idx] = row["anomaly_type"].encode("ascii")
                    quarter_array[idx] = row["quarter"].encode("ascii")
                indices[name] += 1
        # Make the day's transactions visible only to *future* dates.
        for row in daily:
            event = row_event(row)
            histories[row["sender"]].append(event)
            lifetimes[row["sender"]].add(event)
    for name, count in counts.items():
        if indices[name] != count:
            raise RuntimeError(f"{name}: expected {count} rows, wrote {indices[name]}")
        for array in arrays[name]:
            array.flush()
    type_array.flush()
    quarter_array.flush()
    connection.close()
    return counts, maps, {"max_90d_history_rows": max_history,
                          "max_lifetime_history_rows": max_lifetime}


def f1_threshold(y_true: np.ndarray, scores: np.ndarray, max_alert_rate: float = 0.02) -> dict:
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
    sorted_scores = np.sort(scores)
    alert_rates = (len(scores) - np.searchsorted(sorted_scores, thresholds, side="left")) / len(scores)
    valid = np.flatnonzero(alert_rates <= max_alert_rate)
    if not len(valid):
        raise ValueError("No threshold meets the alert-rate cap")
    chosen = valid[np.argmax(f1[valid])]
    return {"threshold": float(thresholds[chosen]), "dev_f1": float(f1[chosen]),
            "dev_alert_rate": float(alert_rates[chosen]), "max_alert_rate": max_alert_rate}


def at_alert_count(y: np.ndarray, scores: np.ndarray, count: int) -> dict:
    count = min(max(0, count), len(scores))
    order = np.argsort(scores, kind="stable")[-count:] if count else np.array([], dtype=int)
    tp = int(y[order].sum())
    return {"alerts": count, "tp": tp, "fp": count - tp,
            "precision": tp / count if count else 0.0,
            "recall": tp / int(y.sum()) if int(y.sum()) else 0.0}


def cohort_report(y: np.ndarray, scores: np.ndarray, threshold: float, mask: np.ndarray) -> dict:
    if not np.any(mask):
        return {"rows": 0, "positives": 0}
    if len(np.unique(y[mask])) < 2:
        predicted = scores[mask] >= threshold
        positives = int(y[mask].sum())
        alerts = int(predicted.sum())
        tp = int(np.count_nonzero(predicted & (y[mask] == 1)))
        return {"rows": int(mask.sum()), "positives": positives, "average_precision": None,
                "roc_auc": None, "threshold": threshold, "alert_rate": alerts / int(mask.sum()),
                "precision": tp / alerts if alerts else 0.0,
                "recall": tp / positives if positives else 0.0,
                "confusion": {"tn": int(mask.sum()) - positives - (alerts - tp),
                              "fp": alerts - tp, "fn": positives - tp, "tp": tp}}
    return metrics(y[mask], scores[mask], threshold)


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> list[float] | None:
    if trials <= 0:
        return None
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    radius = z * np.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)) / denominator
    return [float(max(0, center - radius)), float(min(1, center + radius))]


def add_intervals(result: dict) -> dict:
    if "confusion" in result:
        matrix = result["confusion"]
        result["precision_wilson_95"] = wilson_interval(matrix["tp"], matrix["tp"] + matrix["fp"])
        result["recall_wilson_95"] = wilson_interval(matrix["tp"], matrix["tp"] + matrix["fn"])
    return result


def calibration_bins(y: np.ndarray, scores: np.ndarray) -> dict:
    edges = np.asarray([0.0, 0.001, 0.01, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 0.95, 1.0000001])
    bins = []
    ece = 0.0
    for left, right in zip(edges[:-1], edges[1:]):
        mask = (scores >= left) & (scores < right)
        count = int(mask.sum())
        if not count:
            continue
        mean_score = float(scores[mask].mean())
        observed = float(y[mask].mean())
        ece += count / len(y) * abs(mean_score - observed)
        bins.append({"score_from": float(left), "score_to": float(min(right, 1.0)),
                     "rows": count, "positives": int(y[mask].sum()),
                     "mean_model_score": mean_score, "observed_label_rate": observed,
                     "observed_label_rate_wilson_95": wilson_interval(int(y[mask].sum()), count)})
    return {"brier": float(brier_score_loss(y, scores)), "expected_calibration_error": float(ece),
            "fixed_score_bins": bins,
            "interpretation": "Model score is not a verified fraud probability; calibration is retrospective on synthetic labels."}


def validation_source_rows(stage: Path) -> list[dict]:
    """Reproduce the date/sender extraction order including unlabeled source rows."""
    connection = sqlite3.connect(stage)
    connection.row_factory = sqlite3.Row
    cursor = connection.execute("SELECT * FROM events WHERE event_date LIKE '2024%' ORDER BY event_date,rowid")
    selected = []
    for _, daily in itertools.groupby(cursor, key=lambda row: row["event_date"]):
        grouped = defaultdict(list)
        for row in daily:
            grouped[row["sender"]].append(dict(row))
        for sender_rows in grouped.values():
            selected.extend(row for row in sender_rows if target_split(row) == "validation")
    connection.close()
    return selected


def raw_validation_amounts(stage: Path, y: np.ndarray) -> np.ndarray:
    """Use exact source amounts for equal-amount audits, not rounded float32 logs."""
    rows = validation_source_rows(stage)
    amounts = np.asarray([row["amount"] for row in rows], dtype=np.float64)
    labels = np.asarray([row["label"] for row in rows], dtype=np.uint8)
    if not np.array_equal(labels, y):
        raise RuntimeError("Validation source order does not match feature matrix")
    return amounts


def threshold_tradeoffs(y_tune, tune_scores, y_val, val_scores) -> dict:
    policies = {}
    for budget in (0.001, 0.005, 0.01, 0.02, 0.05):
        chosen = f1_threshold(y_tune, tune_scores, budget)
        threshold = chosen["threshold"]
        policies[f"dev_max_alert_rate_{budget}"] = {
            **chosen,
            "development": add_intervals(metrics(y_tune, tune_scores, threshold)),
            "retrospective_validation": add_intervals(metrics(y_val, val_scores, threshold)),
        }
    precision, recall, thresholds = precision_recall_curve(y_tune, tune_scores)
    for target in (0.7, 0.8, 0.9, 0.95):
        eligible = np.flatnonzero(precision[:-1] >= target)
        chosen = int(eligible[np.argmax(recall[eligible])]) if len(eligible) else None
        if chosen is None:
            policies[f"dev_precision_at_least_{target}"] = {"feasible": False}
            continue
        threshold = float(thresholds[chosen])
        policies[f"dev_precision_at_least_{target}"] = {
            "feasible": True, "threshold": threshold,
            "development": add_intervals(metrics(y_tune, tune_scores, threshold)),
            "retrospective_validation": add_intervals(metrics(y_val, val_scores, threshold)),
        }
    return {"selection_data": "2023Q4 training only", "policies": policies,
            "interpretation": "Development targets do not guarantee the same precision or alert rate after distribution shift."}


def source_quality_audit(stage: Path) -> dict:
    connection = sqlite3.connect(stage)
    by_label = connection.execute("SELECT label,anomaly_type,COUNT(*) FROM events GROUP BY label,anomaly_type").fetchall()
    repeats = connection.execute(
        "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM events "
        "GROUP BY event_date,sender,recipient,sender_bank,recipient_bank,fund,amount,hour,channel,label HAVING n>1)"
    ).fetchone()[0]
    conflicts = connection.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM events GROUP BY event_date,sender,recipient,sender_bank,recipient_bank,fund,amount,hour,channel "
        "HAVING MIN(label)<>MAX(label))"
    ).fetchone()[0]
    support = connection.execute(
        "SELECT split,label,COUNT(*),MIN(amount),MAX(amount),SUM(CASE WHEN amount<2000000 THEN 1 ELSE 0 END) "
        "FROM events GROUP BY split,label"
    ).fetchall()
    connection.close()
    return {"source_label_counts": [{"label": label, "anomaly_type": typ, "rows": count}
                                    for label, typ, count in by_label],
            "amount_label_support": [{"split": split, "label": label, "rows": count,
                                      "min_amount": minimum, "max_amount": maximum,
                                      "below_2m_rows": below} for split, label, count, minimum, maximum, below in support],
            "repeated_raw_and_label_rows": repeats, "conflicting_raw_input_label_groups": conflicts,
            "duplicate_policy": "Retained: no unique transaction ID, so identical date and three-hour-bucket inputs may be repeated real events in the synthetic source.",
            "ordering_limit": "No reliable event order within a day; all same-day events excluded from historical features.",
            "evaluation_limit": "2024 has been examined previously; this is a retrospective synthetic-label evaluation, not a blinded real-world test."}


def repeated_input_cohorts(stage: Path, y, scores, threshold) -> dict:
    """Audit exact nine-input repeats without mistaking coarse timestamps for IDs."""
    connection = sqlite3.connect(stage)
    columns = "event_date,sender,recipient,sender_bank,recipient_bank,fund,amount,hour,channel"
    column_names = columns.split(",")
    ordered = validation_source_rows(stage)
    validation_rows = [tuple(row[name] for name in column_names) for row in ordered]
    if len(validation_rows) != len(y):
        raise RuntimeError("Duplicate audit order does not match validation matrix")
    within_validation = Counter(validation_rows)
    training_matches = Counter()
    for row in connection.execute(f"SELECT {columns} FROM events WHERE split='training' AND event_date LIKE '2024%'"):
        if row in within_validation:
            training_matches[row] += 1
    connection.close()
    within_mask = np.asarray([within_validation[row] > 1 for row in validation_rows])
    training_mask = np.asarray([training_matches[row] > 0 for row in validation_rows])
    repeated = within_mask | training_mask
    return {
        "key": "all nine current raw fields including date; labels excluded",
        "fit_to_development_exact_input_overlap": 0,
        "fit_to_validation_exact_input_overlap": 0,
        "development_to_validation_exact_input_overlap": 0,
        "overlap_basis": "Exact key includes date. Fit ends 2023-09-30, development is 2023Q4, validation is 2024: disjoint date ranges prohibit exact-key overlap.",
        "validation_rows_with_repeated_validation_inputs": int(within_mask.sum()),
        "validation_rows_matching_2024_training_inputs": int(training_mask.sum()),
        "unlabeled_context_overlap_limit": "2024 training labels are never fitted or selected; matching raw input can still recur in coarse source timestamps. Same-day history is excluded.",
        "cohorts": {"repeated_exact_raw_inputs": add_intervals(cohort_report(y, scores, threshold, repeated)),
                    "nonrepeated_exact_raw_inputs": add_intervals(cohort_report(y, scores, threshold, ~repeated))},
    }


def development_group_permutation(model, x: np.ndarray, y: np.ndarray, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    sample = np.sort(rng.choice(len(x), size=min(100_000, len(x)), replace=False))
    sampled_x = np.asarray(x[sample], dtype=np.float32)
    sampled_y = np.asarray(y[sample], dtype=np.uint8)
    reference_ap = float(average_precision_score(sampled_y, model.predict_proba(sampled_x)[:, 1]))
    groups = {
        "amount_derived": ("amount_log", "amount_to_30d_mean", "amount_to_90d_mean",
                           "amount_to_90d_max", "amount_to_all_mean", "amount_to_all_max"),
        "recent_history": tuple(name for name in FEATURE_NAMES if "90d" in name or "30d" in name or "7d" in name),
        "lifetime_history": tuple(name for name in FEATURE_NAMES if "_all_" in name or name in
                                  ("days_since_first_sender", "days_since_last_sender_all", "days_since_last_recipient_all")),
        "transaction_context": ("time_bucket", "fund_code", "channel_code", "same_institution", "day_of_week"),
    }
    drops = {}
    for group, names in groups.items():
        indices = [FEATURE_NAMES.index(name) for name in names]
        permuted = sampled_x.copy()
        order = rng.permutation(len(permuted))
        permuted[:, indices] = sampled_x[order][:, indices]
        ap = float(average_precision_score(sampled_y, model.predict_proba(permuted)[:, 1]))
        drops[group] = {"feature_names": names, "permuted_ap": ap, "ap_drop": reference_ap - ap}
    return {"source": "2023Q4 training holdout only; 100000-row random diagnostic sample",
            "rows": len(sample), "positives": int(sampled_y.sum()), "reference_ap": reference_ap,
            "groups": drops,
            "interpretation": "Permutation changes model inputs jointly; it is global sensitivity, not an individual causal reason."}


def same_amount_discrimination(y: np.ndarray, amount: np.ndarray,
                               candidate_scores: np.ndarray, baseline_scores: np.ndarray) -> dict:
    """AUC of positive-negative pairs *within* identical transaction amounts."""
    groups = defaultdict(list)
    for index, value in enumerate(np.rint(amount).astype(np.int64)):
        groups[int(value)].append(index)
    mixed = [np.asarray(indices, dtype=int) for indices in groups.values()
             if 0 < int(y[indices].sum()) < len(indices)]
    pair_total = 0
    weighted = {"candidate": 0.0, "legacy": 0.0}
    rows = positives = 0
    for indices in mixed:
        labels = y[indices]
        pos = int(labels.sum())
        neg = len(indices) - pos
        pairs = pos * neg
        pair_total += pairs
        rows += len(indices)
        positives += pos
        weighted["candidate"] += roc_auc_score(labels, candidate_scores[indices]) * pairs
        weighted["legacy"] += roc_auc_score(labels, baseline_scores[indices]) * pairs
    return {"mixed_label_amount_values": len(mixed), "rows": rows, "positives": positives,
            "positive_negative_pairs": pair_total,
            "candidate_within_amount_auc": weighted["candidate"] / pair_total if pair_total else None,
            "legacy_within_amount_auc": weighted["legacy"] / pair_total if pair_total else None,
            "amount_only_within_amount_auc": 0.5 if pair_total else None,
            "interpretation": "Pairwise rank discrimination among transactions with the same amount; retrospective 2024 synthetic validation."}


def evaluate(candidate, x_tune, y_tune, x_val, y_val, type_val, quarter_val,
             baseline_scores, baseline_threshold, threshold, exact_amounts=None):
    tune_scores = candidate.predict_proba(x_tune)[:, 1]
    val_scores = candidate.predict_proba(x_val)[:, 1]
    baseline_count = int(np.count_nonzero(baseline_scores >= baseline_threshold))
    amount = exact_amounts if exact_amounts is not None else np.expm1(x_val[:, 0].astype(np.float64))
    high = amount >= 2_000_000
    prior_count = np.expm1(x_val[:, FEATURE_NAMES.index("sender_90d_count_log")])
    lifetime_count = np.expm1(x_val[:, FEATURE_NAMES.index("sender_all_count_log")])
    cohorts = {
        "cold_no_prior_ever": lifetime_count < 0.5,
        "known_sender_no_prior_90d": (lifetime_count >= 0.5) & (prior_count < 0.5),
        "sparse_1_to_4_prior_90d": (prior_count >= 0.5) & (prior_count < 4.5),
        "warm_5_plus_prior_90d": prior_count >= 4.5,
        "high_amount_2m_plus": high,
        "high_amount_warm": high & (prior_count >= 4.5),
    }
    amount_bands = {
        "below_100k": amount < 100_000,
        "100k_to_below_1m": (amount >= 100_000) & (amount < 1_000_000),
        "1m_to_below_2m": (amount >= 1_000_000) & (amount < 2_000_000),
        "2m_to_below_5m": (amount >= 2_000_000) & (amount < 5_000_000),
        "5m_plus": amount >= 5_000_000,
    }
    report = {
        "tuning": add_intervals(metrics(y_tune, tune_scores, threshold)),
        "validation": add_intervals(metrics(y_val, val_scores, threshold)),
        "matched_legacy_alert_count": {
            "count": baseline_count,
            "candidate": at_alert_count(y_val, val_scores, baseline_count),
            "legacy": at_alert_count(y_val, baseline_scores, baseline_count),
            "amount_only": at_alert_count(y_val, amount, baseline_count),
        },
        "high_amount_matched_legacy_alert_count": {
            "count": int(np.count_nonzero((baseline_scores >= baseline_threshold) & high)),
        },
        "amount_only_reference": {"average_precision": float(average_precision_score(y_val, amount)),
                                  "high_amount_average_precision": float(average_precision_score(y_val[high], amount[high]))},
        "same_amount_discrimination": same_amount_discrimination(y_val, amount, val_scores, baseline_scores),
        "cohorts": {name: add_intervals(cohort_report(y_val, val_scores, threshold, mask))
                    for name, mask in cohorts.items()},
        "amount_bands": {name: add_intervals(cohort_report(y_val, val_scores, threshold, mask))
                         for name, mask in amount_bands.items()},
        "quarter": {}, "anomaly_type": {},
        "calibration": {"tuning": calibration_bins(y_tune, tune_scores),
                        "validation": calibration_bins(y_val, val_scores)},
    }
    missing_context = np.asarray(x_val).copy()
    missing_context[:, 6:] = 0.0
    for name in ("days_since_sender", "days_since_recipient"):
        missing_context[:, FEATURE_NAMES.index(name)] = 91.0
    for name in ("days_since_first_sender", "days_since_last_sender_all", "days_since_last_recipient_all"):
        missing_context[:, FEATURE_NAMES.index(name)] = 3651.0
    missing_scores = candidate.predict_proba(missing_context)[:, 1]
    report["missing_history_stress"] = {
        "retrospective_validation": add_intervals(metrics(y_val, missing_scores, threshold)),
        "alert_decision_changes": int(np.count_nonzero((val_scores >= threshold) != (missing_scores >= threshold))),
        "interpretation": "Counterfactual removal of all history from the same 2024 rows. This is an input-availability stress test; it is not an observed cold-start cohort or verified fresh-clone accuracy.",
    }
    high_count = report["high_amount_matched_legacy_alert_count"]["count"]
    for label, values in (("candidate", val_scores), ("legacy", baseline_scores), ("amount_only", amount)):
        report["high_amount_matched_legacy_alert_count"][label] = at_alert_count(y_val[high], values[high], high_count)
    for quarter in np.unique(quarter_val):
        key = quarter.decode("ascii")
        mask = quarter_val == quarter
        report["quarter"][key] = add_intervals(cohort_report(y_val, val_scores, threshold, mask))
    predictions = val_scores >= threshold
    for anomaly_type in np.unique(type_val[y_val == 1]):
        key = anomaly_type.decode("ascii")
        mask = (y_val == 1) & (type_val == anomaly_type)
        report["anomaly_type"][key] = {"positives": int(mask.sum()),
                                        "detected": int(np.count_nonzero(predictions & mask)),
                                        "recall": float(np.mean(predictions[mask])),
                                        "recall_wilson_95": wilson_interval(int(np.count_nonzero(predictions & mask)), int(mask.sum()))}
    return report


def legacy_bank_scores(x: np.ndarray) -> tuple[np.ndarray, float]:
    # The saved legacy model expects four feature columns.  Its categorical
    # mapping was learned separately, so map contextual category code -> label
    # -> legacy category code before scoring.
    from model_artifact import load_artifact
    legacy = load_artifact("bank")
    new_maps = json.loads((CACHE / "mappings.json").read_text(encoding="utf-8"))
    raw = np.empty((len(x), 4), dtype=np.float32)
    raw[:, :2] = x[:, :2]
    for out_col, name in ((2, "자금구분"), (3, "매체구분")):
        inverse = {index: label for label, index in new_maps[name].items()}
        mapping = legacy["mappings"][name]
        raw[:, out_col] = [mapping.get(inverse.get(int(code), ""), -1) for code in x[:, out_col]]
    return legacy["model"].predict_proba(raw)[:, 1], float(legacy["threshold"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reuse-matrices", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    sources = files()
    manifest = source_fingerprint(sources)
    manifest_path = CACHE / "manifest.json"
    stage_path = CACHE / "stage.sqlite"
    if args.reuse_matrices:
        if not manifest_path.exists() or json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Cached matrices do not match source files")
        counts = json.loads((CACHE / "counts.json").read_text(encoding="utf-8"))
        mappings = json.loads((CACHE / "mappings.json").read_text(encoding="utf-8"))
        extraction = json.loads((CACHE / "extraction.json").read_text(encoding="utf-8"))
        source_counts = json.loads((CACHE / "source_counts.json").read_text(encoding="utf-8"))
    else:
        source_counts = prepare_stage(stage_path, sources)
        counts, mappings, extraction = build_matrices(stage_path, CACHE)
        for name, value in (("manifest", manifest), ("counts", counts), ("mappings", mappings),
                            ("extraction", extraction), ("source_counts", source_counts)):
            (CACHE / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "prepared", "counts": counts, **extraction}), flush=True)
    if args.prepare_only:
        return
    arrays = {}
    for name in counts:
        xp, yp = matrix_paths(CACHE, name)
        arrays[name] = (np.load(xp, mmap_mode="r"), np.load(yp, mmap_mode="r"))
    x_fit, y_fit = arrays["fit"]
    x_tune, y_tune = arrays["tune"]
    x_val, y_val = arrays["validation"]
    if int(y_fit.sum()) == 0 or int(y_tune.sum()) == 0 or int(y_val.sum()) == 0:
        raise ValueError("Every split must contain positive labels")
    type_val = np.load(CACHE / "type_validation.npy", mmap_mode="r")
    quarter_val = np.load(CACHE / "quarter_validation.npy", mmap_mode="r")
    candidates = CANDIDATES
    experiments = []
    winner = None
    candidate_cache = CACHE / "candidates"
    candidate_cache.mkdir(exist_ok=True)
    for index, params in enumerate(candidates):
        signature = hashlib.sha256(json.dumps({"params": params, "seed": args.seed,
                                               "features": FEATURE_NAMES, "manifest": manifest,
                                               "sklearn": sklearn.__version__}, sort_keys=True).encode()).hexdigest()
        checkpoint = candidate_cache / f"{signature}.skops"
        started = time.monotonic()
        reused = checkpoint.exists()
        if reused:
            model = sio.load(checkpoint, trusted=TRUSTED_TYPES)
        else:
            model = HistGradientBoostingClassifier(
                categorical_features=list(CATEGORICAL_INDICES),
                early_stopping=False, random_state=args.seed, **params,
            )
            model.fit(x_fit, y_fit)
            sio.dump(model, checkpoint)
        tune_scores = model.predict_proba(x_tune)[:, 1]
        ap = float(average_precision_score(y_tune, tune_scores))
        cutoff = f1_threshold(y_tune, tune_scores)
        item = {"candidate": index, "params": params, "tuning_ap": ap,
                "fit_seconds": None if reused else round(time.monotonic() - started, 2),
                "checkpoint_load_and_score_seconds": round(time.monotonic() - started, 2) if reused else None,
                "checkpoint_reused": reused, "n_iter": int(model.n_iter_),
                "development_operating_point": add_intervals(metrics(y_tune, tune_scores, cutoff["threshold"]))}
        experiments.append(item)
        print(json.dumps(item), flush=True)
        if winner is None or ap > winner[0]:
            winner = (ap, index, model, tune_scores)
    _, chosen_index, model, tune_scores = winner
    threshold_selection = f1_threshold(y_tune, tune_scores)
    threshold = threshold_selection["threshold"]
    baseline_scores, baseline_threshold = legacy_bank_scores(x_val)
    legacy_validation = metrics(y_val, baseline_scores, baseline_threshold)
    previous_baseline = json.loads((ROOT / "reports" / "bank_baseline.json").read_text(encoding="utf-8"))["validation"]
    if legacy_validation["confusion"] != previous_baseline["confusion"] or abs(legacy_validation["average_precision"] - previous_baseline["average_precision"]) > 1e-10:
        raise RuntimeError("Current legacy model does not reproduce its published 2024 validation metrics")
    exact_amounts = raw_validation_amounts(stage_path, y_val)
    evaluation = evaluate(model, x_tune, y_tune, x_val, y_val, type_val, quarter_val,
                          baseline_scores, baseline_threshold, threshold, exact_amounts)
    val_scores = model.predict_proba(x_val)[:, 1]
    evaluation["duplicate_input_audit"] = repeated_input_cohorts(stage_path, y_val, val_scores, threshold)
    evaluation["threshold_tradeoffs"] = threshold_tradeoffs(y_tune, tune_scores, y_val, val_scores)
    ablations = {}
    amountless = [i for i, name in enumerate(FEATURE_NAMES) if "amount" not in name]
    for name, columns in (("full_fit_four_fields", [0, 1, 2, 3]),
                          ("no_history_six_current_fields", list(range(6))),
                          ("without_all_amount_related_features", amountless)):
        categorical = [columns.index(i) for i in CATEGORICAL_INDICES if i in columns]
        params = candidates[chosen_index]
        diagnostic = HistGradientBoostingClassifier(categorical_features=categorical,
                                                    early_stopping=False, random_state=args.seed, **params)
        started = time.monotonic()
        diagnostic.fit(x_fit[:, columns], y_fit)
        dev = diagnostic.predict_proba(x_tune[:, columns])[:, 1]
        retrospective = diagnostic.predict_proba(x_val[:, columns])[:, 1]
        cutoff = f1_threshold(y_tune, dev)
        ablations[name] = {"features": [FEATURE_NAMES[i] for i in columns], "fit_rows": len(y_fit),
                           "fit_seconds": round(time.monotonic() - started, 2),
                           "development": add_intervals(metrics(y_tune, dev, cutoff["threshold"])),
                           "retrospective_validation": add_intervals(metrics(y_val, retrospective, cutoff["threshold"])),
                           "matched_legacy_alert_count": at_alert_count(y_val, retrospective, int(np.count_nonzero(baseline_scores >= baseline_threshold)))}
        print(json.dumps({"stage": "ablation", "name": name,
                          "development_ap": ablations[name]["development"]["average_precision"]}), flush=True)
    evaluation["full_data_ablations"] = ablations
    evaluation["legacy_validation_reproduced"] = add_intervals(legacy_validation)
    evaluation["development_group_permutation"] = development_group_permutation(model, x_tune, y_tune, args.seed)
    artifact = {
        "model": model, "mappings": mappings, "threshold": threshold,
        "feature_names": FEATURE_NAMES, "feature_schema_version": SCHEMA_VERSION,
        "history_days": HISTORY_DAYS,
    }
    ARTIFACT_PATH.parent.mkdir(exist_ok=True)
    sio.dump(artifact, ARTIFACT_PATH)
    digest = hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
    report = {
        "target": "AI Hub synthetic electronic-transfer 이상거래여부; not verified for real transactions",
        "target_definition": {
            "positive_label": "Synthetic source 이상거래여부=1, binary label only; an alert is a review signal, not proof of financial fraud.",
            "negative_label": "Synthetic source 이상거래여부=0; no alert is not a guarantee that a transaction is safe.",
            "source_anomaly_type_labels": {"1.0": "갑작스러운 거래패턴의 변화", "2.0": "신규 수신처 거래",
                                            "3.0": "분할 거래", "4.0": "다중거래의 동시 요청",
                                            "5.0": "거액 입금 후 당일 인출", "7.0": "심야/새벽 대량 거래"},
            "scope_limits": ["Binary model does not identify a fraud subtype or establish intent.",
                             "Reliable within-day order and inbound-account balance are absent; same-day multi-event mechanisms cannot be reconstructed completely.",
                             "Source subtype descriptions and labels never enter input features; evaluation measures agreement with these synthetic labels.",
                             "Real-bank fraud detection, unseen fraud patterns, and economic loss reduction have not been validated."],
        },
        "model": "HistGradientBoostingClassifier", "sklearn_version": sklearn.__version__,
        "model_version": f"bank-context-v1-{digest[:12]}", "artifact_sha256": digest,
        "feature_schema_version": SCHEMA_VERSION, "feature_names": FEATURE_NAMES,
        "seed": args.seed,
        "split_policy": "fit all training rows through 2023Q3; tune/threshold training 2023Q4; retrospective evaluation validation 2024",
        "source_manifest": manifest, "source_counts": source_counts, "matrix_counts": counts,
        "training_positives": int(y_fit.sum()), "tuning_positives": int(y_tune.sum()),
        "validation_positives": int(y_val.sum()),
        "history_policy": "detailed same-sender prior 90 calendar days plus all-time prior aggregates; exclude all same-day transactions; historical raw inputs from both training and validation, labels never used",
        "label_usage_policy": "Only pre-2023Q4 training labels enter fit; only 2023Q4 training labels select model and threshold. All 2024 training labels are excluded from fit and selection. Earlier raw events may be used as unlabeled context only.",
        "fit_sampling_rate": 1.0,
        "source_quality": source_quality_audit(stage_path),
        "history_max_observed": extraction["max_90d_history_rows"],
        "lifetime_max_observed": extraction["max_lifetime_history_rows"],
        "candidate_experiments": experiments, "selected_candidate": chosen_index,
        "threshold_policy": {"objective": "maximize 2023Q4 F1 subject to <=2% alert rate", **threshold_selection},
        "evaluation": evaluation,
        "uncertainty_policy": "Wilson intervals treat labeled rows as independent trials; repeated and account-correlated events can make real uncertainty larger. No real-world fraud-rate or production guarantee is implied.",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "trained", "chosen": chosen_index,
                      "validation": evaluation["validation"], "model_version": report["model_version"]}), flush=True)


if __name__ == "__main__":
    main()
