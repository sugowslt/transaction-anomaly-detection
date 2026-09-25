"""Train a reproducible card-transaction anomaly baseline.

IDs, anomaly labels, anomaly types, and explanation text are deliberately
excluded from model features. Runtime availability of other source fields
still needs to be established before any operational use.
"""

import argparse
import csv
import json
import math
import random
import re
from pathlib import Path

import numpy as np
import sklearn
import skops.io as sio
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, confusion_matrix, roc_auc_score

from release_assets import model_version


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NUMERIC = (
    "통합승인금액_log",
    "카드이용한도금액_log",
    "한도대비승인금액",
    "승인시간대",
    "경과일수_최종이용일자_log",
    "전월_매출건수_log",
    "전월_매출금액_log",
    "가맹점누적매출금액_구간화",
    "연령",
    "할부가능개월수",
)
CATEGORICAL = (
    "국내해외여부",
    "개인법인구분코드_회원",
    "승인거래코드",
    "승인발생경로코드",
    "가맹점여부_신규",
    "인터넷판매여부",
    "가맹점상태코드",
    "가맹점형태구분코드",
    "일시불할부구분코드",
    "카드구분코드",
    "가맹점광역시도코드",
)
FEATURE_NAMES = NUMERIC + CATEGORICAL
SOURCE_FEATURE_FIELDS = frozenset({
    "통합승인금액", "카드이용한도금액", "승인시간대", "경과일수_최종이용일자",
    "전월_매출건수", "전월_매출금액", "가맹점누적매출금액_구간화",
    "연령", "할부가능개월수", *CATEGORICAL,
})


def number(value: str) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else math.nan
    except (TypeError, ValueError):
        return math.nan


def log_nonnegative(value: float) -> float:
    return math.log1p(max(value, 0.0)) if math.isfinite(value) else math.nan


def features(row: dict, mappings: dict, *, fit: bool) -> list[float]:
    amount = number(row["통합승인금액"])
    limit = number(row["카드이용한도금액"])
    ratio = amount / limit if math.isfinite(amount) and math.isfinite(limit) and limit > 0 else math.nan
    values = [
        log_nonnegative(amount),
        log_nonnegative(limit),
        min(ratio, 10.0) if math.isfinite(ratio) else math.nan,
        number(row["승인시간대"]),
        log_nonnegative(number(row["경과일수_최종이용일자"])),
        log_nonnegative(number(row["전월_매출건수"])),
        log_nonnegative(number(row["전월_매출금액"])),
        number(row["가맹점누적매출금액_구간화"]),
        number(row["연령"]),
        number(row["할부가능개월수"]),
    ]
    for column in CATEGORICAL:
        value = row[column]
        if value == "":
            values.append(math.nan)
            continue
        mapping = mappings[column]
        if fit and value not in mapping:
            mapping[value] = len(mapping)
        values.append(float(mapping[value]) if value in mapping else math.nan)
    return values


def period(path: Path) -> tuple[int, int]:
    match = re.search(r"_(\d{4})_(\d)분기\.csv$", path.name)
    if not match:
        raise ValueError(f"Cannot read quarter from {path.name}")
    return int(match.group(1)), int(match.group(2))


def load_rows(split: str, mappings: dict, *, sample_rate: float, seed: int, fit: bool, select):
    rng = random.Random(seed)
    directory = DATA / split / "카드거래"
    paths = [path for path in sorted(directory.glob("*.csv")) if select(period(path))]
    if not paths:
        raise FileNotFoundError(directory)
    x, y = [], []
    source_rows = 0
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = SOURCE_FEATURE_FIELDS | {"이상거래여부"}
            if not required.issubset(reader.fieldnames or []):
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


def metrics(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    predictions = scores >= threshold
    tn, fp, fn, tp = (int(v) for v in confusion_matrix(y_true, predictions, labels=[0, 1]).ravel())
    return {
        "rows": int(len(y_true)),
        "positives": int(y_true.sum()),
        "prevalence": float(y_true.mean()),
        "average_precision": float(average_precision_score(y_true, scores)),
        "roc_auc": float(roc_auc_score(y_true, scores)),
        "threshold": threshold,
        "alert_rate": float(predictions.mean()),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-rate", type=float, default=0.16)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 0 < args.sample_rate <= 1:
        parser.error("--sample-rate must be in (0, 1]")
    mappings = {name: {} for name in CATEGORICAL}
    x_fit, y_fit, train_source_rows, train_files = load_rows(
        "training", mappings, sample_rate=args.sample_rate, seed=args.seed,
        fit=True, select=lambda p: p < (2023, 4),
    )
    if len(np.unique(y_fit)) != 2:
        raise ValueError("Training sample must contain both label classes")
    x_tune, y_tune, tuning_source_rows, tuning_files = load_rows(
        "training", mappings, sample_rate=1.0, seed=args.seed,
        fit=False, select=lambda p: p == (2023, 4),
    )
    model = HistGradientBoostingClassifier(
        categorical_features=list(range(len(NUMERIC), len(FEATURE_NAMES))),
        max_iter=100,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=args.seed,
    )
    model.fit(x_fit, y_fit)
    tune_scores = model.predict_proba(x_tune)[:, 1]
    threshold = float(np.quantile(tune_scores, 0.99))
    x_val, y_val, val_source_rows, val_files = load_rows(
        "validation", mappings, sample_rate=1.0, seed=args.seed,
        fit=False, select=lambda p: p[0] == 2024,
    )
    val_scores = model.predict_proba(x_val)[:, 1]
    report = {
        "model": "HistGradientBoostingClassifier",
        "sklearn_version": sklearn.__version__,
        "seed": args.seed,
        "split_policy": "fit: 2021 Q1–2023 Q3 training; threshold: 2023 Q4 training; evaluation: 2024 validation",
        "training_sample_rate": args.sample_rate,
        "training_source_rows": train_source_rows,
        "training_sample_rows": int(len(y_fit)),
        "model_fit_rows": int(len(y_fit)),
        "tuning_source_rows": tuning_source_rows,
        "tuning_rows": int(len(y_tune)),
        "validation_source_rows": val_source_rows,
        "training_files": train_files,
        "tuning_files": tuning_files,
        "validation_files": val_files,
        "feature_names": FEATURE_NAMES,
        "threshold_policy": "top 1% of scores on internal training holdout",
        "tuning": metrics(y_tune, tune_scores, threshold),
        "validation": metrics(y_val, val_scores, threshold),
    }
    (ROOT / "models").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    sio.dump(
        {"model": model, "mappings": mappings, "threshold": threshold, "feature_names": FEATURE_NAMES},
        ROOT / "models" / "card_baseline.skops",
    )
    report["model_version"] = model_version("card")
    (ROOT / "reports" / "card_baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"training_sample_rows": report["training_sample_rows"], "validation": report["validation"]}))


if __name__ == "__main__":
    main()
