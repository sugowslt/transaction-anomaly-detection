"""Score fictional transactions created for the public dashboard.

No AI Hub source row is copied into this file or its output.
"""

import json

import numpy as np

from model_artifact import load_artifact
from train_card_baseline import ROOT, features


BASE = {
    "통합승인금액": "28000",
    "카드이용한도금액": "5000000",
    "승인시간대": "14",
    "경과일수_최종이용일자": "2",
    "전월_매출건수": "120",
    "전월_매출금액": "9000000",
    "가맹점누적매출금액_구간화": "2",
    "연령": "4",
    "할부가능개월수": "12",
    "국내해외여부": "0",
    "개인법인구분코드_회원": "1",
    "승인거래코드": "00",
    "승인발생경로코드": "C",
    "가맹점여부_신규": "0",
    "인터넷판매여부": "0",
    "가맹점상태코드": "30",
    "가맹점형태구분코드": "00",
    "일시불할부구분코드": "A",
    "카드구분코드": "1",
    "가맹점광역시도코드": "09",
}

SCENARIOS = (
    ("소액 일시불", {}),
    ("야간 소액", {"통합승인금액": "18000", "승인시간대": "2"}),
    ("고액 일시불", {"통합승인금액": "1800000", "승인시간대": "22"}),
    ("고액 할부", {"통합승인금액": "2400000", "일시불할부구분코드": "B", "할부가능개월수": "24"}),
    ("온라인 고액", {"통합승인금액": "3200000", "인터넷판매여부": "1", "승인발생경로코드": "O"}),
    ("한도 근접", {"통합승인금액": "4700000", "승인거래코드": "01", "일시불할부구분코드": "B"}),
    ("해외 야간", {"통합승인금액": "950000", "승인시간대": "3", "국내해외여부": "1"}),
    ("장기 미사용", {"통합승인금액": "870000", "경과일수_최종이용일자": "180", "승인거래코드": "01"}),
)


def main() -> None:
    artifact = load_artifact()
    examples = []
    for index, (scenario, overrides) in enumerate(SCENARIOS, 1):
        transaction = {**BASE, **overrides}
        vector = np.asarray([features(transaction, artifact["mappings"], fit=False)], dtype=np.float32)
        score = float(artifact["model"].predict_proba(vector)[0, 1])
        examples.append({
            "id": f"DEMO-{index:02d}",
            "scenario": scenario,
            "amount": int(transaction["통합승인금액"]),
            "riskScore": score,
            "alert": score >= artifact["threshold"],
            "transaction": transaction,
        })
    destination = ROOT / "reports" / "demo_transactions.json"
    destination.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"examples": len(examples), "alerts": sum(item["alert"] for item in examples)}))


if __name__ == "__main__":
    main()
