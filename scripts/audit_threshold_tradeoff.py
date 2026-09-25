"""Compare alert-volume choices without choosing thresholds on 2024 validation data."""

import json

import numpy as np

from model_artifact import load_artifact
from release_assets import ROOT, model_version
from train_bank_baseline import load_rows as load_bank_rows
from train_card_baseline import load_rows as load_card_rows


TARGET_ALERT_RATES = (0.005, 0.01, 0.02, 0.05)


def operating_metrics(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    predicted = scores >= threshold
    positives = labels == 1
    tp = int(np.count_nonzero(predicted & positives))
    fp = int(np.count_nonzero(predicted & ~positives))
    fn = int(np.count_nonzero(~predicted & positives))
    tn = int(np.count_nonzero(~predicted & ~positives))
    return {
        "alerts": tp + fp,
        "alert_rate": (tp + fp) / len(labels),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def evaluate(kind: str, loader) -> dict:
    artifact = load_artifact(kind)
    mappings = artifact["mappings"]
    x_tune, y_tune, _, _ = loader(
        "training", mappings, sample_rate=1.0, seed=42,
        fit=False, select=lambda period: period == (2023, 4),
    )
    x_validation, y_validation, _, _ = loader(
        "validation", mappings, sample_rate=1.0, seed=42,
        fit=False, select=lambda period: period[0] == 2024,
    )
    model = artifact["model"]
    tune_scores = model.predict_proba(x_tune)[:, 1]
    validation_scores = model.predict_proba(x_validation)[:, 1]
    stored = json.loads((ROOT / "reports" / f"{kind}_baseline.json").read_text(encoding="utf-8"))
    current = operating_metrics(y_validation, validation_scores, artifact["threshold"])
    if current["confusion"] != stored["validation"]["confusion"]:
        raise ValueError(f"{kind} validation results do not match the baseline report")
    scenarios = []
    for target in TARGET_ALERT_RATES:
        threshold = float(np.quantile(tune_scores, 1 - target))
        if target == 0.01 and not np.isclose(threshold, artifact["threshold"], rtol=0, atol=1e-12):
            raise ValueError(f"{kind} stored threshold does not match the tuning scores")
        scenarios.append({
            "target_tuning_alert_rate": target,
            "threshold": threshold,
            "tuning_alert_rate": float(np.mean(tune_scores >= threshold)),
            "validation": operating_metrics(y_validation, validation_scores, threshold),
        })
    return {
        "model_version": model_version(kind),
        "tuning_rows": len(y_tune),
        "validation_rows": len(y_validation),
        "scenarios": scenarios,
    }


def main() -> None:
    report = {
        "threshold_selection": "2023 Q4 training holdout score quantiles",
        "evaluation": "2024 validation",
        "models": {
            "card": evaluate("card", load_card_rows),
            "bank": evaluate("bank", load_bank_rows),
        },
    }
    destination = ROOT / "reports" / "threshold_tradeoff.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        kind: [(item["target_tuning_alert_rate"], item["validation"]["precision"], item["validation"]["recall"])
               for item in result["scenarios"]]
        for kind, result in report["models"].items()
    }))


if __name__ == "__main__":
    main()
