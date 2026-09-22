"""Audit the transfer baseline by quarter and input feature."""

import csv
import json

import numpy as np
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score

from model_artifact import load_artifact
from train_bank_baseline import CATEGORY, FEATURE_NAMES, features
from train_card_baseline import DATA, ROOT, metrics, period


def main() -> None:
    artifact = load_artifact("bank")
    model = artifact["model"]
    mappings = artifact["mappings"]
    threshold = artifact["threshold"]
    by_file = []
    x_all, y_all = [], []
    for path in sorted((DATA / "validation" / CATEGORY).glob("*.csv")):
        if period(path)[0] != 2024:
            continue
        x, y = [], []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                x.append(features(row, mappings, fit=False))
                y.append(int(row["이상거래여부"]))
        x = np.asarray(x, dtype=np.float32)
        y = np.asarray(y, dtype=np.uint8)
        scores = model.predict_proba(x)[:, 1]
        by_file.append({"file": path.name, **metrics(y, scores, threshold)})
        x_all.append(x)
        y_all.append(y)
    if not by_file:
        raise FileNotFoundError(DATA / "validation" / CATEGORY)
    x_all = np.vstack(x_all)
    y_all = np.concatenate(y_all)
    rng = np.random.default_rng(42)
    sample = rng.choice(len(y_all), size=min(30_000, len(y_all)), replace=False)
    importance = permutation_importance(
        model, x_all[sample], y_all[sample],
        n_repeats=2, random_state=42, scoring="average_precision",
    )
    ranked = sorted(
        ({"feature": name, "ap_drop": float(drop)}
         for name, drop in zip(FEATURE_NAMES, importance.importances_mean)),
        key=lambda item: item["ap_drop"], reverse=True,
    )
    report = {
        "permutation_sample_rows": len(sample),
        "permutation_sample_positives": int(y_all[sample].sum()),
        "permutation_sample_ap": float(average_precision_score(y_all[sample], model.predict_proba(x_all[sample])[:, 1])),
        "feature_ap_drop": ranked,
        "by_file": by_file,
    }
    destination = ROOT / "reports" / "bank_baseline_audit.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"top_features": ranked, "files": len(by_file)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
