import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from card_input_policy import MAX_CARD_AMOUNT
from serve_model import score_transaction


class CardScoringTests(unittest.TestCase):
    def test_card_amount_limit_belongs_to_packaged_model_report(self):
        path = Path(__file__).resolve().parents[1] / "reports" / "card_baseline.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(MAX_CARD_AMOUNT, report["input_limits"]["max_amount"])

    def test_demo_input_scores_but_identifiers_and_missing_fields_are_rejected(self):
        path = Path(__file__).resolve().parents[1] / "reports" / "demo_transactions.json"
        transaction = json.loads(path.read_text(encoding="utf-8"))[0]["transaction"]
        self.assertIn("riskScore", score_transaction(transaction))
        with self.assertRaisesRegex(ValueError, "exactly the model input fields"):
            score_transaction({**transaction, "카드KEY": "private-card"})
        with self.assertRaisesRegex(ValueError, "exactly the model input fields"):
            score_transaction({key: value for key, value in transaction.items() if key != "승인시간대"})

    def test_card_amount_outside_fit_range_is_not_scored(self):
        path = Path(__file__).resolve().parents[1] / "reports" / "demo_transactions.json"
        transaction = next(item["transaction"] for item in json.loads(path.read_text(encoding="utf-8"))
                           if item["id"] == "DEMO-03")
        self.assertIn("riskScore", score_transaction({**transaction, "통합승인금액": str(MAX_CARD_AMOUNT)}))
        for invalid in ("1800000000000000000000000000000000000000000000000000000000",
                        str(MAX_CARD_AMOUNT + 1), "-1", "1.5", "NaN", True):
            with self.subTest(amount=invalid), self.assertRaisesRegex(ValueError, "학습 표본 범위"):
                score_transaction({**transaction, "통합승인금액": invalid})


if __name__ == "__main__":
    unittest.main()
