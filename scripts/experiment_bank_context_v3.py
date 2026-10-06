"""Resumable, past-only context experiments. Never replaces a serving artifact."""
from __future__ import annotations

import argparse
import gc
import hashlib
import itertools
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import skops.io as sio
import xgboost as xgb
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits

import experiment_bank_mechanism_heads as old
import bank_context_v3_features as features
from select_bank_capacity_policy import eligible_policy
from select_bank_mechanism_policy import array_digest
from model_artifact import TRUSTED_TYPES

ROOT = old.base.ROOT
CACHE = old.base.DATA / '.bank-context-v3-cache'
PLAN = ROOT / 'reports' / 'bank_context_v3_protocol.json'
FEATURE_NAMES = old.FEATURE_NAMES + features.PRIOR_FEATURE_NAMES + features.WINDOW_FEATURE_NAMES


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def prepare():
    plan = read(PLAN)
    parent = read(old.PROTOCOL)
    old.validate_boundaries(parent)
    if plan['parent_protocol_sha256'] != old.digest(old.PROTOCOL):
        raise ValueError('Frozen parent protocol changed')
    if (plan['past_end'] != max(f['development_end'] for f in parent['selection_folds'])
            or plan['past_end'] >= '20240101' or plan['folds'] != [f['id'] for f in parent['selection_folds']]):
        raise ValueError('Context experiment must retain all declared past-only boundaries')
    prior = old.window.prepare(True)
    signature = {'parent': old.digest(old.PROTOCOL), 'plan': old.digest(PLAN),
                 'features': old.digest(features.__file__), 'builder': old.digest(__file__),
                 'prior_cache': prior['provenance'], 'names': list(FEATURE_NAMES)}
    CACHE.mkdir(exist_ok=True)
    done = CACHE / 'complete.json'
    if done.exists():
        meta = read(done)
        if meta['signature'] != signature:
            raise ValueError('Context cache signature changed; do not overwrite old evidence')
        if old.digest(CACHE / 'extra.npy') != meta['extra_sha256']:
            raise ValueError('Context feature cache checksum mismatch')
        return meta
    # Source query excludes 2024 entirely. Same-day state is committed only after snapshots.
    con = sqlite3.connect(f'file:{old.base.CACHE / "stage.sqlite"}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    count = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?', (plan['past_end'],)).fetchone()[0]
    labels = np.concatenate([np.load(old.graph.CACHE / f'y_{n}.npy') for n in ('fit', 'tune')])
    kinds = np.concatenate([np.load(old.graph.CACHE / f'type_{n}.npy') for n in ('fit', 'tune')])
    if count != len(labels):
        raise ValueError('Past source population differs from existing verified matrices')
    matrix = np.lib.format.open_memmap(CACHE / 'extra.tmp.npy', mode='w+', dtype=np.float32,
                                      shape=(count, len(FEATURE_NAMES) - len(old.FEATURE_NAMES)))
    state = features.PastContextState()
    digest = hashlib.sha256()
    index = 0
    cursor = con.execute('SELECT rowid,* FROM events WHERE event_date<=? ORDER BY event_date,rowid', (plan['past_end'],))
    for day, daily in itertools.groupby(cursor, key=lambda row: row['event_date']):
        rows = [dict(row) for row in daily]
        buckets, senders, extra = defaultdict(list), defaultdict(list), {}
        for row in rows:
            buckets[row['hour']].append(row)
            senders[(row['sender_bank'], row['sender'])].append(row)
        for bucket in buckets.values():
            if len(bucket) > 20_000:
                raise ValueError('Runtime bucket bound exceeded; no truncation allowed')
            raw = [old.base.row_event(row) for row in bucket]
            extension = features.window_vectors(raw)
            for row, event, value in zip(bucket, raw, extension, strict=True):
                extra[row['rowid']] = (*state.snapshot(event), *value)
        for sender_rows in senders.values():
            for row in sender_rows:
                if labels[index] != row['label'] or kinds[index] != row['anomaly_type'].encode('ascii'):
                    raise ValueError('Context cache source label/type ordering mismatch')
                matrix[index] = extra[row['rowid']]
                digest.update(np.asarray([row['rowid']], dtype='<i8').tobytes())
                index += 1
        for row in rows:
            state.add(old.base.row_event(row))
        if day.endswith('01'):
            print(json.dumps({'stage': 'context_features', 'day': day, 'rows': index}), flush=True)
    con.close()
    if index != count or not np.isfinite(matrix).all():
        raise ValueError('Incomplete or nonfinite context features')
    existing = read(ROOT / 'reports' / 'bank_capacity_2023Q4_config1.json')['signature']['population']
    if digest.hexdigest() != existing['ordered_source_identity_sha256'] or count != existing['verified_rows']:
        raise ValueError('Context extraction order differs from previously verified original rows')
    matrix.flush()
    del matrix
    (CACHE / 'extra.tmp.npy').replace(CACHE / 'extra.npy')
    meta = {'signature': signature, 'rows': count, 'shape': [count, len(FEATURE_NAMES) - 86],
            'ordered_source_identity_sha256': digest.hexdigest(), 'extra_sha256': old.digest(CACHE / 'extra.npy'),
            'all_available_past_used': True, 'no_2024_rows_read': True}
    old.write_json(done, meta)
    return meta


