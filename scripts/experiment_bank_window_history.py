"""Signed, checkpointed 142-input research using strictly past temporal folds."""
from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import time

import numpy as np
import xgboost as xgb
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits

import build_bank_window_history as history
import experiment_bank_context_v3 as v3
import experiment_bank_context_v3_fine_bins as fine
import experiment_bank_deep_context as deep
from bank_joint_policy import verify_counts

PLAN = v3.ROOT / 'reports' / 'bank_window_history_training_protocol.json'
STOP = deep.STOP
FEATURE_NAMES = v3.FEATURE_NAMES + history.features.FEATURE_NAMES


def specification():
    plan = v3.read(PLAN)
    feature_plan, base, parent = map(v3.read, (history.PLAN, v3.PLAN, v3.old.PROTOCOL))
    _, _, control = fine.specification()
    v3.old.validate_boundaries(parent)
    if (plan['feature_protocol_sha256'] != v3.old.digest(history.PLAN)
            or plan['context_protocol_sha256'] != v3.old.digest(v3.PLAN)
            or plan['parent_protocol_sha256'] != v3.old.digest(v3.old.PROTOCOL)
            or plan['fine_control_protocol_sha256'] != v3.old.digest(fine.PLAN)
            or plan['folds'] != [f['id'] for f in parent['selection_folds']]
            or plan['targets'] != base['targets'] or plan['targets'] != feature_plan['targets']
            or plan['past_end'] != feature_plan['past_end'] or plan['past_end'] >= '20240101'
            or plan['feature_names'] != list(FEATURE_NAMES)
            or len(FEATURE_NAMES) != 142 or len(set(FEATURE_NAMES)) != 142
            or plan['parameters'] != control['parameters'] or plan['rounds'] != control['rounds']
            or plan['parameters']['subsample'] != 1 or plan['parameters']['colsample_bytree'] != 1
            or plan['checkpoint_round_interval'] <= 0):
        raise ValueError('Frozen window-history training/control contract changed')
    return plan, parent, control


def augment(data, extra, start, end):
    xf, yf, tf, xd, yd, td, amounts, population = data
    if (extra.ndim != 2 or extra.shape[1] != 28 or xf.shape != (len(yf), 114)
            or xd.shape != (len(yd), 114) or start != len(yf)
            or end - start != len(yd) or not 0 <= start <= end <= len(extra)
            or len(tf) != len(yf) or len(td) != len(yd)):
        raise ValueError('History extension/control temporal population mismatch')
    fit_extra, dev_extra = extra[:start], extra[start:end]
    if not np.isfinite(fit_extra).all() or not np.isfinite(dev_extra).all():
        raise ValueError('Nonfinite window-history features')
    return (np.concatenate((xf, fit_extra), axis=1), yf, tf,
            np.concatenate((xd, dev_extra), axis=1), yd, td, amounts, population)


def dataset(fold, meta, control):
    validate_context_cache(meta)
    data = v3.dataset(fold, meta['signature']['base_context'])
    expected = fine.signature(fold, control, meta['signature']['base_context'], data)
    record = v3.read(fine.report_path(fold))
    v3.old.verify_resume(expected, record)
    if record['stage'] != 'completed_fine_unit' or not record['serialized_predictions_verified']:
        raise ValueError('Completed fine control or original row identity mismatch')
    for field in ('artifact', 'score'):
        if v3.old.digest(v3.ROOT / record[field + '_path']) != record[field + '_sha256']:
            raise ValueError('Frozen fine control artifact changed')
    con = sqlite3.connect(f'file:{v3.old.base.CACHE / "stage.sqlite"}?mode=ro', uri=True)
    try:
        start = con.execute('SELECT COUNT(*) FROM events WHERE event_date<?', (fold['development_start'],)).fetchone()[0]
        end = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?', (fold['development_end'],)).fetchone()[0]
    finally:
        con.close()
    extra = np.load(history.CACHE / 'extra.npy', mmap_mode='r')
    if tuple(extra.shape) != tuple(meta['shape']) or meta['rows'] != len(extra):
        raise ValueError('History completion shape mismatch')
    return augment(data, extra, start, end), record


def validate_context_cache(meta):
    base = meta['signature']['base_context']
    if (meta['rows'] != base['rows'] or meta['shape'] != [base['rows'], 28]
            or meta['ordered_source_identity_sha256'] != base['ordered_source_identity_sha256']
            or meta['all_available_past_used'] is not True or meta['no_2024_rows_read'] is not True):
        raise ValueError('Window-history completion original row identity mismatch')


