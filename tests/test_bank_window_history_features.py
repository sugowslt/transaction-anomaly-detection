import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bank_window_history_features import WindowHistoryState, FEATURE_NAMES
from test_bank_context_v3_features import row


class WindowHistoryTest(unittest.TestCase):
    def test_cold_start_and_schema(self):
        vector = WindowHistoryState().snapshot([row()])[0]
        self.assertEqual(len(FEATURE_NAMES), 28)
        self.assertEqual(len(set(FEATURE_NAMES)), 28)
        self.assertEqual(len(vector), 28)
        self.assertEqual(vector[0], 1)
        self.assertEqual(vector[14], 1)
        self.assertTrue(all(math.isfinite(v) for v in vector))

    def test_current_bucket_excluded_and_order_invariant(self):
        state = WindowHistoryState()
        state.add([row(day='20220101')])
        current = [row(day='20220102', amount=100), row(day='20220102', recipient='c', amount=200)]
        expected = state.snapshot(current)
        self.assertEqual(expected, state.snapshot(current[::-1])[::-1])
        self.assertEqual(expected[0][1], math.log1p(1))
        self.assertEqual(expected, state.snapshot(current))

    def test_same_day_earlier_bucket_allowed(self):
        state = WindowHistoryState(); state.add([row(hour=0)])
        later = state.snapshot([row(hour=3)])[0]
        self.assertEqual(later[0], 0)
        self.assertEqual(later[14], 1)
        with self.assertRaisesRegex(ValueError, 'strictly earlier'):
            state.snapshot([row(hour=0)])
        with self.assertRaises(ValueError):
            state.add([row(hour=0)])

    def test_same_hour_only_and_complete_window_updates(self):
        state = WindowHistoryState()
        state.add([row(hour=0), row(hour=0, recipient='c')])
        state.add([row(hour=3, amount=900)])
        result = state.snapshot([row(day='20220102', hour=0)])[0]
        self.assertEqual(result[1], math.log1p(2))
        self.assertEqual(result[15], math.log1p(1))
        self.assertEqual(result[16], math.log1p(2))
        self.assertLess(result[18], 0)

    def test_bank_scoped_identifiers(self):
        state = WindowHistoryState(); state.add([row(bank='x')])
        vector = state.snapshot([row(day='20220102', bank='y')])[0]
        self.assertEqual(vector[0], 1)
        self.assertEqual(vector[14], 1)

    def test_labels_mixed_and_future_buckets_rejected(self):
        state = WindowHistoryState()
        labeled = row(); labeled['label'] = 1
        with self.assertRaises(ValueError): state.snapshot([labeled])
        with self.assertRaises(ValueError): state.snapshot([row(), row(hour=3)])
        state.add([row(day='20220103')])
        with self.assertRaises(ValueError): state.snapshot([row(day='20220102')])

    def test_history_is_not_truncated(self):
        state = WindowHistoryState()
        for day in range(1, 25):
            state.add([row(day=f'202201{day:02d}')])
        vector = state.snapshot([row(day='20220125')])[0]
        self.assertEqual(vector[1], math.log1p(24))
        self.assertEqual(vector[15], math.log1p(24))


if __name__ == '__main__': unittest.main()
