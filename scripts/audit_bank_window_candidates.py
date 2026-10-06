"""Replay frozen window candidates and report aggregate errors without fitting."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import skops.io as sio

import train_bank_contextual as base
import train_bank_closed_window as window
from audit_model_readiness import assess, verify_metrics
from bank_window_features import FEATURE_NAMES
from evaluate_bank_graph_all_2024 import CACHE as ALL_CACHE
from experiment_bank_window_multiclass import anomaly_scores
from model_artifact import TRUSTED_TYPES
from release_assets import verify_window_report, verify_window_hybrid_report

REPORT = base.ROOT / "reports" / "bank_window_candidate_audit.json"
TYPE_NAMES = {
    "1.0": "갑작스러운 거래패턴의 변화", "2.0": "신규 수신처 거래",
    "3.0": "분할 거래", "4.0": "다중 거래의 동시 요청",
    "5.0": "거액 입금 후 당일 인출", "7.0": "심야·새벽 대량 거래",
}


def load_verified(path, digest, fields, threshold):
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Candidate artifact/report digest mismatch")
    unexpected = set(sio.get_untrusted_types(file=path)) - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected candidate model types: {sorted(unexpected)}")
    artifact = sio.load(path, trusted=TRUSTED_TYPES)
    if tuple(artifact.get("feature_names", ())) != tuple(fields) or artifact.get("threshold") != threshold:
        raise ValueError("Candidate feature/threshold contract mismatch")
    if artifact["model"].n_features_in_ != len(fields):
        raise ValueError("Candidate estimator feature count mismatch")
    return artifact


def decision_metrics(y, alert):
    if len(y) != len(alert) or not np.isin(y, [0, 1]).all() or alert.dtype != np.bool_:
        raise ValueError("Invalid replay labels or alert array")
    positive = y == 1
    tp = int(np.count_nonzero(positive & alert)); fp = int(np.count_nonzero(~positive & alert))
    fn = int(np.count_nonzero(positive & ~alert)); tn = int(np.count_nonzero(~positive & ~alert))
    return {"rows": len(y), "positives": tp + fn,
            "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn),
            "precision_defined": tp + fp > 0, "recall_defined": tp + fn > 0,
            "false_positive_rate": fp / max(1, tn + fp), "alert_count": tp + fp,
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn}}


def subtype_metrics(y, types, alert):
    result = {}
    for kind in np.unique(types[y == 1]):
        mask = (y == 1) & (types == kind)
        count = int(mask.sum()); detected = int(np.count_nonzero(mask & alert))
        name = kind.decode("ascii")
        result[name] = {"name": TYPE_NAMES[name], "positives": count, "detected": detected,
                        "missed": count - detected, "recall": detected / count,
                        "recall_wilson_95": base.wilson_interval(detected, count)}
    return result


def verify_replay(actual, expected, actual_types, expected_types):
    verify_metrics(expected)
    if actual["confusion"] != expected["confusion"] or actual["rows"] != expected["rows"]:
        raise ValueError("Frozen candidate replay differs from recorded confusion counts")
    for key in ("precision", "recall"):
        if not math.isclose(actual[key], expected[key], abs_tol=1e-12):
            raise ValueError(f"Frozen candidate replay differs in {key}")
    if set(actual_types) != set(expected_types) or any(
        actual_types[key][field] != expected_types[key][field]
        for key in actual_types for field in ("positives", "detected")
    ):
        raise ValueError("Frozen candidate subtype replay differs from report")


def main():
    started = time.monotonic()
    meta = window.prepare(True)  # Read and verify existing cache; never rebuild or fit.
    general = verify_window_report(); hybrid = verify_window_hybrid_report()
    multiclass = json.loads((base.ROOT / "reports" / "bank_window_multiclass_experiment.json").read_text(encoding="utf-8"))
    if multiclass.get("stage") != "completed" or multiclass.get("release_ready") is not False:
        raise ValueError("Multiclass experiment is incomplete or incorrectly promoted")
    for report in (general, hybrid, multiclass):
        if report["source_manifest"] != meta["provenance"]["source_manifest"]:
            raise ValueError("Candidate source manifest mismatch")
    fit_rows = meta["counts"]["fit"] + meta["counts"]["tune"]
    if any(count != fit_rows for count in (general["fit_rows"], hybrid["fit_rows_each_component"], multiclass["fit_rows"])):
        raise ValueError("Candidates do not cover the same full-past fitting population")
    multiclass_path = base.ROOT / "models" / "bank_window_candidates" / "closed_window_multiclass.skops"
    if multiclass_path.relative_to(base.ROOT).as_posix() != multiclass["artifact_path"]:
        raise ValueError("Unexpected multiclass artifact path")
    artifacts = {
        "general": load_verified(base.ROOT / "models" / "bank_closed_window.skops", general["artifact_sha256"], FEATURE_NAMES, hybrid["general_threshold"]),
        "specialist": load_verified(base.ROOT / "models" / "bank_concurrent_specialist.skops", hybrid["specialist_artifact_sha256"], hybrid["specialist_feature_names"], hybrid["specialist_threshold"]),
        "multiclass": load_verified(multiclass_path, multiclass["artifact_sha256"], FEATURE_NAMES, multiclass["threshold_policy"]["threshold"]),
    }
    if artifacts["multiclass"].get("feature_schema_version") != "bank-closed-window-v1":
        raise ValueError("Multiclass feature schema mismatch")
    x = np.load(window.CACHE / "x_validation.npy", mmap_mode="r")
    y = np.load(ALL_CACHE / "y_validation.npy", mmap_mode="r")
    types = np.load(ALL_CACHE / "type_validation.npy", mmap_mode="r")
    quarters = np.load(ALL_CACHE / "quarter_validation.npy", mmap_mode="r")
    amounts = np.load(ALL_CACHE / "amount_validation.npy", mmap_mode="r")
    if x.shape != (len(y), len(FEATURE_NAMES)) or any(len(array) != len(y) for array in (types, quarters, amounts)) or len(y) != meta["counts"]["validation"]:
        raise ValueError("Replay matrix population mismatch")
    scores = {name: np.empty(len(y), dtype=np.float64) for name in artifacts}
    specialist_indices = [FEATURE_NAMES.index(field) for field in hybrid["specialist_feature_names"]]
    for start in range(0, len(y), 100_000):
        end = min(start + 100_000, len(y)); values = x[start:end]
        if not np.isfinite(values).all():
            raise ValueError("Nonfinite replay features")
        scores["general"][start:end] = artifacts["general"]["model"].predict_proba(values)[:, 1]
        scores["specialist"][start:end] = artifacts["specialist"]["model"].predict_proba(values[:, specialist_indices])[:, 1]
        scores["multiclass"][start:end] = anomaly_scores(artifacts["multiclass"]["model"], values)
        print(json.dumps({"stage": "frozen_replay", "rows": end, "total": len(y)}), flush=True)
    if any(not np.isfinite(values).all() or not ((values >= 0) & (values <= 1)).all() for values in scores.values()):
        raise ValueError("Invalid replay model scores")
    alerts = {"general": scores["general"] >= hybrid["general_threshold"],
              "hybrid": (scores["general"] >= hybrid["general_threshold"]) | (scores["specialist"] >= hybrid["specialist_threshold"]),
              "multiclass": scores["multiclass"] >= multiclass["threshold_policy"]["threshold"]}
    metrics = {}
    expected = {"general": (general["evaluation"]["all_2024"], general["evaluation"]["anomaly_type"]),
                "hybrid": (hybrid["evaluation"]["all_2024"], hybrid["evaluation"]["anomaly_type"]),
                "multiclass": (multiclass["evaluation"], multiclass["evaluation"]["subtypes"])}
    for name, alert in alerts.items():
        actual = decision_metrics(y, alert); subtypes = subtype_metrics(y, types, alert)
        verify_replay(actual, expected[name][0], subtypes, expected[name][1])
        metrics[name] = {**actual, "subtypes": subtypes, "readiness": assess(actual, subtypes)}
    masks = {f"quarter/{quarter.decode()}": quarters == quarter for quarter in np.unique(quarters)}
    masks.update({"amount/below_2m": amounts < 2_000_000,
                  "amount/2m_to_10m": (amounts >= 2_000_000) & (amounts < 10_000_000),
                  "amount/at_least_10m": amounts >= 10_000_000,
                  "history/no_previous_outflow": x[:, FEATURE_NAMES.index("sender_all_count_log")] == 0,
                  "history/previous_outflow": x[:, FEATURE_NAMES.index("sender_all_count_log")] > 0,
                  "recipient/no_previous_sender_pair": x[:, FEATURE_NAMES.index("recipient_all_count_log")] == 0,
                  "recipient/previous_sender_pair": x[:, FEATURE_NAMES.index("recipient_all_count_log")] > 0})
    cohorts = {name: {model: decision_metrics(y[mask], alert[mask]) for model, alert in alerts.items()}
               for name, mask in masks.items() if mask.any()}
    pair = {}
    for label, mask in (("positive", y == 1), ("normal", y == 0)):
        pair[label] = {"both_alert": int(np.count_nonzero(mask & alerts["hybrid"] & alerts["multiclass"])),
                       "hybrid_only": int(np.count_nonzero(mask & alerts["hybrid"] & ~alerts["multiclass"])),
                       "multiclass_only": int(np.count_nonzero(mask & ~alerts["hybrid"] & alerts["multiclass"])),
                       "neither_alert": int(np.count_nonzero(mask & ~alerts["hybrid"] & ~alerts["multiclass"]))}
    pair["by_positive_type"] = {}
    for kind in np.unique(types[y == 1]):
        mask = (y == 1) & (types == kind)
        pair["by_positive_type"][kind.decode()] = {
            "hybrid_only": int(np.count_nonzero(mask & alerts["hybrid"] & ~alerts["multiclass"])),
            "multiclass_only": int(np.count_nonzero(mask & ~alerts["hybrid"] & alerts["multiclass"])),
            "neither_alert": int(np.count_nonzero(mask & ~alerts["hybrid"] & ~alerts["multiclass"]))}
    con = sqlite3.connect(f"file:{(base.CACHE / 'stage.sqlite').as_posix()}?mode=ro", uri=True)
    quarter_counts = [{"quarter": row[0], "type": row[1] or "normal", "label": row[2], "rows": row[3]}
        for row in con.execute("SELECT quarter,anomaly_type,label,COUNT(*) FROM events WHERE event_date<'20240101' GROUP BY quarter,anomaly_type,label ORDER BY quarter,anomaly_type,label")]
    con.close()
    output = {"scope": "Frozen delayed closed-window replay; retrospective synthetic labels, not independent test or deployment approval",
              "fit_rows_each_candidate": fit_rows, "evaluated_rows": len(y), "source_manifest": meta["provenance"]["source_manifest"],
              "artifact_sha256": {"general": general["artifact_sha256"], "specialist": hybrid["specialist_artifact_sha256"], "multiclass": multiclass["artifact_sha256"]},
              "thresholds_unchanged": {"general": hybrid["general_threshold"], "specialist": hybrid["specialist_threshold"], "multiclass": multiclass["threshold_policy"]["threshold"]},
              "recorded_confusions_and_subtypes_reproduced": True, "metrics": metrics, "cohorts": cohorts,
              "hybrid_multiclass_disagreement": pair, "pre2024_quarter_type_counts": quarter_counts,
              "diagnostic_only_combinations": {
                  "or": decision_metrics(y, alerts["hybrid"] | alerts["multiclass"]),
                  "and": decision_metrics(y, alerts["hybrid"] & alerts["multiclass"]),
                  "selection_allowed": False, "limit": "Previously inspected2024 diagnostics, not a fitted or selected policy"},
              "promotion": {"candidate_selected": False, "threshold_retuned": False, "operational_release_ready": False},
              "elapsed_seconds": round(time.monotonic() - started, 2), "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(REPORT)
    print(json.dumps({"stage": "audit_completed", "metrics": {name: {key: value[key] for key in ("precision", "recall", "confusion")} for name, value in metrics.items()}, "disagreement": pair}), flush=True)


if __name__ == "__main__":
    main()
