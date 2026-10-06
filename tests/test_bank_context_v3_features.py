"""Bounded pure feature-contract tests; no model training or data access."""
import math
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from bank_context_v3_features import FEATURE_NAMES, PRIOR_FEATURE_NAMES, WINDOW_FEATURE_NAMES, PastContextState, window_vectors


def row(sender="a", recipient="b", amount=100.0, day="20220101", hour=0, channel="1", fund="01", bank="x"):
    return {"출금계좌일련번호": sender, "입금계좌일련번호": recipient,
            "출금금융회사일련번호": bank, "입금금융회사일련번호": bank,
            "거래금액": amount, "거래일자": day, "거래시간대": hour,
            "매체구분": channel, "자금구분": fund}


class FeatureContractTest(unittest.TestCase):
    def test_cold_start_is_finite_and_ordered(self):
        result = PastContextState().snapshot(row())
        self.assertEqual(len(FEATURE_NAMES), 28)
        self.assertEqual(len(set(FEATURE_NAMES)), 28)
        self.assertEqual(len(result), len(PRIOR_FEATURE_NAMES))
        self.assertEqual(result[:2], (1.0, 1.0))
        self.assertTrue(all(math.isfinite(value) for value in result))

    def test_all_same_date_snapshots_exclude_peers(self):
        state = PastContextState()
        first = state.snapshot(row())
        self.assertEqual(first, state.snapshot(row(amount=500, hour=21)))
        state.add(row())
        with self.assertRaisesRegex(ValueError, "strictly later date"):
            state.snapshot(row(hour=21))

    def test_future_and_reverse_date_guard(self):
        state = PastContextState()
        state.add(row(day="20220103"))
        with self.assertRaises(ValueError):
            state.snapshot(row(day="20220102"))
        with self.assertRaises(ValueError):
            state.add(row(day="20220102"))

    def test_history_dispersion_rank_and_time_change(self):
        state = PastContextState()
        state.add(row(amount=10, hour=0))
        state.add(row(amount=10000, hour=3, recipient="c", channel="2"))
        result = dict(zip(PRIOR_FEATURE_NAMES, state.snapshot(row(amount=1000000, hour=21, day="20220102"))))
        self.assertGreater(result["v3_sender_log_amount_std"], 0)
        self.assertGreater(result["v3_amount_log_zscore"], 0)
        self.assertEqual(result["v3_amount_log_hist_percentile"], 1)
        self.assertGreater(result["v3_sender_hour_entropy"], 0)
        self.assertGreater(result["v3_hour_circular_distance"], 0)
        self.assertAlmostEqual(result["v3_sender_recipient_concentration"], 0.5)

    def test_bank_scoping_and_normalized_date(self):
        state = PastContextState()
        state.add(row(bank="x"))
        raw = row(bank="y", day="20220102")
        raw["date"] = date(2022, 1, 2)
        self.assertEqual(state.snapshot(raw)[0], 1)
        raw["date"] = date(2022, 1, 3)
        with self.assertRaises(ValueError):
            state.snapshot(raw)

    def test_bucket_permutation_and_cross_flow(self):
        rows = [row(amount=10), row(recipient="c", amount=30, channel="2"), row(sender="b", recipient="a", amount=20)]
        original = window_vectors(rows)
        reversed_result = window_vectors(list(reversed(rows)))
        self.assertEqual(original, list(reversed(reversed_result)))
        values = dict(zip(WINDOW_FEATURE_NAMES, original[0]))
        self.assertAlmostEqual(values["v3_window_sender_net_flow_share"], -1 / 3)
        self.assertAlmostEqual(values["v3_window_sender_amount_concentration"], 0.625)
        self.assertAlmostEqual(values["v3_window_sender_amount_percentile"], 0.25)

    def test_bucket_identifier_renaming_invariance(self):
        rows = [row(), row(recipient="c", amount=300), row(sender="b", recipient="a", amount=50)]
        renamed = []
        for item in rows:
            renamed.append({key: ("renamed-" + value if "일련번호" in key else value) for key, value in item.items()})
        self.assertEqual(window_vectors(rows), window_vectors(renamed))

    def test_labels_metadata_and_incomplete_bucket_rejected(self):
        for key in ("label", "이상거래유형", "description"):
            raw = {**row(), key: "1"}
            with self.assertRaises(ValueError):
                PastContextState().snapshot(raw)
            with self.assertRaises(ValueError):
                window_vectors([raw])
        with self.assertRaises(ValueError):
            window_vectors([row(), row(day="20220102")])
        with self.assertRaises(ValueError):
            window_vectors([])

    def test_zero_amount_and_tied_rank_finite(self):
        values = window_vectors([row(amount=0), row(amount=0)])
        self.assertEqual(values[0], values[1])
        self.assertTrue(all(math.isfinite(value) for value in values[0]))
        self.assertEqual(values[0][WINDOW_FEATURE_NAMES.index("v3_window_sender_amount_percentile")], 0.5)


if __name__ == "__main__":
    unittest.main()