def dataset(fold, meta):
    prior = old.window.prepare(True)
    xf, yf, tf, xd, yd, td, amounts, population = old.prepare_fold(fold, prior)
    if meta['rows'] != prior['counts']['fit'] + prior['counts']['tune']:
        raise ValueError('Context/prior count mismatch')
    con = sqlite3.connect(f'file:{old.base.CACHE / "stage.sqlite"}?mode=ro', uri=True)
    start = con.execute('SELECT COUNT(*) FROM events WHERE event_date<?', (fold['development_start'],)).fetchone()[0]
    end = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?', (fold['development_end'],)).fetchone()[0]
    con.close()
    extra = np.load(CACHE / 'extra.npy', mmap_mode='r')
    if end - start != len(yd):
        raise ValueError('Development feature boundary mismatch')
    return (np.concatenate((xf, extra[:len(yf)]), axis=1), yf, tf,
            np.concatenate((xd, extra[start:end]), axis=1), yd, td, amounts, population)


def unit_signature(fold, config, meta, xf, yf, tf, xd, yd, td, population):
    return {'plan_sha256': old.digest(PLAN), 'code_sha256': old.digest(__file__),
                 'feature_code_sha256': old.digest(features.__file__), 'cache_sha256': meta['extra_sha256'],
                 'feature_sha256': array_digest(xf, xd), 'target_sha256': array_digest(yf, yd, tf, td),
                 'fold': fold, 'configuration': config, 'population': population,
                 'environment': {'numpy': np.__version__, 'sklearn': __import__('sklearn').__version__,
                                 'xgboost': xgb.__version__, 'skops': __import__('skops').__version__}}


def run(fold_id, config_id):
    started = time.monotonic()
    plan, parent = read(PLAN), read(old.PROTOCOL)
    fold = next(f for f in parent['selection_folds'] if f['id'] == fold_id)
    config = next(c for c in plan['configurations'] if c['id'] == config_id)
    meta = prepare()
    xf, yf, tf, xd, yd, td, _, population = dataset(fold, meta)
    signature = unit_signature(fold, config, meta, xf, yf, tf, xd, yd, td, population)
    path = ROOT / 'reports' / f'bank_context_v3_{fold_id}_{config_id}.json'
    output = ROOT / 'models' / 'bank_window_candidates' / 'context-v3' / f'{fold_id}-{config_id}'
    output.mkdir(parents=True, exist_ok=True)
    scores_path = CACHE / f'{fold_id}-{config_id}.npy'
    artifact_path = output / ('model.ubj' if config['algorithm'] == 'xgboost' else 'model.skops')
    if path.exists():
        report = read(path)
        old.verify_resume(signature, report)
    else:
        report = {'stage': 'prepared', 'signature': signature, 'fit_rows': len(yf), 'development_rows': len(yd),
                  'fit_sample_rate': 1.0, 'operational_release_ready': False, 'runtime_model_changed': False}
        old.write_json(path, report)
    params = config['parameters']
    if 'artifact_sha256' not in report:
        print(json.dumps({'stage': 'context_fit_started', 'fold': fold_id, 'config': config_id,
                          'rows': len(yf), 'features': xf.shape[1]}), flush=True)
        fit_started = time.monotonic()
        if config['algorithm'] == 'xgboost':
            types = ['c' if i in (2, 3) else 'q' for i in range(len(FEATURE_NAMES))]
            train = xgb.QuantileDMatrix(xf, label=yf, feature_types=types, enable_categorical=True, nthread=8)
            model = xgb.train(params, train, num_boost_round=config['rounds'])
            model.save_model(artifact_path)
            del train
        else:
            model = HistGradientBoostingClassifier(**params, categorical_features=[2, 3])
            model.fit(xf, yf)
            sio.dump(model, artifact_path)
        fitted_score = (model.inplace_predict(xd) if config['algorithm'] == 'xgboost'
                        else model.predict_proba(xd)[:, 1])
        old.save_scores(scores_path, fitted_score)
        report.update(stage='fitted', artifact_path=artifact_path.relative_to(ROOT).as_posix(),
                      artifact_sha256=old.digest(artifact_path), score_sha256=old.digest(scores_path),
                      fit_seconds=round(time.monotonic() - fit_started, 3))
        old.write_json(path, report)
    if old.digest(artifact_path) != report['artifact_sha256']:
        raise ValueError('Context artifact changed')
    if config['algorithm'] == 'xgboost':
        model = xgb.Booster(); model.load_model(artifact_path)
        score = model.inplace_predict(xd)
    else:
        unexpected = set(sio.get_untrusted_types(file=artifact_path)) - set(TRUSTED_TYPES)
        if unexpected:
            raise ValueError(f'Unexpected research model types: {sorted(unexpected)}')
        model = sio.load(artifact_path, trusted=TRUSTED_TYPES)
        score = model.predict_proba(xd)[:, 1]
    if scores_path.exists() and 'score_sha256' in report:
        if old.digest(scores_path) != report['score_sha256'] or not np.array_equal(score, np.load(scores_path)):
            raise ValueError('Reloaded context predictions differ')
    else:
        old.save_scores(scores_path, score)
    targets = plan['targets']
    policy = old.precision_policy(yd, score, targets['precision'], targets['maximum_alert_rate'])
    alert = score >= policy['threshold'] if policy['feasible'] else np.zeros(len(yd), dtype=bool)
    report.update(stage='completed_context_unit', score_path=scores_path.relative_to(ROOT).as_posix(),
                  score_sha256=old.digest(scores_path), development_average_precision=float(average_precision_score(yd, score)),
                  precision_policy=policy, diagnostic=old.evaluate(yd, td, alert), serialized_predictions_verified=True,
                  seconds=round(time.monotonic() - started, 3))
    old.write_json(path, report)
    print(json.dumps({'stage': report['stage'], 'fold': fold_id, 'config': config_id,
                      'ap': report['development_average_precision'], 'metrics': report['diagnostic'],
                      'fit_seconds': report['fit_seconds']}), flush=True)


