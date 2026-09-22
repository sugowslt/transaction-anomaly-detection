"""Create fictional electronic-finance transactions for the public dashboard."""

import json

import numpy as np

from model_artifact import load_artifact
from train_bank_baseline import features
from train_card_baseline import ROOT


BASE = {"거래금액": 30_000, "거래시간대": 9, "자금구분": "0", "매체구분": "2"}
SCENARIOS = (
    ("소액 이체", {}),
    ("야간 소액", {"거래금액": 90_000, "거래시간대": 3}),
    ("오후 중액", {"거래금액": 800_000, "거래시간대": 15, "매체구분": "4"}),
    ("고액 이체", {"거래금액": 5_000_000, "거래시간대": 9}),
    ("이른 시간 고액", {"거래금액": 5_000_000, "거래시간대": 6}),
    ("자금구분 변경", {"거래금액": 5_000_000, "자금구분": "1"}),
    ("다른 매체의 고액", {"거래금액": 8_000_000, "매체구분": "7"}),
    ("초고액 이체", {"거래금액": 50_000_000, "거래시간대": 21}),
)


def main() -> None:
    artifact = load_artifact("bank")
    examples = []
    for index, (scenario, overrides) in enumerate(SCENARIOS, 1):
        transaction = {**BASE, **overrides}
        vector = np.asarray([features(transaction, artifact["mappings"], fit=False)], dtype=np.float32)
        score = float(artifact["model"].predict_proba(vector)[0, 1])
        examples.append({
            "id": f"TRANSFER-{index:02d}",
            "scenario": scenario,
            "amount": transaction["거래금액"],
            "riskScore": score,
            "alert": score >= artifact["threshold"],
            "transaction": transaction,
        })
    destination = ROOT / "reports" / "bank_demo_transactions.json"
    destination.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"examples": len(examples), "alerts": sum(item["alert"] for item in examples)}))


if __name__ == "__main__":
    main()
