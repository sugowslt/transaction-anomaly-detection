"""Verify all declared artifacts and search a fixed grid on past OOF scores."""
from __future__ import annotations

import gc
import itertools
import json
import time
from datetime import datetime, timezone

import numpy as np
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits

import experiment_bank_mechanism_heads as experiment
from audit_model_readiness import assess

ROOT = experiment.base.ROOT
POLICY_PROTOCOL = ROOT / "reports" / "bank_mechanism_policy_protocol.json"
REPORT = ROOT / "reports" / "bank_mechanism_policy_selection.json"
HEADS = ("general", "type1", "type2")


def array_digest(*arrays):
    import hashlib
    checksum = hashlib.sha256()
    for array in arrays:
        checksum.update(np.ascontiguousarray(array).data)
    return checksum.hexdigest()


def verify_report(report, fold, configuration, protocol, provenance):
    signature = report["signature"]
    expected_params = {**protocol["first_experiment"]["configurations"][configuration - 1],
                       **{key: protocol["first_experiment"][key] for key in ("random_state", "early_stopping", "class_weight")}}
    if (report.get("stage") != "completed_one_fold_configuration" or signature["fold"] != fold
            or signature["configuration"] != configuration or signature["parameters"] != expected_params
            or signature["protocol_sha256"] != experiment.digest(experiment.PROTOCOL)
            or signature["experiment_code_sha256"] != experiment.digest(experiment.__file__)
            or signature["cache_provenance"] != provenance or set(report["models"]) != set(HEADS)
            or report.get("operational_release_ready") is not False or report.get("runtime_model_changed") is not False):
        raise ValueError("Incomplete or mismatched fold report")
    if signature["environment"] != {"numpy": np.__version__, "sklearn": __import__("sklearn").__version__, "skops": __import__("skops").__version__}:
        raise ValueError("Fold environment changed")


def threshold_grid(scores, rates):
    if not len(scores) or not np.isfinite(scores).all() or not ((scores >= 0) & (scores <= 1)).all():
        raise ValueError("Invalid OOF score values")
    if not rates or any(not 0 < rate <= .02 for rate in rates):
        raise ValueError("Undeclared or invalid alert-rate grid")
    return [None] + sorted(set(float(value) for value in np.quantile(scores, 1 - np.asarray(rates), method="higher")), reverse=True)


def packed_count(values):
    return int(np.bitwise_count(values).sum(dtype=np.uint64))


def search_policy(y, types, scores, rates, targets, budget):
    """Packed masks preserve exact decisions and avoid an N-by-policy matrix."""
    if len(y) != len(types) or not np.isin(y, [0, 1]).all() or any(len(scores[head]) != len(y) for head in HEADS):
        raise ValueError("Invalid OOF policy population")
    grids = {head: threshold_grid(scores[head], rates) for head in HEADS}
    masks = {head: [np.packbits(np.zeros(len(y), dtype=bool) if threshold is None else scores[head] >= threshold)
                    for threshold in grids[head]] for head in HEADS}
    positive = np.packbits(y == 1)
    kinds = {kind.decode(): np.packbits((y == 1) & (types == kind)) for kind in np.unique(types[y == 1])}
    counts = {kind: packed_count(mask) for kind, mask in kinds.items()}
    positives = int(y.sum())
    best, best_precision, best_f1 = None, None, None
    evaluated = eligible = 0
    for indices in itertools.product(*(range(len(grids[head])) for head in HEADS)):
        combined = masks["general"][indices[0]] | masks["type1"][indices[1]] | masks["type2"][indices[2]]
        alerts = packed_count(combined); tp = packed_count(combined & positive); fp = alerts - tp
        if not alerts:
            evaluated += 1
            continue
        precision, recall = tp / alerts, tp / positives
        point = {"thresholds": {head: grids[head][index] for head, index in zip(HEADS, indices)},
                 "tp": tp, "fp": fp, "fn": positives - tp, "precision": precision, "recall": recall,
                 "alert_rate": alerts / len(y), "f1": 2 * tp / (positives + alerts)}
        evaluated += 1
        if point["alert_rate"] > budget:
            continue
        if best_f1 is None or (point["f1"], tp, -fp) > (best_f1["f1"], best_f1["tp"], -best_f1["fp"]):
            best_f1 = point
        if precision < targets["precision"]:
            continue
        if best_precision is None or (tp, -fp) > (best_precision["tp"], -best_precision["fp"]):
            best_precision = point
        if recall < targets["recall"]:
            continue
        if any(packed_count(combined & mask) / counts[kind] < targets["each_observed_type_recall"] for kind, mask in kinds.items()):
            continue
        eligible += 1
        if best is None or (tp, -fp) > (best["tp"], -best["fp"]):
            best = point
    return {"grid_sizes": {head: len(grid) for head, grid in grids.items()}, "policies_evaluated": evaluated,
            "eligible_policies": eligible, "selected": best, "precision_constrained_diagnostic": best_precision,
            "f1_diagnostic": best_f1}


