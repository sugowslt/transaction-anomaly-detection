"""Checkpointed full-past interaction learning; frozen temporal development only."""
import argparse
import gc
import json
import time
import numpy as np
import xgboost as xgb
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits
import experiment_bank_context_v3 as v3
import experiment_bank_context_v3_fine_bins as fine
from bank_joint_policy import verify_counts

PLAN = v3.ROOT / 'reports' / 'bank_deep_context_protocol.json'
STOP = v3.ROOT / 'data' / 'work-state' / 'stage27-stop-training.json'


def specification():
    plan, base, parent = v3.read(PLAN), v3.read(v3.PLAN), v3.read(v3.old.PROTOCOL)
    if (plan['context_protocol_sha256'] != v3.old.digest(v3.PLAN)
            or plan['parent_protocol_sha256'] != v3.old.digest(v3.old.PROTOCOL)
            or plan['folds'] != [f['id'] for f in parent['selection_folds']]
            or plan['targets'] != base['targets'] or plan['parameters']['subsample'] != 1
            or plan['parameters']['colsample_bytree'] != 1):
        raise ValueError('Frozen deep-context boundaries/targets/full-data contract changed')
    return plan, parent


def signature(fold, meta, data, plan):
    config = {'id': 'deep-context', 'algorithm': 'xgboost', 'rounds': plan['rounds'], 'parameters': plan['parameters']}
    xf, yf, tf, xd, yd, td, _, population = data
    return {'base': v3.unit_signature(fold, config, meta, xf, yf, tf, xd, yd, td, population),
            'deep_plan_sha256': v3.old.digest(PLAN), 'deep_code_sha256': v3.old.digest(__file__),
            'source_stage_sha256': v3.old.digest(v3.old.base.CACHE / 'stage.sqlite'),
            'feature_preparation_code_sha256': v3.old.digest(v3.old.__file__)}


def record_path(fold):
    return v3.ROOT / 'reports' / f"bank_deep_context_{fold['id']}.json"


class Checkpoint(xgb.callback.TrainingCallback):
    def __init__(self, record, path, artifact, total, interval):
        self.record, self.path, self.artifact = record, path, artifact
        self.total, self.interval = total, interval
        self.started = time.monotonic()

    def after_iteration(self, model, epoch, evals_log):
        completed = model.num_boosted_rounds()
        stop = STOP.exists() and v3.read(STOP).get('stop') is True
        if completed % self.interval == 0 or completed == self.total or stop:
            temporary = self.artifact.with_name('checkpoint.tmp.ubj')
            model.save_model(temporary); temporary.replace(self.artifact)
            self.record.update(stage='checkpointed', completed_rounds=completed,
                               checkpoint_sha256=v3.old.digest(self.artifact))
            v3.old.write_json(self.path, self.record)
            print(json.dumps({'stage': 'deep_context_checkpoint', 'fold': self.record['signature']['base']['fold']['id'],
                              'rounds': completed, 'total_rounds': self.total, 'stop_requested': stop,
                              'session_seconds': round(time.monotonic() - self.started, 3)}), flush=True)
        return stop


