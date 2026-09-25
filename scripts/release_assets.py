"""Identify the packaged models and check their paired reports without dependencies."""

import hashlib
import json
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
    for kind in KINDS:
        report_path = root / "reports" / f"{kind}_baseline.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        expected = model_version(kind, root)
        if report.get("model_version") != expected:
            raise ValueError(
                f"{report_path} model_version does not match {model_path(kind, root)}: "
                f"expected {expected}, found {report.get('model_version')!r}"
            )