def apply_policy(scores, thresholds):
    result = np.zeros(len(scores["general"]), dtype=bool)
    for head in HEADS:
        threshold = thresholds[head]
        if threshold is not None:
            result |= scores[head] >= threshold
    return result


def main():
    started = time.monotonic()
    protocol = json.loads(experiment.PROTOCOL.read_text(encoding="utf-8"))
    plan = json.loads(POLICY_PROTOCOL.read_text(encoding="utf-8"))
    experiment.validate_boundaries(protocol)
    if plan["parent_protocol_sha256"] != experiment.digest(experiment.PROTOCOL) or plan["configurations"] != [1, 2]:
        raise ValueError("Policy plan differs from declared experiment")
    # Require every unit before opening model files or producing pooled selection.
    reports = {}
    for fold in protocol["selection_folds"]:
        for config in plan["configurations"]:
            path = ROOT / "reports" / f"bank_mechanism_{fold['id']}_config{config}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("stage") != "completed_one_fold_configuration":
                raise ValueError("All eight declared fold/configuration units must complete")
            reports[(fold["id"], config)] = record
    meta = experiment.window.prepare(True)
    pooled_y, pooled_types, offsets, evidence = [], [], {}, []
    pooled_scores = {config: {head: [] for head in HEADS} for config in plan["configurations"]}
    offset = 0
    for fold in protocol["selection_folds"]:
        xf, yf, tf, xd, yd, td, _, population = experiment.prepare_fold(fold, meta)
        feature_sha, target_sha = array_digest(xf, xd), array_digest(yf, yd, tf, td)
        offsets[fold["id"]] = (offset, offset + len(yd)); offset += len(yd)
        pooled_y.append(yd.copy()); pooled_types.append(td.copy())
        for config in plan["configurations"]:
            record = reports[(fold["id"], config)]
            verify_report(record, fold, config, protocol, meta["provenance"])
            signature = record["signature"]
            if signature["population"] != population or signature["fold_feature_sha256"] != feature_sha or signature["fold_target_sha256"] != target_sha:
                raise ValueError("OOF population or feature/target fingerprint mismatch")
            for head in HEADS:
                entry = record["models"][head]
                relative = f"models/bank_window_candidates/mechanism_heads/{fold['id']}-config{config}/{head}.skops"
                score_relative = f"data/.bank-mechanism-cache/{fold['id']}-config{config}/{head}.npy"
                if entry["artifact_path"] != relative or entry["score_path"] != score_relative:
                    raise ValueError("Unexpected model or score path")
                artifact = experiment.load_verified(ROOT / relative, entry["artifact_sha256"], experiment.FEATURE_NAMES, None)
                if (artifact["fold"] != fold or artifact["target"] != head or artifact["mappings"] != population["raw_category_mappings"]
                        or artifact["protocol_sha256"] != signature["protocol_sha256"] or artifact["model"].n_iter_ != signature["parameters"]["max_iter"]):
                    raise ValueError("OOF model target, preprocessing or iterations mismatch")
                actual_parameters = artifact["model"].get_params()
                if any(actual_parameters[key] != value for key, value in signature["parameters"].items()) or not np.array_equal(artifact["model"].classes_, [0, 1]):
                    raise ValueError("OOF estimator parameters or classes mismatch")
                if experiment.digest(ROOT / score_relative) != entry["score_sha256"]:
                    raise ValueError("OOF score hash mismatch")
                saved = np.load(ROOT / score_relative)
                actual = artifact["model"].predict_proba(xd)[:, 1]
                if saved.shape != (len(yd),) or not np.array_equal(saved, actual):
                    raise ValueError("OOF saved predictions differ from artifact inference")
                target = yd if head == "general" else ((yd == 1) & (td == (head[-1] + ".0").encode())).astype(np.uint8)
                fit_positive_count = int(yf.sum()) if head == "general" else int(np.count_nonzero((yf == 1) & (tf == (head[-1] + ".0").encode())))
                if entry["fit_rows"] != len(yf) or entry["fit_positives"] != fit_positive_count or entry["development_positives"] != int(target.sum()):
                    raise ValueError("OOF supervised target counts mismatch")
                diagnostic = experiment.decision_metrics(target, saved >= entry["f1_diagnostic_policy"]["threshold"])
                if diagnostic["confusion"] != entry["f1_diagnostic"]["confusion"]:
                    raise ValueError("OOF reported confusion differs from predictions")
                pooled_scores[config][head].append(saved)
                evidence.append({"fold": fold["id"], "configuration": config, "head": head,
                                 "replayed_rows": len(saved), "artifact_sha256": entry["artifact_sha256"],
                                 "score_sha256": entry["score_sha256"], "exact_predictions_match": True})
            print(json.dumps({"stage": "verified_oof_unit", "fold": fold["id"], "configuration": config, "rows": len(yd)}), flush=True)
        del xf, yf, tf, xd, yd, td, artifact
        gc.collect()
    y, types = np.concatenate(pooled_y), np.concatenate(pooled_types)
    results = {}
    for config, heads in pooled_scores.items():
        scores = {head: np.concatenate(values) for head, values in heads.items()}
        search = search_policy(y, types, scores, plan["threshold_grid"]["per_head_alert_rates"],
                               protocol["selection_rule"]["research_targets"], protocol["selection_rule"]["maximum_pooled_alert_rate"])
        search["head_average_precision"] = {head: float(average_precision_score(y if head == "general" else ((y == 1) & (types == (head[-1] + ".0").encode())), values))
                                            for head, values in scores.items()}
        for key in ("selected", "precision_constrained_diagnostic", "f1_diagnostic"):
            if search[key] is None:
                continue
            flag = apply_policy(scores, search[key]["thresholds"])
            metrics = experiment.evaluate(y, types, flag)
            search[key]["metrics"] = metrics
            search[key]["target_check"] = assess(metrics, metrics["subtypes"])
            search[key]["folds"] = {fold: experiment.evaluate(y[start:end], types[start:end], flag[start:end]) for fold, (start, end) in offsets.items()}
        results[str(config)] = search
        print(json.dumps({"stage": "pooled_grid_completed", "configuration": config, "policies": search["policies_evaluated"],
                          "eligible": search["eligible_policies"], "precision_diagnostic": search["precision_constrained_diagnostic"]}), flush=True)
    eligible = [(config, item["selected"]) for config, item in results.items() if item["selected"] is not None]
    chosen = max(eligible, key=lambda item: (item[1]["tp"], -item[1]["fp"], -int(item[0]))) if eligible else None
    report = {"stage": "pooled_selection_completed", "protocol_sha256": experiment.digest(experiment.PROTOCOL),
              "policy_protocol_sha256": experiment.digest(POLICY_PROTOCOL), "selection_code_sha256": experiment.digest(__file__),
              "pooled_rows": len(y), "pooled_positives": int(y.sum()), "pooled_positive_types": experiment.type_counts(y, types),
              "models_verified": len(evidence), "artifact_evidence": evidence, "configurations": results,
              "selected_policy": {"configuration": int(chosen[0]), **chosen[1]} if chosen else None,
              "all_research_targets_passed": chosen is not None, "operational_release_ready": False, "runtime_model_changed": False,
              "evaluation_limit": plan["evaluation_limit"], "grid_limit": plan["threshold_grid"]["limit"],
              "seconds": round(time.monotonic() - started, 3), "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    experiment.write_json(REPORT, report)
    print(json.dumps({"stage": report["stage"], "rows": len(y), "positives": int(y.sum()), "selected": chosen is not None,
                      "report": str(REPORT), "seconds": report["seconds"]}), flush=True)


if __name__ == "__main__":
    with threadpool_limits(limits=8):
        main()
