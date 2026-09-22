"""Train an electronic-finance transaction baseline without identifiers.

The four inputs are transaction amount, time bucket, fund type, and channel.
Account and institution serial numbers, labels, and explanatory text are
excluded. Whether all four fields are available at authorization time in a
real system still needs operational confirmation.
"""

import argparse
import csv
import json
import math
import random

import numpy as np
import sklearn
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier

from train_card_baseline import DATA, ROOT, metrics, period


CATEGORY = "전자금융공동망"
NUMERIC = ("거래금액_log", "거래시간대")
CATEGORICAL = ("자금구분", "매체구분")
FEATURE_NAMES = NUMERIC + CATEGORICAL
REQUIRED = {"거래금액", "거래시간대", *CATEGORICAL, "이상거래여부"}
HOUR_CODES = frozenset(range(0, 24, 3))


def features(row: dict, mappings: dict, *, fit: bool) -> list[float]:
    amount = float(row["거래금액"])
    hour = float(row["거래시간대"])
    if not math.isfinite(amount) or amount < 0 or hour not in HOUR_CODES:
        raise ValueError("Invalid transaction amount or time bucket")
    values = [math.log1p(amount), hour]
    for column in CATEGORICAL:
        value = row[column]
        mapping = mappings[column]
        if fit and value not in mapping:
            mapping[value] = len(mapping)
        values.append(float(mapping[value]) if value in mapping else math.nan)
    return values


def load_rows(split: str, mappings: dict, *, sample_rate: float, seed: int, fit: bool, select):
    rng = random.Random(seed)
    directory = DATA / split / CATEGORY
    paths = [path for path in sorted(directory.glob("*.csv")) if select(period(path))]
    if not paths:
        raise FileNotFoundError(directory)
    x, y = [], []
    source_rows = 0
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not REQUIRED.issubset(reader.fieldnames or []):
                raise ValueError(f"Missing required columns in {path}")
            for row in reader:
                source_rows += 1
                if rng.random() >= sample_rate:
                    continue
                label = row["이상거래여부"]
                if label not in ("0", "1"):
                    raise ValueError(f"Unexpected label {label!r} in {path}")
                x.append(features(row, mappings, fit=fit))
                y.append(int(label))
    return np.asarray(x, dtype=np.float32), np.asarray(y, dtype=np.uint8), source_rows, [p.name for p in paths]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-rate", type=float, default=0.16)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 0 < args.sample_rate <= 1:
        parser.error("--sample-rate must be in (0, 1]")
    mappings = {name: {} for name in CATEGORICAL}
    x_fit, y_fit, fit_rows, fit_files = load_rows(
        "training", mappings, sample_rate=args.sample_rate, seed=args.seed,
        fit=True, select=lambda p: p < (2023, 4),
    )
    if len(np.unique(y_fit)) != 2:
        raise ValueError("Training sample must contain both label classes")
    x_tune, y_tune, tune_rows, tune_files = load_rows(
        "training", mappings, sample_rate=1.0, seed=args.seed,
        fit=False, select=lambda p: p == (2023, 4),
    )
    model = HistGradientBoostingClassifier(
        categorical_features=[2, 3],
        max_iter=100,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=args.seed,
    )
    model.fit(x_fit, y_fit)
    tune_scores = model.predict_proba(x_tune)[:, 1]
    threshold = float(np.quantile(tune_scores, 0.99))
    x_val, y_val, val_rows, val_files = load_rows(
        "validation", mappings, sample_rate=1.0, seed=args.seed,
        fit=False, select=lambda p: p[0] == 2024,
    )
    val_scores = model.predict_proba(x_val)[:, 1]
    amount_only_threshold = float(np.quantile(x_tune[:, 0], 0.99))
    report = {
        "model": "HistGradientBoostingClassifier",
        "sklearn_version": sklearn.__version__,
        "seed": args.seed,
        "split_policy": "fit: 2021 Q3–2023 Q3 training; threshold: 2023 Q4 training; evaluation: 2024 validation",
        "training_sample_rate": args.sample_rate,
        "training_source_rows": fit_rows,
        "training_sample_rows": len(y_fit),
        "tuning_source_rows": tune_rows,
        "validation_source_rows": val_rows,
        "training_files": fit_files,
        "tuning_files": tune_files,
        "validation_files": val_files,
        "feature_names": FEATURE_NAMES,
        "excluded_fields": ["출금계좌일련번호", "입금계좌일련번호", "출금금융회사일련번호", "입금금융회사일련번호", "거래일자", "이상거래유형", "이상거래여부", "이상거래설명"],
        "threshold_policy": "top 1% of scores on internal training holdout",
        "amount_only_reference": metrics(y_val, x_val[:, 0], amount_only_threshold),
        "tuning": metrics(y_tune, tune_scores, threshold),
        "validation": metrics(y_val, val_scores, threshold),
    }
    (ROOT / "models").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    sio.dump(
        {"model": model, "mappings": mappings, "threshold": threshold, "feature_names": FEATURE_NAMES},
        ROOT / "models" / "bank_baseline.skops",
    )
    (ROOT / "reports" / "bank_baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"training_sample_rows": len(y_fit), "validation": report["validation"]}))


if __name__ == "__main__":
    main()
