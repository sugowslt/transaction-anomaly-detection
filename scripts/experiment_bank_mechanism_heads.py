"""Run one declared past-only fold/configuration, with verified resume artifacts.

Thresholds below are development diagnostics. A joint deployment policy must
still be selected from pooled temporal out-of-fold predictions, then frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, precision_recall_curve
from threadpoolctl import threadpool_limits

import train_bank_contextual as base
import train_bank_graph as graph
import train_bank_closed_window as window
from audit_bank_window_candidates import decision_metrics, load_verified, subtype_metrics
from bank_window_features import FEATURE_NAMES
from validate_bank_training_protocol import PROTOCOL, REPORT as PROTOCOL_AUDIT, validate_boundaries


def digest(path):
    with Path(path).open("rb") as stream:
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def fit_categories(x_fit, categorical_indices):
    """Never learn vocabulary from development rows or the global cache map."""
    return {str(index): sorted(float(value) for value in np.unique(x_fit[:, index])
                              if np.isfinite(value) and value >= 0) for index in categorical_indices}


def apply_categories(x, vocabulary):
    result = np.array(x, dtype=np.float32, copy=True)
    unknown = {}
    for field, values in vocabulary.items():
        index = int(field)
        column = result[:, index].copy()
        result[:, index] = np.nan
        for code, value in enumerate(values):
            result[column == value, index] = code
        unknown[FEATURE_NAMES[index]] = int(np.isnan(result[:, index]).sum())
    return result, unknown


def verify_source_order(connection, x, y, types, end_date, mappings):
    """Verify every selected row, not only aggregate positive counts."""
    cursor = connection.execute("SELECT rowid,* FROM events WHERE event_date<=? ORDER BY event_date,rowid", (end_date,))
    index = 0
    identity = hashlib.sha256()
    amounts = np.empty(len(y), dtype=np.float64)
    for day, daily in itertools.groupby(cursor, key=lambda row: row["event_date"]):
        senders = defaultdict(list)
        for row in daily:
            senders[(row["sender_bank"], row["sender"])].append(row)
        rows = [row for group in senders.values() for row in group]
        end = index + len(rows)
        if end > len(y):
            raise ValueError("Temporal source/cache population mismatch")
        labels = np.asarray([row["label"] for row in rows], dtype=np.uint8)
        kinds = np.asarray([row["anomaly_type"].encode("ascii") for row in rows], dtype="S16")
        raw = np.asarray([[np.log1p(row["amount"]), row["hour"], mappings["자금구분"][row["fund"]],
                           mappings["매체구분"][row["channel"]], row["sender_bank"] == row["recipient_bank"]]
                          for row in rows], dtype=np.float32)
        if not np.array_equal(labels, y[index:end]) or not np.array_equal(kinds, types[index:end]):
            raise ValueError("Temporal source/cache label or subtype order mismatch")
        if not np.array_equal(raw, x[index:end, :5]) or not np.all(x[index:end, 5] == datetime.strptime(day, "%Y%m%d").weekday()):
            raise ValueError("Temporal source/cache raw-feature order mismatch")
        amounts[index:end] = [row["amount"] for row in rows]
        identity.update(np.asarray([row["rowid"] for row in rows], dtype="<i8").tobytes())
        index = end
    if index != len(y) or not np.isin(y, [0, 1]).all() or not np.isfinite(x).all():
        raise ValueError("Incomplete or invalid temporal fold")
    return {"verified_rows": index, "ordered_source_identity_sha256": identity.hexdigest()}, amounts


def type_counts(y, types):
    return {kind.decode(): int(np.count_nonzero((types == kind) & (y == 1))) for kind in np.unique(types[y == 1])}


def prepare_fold(fold, meta):
    connection = sqlite3.connect(f"file:{(base.CACHE / 'stage.sqlite').as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        fit_end = connection.execute("SELECT COUNT(*) FROM events WHERE event_date<=?", (fold["fit_end"],)).fetchone()[0]
        dev_start = connection.execute("SELECT COUNT(*) FROM events WHERE event_date<?", (fold["development_start"],)).fetchone()[0]
        dev_end = connection.execute("SELECT COUNT(*) FROM events WHERE event_date<=?", (fold["development_end"],)).fetchone()[0]
        if not 0 < fit_end <= dev_start < dev_end:
            raise ValueError("Empty or overlapping temporal fold")
        arrays = []
        for cache, prefix in ((window.CACHE, "x"), (graph.CACHE, "y"), (graph.CACHE, "type")):
            first = np.load(cache / f"{prefix}_fit.npy", mmap_mode="r")
            if len(first) != meta["counts"]["fit"]:
                raise ValueError("Past cache row count mismatch")
            if dev_end <= len(first):
                selected = first[:dev_end]
            else:
                second = np.load(cache / f"{prefix}_tune.npy", mmap_mode="r")
                if len(second) != meta["counts"]["tune"] or dev_end > len(first) + len(second):
                    raise ValueError("Fold exceeds pre2024 cache")
                selected = np.concatenate((first, second[:dev_end - len(first)]))
            arrays.append(selected)
        x, y, types = arrays
        if x.shape != (len(y), len(FEATURE_NAMES)) or len(types) != len(y):
            raise ValueError("Temporal feature schema mismatch")
        mappings = json.loads((base.CACHE / "mappings.json").read_text(encoding="utf-8"))
        alignment, amounts = verify_source_order(connection, x, y, types, fold["development_end"], mappings)
        vocabulary = fit_categories(x[:fit_end], base.CATEGORICAL_INDICES)
        xf, fit_unknown = apply_categories(x[:fit_end], vocabulary)
        xd, dev_unknown = apply_categories(x[dev_start:], vocabulary)
        raw_mappings = {name: {raw: vocabulary[str(index)].index(float(code)) for raw, code in mappings[name].items()
                              if float(code) in vocabulary[str(index)]}
                        for name, index in zip(("자금구분", "매체구분"), base.CATEGORICAL_INDICES)}
        metadata = {"fit_rows": fit_end, "development_rows": dev_end - dev_start, "fit_sample_rate": 1.0,
                    "fit_positive_types": type_counts(y[:fit_end], types[:fit_end]),
                    "development_positive_types": type_counts(y[dev_start:], types[dev_start:]),
                    "unseen_positive_types": sorted(set(type_counts(y[dev_start:], types[dev_start:])) - set(type_counts(y[:fit_end], types[:fit_end]))),
                    "category_vocabulary_from_fit_only": vocabulary, "raw_category_mappings": raw_mappings,
                    "fit_unknown_categories": fit_unknown, "development_unknown_categories": dev_unknown, **alignment}
        return xf, y[:fit_end], types[:fit_end], xd, y[dev_start:], types[dev_start:], amounts[dev_start:], metadata
    finally:
        connection.close()


def precision_policy(y, scores, target, budget):
    precision, recall, thresholds = precision_recall_curve(y, scores)
    rates = (len(scores) - np.searchsorted(np.sort(scores), thresholds, side="left")) / len(scores)
    valid = np.flatnonzero((precision[:-1] >= target) & (rates <= budget))
    if not len(valid):
        return {"feasible": False, "threshold": None, "target_precision": target, "max_alert_rate": budget}
    # Highest recall, then fewer alerts. This remains a development-selected diagnostic.
    chosen = min(valid, key=lambda i: (-recall[i], rates[i]))
    return {"feasible": True, "threshold": float(thresholds[chosen]), "target_precision": target, "max_alert_rate": budget}


def evaluate(y, types, alert):
    result = base.add_intervals(decision_metrics(y, alert))
    result["alert_rate"] = result["alert_count"] / len(y)
    result["subtypes"] = subtype_metrics(y, types, alert)
    return result


def verify_resume(expected, stored):
    if stored.get("signature") != expected:
        raise ValueError("Resume fingerprint differs; preserve old results and declare a new experiment")


def save_scores(path, scores):
    temporary = path.with_suffix(".tmp.npy")
    np.save(temporary, scores)
    temporary.replace(path)


def run(fold_id, configuration):
    started = time.monotonic()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    validate_boundaries(protocol)
    audit = json.loads(PROTOCOL_AUDIT.read_text(encoding="utf-8"))
    if audit["protocol_sha256"] != digest(PROTOCOL):
        raise ValueError("Protocol source audit fingerprint mismatch")
    fold = next((item for item in protocol["selection_folds"] if item["id"] == fold_id), None)
    configs = protocol["first_experiment"]["configurations"]
    if fold is None or not 1 <= configuration <= len(configs):
        raise ValueError("Undeclared temporal fold or configuration")
    meta = window.prepare(True)
    xf, yf, tf, xd, yd, td, amounts, population = prepare_fold(fold, meta)
    declared = next(item for item in audit["folds"] if item["id"] == fold_id)
    for key in ("fit_rows", "development_rows", "fit_positive_types", "development_positive_types", "unseen_positive_types"):
        if population[key] != declared[key]:
            raise ValueError("Fold population differs from declared source audit")
    params = {**configs[configuration - 1], "random_state": protocol["first_experiment"]["random_state"],
              "early_stopping": protocol["first_experiment"]["early_stopping"], "class_weight": protocol["first_experiment"]["class_weight"]}
    signature = {"protocol_sha256": digest(PROTOCOL), "experiment_code_sha256": digest(__file__),
                 "fold": fold, "configuration": configuration, "parameters": params, "population": population,
                 "cache_provenance": meta["provenance"],
                 "fold_feature_sha256": hashlib.sha256(xf.tobytes() + xd.tobytes()).hexdigest(),
                 "fold_target_sha256": hashlib.sha256(yf.tobytes() + yd.tobytes() + tf.tobytes() + td.tobytes()).hexdigest(),
                 "environment": {"numpy": np.__version__, "sklearn": __import__("sklearn").__version__, "skops": __import__("skops").__version__}}
    report_path = base.ROOT / "reports" / f"bank_mechanism_{fold_id}_config{configuration}.json"
    output = base.ROOT / "models" / "bank_window_candidates" / "mechanism_heads" / f"{fold_id}-config{configuration}"
    scores_dir = base.DATA / ".bank-mechanism-cache" / f"{fold_id}-config{configuration}"
    output.mkdir(parents=True, exist_ok=True); scores_dir.mkdir(parents=True, exist_ok=True)
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8")); verify_resume(signature, report)
    else:
        report = {"stage": "prepared", "signature": signature, "models": {}, "operational_release_ready": False,
                  "selection_complete": False, "runtime_model_changed": False,
                  "task": "Delayed closed three-hour window; synthetic anomaly targets only",
                  "evaluation_limit": "Unseen-row predictions but development-selected thresholds; not independent policy validation. No2024 score or label is used.",
                  "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        write_json(report_path, report)
    print(json.dumps({"stage": "fold_verified", "fold": fold_id, "configuration": configuration,
                      "fit_rows": len(yf), "development_rows": len(yd), "unseen_types": population["unseen_positive_types"]}), flush=True)
    scores = {}
    targets = {"general": (yf, yd)}
    for kind in protocol["first_experiment"]["target_types"]:
        targets["type" + kind.split(".")[0]] = (((yf == 1) & (tf == kind.encode())).astype(np.uint8),
                                                ((yd == 1) & (td == kind.encode())).astype(np.uint8))
    budget = protocol["selection_rule"]["maximum_pooled_alert_rate"]
    precision_target = protocol["selection_rule"]["research_targets"]["precision"]
    for name, (fit_target, dev_target) in targets.items():
        entry = report["models"].get(name, {})
        path = output / f"{name}.skops"; score_path = scores_dir / f"{name}.npy"
        if len(np.unique(fit_target)) != 2 or len(np.unique(dev_target)) != 2:
            raise ValueError("Declared model target lacks positive or negative examples")
        if "artifact_sha256" not in entry:
            fit_started = time.monotonic()
            print(json.dumps({"stage": "fit_started", "model": name, "rows": len(fit_target), "positives": int(fit_target.sum())}), flush=True)
            model = HistGradientBoostingClassifier(**params, categorical_features=list(base.CATEGORICAL_INDICES))
            model.fit(xf, fit_target)
            artifact = {"model": model, "feature_names": FEATURE_NAMES, "threshold": None,
                        "feature_schema_version": "bank-closed-window-v1", "mappings": population["raw_category_mappings"],
                        "target": name, "fold": fold, "protocol_sha256": signature["protocol_sha256"]}
            temporary = path.with_suffix(".tmp.skops"); sio.dump(artifact, temporary); temporary.replace(path)
            entry = {"artifact_path": path.relative_to(base.ROOT).as_posix(), "artifact_sha256": digest(path),
                     "fit_seconds": round(time.monotonic() - fit_started, 3), "fit_rows": len(fit_target),
                     "fit_positives": int(fit_target.sum()), "iterations": int(model.n_iter_), "development_positives": int(dev_target.sum())}
            report["models"][name] = entry; report["stage"] = "fitting_or_scoring"; write_json(report_path, report)
        artifact = load_verified(path, entry["artifact_sha256"], FEATURE_NAMES, None)
        if artifact["mappings"] != population["raw_category_mappings"] or artifact["fold"] != fold or artifact["target"] != name:
            raise ValueError("Resume artifact preprocessing or target mismatch")
        if "score_sha256" in entry:
            if digest(score_path) != entry["score_sha256"]:
                raise ValueError("Resume score fingerprint mismatch")
            values = np.load(score_path)
        else:
            values = artifact["model"].predict_proba(xd)[:, 1]
            save_scores(score_path, values)
            entry["score_sha256"] = digest(score_path)
            entry["score_path"] = score_path.relative_to(base.ROOT).as_posix()
        if values.shape != (len(yd),) or not np.isfinite(values).all() or not ((values >= 0) & (values <= 1)).all():
            raise ValueError("Invalid development scores")
        scores[name] = values
        policy = base.f1_threshold(dev_target, values, budget)
        strict = precision_policy(dev_target, values, precision_target, budget)
        entry["development_average_precision"] = float(average_precision_score(dev_target, values))
        entry["f1_diagnostic_policy"] = policy
        entry["target_precision_diagnostic_policy"] = strict
        entry["f1_diagnostic"] = base.add_intervals(decision_metrics(dev_target, values >= policy["threshold"]))
        entry["target_precision_diagnostic"] = base.add_intervals(decision_metrics(dev_target, values >= strict["threshold"])) if strict["feasible"] else None
        write_json(report_path, report)
        print(json.dumps({"stage": "model_completed", "model": name, "fit_seconds": entry["fit_seconds"],
                          "development": entry["f1_diagnostic"]}), flush=True)
    alerts = {name: values >= report["models"][name]["f1_diagnostic_policy"]["threshold"] for name, values in scores.items()}
    alerts["naive_or_diagnostic"] = np.logical_or.reduce(list(alerts.values()))
    report["development_diagnostics"] = {name: evaluate(yd, td, flag) for name, flag in alerts.items()}
    masks = {"amount_below_2m": amounts < 2_000_000, "amount_2m_to_10m": (amounts >= 2_000_000) & (amounts < 10_000_000),
             "amount_at_least_10m": amounts >= 10_000_000,
             "no_sender_history": xd[:, FEATURE_NAMES.index("sender_all_count_log")] == 0,
             "sender_history": xd[:, FEATURE_NAMES.index("sender_all_count_log")] > 0,
             "new_sender_recipient_pair": xd[:, FEATURE_NAMES.index("recipient_all_count_log")] == 0,
             "existing_sender_recipient_pair": xd[:, FEATURE_NAMES.index("recipient_all_count_log")] > 0}
    report["cohorts"] = {name: {model: evaluate(yd[mask], td[mask], flag[mask]) for model, flag in alerts.items()}
                         for name, mask in masks.items() if mask.any()}
    general, joined = alerts["general"], alerts["naive_or_diagnostic"]
    report["naive_or_delta"] = {"additional_true_alerts": int(np.count_nonzero((yd == 1) & joined & ~general)),
                                 "additional_false_alerts": int(np.count_nonzero((yd == 0) & joined & ~general)), "promotion_allowed": False}
    report["stage"] = "completed_one_fold_configuration"
    report["required_followup"] = ["remaining declared configurations and folds", "pooled pre2024 policy selection",
                                    "amount and history ablations", "input stress checks", "full-past frozen refit", "independent future validation"]
    report["last_run_seconds"] = round(time.monotonic() - started, 3)
    write_json(report_path, report)
    print(json.dumps({"stage": report["stage"], "report": str(report_path), "seconds": report["last_run_seconds"],
                      "general": report["development_diagnostics"]["general"], "naive_or": report["development_diagnostics"]["naive_or_diagnostic"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fold", required=True)
    parser.add_argument("--configuration", type=int, required=True)
    args = parser.parse_args()
    with threadpool_limits(limits=8):
        run(args.fold, args.configuration)
