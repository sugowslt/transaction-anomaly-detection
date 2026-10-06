"""Identify the packaged models and check their paired reports without dependencies."""

import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KINDS = ("card", "bank")


def model_path(kind: str, root: Path = ROOT) -> Path:
    if kind not in KINDS:
        raise ValueError(f"Unknown model kind: {kind}")
    return root / "models" / f"{kind}_baseline.skops"


def model_version(kind: str, root: Path = ROOT) -> str:
    digest = hashlib.sha256(model_path(kind, root).read_bytes()).hexdigest()
    return f"{kind}-{digest[:12]}"


def verify_report_versions(root: Path = ROOT) -> None:
    tradeoff_path = root / "reports" / "threshold_tradeoff.json"
    tradeoff = json.loads(tradeoff_path.read_text(encoding="utf-8"))
    for kind in KINDS:
        expected = model_version(kind, root)
        for suffix in ("", "_audit"):
            report_path = root / "reports" / f"{kind}_baseline{suffix}.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("model_version") != expected:
                raise ValueError(
                    f"{report_path} model_version does not match {model_path(kind, root)}: "
                    f"expected {expected}, found {report.get('model_version')!r}"
                )
        actual = tradeoff.get("models", {}).get(kind, {}).get("model_version")
        if actual != expected:
            raise ValueError(
                f"{tradeoff_path} {kind} model_version does not match {model_path(kind, root)}: "
                f"expected {expected}, found {actual!r}"
            )


def verify_contextual_report(root: Path = ROOT) -> dict:
    """Reject missing, stale, or internally inconsistent contextual releases."""
    return _verify_context_report(root, "bank_contextual_v1", "bank-context-v1")


def verify_graph_report(root: Path = ROOT) -> dict:
    """A research candidate must still have an exact artifact/report contract."""
    report = _verify_context_report(root, "bank_contextual_v2_candidate", "bank-context-v2-candidate")
    names = report.get("feature_names", [])
    reference = report.get("explanation_reference", {})
    values = reference.get("values", [])
    if (len(names) != 66 or len(set(names)) != 66 or reference.get("feature_names") != names or
            len(values) != 66 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values) or
            isinstance(reference.get("normal_fit_rows"), bool) or not isinstance(reference.get("normal_fit_rows"), int) or reference["normal_fit_rows"] <= 0 or
            report.get("deployment_enabled_by_default") is not False):
        raise ValueError("Invalid graph candidate explanation or deployment contract")
    return report


