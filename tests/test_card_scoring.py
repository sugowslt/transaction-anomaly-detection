import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from serve_model import score_transaction


class CardScoringTests(unittest.TestCase):
    def test_demo_input_scores_but_identifiers_and_missing_fields_are_rejected(self):
        path = Path(__file__).resolve().parents[1] / "reports" / "demo_transactions.json"
        transaction = json.loads(path.read_text(encoding="utf-8"))[0]["transaction"]
        self.assertIn("riskScore", score_transaction(transaction))
        with self.assertRaisesRegex(ValueError, "exactly the model input fields"):
            score_transaction({**transaction, "카드KEY": "private-card"})
        with self.assertRaisesRegex(ValueError, "exactly the model input fields"):
            score_transaction({key: value for key, value in transaction.items() if key != "승인시간대"})


if __name__ == "__main__":
    unittest.main()