def signature(fold, meta, data, plan, control_record):
    config = {'id': 'window-history-142', 'algorithm': 'xgboost',
              'rounds': plan['rounds'], 'parameters': plan['parameters']}
    xf, yf, tf, xd, yd, td, _, population = data
    base = v3.unit_signature(fold, config, meta['signature']['base_context'],
                             xf, yf, tf, xd, yd, td, population)
    if base['target_sha256'] != control_record['signature']['base']['target_sha256']:
        raise ValueError('Window-history targets differ from frozen control')
    return {'base': base, 'feature_names': list(FEATURE_NAMES),
            'training_protocol_sha256': v3.old.digest(PLAN), 'training_code_sha256': v3.old.digest(__file__),
            'history_preparation_signature': meta['signature'], 'history_sha256': meta['extra_sha256'],
            'checkpoint_code_sha256': v3.old.digest(deep.__file__),
            'control_record_sha256': v3.old.digest(fine.report_path(fold))}


def record_path(fold):
    return v3.ROOT / 'reports' / f"bank_window_history_{fold['id']}.json"


def restore_checkpoint(record, checkpoint, total):
    completed = record['completed_rounds']
    if isinstance(completed, bool) or not isinstance(completed, int) or not 0 <= completed <= total:
        raise ValueError('Invalid window-history checkpoint round count')
    if completed == 0:
        return None
    if v3.old.digest(checkpoint) != record['checkpoint_sha256']:
        raise ValueError('Window-history checkpoint checksum differs')
    model = xgb.Booster()
    model.load_model(checkpoint)
    if model.num_boosted_rounds() != completed:
        raise ValueError('Window-history checkpoint rounds differ')
    return model


def run(fold_id):
    started = time.monotonic()
    plan, parent, control = specification()
    if STOP.exists() and v3.read(STOP).get('stop') is True:
        print(json.dumps({'stage': 'stopped_before_fit'}), flush=True)
        return False
    fold = next(f for f in parent['selection_folds'] if f['id'] == fold_id)
    meta = history.prepare()
    data, control_record = dataset(fold, meta, control)
    expected = signature(fold, meta, data, plan, control_record)
    xf, yf, tf, xd, yd, td, _, _ = data
    output = v3.ROOT / 'models' / 'bank_window_candidates' / 'window-history-142' / fold_id
    output.mkdir(parents=True, exist_ok=True)
    artifact, checkpoint = output / 'model.ubj', output / 'checkpoint.ubj'
    scores = history.CACHE / f'{fold_id}-scores.npy'
    path = record_path(fold)
    record = v3.read(path) if path.exists() else {
        'stage': 'prepared', 'signature': expected, 'fit_rows': len(yf), 'development_rows': len(yd),
        'fit_sample_rate': 1.0, 'completed_rounds': 0, 'training_segments': [],
        'runtime_model_changed': False, 'operational_release_ready': False}
    v3.old.verify_resume(expected, record)
    if 'artifact_sha256' not in record:
        model = restore_checkpoint(record, checkpoint, plan['rounds'])
        completed = record['completed_rounds']
        if completed < plan['rounds']:
            if STOP.exists() and v3.read(STOP).get('stop') is True:
                v3.old.write_json(path, record)
                return False
            v3.old.write_json(path, record)
            print(json.dumps({'stage': 'window_history_fit_started', 'fold': fold_id,
                              'rows': len(yf), 'features': xf.shape[1], 'start_round': completed}), flush=True)
            types = ['c' if i in (2, 3) else 'q' for i in range(len(FEATURE_NAMES))]
            matrix = xgb.QuantileDMatrix(xf, label=yf, max_bin=plan['parameters']['max_bin'],
                                        feature_types=types, enable_categorical=True, nthread=8)
            callback = deep.Checkpoint(record, path, checkpoint, plan['rounds'], plan['checkpoint_round_interval'])
            fit_started = time.monotonic()
            model = xgb.train(plan['parameters'], matrix, num_boost_round=plan['rounds'] - completed,
                              xgb_model=model, callbacks=[callback])
            record['training_segments'].append({'start_round': completed, 'end_round': model.num_boosted_rounds(),
                                               'seconds': round(time.monotonic() - fit_started, 3)})
            del matrix
        record['completed_rounds'] = model.num_boosted_rounds()
        if record['completed_rounds'] != plan['rounds']:
            record['stage'] = 'paused_training'
            v3.old.write_json(path, record)
            print(json.dumps({'stage': 'window_history_paused', 'rounds': record['completed_rounds']}), flush=True)
            return False
        initial = model.inplace_predict(xd)
        model.save_model(artifact)
        v3.old.save_scores(scores, initial)
        record.update(stage='fitted', artifact_path=artifact.relative_to(v3.ROOT).as_posix(),
                      artifact_sha256=v3.old.digest(artifact), score_path=scores.relative_to(v3.ROOT).as_posix(),
                      score_sha256=v3.old.digest(scores))
        v3.old.write_json(path, record)
    if v3.old.digest(artifact) != record['artifact_sha256'] or v3.old.digest(scores) != record['score_sha256']:
        raise ValueError('Window-history artifact or scores changed')
    model = xgb.Booster()
    model.load_model(artifact)
    actual = model.inplace_predict(xd)
    if model.num_boosted_rounds() != plan['rounds'] or not np.array_equal(actual, np.load(scores)):
        raise ValueError('Window-history serialized predictions differ')
    targets = plan['targets']
    policy = v3.old.precision_policy(yd, actual, targets['precision'], targets['maximum_alert_rate'])
    flags = actual >= policy['threshold'] if policy['feasible'] else np.zeros(len(yd), dtype=bool)
    diagnostic = v3.old.evaluate(yd, td, flags)
    verify_counts(yd, td, flags, diagnostic)
    record.update(stage='completed_window_history_unit', ap=float(average_precision_score(yd, actual)),
                  policy=policy, diagnostic=diagnostic, serialized_predictions_verified=True,
                  seconds=round(time.monotonic() - started, 3))
    v3.old.write_json(path, record)
    print(json.dumps({'stage': record['stage'], 'fold': fold_id, 'ap': record['ap'],
                      'precision': diagnostic['precision'], 'recall': diagnostic['recall']}), flush=True)
    return True


