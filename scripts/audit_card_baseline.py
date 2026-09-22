"""Inspect feature reliance and per-file results before interpreting the baseline."""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score

from train_card_baseline import DATA, FEATURE_NAMES, ROOT, features, metrics, period


def main() -> None:
    artifact = joblib.load(ROOT / "models" / "card_baseline.joblib")
    model = artifact["model"]
    mappings = artifact["mappings"]
    threshold = artifact["threshold"]
    by_file = []
    x_all, y_all = [], []
    approval_codes = defaultdict(Counter)
    for path in sorted((DATA / "validation" / "카드거래").glob("*.csv")):
        if period(path)[0] != 2024:
            continue
        x, y = [], []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                x.append(features(row, mappings, fit=False))
                label = int(row["이상거래여부"])
                y.append(label)
                approval_codes[row["승인거래코드"]][str(label)] += 1
        x = np.asarray(x, dtype=np.float32)
        y = np.asarray(y, dtype=np.uint8)
        scores = model.predict_proba(x)[:, 1]
        by_file.append({"file": path.name, **metrics(y, scores, threshold)})
        x_all.append(x)
        y_all.append(y)
    x_all = np.vstack(x_all)
    y_all = np.concatenate(y_all)
    rng = np.random.default_rng(42)
    sample = rng.choice(len(y_all), size=min(15_000, len(y_all)), replace=False)
    importance = permutation_importance(
        model,
        x_all[sample],
        y_all[sample],
        n_repeats=2,
        random_state=42,
        scoring="average_precision",
    )
    ranked = sorted(
        ({"feature": name, "ap_drop": float(drop)}
         for name, drop in zip(FEATURE_NAMES, importance.importances_mean)),
        key=lambda item: item["ap_drop"],
        reverse=True,
    )
    report = {
        "permutation_sample_rows": int(len(sample)),
        "permutation_sample_positives": int(y_all[sample].sum()),
        "permutation_sample_ap": float(average_precision_score(y_all[sample], model.predict_proba(x_all[sample])[:, 1])),
        "feature_ap_drop": ranked,
        "approval_code_label_counts": {code: dict(counts) for code, counts in sorted(approval_codes.items())},
        "by_file": by_file,
    }
    destination = ROOT / "reports" / "card_baseline_audit.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"top_features": ranked[:8], "files": len(by_file)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