def run(fold_id):
    started = time.monotonic()
    plan, parent = specification()
    if STOP.exists() and v3.read(STOP).get('stop') is True:
        print(json.dumps({'stage': 'stopped_before_fit'}), flush=True); return
    fold = next(f for f in parent['selection_folds'] if f['id'] == fold_id)
    meta = v3.prepare(); data = v3.dataset(fold, meta)
    expected = signature(fold, meta, data, plan)
    control = v3.read(fine.report_path(fold))['signature']['base']
    if any(expected['base'][k] != control[k] for k in ('feature_sha256', 'target_sha256', 'population')):
        raise ValueError('Deep candidate/control population changed')
    xf, yf, tf, xd, yd, td, _, _ = data
    output = v3.ROOT / 'models' / 'bank_window_candidates' / 'deep-context' / fold_id
    output.mkdir(parents=True, exist_ok=True)
    artifact, checkpoint = output / 'model.ubj', output / 'checkpoint.ubj'
    scores = v3.CACHE / f'{fold_id}-deep.npy'; path = record_path(fold)
    record = v3.read(path) if path.exists() else {'stage': 'prepared', 'signature': expected, 'fit_rows': len(yf),
             'development_rows': len(yd), 'fit_sample_rate': 1.0, 'completed_rounds': 0, 'training_segments': [],
             'runtime_model_changed': False, 'operational_release_ready': False}
    v3.old.verify_resume(expected, record)
    if 'artifact_sha256' not in record:
        completed = record['completed_rounds']
        model = None
        if completed:
            if v3.old.digest(checkpoint) != record['checkpoint_sha256']:
                raise ValueError('Deep training checkpoint changed')
            model = xgb.Booster(); model.load_model(checkpoint)
            if model.num_boosted_rounds() != completed:
                raise ValueError('Checkpoint round count differs')
        if completed < plan['rounds']:
            v3.old.write_json(path, record)
            print(json.dumps({'stage': 'deep_context_fit_started', 'fold': fold_id, 'rows': len(yf),
                              'features': xf.shape[1], 'start_round': completed}), flush=True)
            types = ['c' if i in (2, 3) else 'q' for i in range(len(v3.FEATURE_NAMES))]
            matrix = xgb.QuantileDMatrix(xf, label=yf, max_bin=plan['parameters']['max_bin'],
                                        feature_types=types, enable_categorical=True, nthread=8)
            callback = Checkpoint(record, path, checkpoint, plan['rounds'], plan['checkpoint_round_interval'])
            fit_started = time.monotonic()
            model = xgb.train(plan['parameters'], matrix, num_boost_round=plan['rounds'] - completed,
                              xgb_model=model, callbacks=[callback])
            record['training_segments'].append({'start_round': completed, 'end_round': model.num_boosted_rounds(),
                                               'seconds': round(time.monotonic() - fit_started, 3)})
            del matrix
        record['completed_rounds'] = model.num_boosted_rounds()
        if record['completed_rounds'] != plan['rounds']:
            record['stage'] = 'paused_training'; v3.old.write_json(path, record)
            print(json.dumps({'stage': 'paused_training', 'rounds': record['completed_rounds']}), flush=True); return
        initial = model.inplace_predict(xd)
        model.save_model(artifact); v3.old.save_scores(scores, initial)
        record.update(stage='fitted', artifact_path=artifact.relative_to(v3.ROOT).as_posix(),
                      artifact_sha256=v3.old.digest(artifact), score_path=scores.relative_to(v3.ROOT).as_posix(),
                      score_sha256=v3.old.digest(scores))
        v3.old.write_json(path, record)
    if v3.old.digest(artifact) != record['artifact_sha256'] or v3.old.digest(scores) != record['score_sha256']:
        raise ValueError('Deep artifact changed')
    model = xgb.Booster(); model.load_model(artifact); actual = model.inplace_predict(xd)
    if model.num_boosted_rounds() != plan['rounds'] or not np.array_equal(actual, np.load(scores)):
        raise ValueError('Deep serialized rounds or predictions differ')
    targets = plan['targets']; policy = v3.old.precision_policy(yd, actual, targets['precision'], targets['maximum_alert_rate'])
    flags = actual >= policy['threshold'] if policy['feasible'] else np.zeros(len(yd), dtype=bool)
    record.update(stage='completed_deep_context_unit', ap=float(average_precision_score(yd, actual)),
                  policy=policy, diagnostic=v3.old.evaluate(yd, td, flags), serialized_predictions_verified=True,
                  seconds=round(time.monotonic() - started, 3))
    v3.old.write_json(path, record)
    print(json.dumps({'stage': record['stage'], 'fold': fold_id, 'ap': record['ap'],
                      'precision': record['diagnostic']['precision'], 'recall': record['diagnostic']['recall']}), flush=True)


def select():
    plan, parent = specification(); meta = v3.prepare()
    yparts, tparts, sparts, evidence, offsets, offset = [], [], [], [], {}, 0
    for fold in parent['selection_folds']:
        data = v3.dataset(fold, meta); record = v3.read(record_path(fold))
        v3.old.verify_resume(signature(fold, meta, data, plan), record)
        if record['stage'] != 'completed_deep_context_unit' or not record['serialized_predictions_verified']:
            raise ValueError('Four completed deep units required')
        for field in ('artifact', 'score'):
            if v3.old.digest(v3.ROOT / record[field + '_path']) != record[field + '_sha256']:
                raise ValueError('Deep selection evidence changed')
        score = np.load(v3.ROOT / record['score_path'])
        if score.shape != data[4].shape or not np.isfinite(score).all():
            raise ValueError('Deep score population differs')
        yparts.append(data[4]); tparts.append(data[5]); sparts.append(score)
        offsets[fold['id']] = [offset, offset + len(data[4])]; offset += len(data[4])
        evidence.append({'fold': fold['id'], 'signature': record['signature'],
                         'artifact_sha256': record['artifact_sha256'], 'score_sha256': record['score_sha256']})
        del data; gc.collect()
    y, t, s = map(np.concatenate, (yparts, tparts, sparts))
    search = v3.eligible_policy(y, t, s, plan['targets'])
    policy = v3.old.precision_policy(y, s, plan['targets']['precision'], plan['targets']['maximum_alert_rate'])
    flag = s >= policy['threshold'] if policy['feasible'] else np.zeros(len(y), dtype=bool)
    diagnostic = v3.old.evaluate(y, t, flag); verify_counts(y, t, flag, diagnostic)
    selected = v3.old.evaluate(y, t, s >= search['selected']['threshold']) if search['selected'] else None
    if selected:
        verify_counts(y, t, s >= search['selected']['threshold'], selected, plan['targets'])
    report = {'stage': 'deep_context_pooled_selection_completed', 'plan_sha256': v3.old.digest(PLAN),
              'code_sha256': v3.old.digest(__file__), 'evidence': evidence, 'rows': len(y), 'positives': int(y.sum()),
              'ap': float(average_precision_score(y, s)), 'search': search, 'selected_policy': search['selected'],
              'policy': policy, 'diagnostic': diagnostic, 'selected_evaluation': selected,
              'folds': {k: v3.old.evaluate(y[a:b], t[a:b], flag[a:b]) for k, (a, b) in offsets.items()},
              'measured_research_targets_passed': selected is not None, 'runtime_model_changed': False,
              'operational_release_ready': False, 'evaluation_limit': plan['evaluation_limit']}
    v3.old.write_json(v3.ROOT / 'reports' / 'bank_deep_context_selection.json', report)
    print(json.dumps({'stage': report['stage'], 'selected_policy': report['selected_policy'], 'diagnostic': diagnostic}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--fold'); parser.add_argument('--select', action='store_true')
    args = parser.parse_args()
    with threadpool_limits(limits=8): select() if args.select else run(args.fold)
