"""Check measured research targets without declaring real-bank readiness."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from release_assets import ROOT

TARGETS = {"precision": 0.90, "recall": 0.80, "each_observed_type_recall": 0.50}


def verify_metrics(metrics):
    confusion = metrics["confusion"]
    if set(confusion) != {"tn", "fp", "fn", "tp"} or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in confusion.values()):
        raise ValueError("Invalid confusion matrix")
    total = sum(confusion.values())
    tp, fp, fn = (confusion[name] for name in ("tp", "fp", "fn"))
    if total <= 0 or metrics["rows"] != total or metrics["positives"] != tp + fn:
        raise ValueError("Confusion counts disagree with evaluation population")
    for name, value in (("precision", tp / max(1, tp + fp)), ("recall", tp / max(1, tp + fn))):
        if not math.isclose(metrics[name], value, rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError(f"Reported {name} disagrees with counts")


def assess(metrics, types):
    verify_metrics(metrics)
    failures = []
    for key in ("precision", "recall"):
        if metrics[key] < TARGETS[key]:
            failures.append({"check": key, "measured": metrics[key], "target": TARGETS[key]})
    for name, cohort in types.items():
        count = cohort["positives"]
        detected = cohort["detected"]
        if not isinstance(count, int) or not isinstance(detected, int) or not 0 <= detected <= count:
            raise ValueError("Invalid subtype counts")
        if not count:
            failures.append({"check": "type_not_evaluated", "type": name})
        elif detected / count < TARGETS["each_observed_type_recall"]:
            failures.append({"check": "type_recall", "type": name, "measured": detected / count,
                             "target": TARGETS["each_observed_type_recall"], "missed": count - detected, "positives": count})
    if not types:
        failures.append({"check": "missing_subtype_evaluation"})
    if sum(item["positives"] for item in types.values()) != metrics["positives"] or sum(item["detected"] for item in types.values()) != metrics["confusion"]["tp"]:
        raise ValueError("Subtype totals disagree with binary evaluation counts")
    return {"measured_research_targets_passed": not failures, "failures": failures,
            "operational_release_ready": False,
            "operational_limit": "Retrospective synthetic labels do not establish real-fraud detection or independently confirmed future performance."}


def multiclass_readiness(root=ROOT):
    path = root / "reports" / "bank_window_multiclass_experiment.json"
    if not path.exists():
        return {"status": "evaluation_not_completed", "operational_release_ready": False}
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("stage") != "completed":
        return {"status": "evaluation_not_completed", "operational_release_ready": False}
    artifact = root / "models" / "bank_window_candidates" / "closed_window_multiclass.skops"
    if report.get("artifact_path") != artifact.relative_to(root).as_posix():
        raise ValueError("Unexpected multiclass readiness artifact path")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if report.get("artifact_sha256") != digest or report.get("release_ready") is not False:
        raise ValueError("Multiclass readiness artifact/report mismatch")
    metrics = report["evaluation"]
    if metrics["threshold"] != report["threshold_policy"]["threshold"]:
        raise ValueError("Multiclass evaluation threshold mismatch")
    return {"target": report["target"], "candidate_id": f"bank-window-multiclass-{digest[:12]}",
            "artifact_sha256": digest, "fit_rows": report["fit_rows"], "evaluated_rows": metrics["rows"],
            "precision": metrics["precision"], "recall": metrics["recall"],
            "deployment_enabled_by_default": False, **assess(metrics, metrics["subtypes"])}


def main():
    results = {}
    card = json.loads((ROOT / "reports" / "card_final_refit.json").read_text(encoding="utf-8"))
    general_path = ROOT / card["artifact_path"].replace("\\", "/")
    combined = card["combined_all_2024"]
    results["card_all_past_combined"] = {"fit_rows": card["rows"]["fit"], "evaluated_rows": combined["rows"],
        "general_artifact_sha256": hashlib.sha256(general_path.read_bytes()).hexdigest(),
        "specialist_artifact_sha256": hashlib.sha256((ROOT / card["specialist_artifact_path"].replace("\\", "/")).read_bytes()).hexdigest(),
        "precision": combined["precision"], "recall": combined["recall"], **assess(combined, combined["subtypes"])}
    for name, filename, key in (
        ("bank_immediate_all_past", "bank_contextual_v2_full_past_all_2024.json", "validation"),
        ("bank_closed_window_all_past", "bank_closed_window.json", "all_2024"),
        ("bank_closed_window_hybrid_all_past", "bank_closed_window_hybrid.json", "all_2024"),
    ):
        path = ROOT / "reports" / filename
        if not path.exists():
            results[name] = {"status": "evaluation_not_completed", "operational_release_ready": False}
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        metrics = report["evaluation"][key]
        results[name] = {"model_version": report["model_version"], "evaluated_rows": metrics["rows"],
                         "precision": metrics["precision"], "recall": metrics["recall"],
                         **assess(metrics, report["evaluation"]["anomaly_type"])}
    hybrid = json.loads((ROOT / "reports" / "bank_closed_window_hybrid.json").read_text(encoding="utf-8"))
    head = hybrid["evaluation"]["concurrent_specialist"]
    results["bank_concurrent_specialist_only"] = {"target": "Synthetic type4 only; delayed closed-window review",
        "model_version": hybrid["specialist_model_version"], "evaluated_rows": head["rows"],
        "precision": head["precision"], "recall": head["recall"],
        **assess(head, {"4.0": {"positives": head["positives"], "detected": head["confusion"]["tp"]}})}
    results["bank_closed_window_multiclass_all_past"] = multiclass_readiness()
    report = {"research_targets": TARGETS, "target_basis": "Explicit engineering research objectives, not a banking compliance standard or service guarantee.",
              "models": results, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (ROOT / "reports" / "model_readiness.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