def verify_window_report(root: Path = ROOT) -> dict:
    """Delayed review is a separate task; reject stale identities and overstated releases."""
    from bank_window_features import FEATURE_NAMES
    artifact = root / "models" / "bank_closed_window.skops"
    report = json.loads((root / "reports" / "bank_closed_window.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    threshold = report.get("threshold_policy", {}).get("threshold")
    if (report.get("artifact_sha256") != digest or report.get("model_version") != f"bank-window-{digest[:12]}" or
            report.get("feature_schema_version") != "bank-closed-window-v1" or tuple(report.get("feature_names", ())) != FEATURE_NAMES or
            report.get("release_ready") is not False):
        raise ValueError("Closed-window identity or research contract mismatch")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid closed-window threshold")
    for key in ("all_2024", "official_validation"):
        metric = report.get("evaluation", {}).get(key, {})
        counts = metric.get("confusion", {})
        if (set(counts) != {"tn", "fp", "fn", "tp"} or
                any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts.values()) or
                metric.get("rows") != sum(counts.values()) or sum(counts.values()) <= 0 or
                metric.get("positives") != counts["tp"] + counts["fn"] or metric.get("threshold") != threshold):
            raise ValueError("Invalid closed-window evaluation counts")
        for name, expected in (("precision", counts["tp"] / max(1, counts["tp"] + counts["fp"])),
                               ("recall", counts["tp"] / max(1, counts["tp"] + counts["fn"]))):
            value = metric.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isclose(value, expected, rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f"Closed-window {name} disagrees with counts")
    return report


def verify_window_hybrid_report(root: Path = ROOT) -> dict:
    """Bind both frozen heads and measured OR outcomes to their exact artifacts."""
    from bank_window_features import FEATURE_NAMES
    general = verify_window_report(root)
    report = json.loads((root / "reports" / "bank_closed_window_hybrid.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((root / "models" / "bank_concurrent_specialist.skops").read_bytes()).hexdigest()
    version_digest = hashlib.sha256(f"{general['artifact_sha256']}:{digest}".encode("ascii")).hexdigest()
    if (report.get("model_version") != f"bank-window-hybrid-{version_digest[:12]}" or
            report.get("general_model_version") != general["model_version"] or
            report.get("general_artifact_sha256") != general["artifact_sha256"] or
            report.get("specialist_artifact_sha256") != digest or
            report.get("specialist_model_version") != f"bank-concurrent-{digest[:12]}" or
            report.get("specialist_feature_names") != list(FEATURE_NAMES[:6] + FEATURE_NAMES[66:]) or
            report.get("general_threshold") != general["threshold_policy"]["threshold"] or
            report.get("decision_policy") != "general_or_concurrent_specialist_v1" or
            report.get("feature_schema_version") != "bank-closed-window-v1" or
            report.get("release_ready") is not False or report.get("deployment_enabled_by_default") is not False):
        raise ValueError("Window hybrid artifact or research contract mismatch")
    threshold = report.get("specialist_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0.5 <= threshold <= 1:
        raise ValueError("Invalid strict specialist threshold")
    for key in ("all_2024", "concurrent_specialist"):
        metric = report.get("evaluation", {}).get(key, {})
        counts = metric.get("confusion", {})
        if (set(counts) != {"tn", "fp", "fn", "tp"} or
                any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts.values()) or
                metric.get("rows") != sum(counts.values()) or not sum(counts.values()) or
                metric.get("positives") != counts["tp"] + counts["fn"]):
            raise ValueError("Invalid window hybrid evaluation counts")
        for name, expected in (("precision", counts["tp"] / max(1, counts["tp"] + counts["fp"])),
                               ("recall", counts["tp"] / max(1, counts["tp"] + counts["fn"]))):
            value = metric.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isclose(value, expected, rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f"Window hybrid {name} disagrees with counts")
    types = report.get("evaluation", {}).get("anomaly_type", {})
    combined = report["evaluation"]["all_2024"]
    if (not types or any(not isinstance(t.get("positives"), int) or not isinstance(t.get("detected"), int) or
                         not 0 <= t["detected"] <= t["positives"] for t in types.values()) or
            sum(t["positives"] for t in types.values()) != combined["positives"] or
            sum(t["detected"] for t in types.values()) != combined["confusion"]["tp"]):
        raise ValueError("Window hybrid subtype counts disagree")
    return report


def _verify_context_report(root: Path, stem: str, schema: str) -> dict:
    artifact = root / "models" / f"{stem}.skops"
    report_path = root / "reports" / f"{stem}.json"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("artifact_sha256") != digest or
            report.get("model_version") != f"{schema}-{digest[:12]}" or
            report.get("feature_schema_version") != schema):
        raise ValueError("Contextual bank model/report identity mismatch")
    threshold = report.get("threshold_policy", {}).get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid contextual bank threshold")
    validation = report.get("evaluation", {}).get("validation", {})
    counts = validation.get("confusion", {})
    if (set(counts) != {"tn", "fp", "fn", "tp"} or
            any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()) or
            validation.get("rows") != sum(counts.values()) or sum(counts.values()) <= 0 or
            validation.get("positives") != counts["tp"] + counts["fn"] or
            validation.get("threshold") != threshold):
        raise ValueError("Invalid contextual bank validation counts")
    expected = {
        "precision": counts["tp"] / max(1, counts["tp"] + counts["fp"]),
        "recall": counts["tp"] / max(1, counts["tp"] + counts["fn"]),
        "alert_rate": (counts["tp"] + counts["fp"]) / validation["rows"],
    }
    for name, value in expected.items():
        actual = validation.get(name)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isclose(actual, value, rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError(f"Contextual bank {name} does not match validation counts")
    return report
