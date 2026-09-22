"""Create a small, ID-free demo set from the held-out 2024 validation data."""

import csv
import json
from pathlib import Path

import joblib
import numpy as np

from train_card_baseline import CATEGORICAL, DATA, ROOT, features, period


RAW_NUMERIC = (
    "통합승인금액", "카드이용한도금액", "승인시간대", "경과일수_최종이용일자",
    "전월_매출건수", "전월_매출금액", "가맹점누적매출금액_구간화",
    "연령", "할부가능개월수",
)
RAW_FIELDS = RAW_NUMERIC + CATEGORICAL
QUOTAS = {"TP": 4, "FN": 3, "TN": 3, "FP": 1}


def main() -> None:
    artifact = joblib.load(ROOT / "models" / "card_baseline.joblib")
    selected = {key: [] for key in QUOTAS}
    for path in sorted((DATA / "validation" / "카드거래").glob("*.csv")):
        if period(path)[0] != 2024:
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        vectors = np.asarray(
            [features(row, artifact["mappings"], fit=False) for row in rows], dtype=np.float32
        )
        scores = artifact["model"].predict_proba(vectors)[:, 1]
        for row, score in zip(rows, scores):
            label = int(row["이상거래여부"])
            alert = bool(score >= artifact["threshold"])
            outcome = ("T" if alert == bool(label) else "F") + ("P" if alert else "N")
            if len(selected[outcome]) >= QUOTAS[outcome]:
                continue
            selected[outcome].append({
                "date": row["승인일자"],
                "amount": float(row["통합승인금액"]),
                "label": label,
                "riskScore": float(score),
                "alert": alert,
                "outcome": outcome,
                "transaction": {key: row[key] for key in RAW_FIELDS},
            })
    examples = [item for group in selected.values() for item in group]
    for index, item in enumerate(examples, 1):
        item["id"] = f"TX-{index:03d}"
    destination = ROOT / "reports" / "demo_transactions.json"
    destination.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"examples": len(examples), "outcomes": {k: len(v) for k, v in selected.items()}}))


if __name__ == "__main__":
    main()
