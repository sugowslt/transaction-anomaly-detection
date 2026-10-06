"""Earlier temporal validation for rare transfer types, never tune on 2024."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score

import train_bank_contextual as base
import train_bank_graph as graph
import train_bank_closed_window as window
from evaluate_bank_graph_all_2024 import CACHE as ALL_CACHE
from bank_window_features import FEATURE_NAMES


def weights(y, types, balanced):
    values = np.ones(len(y), dtype=np.float64)
    if not balanced:
        return values
    labels, counts = np.unique(types[y == 1], return_counts=True)
    for label, count in zip(labels, counts):
        values[(y == 1) & (types == label)] = min(8.0, np.sqrt(counts.sum() / (len(labels) * count)))
    # Keep the positive class's total mass unchanged; the type labels affect fitting only.
    values[y == 1] *= int(np.count_nonzero(y)) / values[y == 1].sum()
    return values


def evaluate(y, types, scores, threshold):
    result = base.add_intervals(base.metrics(y, scores, threshold))
    result["subtypes"] = {}
    for label in np.unique(types[y == 1]):
        mask = (y == 1) & (types == label)
        count = int(mask.sum()); detected = int(np.count_nonzero(scores[mask] >= threshold))
        result["subtypes"][label.decode()] = {"positives": count, "detected": detected, "recall": detected / count,
                                              "recall_wilson_95": base.wilson_interval(detected, count)}
    return result


def main():
    meta = window.prepare(True)
    x = np.load(window.CACHE / "x_fit.npy", mmap_mode="r")
    y = np.load(graph.CACHE / "y_fit.npy", mmap_mode="r")
    types = np.load(graph.CACHE / "type_fit.npy", mmap_mode="r")
    con = sqlite3.connect(f"file:{(base.CACHE / 'stage.sqlite').as_posix()}?mode=ro", uri=True)
    fit_end = con.execute("SELECT COUNT(*) FROM events WHERE event_date<='20220930'").fetchone()[0]
    dev_end = con.execute("SELECT COUNT(*) FROM events WHERE event_date<='20221231'").fetchone()[0]
    expected = dict(con.execute("SELECT anomaly_type,COUNT(*) FROM events WHERE label=1 AND event_date>'20220930' AND event_date<='20221231' GROUP BY anomaly_type"))
    con.close()
    observed = {kind.decode(): int(np.count_nonzero((types[fit_end:dev_end] == kind) & (y[fit_end:dev_end] == 1))) for kind in np.unique(types[fit_end:dev_end][y[fit_end:dev_end] == 1])}
    if expected != observed or len(y) != meta["counts"]["fit"]:
        raise ValueError("Earlier temporal fold/cache order mismatch")
    params = {"max_iter": 400, "max_leaf_nodes": 63, "min_samples_leaf": 20, "l2_regularization": 1.0}
    candidates = []
    for balanced in (False, True):
        model = HistGradientBoostingClassifier(**params, early_stopping=False, random_state=42, categorical_features=list(base.CATEGORICAL_INDICES))
        model.fit(x[:fit_end], y[:fit_end], sample_weight=weights(y[:fit_end], types[:fit_end], balanced))
        scores = model.predict_proba(x[fit_end:dev_end])[:, 1]
        policy = base.f1_threshold(y[fit_end:dev_end], scores)
        result = evaluate(y[fit_end:dev_end], types[fit_end:dev_end], scores, policy["threshold"])
        candidates.append({"balanced": balanced, "early_development": result, "policy": policy})
        print(json.dumps({"stage": "earlier_fold", **candidates[-1]}), flush=True)
    # Earlier-fold AP selects fitting policy. The later development interval freezes its threshold.
    chosen = max(candidates, key=lambda item: item["early_development"]["average_precision"])
    xt = np.load(window.CACHE / "x_tune.npy", mmap_mode="r")
    yt = np.load(graph.CACHE / "y_tune.npy", mmap_mode="r")
    tt = np.load(graph.CACHE / "type_tune.npy", mmap_mode="r")
    model = HistGradientBoostingClassifier(**params, early_stopping=False, random_state=42, categorical_features=list(base.CATEGORICAL_INDICES))
    model.fit(x, y, sample_weight=weights(y, types, chosen["balanced"]))
    dev_scores = model.predict_proba(xt)[:, 1]
    policy = base.f1_threshold(yt, dev_scores)
    development = evaluate(yt, tt, dev_scores, policy["threshold"])
    print(json.dumps({"stage": "late_development", "balanced": chosen["balanced"], "development": development}), flush=True)
    full_x = np.load(window.CACHE / "x_full_past.npy", mmap_mode="r")
    full_y = np.concatenate((y, yt)); full_types = np.concatenate((types, tt))
    model.fit(full_x, full_y, sample_weight=weights(full_y, full_types, chosen["balanced"]))
    directory = base.ROOT / "models" / "bank_window_candidates"
    directory.mkdir(exist_ok=True)
    path = directory / "earlier_temporal_selected.skops"
    sio.dump({"model": model, "threshold": policy["threshold"], "feature_names": FEATURE_NAMES,
              "feature_schema_version": "bank-closed-window-v1"}, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    future_x = np.load(window.CACHE / "x_validation.npy", mmap_mode="r")
    future_y = np.load(ALL_CACHE / "y_validation.npy", mmap_mode="r")
    future_types = np.load(ALL_CACHE / "type_validation.npy", mmap_mode="r")
    result = evaluate(future_y, future_types, model.predict_proba(future_x)[:, 1], policy["threshold"])
    report = {"earlier_fold": {"fit_end": "20220930", "development_end": "20221231", "fit_rows": fit_end,
                               "development_rows": dev_end - fit_end, "positive_types": observed},
              "candidates": candidates, "selection": "Max AP on 2022Q4, threshold F1 on 2023Q4, full past refit; no 2024 selection or tuning.",
              "selected_balanced": chosen["balanced"], "weight_policy": "Capped square-root inverse subtype frequency, normalized positive mass; labels used during fitting only.",
              "development": development, "threshold_policy": policy, "fit_rows": len(full_y), "params": params,
              "artifact_path": path.relative_to(base.ROOT).as_posix(), "artifact_sha256": digest,
              "evaluation": {"all_2024": result}, "source_manifest": meta["provenance"]["source_manifest"],
              "release_ready": False, "evaluation_limit": "Previously inspected retrospective synthetic labels; not a fresh independent test or real-fraud proof.",
              "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (base.ROOT / "reports" / "bank_window_balanced_experiment.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "rare_types_final", "balanced": chosen["balanced"], "evaluation": result}), flush=True)


if __name__ == "__main__":
    main()
