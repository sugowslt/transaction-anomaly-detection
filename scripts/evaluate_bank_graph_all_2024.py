"""Frozen-candidate retrospective evaluation on both 2024 source partitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import skops.io as sio

import train_bank_contextual as v1
import train_bank_graph as graph
from bank_context_v2 import FEATURE_NAMES
from model_artifact import TRUSTED_TYPES


CACHE = v1.DATA / ".bank-graph-all-2024-cache"
REPORT = v1.ROOT / "reports" / "bank_contextual_v2_all_2024.json"


def repeated_mask(rowids):
    columns = "event_date,sender,recipient,sender_bank,recipient_bank,fund,amount,hour,channel"
    connection = sqlite3.connect(v1.CACHE / "stage.sqlite")
    repeated = connection.execute(
        f"SELECT e.rowid FROM events e JOIN (SELECT {columns},COUNT(*) AS n FROM events "
        f"WHERE event_date LIKE '2024%' GROUP BY {columns} HAVING n>1) repeats "
        f"USING({columns}) WHERE e.event_date LIKE '2024%'"
    )
    identifiers = np.fromiter((row[0] for row in repeated), dtype=np.int64)
    connection.close()
    return np.isin(rowids, identifiers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-matrices", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--full-past", action="store_true")
    args = parser.parse_args()
    if args.reuse_matrices:
        meta = json.loads((CACHE / "complete.json").read_text(encoding="utf-8"))
        if (meta["source_manifest"] != v1.source_fingerprint(v1.files()) or
                meta["feature_code_fingerprint"] != graph.feature_code_fingerprint() or
                meta["evaluation_partitions"] != "training+validation" or not meta["evaluation_only"]):
            raise ValueError("All-2024 cache provenance mismatch")
    else:
        meta = graph.build_matrices(output_cache=CACHE, include_all_2024=True, evaluation_only=True)
    if args.prepare_only:
        return
    # Import only after preparation: the frozen artifact and report are checked by the runtime loader.
    if args.full_past:
        candidate_report = json.loads((v1.ROOT / "reports" / "bank_contextual_v2_full_past.json").read_text(encoding="utf-8"))
        filename = candidate_report["artifact_filename"]
        if filename not in ("bank_contextual_v2_full_past.skops", "bank_contextual_v2_full_past.json"):
            raise ValueError("Unexpected full-past artifact path")
        path = v1.ROOT / "models" / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != candidate_report["artifact_sha256"]:
            raise ValueError("Full-past artifact/report digest mismatch")
        if candidate_report["artifact_format"] == "xgboost-json" and filename.endswith(".json"):
            from xgboost import XGBClassifier
            model = XGBClassifier(n_jobs=8)
            model.load_model(path)
            if model.get_booster().num_features() != len(FEATURE_NAMES):
                raise ValueError("Full-past XGBoost feature count mismatch")
        elif candidate_report["artifact_format"] == "skops" and filename.endswith(".skops"):
            artifact = sio.load(path, trusted=TRUSTED_TYPES)
            if (tuple(artifact["feature_names"]) != FEATURE_NAMES or
                    artifact["explanation_reference"] != candidate_report["explanation_reference"]):
                raise ValueError("Full-past artifact feature/reference mismatch")
            model = artifact["model"]
        else:
            raise ValueError("Unsupported full-past artifact format")
        threshold = candidate_report["threshold_policy"]["threshold"]
        version = candidate_report["model_version"]
    else:
        from serve_model import GRAPH_ARTIFACT, GRAPH_MODEL_VERSION
        candidate_report = json.loads(graph.REPORT_PATH.read_text(encoding="utf-8"))
        model, threshold = GRAPH_ARTIFACT["model"], GRAPH_ARTIFACT["threshold"]
        version = GRAPH_MODEL_VERSION
    x = np.load(CACHE / "x_validation.npy", mmap_mode="r")
    y = np.load(CACHE / "y_validation.npy", mmap_mode="r")
    types = np.load(CACHE / "type_validation.npy", mmap_mode="r")
    quarter = np.load(CACHE / "quarter_validation.npy", mmap_mode="r")
    amount = np.load(CACHE / "amount_validation.npy", mmap_mode="r")
    partition = np.load(CACHE / "partition_validation.npy", mmap_mode="r")
    rowids = np.load(CACHE / "rowid_validation.npy", mmap_mode="r")
    official = partition == b"validation"
    # Independent preparation must reproduce every official validation feature and label.
    for field, values in (("x", x), ("y", y), ("type", types)):
        reference = np.load(graph.CACHE / f"{field}_validation.npy", mmap_mode="r")
        if not np.array_equal(values[official], reference):
            raise RuntimeError(f"All-2024 {field} disagrees with original official validation cache")
    print(json.dumps({"stage": "official_cache_parity", "rows": int(official.sum())}), flush=True)
    x_tune = np.load(graph.CACHE / "x_tune.npy", mmap_mode="r")
    y_tune = np.load(graph.CACHE / "y_tune.npy", mmap_mode="r")
    scores = model.predict_proba(x)[:, 1]
    dev_scores = model.predict_proba(x_tune)[:, 1]
    baseline, baseline_threshold = v1.legacy_bank_scores(x)
    evaluation = v1.evaluate(model, x_tune, y_tune, x, y, types, quarter,
                             baseline, baseline_threshold, threshold, amount)
    official_metrics = v1.metrics(y[official], scores[official], threshold)
    if official_metrics["confusion"] != candidate_report["evaluation"]["validation"]["confusion"]:
        raise RuntimeError("Frozen candidate does not reproduce its official validation metrics")
    evaluation["official_validation_partition"] = v1.add_intervals(official_metrics)
    evaluation["2024_training_partition"] = v1.add_intervals(v1.metrics(y[~official], scores[~official], threshold))
    if not args.full_past:
        evaluation["fixed_development_fpr"] = graph.fixed_fpr_points(y_tune, dev_scores, y, scores)
    else:
        evaluation["former_development_after_refit"] = evaluation.pop("tuning")
        evaluation["calibration"]["former_development_after_refit"] = evaluation["calibration"].pop("tuning")
    evaluation["cohorts_prior_day_sender_v1_definition"] = evaluation.pop("cohorts")
    prior = x[:, FEATURE_NAMES.index("sender_all_count_log")] > 0
    count_columns = [i for i, name in enumerate(FEATURE_NAMES) if name.startswith("graph_") and "Count_log" in name]
    graph_available = np.any(x[:, count_columns] > 0, axis=1)
    evaluation["cohorts"] = {
        "cold_no_sender_or_graph_history": v1.add_intervals(v1.cohort_report(y, scores, threshold, ~prior & ~graph_available)),
        "graph_context_without_prior_day_sender_history": v1.add_intervals(v1.cohort_report(y, scores, threshold, ~prior & graph_available)),
        "sender_prior_day_history": v1.add_intervals(v1.cohort_report(y, scores, threshold, prior)),
    }
    repeats = repeated_mask(rowids)
    evaluation["duplicate_input_audit"] = {
        "key": "all nine raw fields including date and three-hour bucket; label excluded",
        "fit_to_development_exact_input_overlap": 0, "fit_to_evaluation_exact_input_overlap": 0,
        "development_to_evaluation_exact_input_overlap": 0,
        "basis": "Disjoint dates; all source rows retained because exact repeated inputs are not reliable transaction IDs.",
        "cohorts": {"repeated_exact_raw_inputs": v1.add_intervals(v1.cohort_report(y, scores, threshold, repeats)),
                    "nonrepeated_exact_raw_inputs": v1.add_intervals(v1.cohort_report(y, scores, threshold, ~repeats))},
    }
    report = {
        "model_version": version, "artifact_sha256": candidate_report["artifact_sha256"],
        "feature_schema_version": graph.SCHEMA_VERSION,
        "selection_and_threshold": "Frozen 2023Q4 selection; no refit, selection, or threshold adjustment using any 2024 labels or scores.",
        "evaluation_scope": "All 2024 rows of official training and validation partitions; retrospective synthetic-label evaluation, previously examined, not a blinded or real-fraud test.",
        "observed_history_sources": "Earlier raw rows from both partitions, including 2024 training observations, with labels excluded and same-bucket/future events excluded.",
        "matrix_counts": meta["counts"], "source_manifest": meta["source_manifest"],
        "retrospective_pattern_availability": meta["retrospective_pattern_availability"],
        "evaluation": evaluation,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report_path = REPORT.with_name("bank_contextual_v2_full_past_all_2024.json") if args.full_past else REPORT
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "all_2024_evaluated", "validation": evaluation["validation"],
                      "types": evaluation["anomaly_type"]}), flush=True)


if __name__ == "__main__":
    main()
