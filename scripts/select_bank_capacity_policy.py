"""Verify every past capacity unit before selecting a common binary threshold."""
from __future__ import annotations

import gc
import json
import time

import numpy as np
from sklearn.metrics import average_precision_score
from threadpoolctl import threadpool_limits

import experiment_bank_capacity as capacity
import experiment_bank_mechanism_heads as experiment
from audit_model_readiness import assess
from select_bank_mechanism_policy import array_digest

PLAN = experiment.base.ROOT / 'reports' / 'bank_capacity_policy_protocol.json'


def eligible_policy(labels, kinds, scores, targets):
    if (labels.ndim != 1 or kinds.shape != labels.shape or scores.shape != labels.shape
            or not len(labels) or not np.isin(labels, [0, 1]).all()
            or not np.isfinite(scores).all() or not ((scores >= 0) & (scores <= 1)).all()):
        raise ValueError('Invalid pooled capacity predictions')
    positive = labels == 1
    if not positive.any():
        raise ValueError('Capacity policy requires observed positive labels')
    order = np.argsort(-scores, kind='stable')
    ranked = scores[order]
    ends = np.r_[np.flatnonzero(ranked[:-1] != ranked[1:]), len(labels) - 1]
    alerts = ends + 1
    tp = np.cumsum(positive[order])[ends]
    fp = alerts - tp
    valid = ((tp / alerts >= targets['precision'])
             & (tp / positive.sum() >= targets['recall'])
             & (alerts / len(labels) <= targets['maximum_alert_rate']))
    for kind in np.unique(kinds[positive]):
        subtype = positive & (kinds == kind)
        detected = np.cumsum(subtype[order])[ends]
        valid &= detected / subtype.sum() >= targets['each_observed_type_recall']
    candidates = np.flatnonzero(valid)
    if not len(candidates):
        return {'distinct_thresholds_checked': len(ends), 'eligible_thresholds': 0, 'selected': None}
    best = min(candidates, key=lambda index: (-int(tp[index]), int(fp[index]), -float(ranked[ends[index]])))
    return {'distinct_thresholds_checked': len(ends), 'eligible_thresholds': len(candidates),
            'selected': {'threshold': float(ranked[ends[best]]), 'tp': int(tp[best]), 'fp': int(fp[best])}}


