"""Temporal alignment, immutable control and actual checkpoint recovery checks."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import experiment_bank_window_history as experiment


class WindowHistoryTrainingTest(unittest.TestCase):
    def data(self):
        return (np.zeros((2, 114), np.float32), np.array([0, 1]), np.array([b'0', b'1']),
                np.ones((2, 114), np.float32), np.array([1, 0]), np.array([b'2', b'0']),
                np.array([100, 200]), {'verified_rows': 4})

    def test_extension_uses_only_matching_fit_and_development_rows(self):
        data = self.data()
        extra = np.arange(6 * 28, dtype=np.float32).reshape(6, 28)
        result = experiment.augment(data, extra, 2, 4)
        self.assertEqual(result[0].shape, (2, 142))
        self.assertEqual(result[3].shape, (2, 142))
        np.testing.assert_array_equal(result[0][:, :114], data[0])
        np.testing.assert_array_equal(result[0][:, 114:], extra[:2])
        np.testing.assert_array_equal(result[3][:, 114:], extra[2:4])
        for index in (1, 2, 4, 5, 6, 7):
            self.assertIs(result[index], data[index])

    def test_temporal_gap_and_wrong_feature_width_are_rejected(self):
        extra = np.zeros((6, 28), np.float32)
        with self.assertRaisesRegex(ValueError, 'temporal population'):
            experiment.augment(self.data(), extra, 3, 5)
        with self.assertRaises(ValueError):
            experiment.augment(self.data(), extra[:, :27], 2, 4)
        with self.assertRaises(ValueError):
            experiment.augment(self.data(), extra, 2, 5)

    def test_nonfinite_used_extension_is_rejected_without_reading_future_rows(self):
        extra = np.zeros((6, 28), np.float32)
        extra[5, 0] = np.nan
        experiment.augment(self.data(), extra, 2, 4)
        extra[3, 0] = np.nan
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            experiment.augment(self.data(), extra, 2, 4)

    def test_full_cache_identity_is_distinct_from_smaller_fold_population(self):
        meta = {'rows': 6, 'shape': [6, 28], 'ordered_source_identity_sha256': 'original',
                'all_available_past_used': True, 'no_2024_rows_read': True,
                'signature': {'base_context': {'rows': 6, 'ordered_source_identity_sha256': 'original'}}}
        experiment.validate_context_cache(meta)
        for field, bad in (('rows', 4), ('ordered_source_identity_sha256', 'wrong'),
                           ('all_available_past_used', False), ('no_2024_rows_read', False)):
            changed = copy.deepcopy(meta)
            changed[field] = bad
            with self.assertRaisesRegex(ValueError, 'original row identity'):
                experiment.validate_context_cache(changed)

    def test_changed_sampling_and_target_contracts_are_rejected(self):
        plan = experiment.v3.read(experiment.PLAN)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'plan.json'
            for field in ('sampling', 'target'):
                changed = copy.deepcopy(plan)
                if field == 'sampling':
                    changed['parameters']['subsample'] = .5
                else:
                    changed['targets']['recall'] = .7
                path.write_text(json.dumps(changed), encoding='utf-8')
                with patch.object(experiment, 'PLAN', path):
                    with self.assertRaisesRegex(ValueError, 'contract changed'):
                        experiment.specification()

    def test_interruption_resume_matches_uninterrupted_training(self):
        rng = np.random.default_rng(42)
        x = rng.normal(size=(60, 3)).astype(np.float32)
        y = (x[:, 0] > 0).astype(np.uint8)
        params = {'objective': 'binary:logistic', 'tree_method': 'hist', 'max_depth': 2,
                  'max_bin': 32, 'eta': .1, 'seed': 42, 'nthread': 1}
        matrix = xgb.QuantileDMatrix(x, label=y, max_bin=32, nthread=1)
        whole = xgb.train(params, matrix, num_boost_round=8)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stop, checkpoint = root / 'stop.json', root / 'checkpoint.ubj'
            record = {'completed_rounds': 0, 'signature': {'base': {'fold': {'id': 'fixture'}}}}
            class Stops(experiment.deep.Checkpoint):
                def after_iteration(self, model, epoch, evals_log):
                    if epoch == 3:
                        experiment.v3.old.write_json(stop, {'stop': True})
                    return super().after_iteration(model, epoch, evals_log)
            with patch.object(experiment.deep, 'STOP', stop):
                partial = xgb.train(params, matrix, num_boost_round=8,
                                    callbacks=[Stops(record, root / 'record.json', checkpoint, 8, 2)])
            self.assertEqual(partial.num_boosted_rounds(), 4)
            saved = experiment.v3.read(root / 'record.json')
            restored = experiment.restore_checkpoint(saved, checkpoint, 8)
            resumed = xgb.train(params, matrix, num_boost_round=4, xgb_model=restored)
            np.testing.assert_array_equal(whole.inplace_predict(x), resumed.inplace_predict(x))
            altered = {**saved, 'completed_rounds': 3}
            with self.assertRaisesRegex(ValueError, 'rounds differ'):
                experiment.restore_checkpoint(altered, checkpoint, 8)
            checkpoint.write_bytes(checkpoint.read_bytes() + b'corrupted')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                experiment.restore_checkpoint(saved, checkpoint, 8)

    def test_invalid_saved_rounds_and_nonfinite_predictions_are_rejected(self):
        for rounds in (True, -1, 9, 1.5):
            with self.assertRaisesRegex(ValueError, 'round count'):
                experiment.restore_checkpoint({'completed_rounds': rounds}, Path('unused'), 8)
        self.assertIsNone(experiment.restore_checkpoint({'completed_rounds': 0}, Path('unused'), 8))
        with self.assertRaisesRegex(ValueError, 'score population'):
            experiment.diagnostic(np.array([0, 1]), np.array([b'0', b'1']),
                                  np.array([0., np.nan]), {'precision': .9, 'maximum_alert_rate': .02})


if __name__ == '__main__':
    unittest.main()
