"""One resumable higher-capacity past-fold experiment; no runtime replacement."""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits

import experiment_bank_mechanism_heads as experiment
from select_bank_mechanism_policy import array_digest

PLAN = experiment.base.ROOT / "reports" / "bank_capacity_protocol.json"


def parameters(plan, configuration):
    spec = next((item for item in plan["configurations"] if item["id"] == configuration), None)
    if spec is None:
        raise ValueError("Undeclared capacity configuration")
    weight = spec["positive_weight"]
    if not np.isfinite(weight) or weight < 1:
        raise ValueError("Invalid positive fitting weight")
    return {**{key: value for key, value in spec.items() if key not in ("id", "positive_weight")},
            **plan["fixed"], "class_weight": None if weight == 1 else {0: 1.0, 1: weight}}


def run(fold_id, configuration):
    started = time.monotonic()
    plan = json.loads(PLAN.read_text(encoding="utf-8")); protocol = json.loads(experiment.PROTOCOL.read_text(encoding="utf-8"))
    experiment.validate_boundaries(protocol)
    if plan["parent_protocol_sha256"] != experiment.digest(experiment.PROTOCOL):
        raise ValueError("Capacity protocol parent fingerprint mismatch")
    fold = next((item for item in protocol["selection_folds"] if item["id"] == fold_id), None)
    if fold is None or fold_id not in plan["folds"]:
        raise ValueError("Undeclared capacity fold")
    params = parameters(plan, configuration)
    meta = experiment.window.prepare(True)
    xf, yf, tf, xd, yd, td, _, population = experiment.prepare_fold(fold, meta)
    signature = {"capacity_plan_sha256": experiment.digest(PLAN), "parent_protocol_sha256": experiment.digest(experiment.PROTOCOL),
                 "code_sha256": experiment.digest(__file__), "feature_preparation_code_sha256": experiment.digest(experiment.__file__),
                 "fold": fold, "configuration": configuration, "parameters": json.loads(json.dumps(params)),
                 "population": population, "cache_provenance": meta["provenance"],
                 "feature_sha256": array_digest(xf, xd), "target_sha256": array_digest(yf, yd, tf, td),
                 "environment": {"numpy": np.__version__, "sklearn": __import__("sklearn").__version__, "skops": __import__("skops").__version__}}
    report_path = experiment.base.ROOT / "reports" / f"bank_capacity_{fold_id}_config{configuration}.json"
    output = experiment.base.ROOT / "models" / "bank_window_candidates" / "capacity" / f"{fold_id}-config{configuration}"
    score_dir = experiment.base.DATA / ".bank-capacity-cache" / f"{fold_id}-config{configuration}"
    output.mkdir(parents=True, exist_ok=True); score_dir.mkdir(parents=True, exist_ok=True)
    path = output / "general.skops"; score_path = score_dir / "development.npy"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8")); experiment.verify_resume(signature, report)
    else:
        report = {"stage": "prepared", "signature": signature, "fit_rows": len(yf), "development_rows": len(yd),
                  "fit_sample_rate": 1.0, "operational_release_ready": False, "runtime_model_changed": False,
                  "selection_complete": False, "evaluation_limit": plan["diagnostics"]}
        experiment.write_json(report_path, report)
    if "artifact_sha256" not in report:
        print(json.dumps({"stage": "capacity_fit_started", "fold": fold_id, "configuration": configuration,
                          "rows": len(yf), "positives": int(yf.sum()), "iterations": params["max_iter"]}), flush=True)
        fit_started = time.monotonic()
        model = HistGradientBoostingClassifier(**params, categorical_features=list(experiment.base.CATEGORICAL_INDICES))
        model.fit(xf, yf)
        artifact = {"model": model, "feature_names": experiment.FEATURE_NAMES, "threshold": None,
                    "mappings": population["raw_category_mappings"], "fold": fold, "configuration": configuration,
                    "capacity_plan_sha256": signature["capacity_plan_sha256"]}
        temporary = path.with_suffix(".tmp.skops"); sio.dump(artifact, temporary); temporary.replace(path)
        report.update(stage="fitted", artifact_path=path.relative_to(experiment.base.ROOT).as_posix(),
                      artifact_sha256=experiment.digest(path), iterations=int(model.n_iter_),
                      fit_seconds=round(time.monotonic() - fit_started, 3))
        experiment.write_json(report_path, report)
    artifact = experiment.load_verified(path, report["artifact_sha256"], experiment.FEATURE_NAMES, None)
    if (artifact["mappings"] != population["raw_category_mappings"] or artifact["fold"] != fold
            or artifact["configuration"] != configuration or artifact["capacity_plan_sha256"] != signature["capacity_plan_sha256"]
            or any(artifact["model"].get_params()[key] != value for key, value in params.items())):
        raise ValueError("Capacity artifact input or parameter contract mismatch")
    if "score_sha256" in report:
        if experiment.digest(score_path) != report["score_sha256"]:
            raise ValueError("Capacity score fingerprint mismatch")
        scores = np.load(score_path)
    else:
        scores = artifact["model"].predict_proba(xd)[:, 1]
        experiment.save_scores(score_path, scores)
        report.update(score_path=score_path.relative_to(experiment.base.ROOT).as_posix(), score_sha256=experiment.digest(score_path))
        experiment.write_json(report_path, report)
    if scores.shape != (len(yd),) or not np.isfinite(scores).all() or not ((scores >= 0) & (scores <= 1)).all():
        raise ValueError("Invalid capacity scores")
    if "train_average_precision" not in report:
        report["train_average_precision"] = float(average_precision_score(yf, artifact["model"].predict_proba(xf)[:, 1]))
    budget = protocol["selection_rule"]["maximum_pooled_alert_rate"]
    policy = experiment.base.f1_threshold(yd, scores, budget)
    strict = experiment.precision_policy(yd, scores, protocol["selection_rule"]["research_targets"]["precision"], budget)
    report.update(stage="completed_one_capacity_unit", development_average_precision=float(average_precision_score(yd, scores)),
                  f1_diagnostic_policy=policy, f1_diagnostic=experiment.evaluate(yd, td, scores >= policy["threshold"]),
                  precision_constrained_diagnostic_policy=strict,
                  precision_constrained_diagnostic=experiment.evaluate(yd, td, scores >= strict["threshold"]) if strict["feasible"] else None,
                  last_run_seconds=round(time.monotonic() - started, 3))
    experiment.write_json(report_path, report)
    print(json.dumps({"stage": report["stage"], "fold": fold_id, "configuration": configuration,
                      "train_ap": report["train_average_precision"], "development_ap": report["development_average_precision"],
                      "f1": report["f1_diagnostic"], "precision_point": report["precision_constrained_diagnostic"],
                      "seconds": report["last_run_seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--fold", required=True); parser.add_argument("--configuration", type=int, required=True)
    args = parser.parse_args()
    with threadpool_limits(limits=8):
        run(args.fold, args.configuration)
