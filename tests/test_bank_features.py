import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from serve_model import score_bank_transaction
from train_bank_baseline import features


class BankFeatureTests(unittest.TestCase):
    def setUp(self):
        self.transaction = {"거래금액": 5_000_000, "거래시간대": 9, "자금구분": "0", "매체구분": "2"}

    def test_identifiers_and_labels_are_not_features(self):
        mappings = {"자금구분": {"0": 0}, "매체구분": {"2": 0}}
        baseline = features(self.transaction, mappings, fit=False)
        enriched = {
            **self.transaction,
            "출금계좌일련번호": "private-account",
            "입금금융회사일련번호": "private-institution",
            "이상거래여부": "1",
            "이상거래설명": "future explanation",
        }
        self.assertEqual(baseline, features(enriched, mappings, fit=False))

    def test_public_scoring_contract_rejects_identifiers_and_unknown_codes(self):
        self.assertIn("riskScore", score_bank_transaction(self.transaction))
        with self.assertRaises(ValueError):
            score_bank_transaction({**self.transaction, "출금계좌일련번호": "private-account"})
        with self.assertRaises(ValueError):
            score_bank_transaction({**self.transaction, "매체구분": "unknown"})

    def test_invalid_time_and_amount_are_rejected(self):
        mappings = {"자금구분": {"0": 0}, "매체구분": {"2": 0}}
        for changed in ({"거래금액": -1}, {"거래시간대": 10}, {"거래시간대": 24}):
            with self.assertRaises(ValueError):
                features({**self.transaction, **changed}, mappings, fit=False)
        with self.assertRaises(ValueError):
            score_bank_transaction({**self.transaction, "거래시간대": 9.5})
        with self.assertRaises(ValueError):
            score_bank_transaction({**self.transaction, "거래시간대": 10})


if __name__ == "__main__":
    unittest.main()
