"""Full-data graph-context candidate; preserves the v1 deployment artifacts."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sqlite3
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone, timedelta

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score

import train_bank_contextual as v1
from bank_context_v2 import EXTRA_FEATURE_NAMES, FEATURE_NAMES, ORDERING_EVIDENCE, SCHEMA_VERSION, GraphState, extra_snapshot, account_key
from model_artifact import TRUSTED_TYPES
from model_score_explanation import make_normal_reference


CACHE = v1.DATA / ".bank-graph-cache"
MODEL_PATH = v1.ROOT / "models" / "bank_contextual_v2_candidate.skops"
REPORT_PATH = v1.ROOT / "reports" / "bank_contextual_v2_candidate.json"
FIT_POLICY = "all pre-2023Q4 training AND validation labels; both 2023Q4 partitions development; 2024 validation retrospective evaluation"


def feature_code_fingerprint():
    return {name: hashlib.sha256((v1.ROOT / "scripts" / name).read_bytes()).hexdigest()
            for name in ("bank_context_features.py", "bank_context_v2.py")}


def target_split(row, *, include_all_2024=False, evaluation_only=False):
    if not evaluation_only and row["event_date"] <= v1.FIT_END:
        return "fit"
    if not evaluation_only and row["event_date"] <= v1.TUNE_END:
        return "tune"
    return "validation" if (include_all_2024 or row["split"] == "validation") and row["event_date"].startswith("2024") else None


def build_matrices(*, output_cache=None, include_all_2024=False, evaluation_only=False) -> dict:
    cache = CACHE if output_cache is None else output_cache
    cache.mkdir(exist_ok=True)
    completion = cache / "complete.json"
    if completion.exists():
        completion.unlink()
    stage = sqlite3.connect(v1.CACHE / "stage.sqlite")
    counts = {} if evaluation_only else {
        "fit": stage.execute("SELECT COUNT(*) FROM events WHERE event_date<=?", (v1.FIT_END,)).fetchone()[0],
        "tune": stage.execute("SELECT COUNT(*) FROM events WHERE event_date>? AND event_date<=?", (v1.FIT_END, v1.TUNE_END)).fetchone()[0]}
    validation_filter = "event_date LIKE '2024%'" + ("" if include_all_2024 else " AND split='validation'")
    counts["validation"] = stage.execute(f"SELECT COUNT(*) FROM events WHERE {validation_filter}").fetchone()[0]
    stage.close()
    source_manifest = v1.source_fingerprint(v1.files())
    if source_manifest != json.loads((v1.CACHE / "manifest.json").read_text(encoding="utf-8")):
        raise ValueError("v1 cache does not match original source files")
    arrays, labels, types = {}, {}, {}
    for name, count in counts.items():
        labels[name] = np.lib.format.open_memmap(cache / f"y_{name}.npy", mode="w+", dtype=np.uint8, shape=(count,))
        arrays[name] = np.lib.format.open_memmap(cache / f"x_{name}.npy", mode="w+", dtype=np.float32,
                                               shape=(count, len(FEATURE_NAMES)))
        types[name] = np.lib.format.open_memmap(cache / f"type_{name}.npy", mode="w+", dtype="S16", shape=(count,))
    provenance = {}
    if include_all_2024:
        for field, dtype in (("amount", np.float64), ("quarter", "S6"), ("partition", "S10"), ("rowid", np.int64)):
            provenance[field] = np.lib.format.open_memmap(cache / f"{field}_validation.npy", mode="w+", dtype=dtype,
                                                        shape=(counts["validation"],))
    connection = sqlite3.connect(v1.CACHE / "stage.sqlite")
    connection.row_factory = sqlite3.Row
    cursor = connection.execute("SELECT rowid,* FROM events ORDER BY event_date,rowid")
    state = GraphState()
    histories = defaultdict(deque)
    lifetimes = defaultdict(lambda: v1.LifetimeState(scoped_institutions=True))
    mappings = json.loads((v1.CACHE / "mappings.json").read_text(encoding="utf-8"))
    indices = dict.fromkeys(counts, 0)
    diagnostics = defaultdict(lambda: {"rows": 0, "with_prior_same_day_outflow": 0,
                                        "with_prior_same_day_inflow": 0, "same_bucket_sender_siblings": 0})
    observed_hours = Counter()
    dates = 0
    for date, daily in itertools.groupby(cursor, key=lambda row: row["event_date"]):
        rows = [dict(row) for row in daily]
        day = datetime.strptime(date, "%Y%m%d").date()
        lower = day - timedelta(days=90)
        for sender in list(histories):
            while histories[sender] and histories[sender][0]["date"] < lower:
                histories[sender].popleft()
            if not histories[sender]:
                del histories[sender]
        grouped = defaultdict(list)
        locations = {}
        for row in rows:
            grouped[(row["sender_bank"], row["sender"])].append(row)
        # Match the original v1 date/sender extraction order exactly.
        for sender_rows in grouped.values():
            first = v1.row_event(sender_rows[0])
            sender_key = account_key(first, "출금")
            base = v1.history_base(first, histories[sender_key], scoped_institutions=True)
            for row in sender_rows:
                name = target_split(row, include_all_2024=include_all_2024, evaluation_only=evaluation_only)
                if name is None:
                    continue
                index = indices[name]
                current = v1.row_event(row)
                snapshot = v1.feature_snapshot(current, v1.summary_from_base(current, base), mappings,
                                                fit=False, prior_summary=lifetimes[sender_key].for_transaction(current))
                if snapshot["fund_code"] < 0 or snapshot["channel_code"] < 0:
                    raise ValueError("Pooled past partition contains a category absent from v1 mappings")
                arrays[name][index, :len(v1.FEATURE_NAMES)] = v1.vector(snapshot)
                labels[name][index] = row["label"]
                locations[row["rowid"]] = (name, index)
                types[name][index] = row["anomaly_type"].encode("ascii")
                if name == "validation" and provenance:
                    for field in provenance:
                        value = row["split"] if field == "partition" else row[field]
                        provenance[field][index] = value.encode("ascii") if isinstance(value, str) else value
                indices[name] += 1
        for hour, bucket in itertools.groupby(sorted(rows, key=lambda row: (row["hour"], row["rowid"])), key=lambda row: row["hour"]):
            bucket_rows = list(bucket)
            sibling_counts = Counter(row["sender"] for row in bucket_rows)
            events = [(row, v1.row_event(row)) for row in bucket_rows]
            for row, event in events:
                observed_hours[str(hour)] += 1
                if row["rowid"] not in locations:
                    continue
                name, index = locations[row["rowid"]]
                context = state.context(event)
                snapshot = extra_snapshot(event, context)
                arrays[name][index, len(v1.FEATURE_NAMES):] = [snapshot[field] for field in EXTRA_FEATURE_NAMES]
                if name == "validation":
                    group = diagnostics[f"label_{row['label']}/type_{row['anomaly_type'] or 'normal'}"]
                    group["rows"] += 1
                    group["with_prior_same_day_outflow"] += int(context["senderDayCount"] > 0)
                    group["with_prior_same_day_inflow"] += int(context["senderDayInflowCount"] > 0)
                    group["same_bucket_sender_siblings"] += int(sibling_counts[row["sender"]] > 1)
            # Never let any event in the current bucket inform another event in that bucket.
            for _, event in events:
                state.add(event)
        for row in rows:
            event = v1.row_event(row)
            sender_key = account_key(event, "출금")
            histories[sender_key].append(event)
            lifetimes[sender_key].add(event)
        dates += 1
        if dates % 200 == 0:
            print(json.dumps({"stage": "graph_features", "through_date": date, "written": indices}), flush=True)
    connection.close()
    if indices != counts:
        raise RuntimeError("Incomplete v2 matrix preparation")
    for array in (*arrays.values(), *types.values(), *labels.values(), *provenance.values()):
        array.flush()
    metadata = {"counts": counts, "source_manifest": source_manifest, "feature_names": FEATURE_NAMES,
                "feature_schema_version": SCHEMA_VERSION, "ordering_evidence": ORDERING_EVIDENCE,
                "fit_policy": FIT_POLICY, "feature_code_fingerprint": feature_code_fingerprint(),
                "evaluation_partitions": "training+validation" if include_all_2024 else "validation",
                "evaluation_only": evaluation_only,
                "source_time_bucket_counts": dict(observed_hours), "retrospective_pattern_availability": dict(diagnostics)}
    (cache / "complete.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def fixed_fpr_points(y_dev, dev_scores, y_val, val_scores) -> dict:
    normal_scores = np.sort(dev_scores[y_dev == 0])
    points = {}
    for rate in (0.001, 0.005, 0.01):
        threshold = float(np.nextafter(np.quantile(normal_scores, 1 - rate, method="higher"), np.inf))
        points[str(rate)] = {"development_target_fpr": rate, "threshold": threshold,
                             "development": v1.add_intervals(v1.metrics(y_dev, dev_scores, threshold)),
                             "retrospective_validation": v1.add_intervals(v1.metrics(y_val, val_scores, threshold))}
    return points


def fit_source_support(end_date=v1.FIT_END) -> dict:
    connection = sqlite3.connect(v1.CACHE / "stage.sqlite")
    values = connection.execute(
        "SELECT split,label,COUNT(*),MIN(amount),MAX(amount),"
        "SUM(CASE WHEN amount<2000000 THEN 1 ELSE 0 END) FROM events "
        "WHERE event_date<=? GROUP BY split,label", (end_date,)
    ).fetchall()
    connection.close()
    return {
        "fit_amount_min": min(row[3] for row in values),
        "fit_amount_max": max(row[4] for row in values),
        "fit_end_date": end_date,
        "partition_label_counts": [
            {"partition": partition, "label": label, "rows": count, "min_amount": minimum,
             "max_amount": maximum, "below_2m_rows": below}
            for partition, label, count, minimum, maximum, below in values],
        "history_requirement": "Observed strictly prior date/bucket context from both source partitions; zero-history requests are unsupported for reliability claims.",
        "amount_limit_interpretation": "Observed fit support, not a permissible payment limit or evidence that an in-range request is safe.",
        "scope": "AI Hub synthetic binary labels only; no real-fraud or unseen-case assurance.",
    }


def attach_reference() -> None:
    """Update provenance of a frozen candidate without fitting or selection."""
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    original_digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    if report["artifact_sha256"] != original_digest:
        raise ValueError("Cannot attach reference to mismatched candidate/report")
    artifact = sio.load(MODEL_PATH, trusted=TRUSTED_TYPES)
    x_fit = np.load(CACHE / "x_fit.npy", mmap_mode="r")
    y_fit = np.load(CACHE / "y_fit.npy", mmap_mode="r")
    reference = make_normal_reference(x_fit, y_fit, FEATURE_NAMES,
                                      categorical_indices=v1.CATEGORICAL_INDICES)
    artifact["explanation_reference"] = reference
    sio.dump(artifact, MODEL_PATH)
    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    report.update(artifact_sha256=digest, model_version=f"bank-context-v2-candidate-{digest[:12]}",
                  explanation_reference=reference, input_support=fit_source_support())
    report["reference_attachment"] = {"frozen_model_sha256_before_metadata": original_digest,
                                       "model_refitted": False, "threshold_changed": False}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "reference_attached", "version": report["model_version"],
                      "normal_fit_rows": reference["normal_fit_rows"]}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reuse-matrices", action="store_true")
    parser.add_argument("--attach-reference-only", action="store_true")
    args = parser.parse_args()
    if args.attach_reference_only:
        attach_reference()
        return
    if args.reuse_matrices:
        meta = json.loads((CACHE / "complete.json").read_text(encoding="utf-8"))
        if (meta["source_manifest"] != v1.source_fingerprint(v1.files()) or
                tuple(meta["feature_names"]) != FEATURE_NAMES or meta["feature_schema_version"] != SCHEMA_VERSION or
                meta.get("fit_policy") != FIT_POLICY or meta.get("feature_code_fingerprint") != feature_code_fingerprint()):
            raise ValueError("v2 matrix cache manifest does not match source and schema")
    else:
        meta = build_matrices()
    print(json.dumps({"stage": "graph_prepared", "counts": meta["counts"], "features": len(FEATURE_NAMES)}), flush=True)
    if args.prepare_only:
        return
    x = {name: np.load(CACHE / f"x_{name}.npy", mmap_mode="r") for name in meta["counts"]}
    y = {name: np.load(CACHE / f"y_{name}.npy", mmap_mode="r") for name in meta["counts"]}
    types = {name: np.load(CACHE / f"type_{name}.npy", mmap_mode="r") for name in meta["counts"]}
    configs = [
        {"max_leaf_nodes": 31, "min_samples_leaf": 100, "l2_regularization": 10.0, "max_iter": 400, "learning_rate": 0.05},
        {"max_leaf_nodes": 63, "min_samples_leaf": 100, "l2_regularization": 25.0, "max_iter": 300},
        {"max_leaf_nodes": 31, "min_samples_leaf": 150, "l2_regularization": 25.0, "max_iter": 300},
    ]
    experiments, winner = [], None
    (CACHE / "candidates").mkdir(exist_ok=True)
    for index, (params, rare_weight) in enumerate([(config, 1.0) for config in configs] + [(configs[0], 5.0)]):
        signature = hashlib.sha256(json.dumps({"params": params, "rare_weight": rare_weight, "meta": meta}, sort_keys=True).encode()).hexdigest()
        checkpoint = CACHE / "candidates" / f"{signature}.skops"
        reused = checkpoint.exists()
        start = time.monotonic()
        if reused:
            model = sio.load(checkpoint, trusted=TRUSTED_TYPES)
        else:
            model = HistGradientBoostingClassifier(categorical_features=list(v1.CATEGORICAL_INDICES),
                                                    early_stopping=False, random_state=42, **params)
            weights = np.where(np.isin(types["fit"], [b"3.0", b"4.0"]), rare_weight, 1.0)
            model.fit(x["fit"], y["fit"], sample_weight=weights)
            sio.dump(model, checkpoint)
        dev_scores = model.predict_proba(x["tune"])[:, 1]
        cutoff = v1.f1_threshold(y["tune"], dev_scores)
        ap = float(average_precision_score(y["tune"], dev_scores))
        item = {"candidate": index, "params": params, "training_type_3_4_weight": rare_weight,
                "fit_rows": len(y["fit"]), "development_ap": ap, "checkpoint_reused": reused,
                "fit_seconds": None if reused else round(time.monotonic() - start, 2),
                "development": v1.add_intervals(v1.metrics(y["tune"], dev_scores, cutoff["threshold"]))}
        experiments.append(item)
        (CACHE / "experiment-progress.json").write_text(json.dumps(experiments, indent=2), encoding="utf-8")
        print(json.dumps(item), flush=True)
        if winner is None or ap > winner[0]:
            winner = (ap, index, model, dev_scores)
    _, index, model, dev_scores = winner
    policy = v1.f1_threshold(y["tune"], dev_scores)
    threshold = policy["threshold"]
    baseline_scores, baseline_threshold = v1.legacy_bank_scores(x["validation"])
    quarters = np.load(v1.CACHE / "quarter_validation.npy", mmap_mode="r")
    amounts = v1.raw_validation_amounts(v1.CACHE / "stage.sqlite", y["validation"])
    evaluation = v1.evaluate(model, x["tune"], y["tune"], x["validation"], y["validation"],
                             types["validation"], quarters, baseline_scores, baseline_threshold, threshold, amounts)
    scores = model.predict_proba(x["validation"])[:, 1]
    evaluation["cohorts_prior_day_sender_v1_definition"] = evaluation.pop("cohorts")
    sender_prior_day = x["validation"][:, v1.FEATURE_NAMES.index("sender_all_count_log")] > 0
    graph_count_columns = [36 + i for i, name in enumerate(FEATURE_NAMES[36:]) if "Count_log" in name]
    any_graph = np.any(x["validation"][:, graph_count_columns] > 0, axis=1)
    evaluation["cohorts"] = {
        "cold_no_sender_or_graph_history": v1.add_intervals(v1.cohort_report(y["validation"], scores, threshold, ~sender_prior_day & ~any_graph)),
        "graph_context_without_prior_day_sender_history": v1.add_intervals(v1.cohort_report(y["validation"], scores, threshold, ~sender_prior_day & any_graph)),
        "sender_prior_day_history": v1.add_intervals(v1.cohort_report(y["validation"], scores, threshold, sender_prior_day)),
    }
    evaluation["fixed_development_fpr"] = fixed_fpr_points(y["tune"], dev_scores, y["validation"], scores)
    evaluation["threshold_tradeoffs"] = v1.threshold_tradeoffs(y["tune"], dev_scores, y["validation"], scores)
    evaluation["duplicate_input_audit"] = v1.repeated_input_cohorts(v1.CACHE / "stage.sqlite", y["validation"], scores, threshold)
    base_artifact = sio.load(v1.ARTIFACT_PATH, trusted=TRUSTED_TYPES)
    base_scores = base_artifact["model"].predict_proba(x["validation"][:, :36])[:, 1]
    alert_count = int(np.count_nonzero(base_scores >= base_artifact["threshold"]))
    evaluation["v1_reference"] = v1.metrics(y["validation"], base_scores, base_artifact["threshold"])
    evaluation["matched_v1_alert_count"] = {"count": alert_count, "v2": v1.at_alert_count(y["validation"], scores, alert_count),
                                             "v1": v1.at_alert_count(y["validation"], base_scores, alert_count)}
    # Refit a controlled ablation on every fit row; no 2024 selection is involved.
    columns = [i for i, name in enumerate(FEATURE_NAMES) if "amount" not in name.lower() and "net_" not in name]
    ablation = HistGradientBoostingClassifier(categorical_features=[columns.index(i) for i in v1.CATEGORICAL_INDICES],
                                             early_stopping=False, random_state=42, **configs[index if index < 3 else 0])
    ablation.fit(x["fit"][:, columns], y["fit"])
    ab_dev = ablation.predict_proba(x["tune"][:, columns])[:, 1]
    ab_scores = ablation.predict_proba(x["validation"][:, columns])[:, 1]
    ab_cutoff = v1.f1_threshold(y["tune"], ab_dev)["threshold"]
    evaluation["without_amount_and_net_amount_features"] = {
        "features": [FEATURE_NAMES[i] for i in columns], "fit_rows": len(y["fit"]),
        "development": v1.metrics(y["tune"], ab_dev, ab_cutoff),
        "retrospective_validation": v1.metrics(y["validation"], ab_scores, ab_cutoff)}
    reference = make_normal_reference(x["fit"], y["fit"], FEATURE_NAMES,
                                      categorical_indices=v1.CATEGORICAL_INDICES)
    artifact = {"model": model, "mappings": base_artifact["mappings"], "threshold": threshold,
                "explanation_reference": reference,
                "feature_names": FEATURE_NAMES, "feature_schema_version": SCHEMA_VERSION, "history_days": 90}
    sio.dump(artifact, MODEL_PATH)
    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    report = {"model": "HistGradientBoostingClassifier", "model_version": f"bank-context-v2-candidate-{digest[:12]}",
              "artifact_sha256": digest, "feature_schema_version": SCHEMA_VERSION, "feature_names": FEATURE_NAMES,
              "target": "AI Hub synthetic binary 이상거래여부; retrospective research candidate, not real-fraud validation",
              "split_policy": meta["fit_policy"],
              "label_usage_policy": "2024 training labels never used for fitting or selection; all historical raw inputs may contribute strictly prior-bucket unlabeled graph context",
              "fit_sampling_rate": 1.0, "matrix_counts": meta["counts"], "source_manifest": meta["source_manifest"],
              "ordering_evidence": ORDERING_EVIDENCE, "retrospective_pattern_availability": meta["retrospective_pattern_availability"],
              "source_time_bucket_counts": meta["source_time_bucket_counts"], "candidate_experiments": experiments,
              "selected_candidate": index, "selection_objective": "maximum development average precision",
              "threshold_policy": {"objective": "maximize 2023Q4 F1 subject to <=2% alert rate", **policy},
              "evaluation": evaluation, "explanation_reference": reference, "deployment_enabled_by_default": False,
              "input_support": fit_source_support(),
              "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "graph_trained", "validation": evaluation["validation"], "version": report["model_version"]}), flush=True)


if __name__ == "__main__":
    main()
