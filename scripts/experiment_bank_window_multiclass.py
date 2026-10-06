"""Separate synthetic anomaly mechanisms during fitting, not client scoring."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier

import train_bank_contextual as base
import train_bank_graph as graph
import train_bank_closed_window as window
from bank_window_features import FEATURE_NAMES
from evaluate_bank_graph_all_2024 import CACHE as ALL_CACHE
from experiment_bank_window_balanced import evaluate

REPORT = base.ROOT / "reports" / "bank_window_multiclass_experiment.json"
DIRECTORY = base.ROOT / "models" / "bank_window_candidates"
PARAMS = {"max_iter": 300, "learning_rate": 0.05, "max_leaf_nodes": 31, "min_samples_leaf": 10, "l2_regularization": 1.0}


def targets(labels, types):
    if len(labels) != len(types) or not np.isin(labels, [0, 1]).all():
        raise ValueError("Invalid binary labels for subtype fitting")
    result = np.zeros(len(labels), dtype=np.uint8)
    for kind in np.unique(types[labels == 1]):
        if kind not in (b"1.0", b"2.0", b"3.0", b"4.0", b"5.0", b"7.0"):
            raise ValueError("Unknown positive subtype")
        result[(labels == 1) & (types == kind)] = int(float(kind))
    return result


def anomaly_scores(model, values):
    normal = np.flatnonzero(model.classes_ == 0)
    if len(normal) != 1:
        raise ValueError("Multiclass model lacks a unique normal class")
    return 1.0 - model.predict_proba(values)[:, int(normal[0])]


def main():
    meta = window.prepare(True)
    x = np.load(window.CACHE / "x_fit.npy", mmap_mode="r")
    y = np.load(graph.CACHE / "y_fit.npy", mmap_mode="r")
    kinds = np.load(graph.CACHE / "type_fit.npy", mmap_mode="r")
    encoded = targets(y, kinds)
    con = sqlite3.connect(f"file:{(base.CACHE / 'stage.sqlite').as_posix()}?mode=ro", uri=True)
    fit_end = con.execute("SELECT COUNT(*) FROM events WHERE event_date<='20220930'").fetchone()[0]
    dev_end = con.execute("SELECT COUNT(*) FROM events WHERE event_date<='20221231'").fetchone()[0]
    expected = dict(con.execute("SELECT anomaly_type,COUNT(*) FROM events WHERE label=1 AND event_date>'20220930' AND event_date<='20221231' GROUP BY anomaly_type"))
    con.close()
    observed = {kind.decode(): int(np.count_nonzero((kinds[fit_end:dev_end] == kind) & (y[fit_end:dev_end] == 1)))
                for kind in np.unique(kinds[fit_end:dev_end][y[fit_end:dev_end] == 1])}
    if expected != observed:
        raise ValueError("Multiclass temporal fold provenance mismatch")
    DIRECTORY.mkdir(exist_ok=True)
    report = {"target": "Synthetic anomaly labels; closed-window only", "params": PARAMS,
              "feature_names": FEATURE_NAMES, "selection": "Fixed multiclass architecture; binary F1 threshold on2022Q4 with2% alert budget, frozen before full-past refit and2024 evaluation.",
              "early_fit_rows": fit_end, "early_development_rows": dev_end-fit_end, "early_development_types": observed,
              "source_manifest": meta["provenance"]["source_manifest"], "release_ready": False}
    def save(stage):
        report["stage"] = stage
        report["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"stage": stage, **{key: report[key] for key in ("development", "evaluation") if key in report}}), flush=True)
    model = HistGradientBoostingClassifier(**PARAMS, early_stopping=False, random_state=42, categorical_features=list(base.CATEGORICAL_INDICES))
    save("development_fit_started")
    start = time.monotonic()
    model.fit(x[:fit_end], encoded[:fit_end])
    scores = anomaly_scores(model, x[fit_end:dev_end])
    policy = base.f1_threshold(y[fit_end:dev_end], scores)
    report.update(development=evaluate(y[fit_end:dev_end], kinds[fit_end:dev_end], scores, policy["threshold"]),
                  threshold_policy=policy, early_fit_seconds=round(time.monotonic()-start, 2))
    save("development_completed")
    tune_y = np.load(graph.CACHE / "y_tune.npy", mmap_mode="r")
    tune_kinds = np.load(graph.CACHE / "type_tune.npy", mmap_mode="r")
    full_y = np.concatenate((y, tune_y)); full_kinds = np.concatenate((kinds, tune_kinds))
    full_x = np.load(window.CACHE / "x_full_past.npy", mmap_mode="r")
    if len(full_x) != len(full_y): raise ValueError("Full-past matrix/target counts differ")
    save("full_past_fit_started")
    start = time.monotonic()
    model.fit(full_x, targets(full_y, full_kinds))
    path = DIRECTORY / "closed_window_multiclass.skops"
    sio.dump({"model": model, "threshold": policy["threshold"], "feature_names": FEATURE_NAMES,
              "feature_schema_version": "bank-closed-window-v1", "output": "1-P(normal); not a calibrated fraud probability"}, path)
    report.update(fit_rows=len(full_y), fit_seconds=round(time.monotonic()-start, 2), artifact_path=path.relative_to(base.ROOT).as_posix(),
                  artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    save("full_past_fit_completed")
    future_y = np.load(ALL_CACHE / "y_validation.npy", mmap_mode="r")
    future_kinds = np.load(ALL_CACHE / "type_validation.npy", mmap_mode="r")
    future_x = np.load(window.CACHE / "x_validation.npy", mmap_mode="r")
    report["evaluation"] = evaluate(future_y, future_kinds, anomaly_scores(model, future_x), policy["threshold"])
    report["evaluation_limit"] = "Previously examined2024 retrospective synthetic labels, not independent blind validation or proof of real-fraud detection. No default runtime promotion."
    save("completed")


if __name__ == "__main__": main()