def main():
    started = time.monotonic()
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    fitting = json.loads(capacity.PLAN.read_text(encoding='utf-8'))
    protocol = json.loads(experiment.PROTOCOL.read_text(encoding='utf-8'))
    experiment.validate_boundaries(protocol)
    targets = {**protocol['selection_rule']['research_targets'],
               'maximum_alert_rate': protocol['selection_rule']['maximum_pooled_alert_rate']}
    if (plan['parent_protocol_sha256'] != experiment.digest(experiment.PROTOCOL)
            or fitting['parent_protocol_sha256'] != plan['parent_protocol_sha256']
            or fitting['folds'] != [fold['id'] for fold in protocol['selection_folds']]
            or plan['targets'] != targets):
        raise ValueError('Capacity selection target or parent fingerprint mismatch')
    records = {}
    for fold in protocol['selection_folds']:
        for configuration in (1, 2):
            path = experiment.base.ROOT / 'reports' / f"bank_capacity_{fold['id']}_config{configuration}.json"
            record = json.loads(path.read_text(encoding='utf-8'))
            if (record.get('stage') != 'completed_one_capacity_unit'
                    or record.get('fit_sample_rate') != 1.0
                    or any(record.get(key) is not False for key in ('operational_release_ready', 'runtime_model_changed', 'selection_complete'))):
                raise ValueError('All eight completed capacity units required before selection')
            records[(fold['id'], configuration)] = record
    meta = experiment.window.prepare(True)
    labels, kinds, amounts, histories, pairs, offsets = [], [], [], [], [], {}
    scores = {1: [], 2: []}; evidence = []; offset = 0
    for fold in protocol['selection_folds']:
        xf, yf, tf, xd, yd, td, money, population = experiment.prepare_fold(fold, meta)
        for configuration in (1, 2):
            record = records[(fold['id'], configuration)]
            params = capacity.parameters(fitting, configuration)
            expected = {'capacity_plan_sha256': experiment.digest(capacity.PLAN),
                        'parent_protocol_sha256': experiment.digest(experiment.PROTOCOL),
                        'code_sha256': experiment.digest(capacity.__file__),
                        'feature_preparation_code_sha256': experiment.digest(experiment.__file__),
                        'fold': fold, 'configuration': configuration,
                        'parameters': json.loads(json.dumps(params)), 'population': population,
                        'cache_provenance': meta['provenance'], 'feature_sha256': array_digest(xf, xd),
                        'target_sha256': array_digest(yf, yd, tf, td),
                        'environment': {'numpy': np.__version__, 'sklearn': __import__('sklearn').__version__, 'skops': __import__('skops').__version__}}
            experiment.verify_resume(expected, record)
            if record['fit_rows'] != len(yf) or record['development_rows'] != len(yd):
                raise ValueError('Capacity population count mismatch')
            path = experiment.base.ROOT / 'models' / 'bank_window_candidates' / 'capacity' / f"{fold['id']}-config{configuration}" / 'general.skops'
            score_path = experiment.base.DATA / '.bank-capacity-cache' / f"{fold['id']}-config{configuration}" / 'development.npy'
            if (record['artifact_path'] != path.relative_to(experiment.base.ROOT).as_posix()
                    or record['score_path'] != score_path.relative_to(experiment.base.ROOT).as_posix()
                    or experiment.digest(score_path) != record['score_sha256']):
                raise ValueError('Unexpected capacity path or score fingerprint')
            artifact = experiment.load_verified(path, record['artifact_sha256'], experiment.FEATURE_NAMES, None)
            model = artifact['model']
            if (artifact['mappings'] != population['raw_category_mappings'] or artifact['fold'] != fold
                    or artifact['configuration'] != configuration or artifact['capacity_plan_sha256'] != expected['capacity_plan_sha256']
                    or any(model.get_params()[key] != value for key, value in params.items())
                    or list(model.categorical_features) != list(experiment.base.CATEGORICAL_INDICES)
                    or model.n_iter_ != params['max_iter'] or record['iterations'] != params['max_iter']
                    or not np.array_equal(model.classes_, [0, 1])):
                raise ValueError('Capacity estimator contract mismatch')
            saved = np.load(score_path); actual = model.predict_proba(xd)[:, 1]
            if saved.shape != (len(yd),) or not np.array_equal(saved, actual):
                raise ValueError('Capacity serialized prediction mismatch')
            ap = float(average_precision_score(yd, actual))
            threshold = record['f1_diagnostic_policy']['threshold']
            metrics = experiment.evaluate(yd, td, saved >= threshold)
            if ap != record['development_average_precision'] or metrics != record['f1_diagnostic']:
                raise ValueError('Capacity reported diagnostics differ from predictions')
            scores[configuration].append(saved)
            evidence.append({'fold': fold['id'], 'configuration': configuration, 'artifact_sha256': record['artifact_sha256'],
                             'score_sha256': record['score_sha256'], 'exact_predictions_match': True,
                             'train_ap': record['train_average_precision'], 'development_ap': ap})
            del artifact, model, actual
        offsets[fold['id']] = (offset, offset + len(yd)); offset += len(yd)
        labels.append(yd.copy()); kinds.append(td.copy()); amounts.append(money.copy())
        histories.append(xd[:, experiment.FEATURE_NAMES.index('sender_all_count_log')] > 0)
        pairs.append(xd[:, experiment.FEATURE_NAMES.index('recipient_all_count_log')] > 0)
        print(json.dumps({'stage': 'capacity_fold_verified', 'fold': fold['id'], 'models': 2}), flush=True)
        del xf, yf, tf, xd, yd, td
        gc.collect()
    y, kind, money = np.concatenate(labels), np.concatenate(kinds), np.concatenate(amounts)
    history, pair = np.concatenate(histories), np.concatenate(pairs)
    masks = {'amount_below_2m': money < 2_000_000, 'amount_2m_to_10m': (money >= 2_000_000) & (money < 10_000_000),
             'amount_at_least_10m': money >= 10_000_000, 'sender_history': history, 'no_sender_history': ~history,
             'existing_sender_recipient_pair': pair, 'new_sender_recipient_pair': ~pair}
    results = {}; eligible = []
    for configuration, parts in scores.items():
        values = np.concatenate(parts)
        search = eligible_policy(y, kind, values, targets)
        diagnostic = experiment.precision_policy(y, values, targets['precision'], targets['maximum_alert_rate'])
        flag = values >= diagnostic['threshold'] if diagnostic['feasible'] else np.zeros(len(y), dtype=bool)
        metrics = experiment.evaluate(y, kind, flag)
        selected = search['selected']
        selected_metrics = experiment.evaluate(y, kind, values >= selected['threshold']) if selected is not None else None
        if selected_metrics is not None and not assess(selected_metrics, selected_metrics['subtypes'])['measured_research_targets_passed']:
            raise ValueError('Eligible capacity threshold failed independent count verification')
        if selected is not None:
            eligible.append({'configuration': configuration, **selected})
        results[str(configuration)] = {'average_precision': float(average_precision_score(y, values)),
            'policy_search': search, 'selected_evaluation': selected_metrics, 'precision_constrained_diagnostic_policy': diagnostic,
            'precision_constrained_diagnostic': metrics, 'target_check': assess(metrics, metrics['subtypes']),
            'folds': {fold: experiment.evaluate(y[start:end], kind[start:end], flag[start:end]) for fold, (start, end) in offsets.items()},
            'cohorts': {name: experiment.evaluate(y[mask], kind[mask], flag[mask]) for name, mask in masks.items() if mask.any()}}
    selected = min(eligible, key=lambda item: (-item['tp'], item['fp'], item['configuration'])) if eligible else None
    report = {'stage': 'capacity_pooled_selection_completed', 'policy_protocol_sha256': experiment.digest(PLAN),
              'capacity_protocol_sha256': experiment.digest(capacity.PLAN), 'selection_code_sha256': experiment.digest(__file__),
              'models_verified': len(evidence), 'artifact_evidence': evidence, 'rows': len(y), 'positives': int(y.sum()),
              'configurations': results, 'selected_policy': selected, 'measured_research_targets_passed': selected is not None,
              'operational_release_ready': False, 'runtime_model_changed': False,
              'evaluation_limit': plan['threshold_evaluation_limit'], 'seconds': round(time.monotonic() - started, 3)}
    experiment.write_json(experiment.base.ROOT / 'reports' / 'bank_capacity_policy_selection.json', report)
    print(json.dumps({'stage': report['stage'], 'selected_policy': selected, 'seconds': report['seconds']}), flush=True)


if __name__ == '__main__':
    with threadpool_limits(limits=8):
        main()