def diagnostic(y, t, scores, targets):
    if scores.shape != y.shape or not np.isfinite(scores).all():
        raise ValueError('Window-history selection score population differs')
    policy = v3.old.precision_policy(y, scores, targets['precision'], targets['maximum_alert_rate'])
    flags = scores >= policy['threshold'] if policy['feasible'] else np.zeros(len(y), dtype=bool)
    result = v3.old.evaluate(y, t, flags)
    verify_counts(y, t, flags, result)
    return {'ap': float(average_precision_score(y, scores)), 'policy': policy, 'diagnostic': result}


def select():
    plan, parent, control = specification()
    meta = history.prepare()
    labels, kinds, scores, controls, evidence, offsets, offset = [], [], [], [], [], {}, 0
    for fold in parent['selection_folds']:
        data, control_record = dataset(fold, meta, control)
        record = v3.read(record_path(fold))
        v3.old.verify_resume(signature(fold, meta, data, plan, control_record), record)
        if record['stage'] != 'completed_window_history_unit' or not record['serialized_predictions_verified']:
            raise ValueError('Four completed window-history units required')
        for field in ('artifact', 'score'):
            if v3.old.digest(v3.ROOT / record[field + '_path']) != record[field + '_sha256']:
                raise ValueError('Window-history selection evidence changed')
        score = np.load(v3.ROOT / record['score_path'])
        baseline = np.load(v3.ROOT / control_record['score_path'])
        if score.shape != data[4].shape or baseline.shape != score.shape or not np.isfinite(score).all() or not np.isfinite(baseline).all():
            raise ValueError('Candidate/control development population differs')
        labels.append(data[4]); kinds.append(data[5]); scores.append(score); controls.append(baseline)
        offsets[fold['id']] = [offset, offset + len(data[4])]
        offset += len(data[4])
        evidence.append({'fold': fold['id'], 'signature': record['signature'],
                         'artifact_sha256': record['artifact_sha256'], 'score_sha256': record['score_sha256']})
        del data
        gc.collect()
    y, t, score, baseline = map(np.concatenate, (labels, kinds, scores, controls))
    measured = diagnostic(y, t, score, plan['targets'])
    control_measured = diagnostic(y, t, baseline, plan['targets'])
    search = v3.eligible_policy(y, t, score, plan['targets'])
    selected = v3.old.evaluate(y, t, score >= search['selected']['threshold']) if search['selected'] else None
    if selected:
        verify_counts(y, t, score >= search['selected']['threshold'], selected, plan['targets'])
    flag = score >= measured['policy']['threshold'] if measured['policy']['feasible'] else np.zeros(len(y), dtype=bool)
    report = {'stage': 'window_history_pooled_selection_completed', 'plan_sha256': v3.old.digest(PLAN),
              'code_sha256': v3.old.digest(__file__), 'evidence': evidence, 'rows': len(y), 'positives': int(y.sum()),
              **measured, 'fine_control': control_measured, 'search': search, 'selected_policy': search['selected'],
              'selected_evaluation': selected,
              'folds': {k: v3.old.evaluate(y[a:b], t[a:b], flag[a:b]) for k, (a, b) in offsets.items()},
              'measured_research_targets_passed': selected is not None, 'runtime_model_changed': False,
              'operational_release_ready': False, 'evaluation_limit': plan['evaluation_limit']}
    v3.old.write_json(v3.ROOT / 'reports' / 'bank_window_history_selection.json', report)
    print(json.dumps({'stage': report['stage'], 'selected_policy': report['selected_policy'],
                      'diagnostic': measured['diagnostic'], 'fine_control': control_measured['diagnostic']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--fold')
    action.add_argument('--select', action='store_true')
    action.add_argument('--all', action='store_true')
    args = parser.parse_args()
    with threadpool_limits(limits=8):
        if args.select:
            select()
        elif args.all:
            for fold in specification()[1]['selection_folds']:
                if not run(fold['id']):
                    raise SystemExit(0)
                gc.collect()
            select()
        else:
            run(args.fold)