def select():
    plan, parent = read(PLAN), read(old.PROTOCOL)
    meta = prepare()
    yparts, tparts, scores, units, offsets = [], [], defaultdict(list), [], {}
    offset = 0
    for fold in parent['selection_folds']:
        xf, yf, tf, xd, yd, td, _, population = dataset(fold, meta)
        for config in plan['configurations']:
            record = read(ROOT / 'reports' / f"bank_context_v3_{fold['id']}_{config['id']}.json")
            if record['stage'] != 'completed_context_unit' or record['signature']['population'] != population:
                raise ValueError('All declared context units must complete before common selection')
            old.verify_resume(unit_signature(fold, config, meta, xf, yf, tf, xd, yd, td, population), record)
            sp = ROOT / record['score_path']; ap = ROOT / record['artifact_path']
            if old.digest(sp) != record['score_sha256'] or old.digest(ap) != record['artifact_sha256']:
                raise ValueError('Context selection artifact changed')
            values = np.load(sp)
            if values.shape != yd.shape or not np.isfinite(values).all() or not ((values >= 0) & (values <= 1)).all():
                raise ValueError('Invalid context scores')
            if record['signature']['feature_sha256'] != array_digest(xf, xd) or record['signature']['target_sha256'] != array_digest(yf, yd, tf, td):
                raise ValueError('Context selection population or features changed')
            scores[config['id']].append(values)
            units.append({'fold': fold['id'], 'configuration': config['id'], 'artifact_sha256': record['artifact_sha256'],
                          'score_sha256': record['score_sha256']})
        yparts.append(yd); tparts.append(td); offsets[fold['id']] = [offset, offset + len(yd)]; offset += len(yd)
        del xf, yf, tf, xd
        gc.collect()
    y, kinds = np.concatenate(yparts), np.concatenate(tparts)
    results, eligible = {}, []
    for key, values in scores.items():
        value = np.concatenate(values)
        search = eligible_policy(y, kinds, value, plan['targets'])
        diagnostic = old.precision_policy(y, value, plan['targets']['precision'], plan['targets']['maximum_alert_rate'])
        flag = value >= diagnostic['threshold'] if diagnostic['feasible'] else np.zeros(len(y), dtype=bool)
        results[key] = {'average_precision': float(average_precision_score(y, value)), 'search': search,
                        'precision_policy': diagnostic, 'diagnostic': old.evaluate(y, kinds, flag),
                        'folds': {f: old.evaluate(y[a:b], kinds[a:b], flag[a:b]) for f, (a, b) in offsets.items()}}
        if search['selected']:
            eligible.append({'configuration': key, **search['selected']})
    selected = min(eligible, key=lambda p: (-p['tp'], p['fp'], p['configuration'])) if eligible else None
    report = {'stage': 'context_pooled_selection_completed', 'rows': len(y), 'positives': int(y.sum()),
              'plan_sha256': old.digest(PLAN), 'code_sha256': old.digest(__file__), 'units': units,
              'results': results, 'selected_policy': selected, 'measured_research_targets_passed': selected is not None,
              'operational_release_ready': False, 'runtime_model_changed': False,
              'evaluation_limit': 'Past development data used for selection and reporting; not independent validation.'}
    old.write_json(ROOT / 'reports' / 'bank_context_v3_selection.json', report)
    print(json.dumps({'stage': report['stage'], 'selected_policy': selected}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true'); parser.add_argument('--select', action='store_true')
    parser.add_argument('--fold'); parser.add_argument('--configuration')
    args = parser.parse_args()
    with threadpool_limits(limits=8):
        if args.prepare:
            print(json.dumps(prepare(), ensure_ascii=True), flush=True)
        elif args.select:
            select()
        else:
            run(args.fold, args.configuration)
