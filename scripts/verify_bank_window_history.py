"""Recheck frozen development populations, actual alerts and hard-case outcomes."""
import json
import sqlite3

import numpy as np

import experiment_bank_window_history as experiment
from bank_joint_policy import verify_counts


def main():
    plan, parent, _ = experiment.specification()
    v3 = experiment.v3
    report = v3.read(v3.ROOT / 'reports/bank_window_history_selection.json')
    if (report['stage'] != 'window_history_pooled_selection_completed'
            or report['plan_sha256'] != v3.old.digest(experiment.PLAN)
            or report['code_sha256'] != v3.old.digest(experiment.__file__)
            or report['runtime_model_changed'] is not False or report['operational_release_ready'] is not False):
        raise ValueError('Window-history completed selection identity differs')
    labels = np.concatenate([np.load(v3.old.graph.CACHE / f'y_{s}.npy', allow_pickle=False) for s in ('fit', 'tune')])
    kinds = np.concatenate([np.load(v3.old.graph.CACHE / f'type_{s}.npy', allow_pickle=False) for s in ('fit', 'tune')])
    history = np.concatenate([np.load(v3.old.window.CACHE / f'x_{s}.npy', mmap_mode='r')[:, 23] for s in ('fit', 'tune')])
    con = sqlite3.connect(f'file:{v3.old.base.CACHE / "stage.sqlite"}?mode=ro', uri=True)
    indices, scores, controls, offsets, offset = [], [], [], {}, 0
    try:
        for fold in parent['selection_folds']:
            if fold['development_end'] >= '20240101':
                raise ValueError('Future rows cannot enter development audit')
            start = con.execute('SELECT COUNT(*) FROM events WHERE event_date<?', (fold['development_start'],)).fetchone()[0]
            end = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?', (fold['development_end'],)).fetchone()[0]
            record = v3.read(experiment.record_path(fold))
            control = v3.read(experiment.fine.report_path(fold))
            if (record['stage'] != 'completed_window_history_unit' or record['completed_rounds'] != plan['rounds']
                    or not record['serialized_predictions_verified'] or record['development_rows'] != end - start
                    or record['signature']['control_record_sha256'] != v3.old.digest(experiment.fine.report_path(fold))):
                raise ValueError('Incomplete or changed candidate/control unit')
            for unit in (record, control):
                for item in ('artifact', 'score'):
                    if v3.old.digest(v3.ROOT / unit[item + '_path']) != unit[item + '_sha256']:
                        raise ValueError('Audit artifact changed')
            values = np.load(v3.ROOT / record['score_path'], allow_pickle=False)
            baseline = np.load(v3.ROOT / control['score_path'], allow_pickle=False)
            for score in (values, baseline):
                if score.shape != (end - start,) or not np.isfinite(score).all() or not ((score >= 0) & (score <= 1)).all():
                    raise ValueError('Invalid audit score population')
            indices.append(np.arange(start, end))
            scores.append(values); controls.append(baseline)
            offsets[fold['id']] = [offset, offset + end - start]
            offset += end - start
    finally:
        con.close()
    idx = np.concatenate(indices)
    y, t, prior = labels[idx], kinds[idx], history[idx]
    score, baseline = np.concatenate(scores), np.concatenate(controls)
    if report['rows'] != len(y) or report['positives'] != int(y.sum()) or len(np.unique(idx)) != len(idx):
        raise ValueError('Audit development population differs or overlaps')
    measured = experiment.diagnostic(y, t, score, plan['targets'])
    control_measured = experiment.diagnostic(y, t, baseline, plan['targets'])
    for key in ('ap', 'policy', 'diagnostic'):
        if report[key] != measured[key] or report['fine_control'][key] != control_measured[key]:
            raise ValueError('Reported candidate/control measurement differs')
    policy = measured['policy']
    flag = score >= policy['threshold'] if policy['feasible'] else np.zeros(len(y), dtype=bool)
    counts = {'tp': int(((y == 1) & flag).sum()), 'fp': int(((y == 0) & flag).sum()),
              'fn': int(((y == 1) & ~flag).sum()), 'tn': int(((y == 0) & ~flag).sum())}
    if counts != report['diagnostic']['confusion']:
        raise ValueError('Independent alert counts differ')
    verify_counts(y, t, flag, report['diagnostic'])
    search = v3.eligible_policy(y, t, score, plan['targets'])
    if search != report['search'] or search['selected'] != report['selected_policy']:
        raise ValueError('Reported common policy search differs')
    if report['measured_research_targets_passed'] != (search['selected'] is not None):
        raise ValueError('Reported readiness differs from selected policy')
    selected = v3.old.evaluate(y, t, score >= search['selected']['threshold']) if search['selected'] else None
    if selected != report['selected_evaluation']:
        raise ValueError('Reported selected evaluation differs')
    if selected:
        verify_counts(y, t, score >= search['selected']['threshold'], selected, plan['targets'])
    folds = {k: v3.old.evaluate(y[a:b], t[a:b], flag[a:b]) for k, (a, b) in offsets.items()}
    if folds != report['folds']:
        raise ValueError('Reported per-fold common threshold outcomes differ')
    cold = (prior == 0) & (y == 1)
    result = {'stage': 'window_history_counts_verified', 'selection_report_sha256': v3.old.digest(v3.ROOT / 'reports/bank_window_history_selection.json'),
              'verification_code_sha256': v3.old.digest(__file__), 'rows': len(y), 'positives': int(y.sum()),
              'models_verified': 4, 'confusion_verified': counts,
              'cold_start_positives': int(cold.sum()), 'cold_start_detected': int((cold & flag).sum()),
              'cold_start_missed': int((cold & ~flag).sum()), 'observed_types': report['diagnostic']['subtypes'],
              'all_policy_targets_rechecked': True, 'measured_research_targets_passed': selected is not None,
              'runtime_model_changed': False, 'operational_release_ready': False,
              'evaluation_limit': plan['evaluation_limit']}
    v3.old.write_json(v3.ROOT / 'reports/bank_window_history_verification.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
