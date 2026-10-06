"""One-factor finer context-bin comparison, retaining frozen earlier evidence."""
import argparse
import json
import time
import numpy as np
import xgboost as xgb
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits
import experiment_bank_context_v3 as v3

PLAN = v3.ROOT / 'reports' / 'bank_context_v3_fine_bins_protocol.json'


def specification():
    plan, base = v3.read(PLAN), v3.read(v3.PLAN)
    if plan['context_protocol_sha256'] != v3.old.digest(v3.PLAN):
        raise ValueError('Frozen context control protocol changed')
    control = next(c for c in base['configurations'] if c['id'] == plan['control_configuration'])
    config = {**control, 'id': plan['configuration'], 'parameters': {**control['parameters'], 'max_bin': 2048}}
    return plan, base, config


def signature(fold, config, meta, data):
    xf, yf, tf, xd, yd, td, _, population = data
    return {'base': v3.unit_signature(fold, config, meta, xf, yf, tf, xd, yd, td, population),
            'fine_plan_sha256': v3.old.digest(PLAN), 'fine_code_sha256': v3.old.digest(__file__)}


def report_path(fold):
    return v3.ROOT / 'reports' / f"bank_context_v3_fine_{fold['id']}.json"


def run(fold_id):
    plan, base, config = specification()
    fold = next(f for f in v3.read(v3.old.PROTOCOL)['selection_folds'] if f['id'] == fold_id)
    meta = v3.prepare(); data = v3.dataset(fold, meta)
    xf, yf, tf, xd, yd, td, _, population = data
    expected = signature(fold, config, meta, data)
    control = v3.read(v3.ROOT / 'reports' / f"bank_context_v3_{fold_id}_{plan['control_configuration']}.json")
    if control['signature']['feature_sha256'] != expected['base']['feature_sha256'] or control['signature']['target_sha256'] != expected['base']['target_sha256']:
        raise ValueError('Fine-bin/control populations or features differ')
    path = report_path(fold)
    output = v3.ROOT / 'models' / 'bank_window_candidates' / 'context-v3-fine' / fold_id
    output.mkdir(parents=True, exist_ok=True)
    model_path, score_path = output / 'model.ubj', v3.CACHE / f'{fold_id}-fine.npy'
    record = v3.read(path) if path.exists() else {'stage': 'prepared', 'signature': expected, 'fit_rows': len(yf),
             'development_rows': len(yd), 'fit_sample_rate': 1.0, 'runtime_model_changed': False, 'operational_release_ready': False}
    v3.old.verify_resume(expected, record)
    v3.old.write_json(path, record)
    if 'artifact_sha256' not in record:
        print(json.dumps({'stage': 'fine_fit_started', 'fold': fold_id, 'rows': len(yf)}), flush=True)
        started = time.monotonic()
        types = ['c' if i in (2, 3) else 'q' for i in range(len(v3.FEATURE_NAMES))]
        matrix = xgb.QuantileDMatrix(xf, label=yf, max_bin=2048, feature_types=types, enable_categorical=True, nthread=8)
        model = xgb.train(config['parameters'], matrix, num_boost_round=config['rounds'])
        initial = model.inplace_predict(xd)
        model.save_model(model_path); v3.old.save_scores(score_path, initial)
        record.update(stage='fitted', artifact_path=model_path.relative_to(v3.ROOT).as_posix(),
                      artifact_sha256=v3.old.digest(model_path), score_path=score_path.relative_to(v3.ROOT).as_posix(),
                      score_sha256=v3.old.digest(score_path), fit_seconds=round(time.monotonic()-started, 3))
        v3.old.write_json(path, record)
    if v3.old.digest(model_path) != record['artifact_sha256'] or v3.old.digest(score_path) != record['score_sha256']:
        raise ValueError('Fine-bin artifact changed')
    model = xgb.Booster(); model.load_model(model_path)
    score = model.inplace_predict(xd)
    if not np.array_equal(score, np.load(score_path)):
        raise ValueError('Fine-bin serialized prediction mismatch')
    targets = base['targets']; policy = v3.old.precision_policy(yd, score, targets['precision'], targets['maximum_alert_rate'])
    flag = score >= policy['threshold'] if policy['feasible'] else np.zeros(len(yd), dtype=bool)
    record.update(stage='completed_fine_unit', ap=float(average_precision_score(yd, score)), policy=policy,
                  diagnostic=v3.old.evaluate(yd, td, flag), serialized_predictions_verified=True)
    v3.old.write_json(path, record)
    print(json.dumps({'stage': record['stage'], 'fold': fold_id, 'ap': record['ap'],
                      'precision': record['diagnostic']['precision'], 'recall': record['diagnostic']['recall'],
                      'subtypes': {k:r['recall'] for k,r in record['diagnostic']['subtypes'].items()}}), flush=True)


def select():
    plan, base, config = specification(); meta = v3.prepare()
    labels, kinds, scores, evidence = [], [], [], []
    for fold in v3.read(v3.old.PROTOCOL)['selection_folds']:
        data = v3.dataset(fold, meta); record = v3.read(report_path(fold))
        v3.old.verify_resume(signature(fold, config, meta, data), record)
        if record['stage'] != 'completed_fine_unit':
            raise ValueError('All fine-bin units required')
        for field in ('artifact', 'score'):
            if v3.old.digest(v3.ROOT / record[field + '_path']) != record[field + '_sha256']:
                raise ValueError('Fine selection evidence changed')
        labels.append(data[4]); kinds.append(data[5]); scores.append(np.load(v3.ROOT / record['score_path']))
        evidence.append({'fold': fold['id'], 'artifact_sha256': record['artifact_sha256'], 'score_sha256': record['score_sha256']})
    y, t, score = np.concatenate(labels), np.concatenate(kinds), np.concatenate(scores)
    search = v3.eligible_policy(y, t, score, base['targets'])
    policy = v3.old.precision_policy(y, score, base['targets']['precision'], base['targets']['maximum_alert_rate'])
    flag = score >= policy['threshold'] if policy['feasible'] else np.zeros(len(y), dtype=bool)
    report = {'stage': 'fine_pooled_selection_completed', 'plan_sha256': v3.old.digest(PLAN),
              'code_sha256': v3.old.digest(__file__), 'evidence': evidence, 'rows': len(y), 'positives': int(y.sum()),
              'ap': float(average_precision_score(y, score)), 'search': search, 'selected_policy': search['selected'],
              'policy': policy, 'diagnostic': v3.old.evaluate(y, t, flag),
              'measured_research_targets_passed': search['selected'] is not None,
              'runtime_model_changed': False, 'operational_release_ready': False}
    v3.old.write_json(v3.ROOT / 'reports' / 'bank_context_v3_fine_selection.json', report)
    print(json.dumps({'stage': report['stage'], 'selected_policy': report['selected_policy'], 'diagnostic': report['diagnostic']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--fold'); parser.add_argument('--select', action='store_true')
    args = parser.parse_args()
    with threadpool_limits(limits=8):
        select() if args.select else run(args.fold)
