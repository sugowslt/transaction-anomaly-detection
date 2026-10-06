"""Train a delayed window review; same-bucket peers are explicitly observed.

Candidates and threshold use held-out2023Q4. The chosen architecture is then
refit on every pre2024 labeled row, with the development threshold frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score

import train_bank_contextual as base
import train_bank_graph as graph
from bank_context_features import SOURCE_FIELDS
from bank_window_features import FEATURE_NAMES, window_vectors
from evaluate_bank_graph_all_2024 import CACHE as ALL_CACHE

CACHE = base.DATA / ".bank-window-cache"
REPORT = base.ROOT / "reports" / "bank_closed_window.json"
MODEL = base.ROOT / "models" / "bank_closed_window.skops"


def signature():
    old = json.loads((graph.CACHE / "complete.json").read_text(encoding="utf-8"))
    future = json.loads((ALL_CACHE / "complete.json").read_text(encoding="utf-8"))
    if old["source_manifest"] != base.source_fingerprint(base.files()) or old["source_manifest"] != future["source_manifest"]:
        raise ValueError("Window source manifest mismatch")
    if old["feature_code_fingerprint"] != graph.feature_code_fingerprint() or future["feature_code_fingerprint"] != graph.feature_code_fingerprint():
        raise ValueError("Window prior feature provenance mismatch")
    return {"source_manifest": old["source_manifest"], "prior_code": old["feature_code_fingerprint"],
            "window_code": hashlib.sha256(Path(__file__).with_name("bank_window_features.py").read_bytes()).hexdigest()}


def prepare(reuse):
    provenance = signature()
    CACHE.mkdir(exist_ok=True)
    if reuse:
        meta = json.loads((CACHE / "complete.json").read_text(encoding="utf-8"))
        if meta["provenance"] != provenance:
            raise ValueError("Window feature cache provenance mismatch")
        return meta
    completion = CACHE / "complete.json"
    if completion.exists():
        completion.unlink()
    matrices, labels = {}, {}
    for name in ("fit", "tune", "validation"):
        source = ALL_CACHE if name == "validation" else graph.CACHE
        prior = np.load(source / f"x_{name}.npy", mmap_mode="r")
        labels[name] = np.load(source / f"y_{name}.npy", mmap_mode="r")
        matrices[name] = np.lib.format.open_memmap(CACHE / f"x_{name}.npy", mode="w+", dtype=np.float32,
                                                  shape=(len(prior), len(FEATURE_NAMES)))
        matrices[name][:, :66] = prior
    indices = {name: 0 for name in matrices}
    connection = sqlite3.connect(base.CACHE / "stage.sqlite")
    connection.row_factory = sqlite3.Row
    cursor = connection.execute("SELECT rowid,* FROM events ORDER BY event_date,rowid")
    for day, daily in itertools.groupby(cursor, key=lambda row: row["event_date"]):
        rows = [dict(row) for row in daily]
        buckets, senders = defaultdict(list), defaultdict(list)
        for row in rows:
            buckets[row["hour"]].append(row)
            senders[(row["sender_bank"], row["sender"])].append(row)
        extension = {}
        for bucket in buckets.values():
            raw = [{field: event[field] for field in SOURCE_FIELDS} for event in map(base.row_event, bucket)]
            # Never truncate an observed bucket to meet a protocol limit.
            if len(raw) > 20000:
                raise ValueError("Source window exceeds reviewed runtime row bound; expand contract explicitly")
            values = window_vectors(raw)
            extension.update((row["rowid"], value) for row, value in zip(bucket, values))
        for sender_rows in senders.values():
            for row in sender_rows:
                name = "fit" if day <= base.FIT_END else "tune" if day <= base.TUNE_END else "validation" if day.startswith("2024") else None
                if name is None:
                    continue
                index = indices[name]
                if row["label"] != labels[name][index]:
                    raise ValueError("Window/source and chronological prior matrix label order differ")
                matrices[name][index, 66:] = extension[row["rowid"]]
                indices[name] += 1
    connection.close()
    if indices != {name: len(labels[name]) for name in labels}:
        raise ValueError("Incomplete window feature extraction")
    for array in matrices.values():
        array.flush()
    meta = {"provenance": provenance, "counts": indices, "feature_names": FEATURE_NAMES,
            "decision_time": "After all observed rows in a three-hour bucket; includes the current row and same-bucket peers; no later bucket."}
    completion.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "window_features", "counts": indices}), flush=True)
    return meta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-matrices", action="store_true")
    args = parser.parse_args()
    meta = prepare(args.reuse_matrices)
    x = {name: np.load(CACHE / f"x_{name}.npy", mmap_mode="r") for name in meta["counts"]}
    y = {name: np.load((ALL_CACHE if name == "validation" else graph.CACHE) / f"y_{name}.npy", mmap_mode="r") for name in x}
    choices = []
    for params in (
        {"max_iter": 300, "max_leaf_nodes": 63, "min_samples_leaf": 20, "l2_regularization": 1.0},
        {"max_iter": 400, "max_leaf_nodes": 127, "min_samples_leaf": 10, "l2_regularization": 0.5},
    ):
        model = HistGradientBoostingClassifier(**params, early_stopping=False, random_state=42,
                                               categorical_features=list(base.CATEGORICAL_INDICES))
        model.fit(x["fit"], y["fit"])
        dev = model.predict_proba(x["tune"])[:, 1]
        policy = base.f1_threshold(y["tune"], dev)
        choices.append({"params": params, "development_ap": float(average_precision_score(y["tune"], dev)),
                        "policy": policy, "development": base.add_intervals(base.metrics(y["tune"], dev, policy["threshold"]))})
        print(json.dumps({"stage": "window_candidate", **choices[-1]}), flush=True)
    chosen = max(choices, key=lambda item: item["development_ap"])
    rows = len(y["fit"]) + len(y["tune"])
    full_x = np.lib.format.open_memmap(CACHE / "x_full_past.npy", mode="w+", dtype=np.float32, shape=(rows, len(FEATURE_NAMES)))
    full_x[:len(y["fit"])] = x["fit"]
    full_x[len(y["fit"]):] = x["tune"]
    full_y = np.concatenate((y["fit"], y["tune"]))
    model = HistGradientBoostingClassifier(**chosen["params"], early_stopping=False, random_state=42,
                                           categorical_features=list(base.CATEGORICAL_INDICES))
    model.fit(full_x, full_y)
    threshold = chosen["policy"]["threshold"]
    artifact = {"model": model, "mappings": json.loads((base.CACHE / "mappings.json").read_text(encoding="utf-8")),
                "threshold": threshold, "feature_names": FEATURE_NAMES, "feature_schema_version": "bank-closed-window-v1"}
    sio.dump(artifact, MODEL)
    digest = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    scores = model.predict_proba(x["validation"])[:, 1]
    partition = np.load(ALL_CACHE / "partition_validation.npy", mmap_mode="r")
    official = partition == b"validation"
    types = np.load(ALL_CACHE / "type_validation.npy", mmap_mode="r")
    recalls = {}
    for kind in np.unique(types[y["validation"] == 1]):
        mask = (types == kind) & (y["validation"] == 1)
        count = int(mask.sum())
        detected = int(np.count_nonzero(scores[mask] >= threshold))
        recalls[kind.decode("ascii")] = {"positives": count, "detected": detected, "recall": detected / count,
                                        "recall_wilson_95": base.wilson_interval(detected, count)}
    report = {"model": type(model).__name__, "model_version": f"bank-window-{digest[:12]}", "artifact_sha256": digest,
              "feature_schema_version": "bank-closed-window-v1", "feature_names": FEATURE_NAMES,
              "fit_rows": rows, "fit_sampling_rate": 1.0, "candidate_experiments": choices,
              "selected_configuration": chosen["params"], "threshold_policy": chosen["policy"],
              "evaluation": {"all_2024": base.add_intervals(base.metrics(y["validation"], scores, threshold)),
                             "official_validation": base.add_intervals(base.metrics(y["validation"][official], scores[official], threshold)),
                             "anomaly_type": recalls},
              "source_manifest": meta["provenance"]["source_manifest"], "observation_policy": meta["decision_time"],
              "evaluation_limit": "Retrospective synthetic labels already inspected; a delayed closed-window task, not instantaneous scoring or a fresh blind test.",
              "release_ready": False, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "window_full_past", "version": report["model_version"], "evaluation": report["evaluation"]}), flush=True)


if __name__ == "__main__":
    main()
