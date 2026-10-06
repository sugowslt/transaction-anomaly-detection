"""Common OR policy: fixed novelty cuts, all distinct supervised cuts and ties."""
import numpy as np


def flags(supervised, novelty, policy):
    result = np.zeros(len(supervised), dtype=bool)
    if policy['supervised_threshold'] is not None:
        result |= supervised >= policy['supervised_threshold']
    if policy['novelty_threshold'] is not None:
        result |= novelty >= policy['novelty_threshold']
    return result


def search(labels, kinds, supervised, novelty, novelty_cuts, targets):
    labels, kinds, supervised, novelty = map(np.asarray, (labels, kinds, supervised, novelty))
    if (labels.ndim != 1 or not len(labels) or any(x.shape != labels.shape for x in (kinds, supervised, novelty))
            or not np.isin(labels, [0, 1]).all() or not np.isfinite(supervised).all()
            or not np.isfinite(novelty).all() or np.any(novelty < 0)
            or not ((supervised >= 0) & (supervised <= 1)).all() or not labels.sum()):
        raise ValueError('Invalid joint policy predictions')
    if not novelty_cuts or any(c is not None and (not np.isfinite(c) or c < 0) for c in novelty_cuts):
        raise ValueError('Invalid novelty thresholds')
    positive = labels == 1
    observed = np.unique(kinds[positive])
    order = np.argsort(-supervised, kind='stable')
    values = supervised[order]
    ends = np.r_[np.flatnonzero(values[:-1] != values[1:]), len(labels) - 1]
    cuts = [None, *map(float, values[ends])]
    eligible, diagnostic, checks = None, None, []
    total_positive = int(positive.sum())
    for novelty_cut in novelty_cuts:
        extra = novelty >= novelty_cut if novelty_cut is not None else np.zeros(len(labels), dtype=bool)
        added = (~extra)[order]
        tp = int((extra & positive).sum()) + np.r_[0, np.cumsum(added & positive[order])[ends]]
        fp = int((extra & ~positive).sum()) + np.r_[0, np.cumsum(added & ~positive[order])[ends]]
        alerts = tp + fp
        precision = np.divide(tp, alerts, out=np.zeros(len(tp), dtype=float), where=alerts > 0)
        basic = (precision >= targets['precision']) & (alerts / len(labels) <= targets['maximum_alert_rate'])
        valid = basic & (tp / total_positive >= targets['recall'])
        for kind in observed:
            mask = positive & (kinds == kind)
            caught = int((mask & extra).sum()) + np.r_[0, np.cumsum(added & mask[order])[ends]]
            valid &= caught / mask.sum() >= targets['each_observed_type_recall']
        counts = {'novelty_threshold': novelty_cut, 'checked_supervised_thresholds': len(cuts),
                  'eligible_policies': int(valid.sum()), 'precision_budget_policies': int(basic.sum())}
        checks.append(counts)
        for mask, name in ((valid, 'eligible'), (basic, 'diagnostic')):
            indices = np.flatnonzero(mask)
            if not len(indices):
                continue
            best = indices[np.lexsort((indices, fp[indices], -tp[indices]))[0]]
            candidate = {'supervised_threshold': cuts[best], 'novelty_threshold': novelty_cut,
                         'tp': int(tp[best]), 'fp': int(fp[best])}
            previous = eligible if name == 'eligible' else diagnostic
            if previous is None or (-candidate['tp'], candidate['fp']) < (-previous['tp'], previous['fp']):
                if name == 'eligible':
                    eligible = candidate
                else:
                    diagnostic = candidate
    return {'checks': checks, 'eligible_policies': sum(c['eligible_policies'] for c in checks),
            'selected': eligible, 'diagnostic': diagnostic}


def verify_counts(labels, kinds, alert, reported, targets=None):
    """Count-based check independent of sorted-prefix policy selection."""
    positive = labels == 1
    expected = {'tp': int((positive & alert).sum()), 'fp': int((~positive & alert).sum()),
                'fn': int((positive & ~alert).sum()), 'tn': int((~positive & ~alert).sum())}
    if reported['confusion'] != expected or reported['rows'] != len(labels):
        raise ValueError('Reported confusion differs')
    observed = {kind.decode('ascii') for kind in np.unique(kinds[positive])}
    if set(reported['subtypes']) != observed:
        raise ValueError('Reported anomaly type set differs')
    for kind in observed:
        mask = positive & (kinds == kind.encode('ascii'))
        info = reported['subtypes'][kind]
        if info['positives'] != int(mask.sum()) or info['detected'] != int((mask & alert).sum()):
            raise ValueError('Reported anomaly type count differs')
    if targets is not None:
        n = expected['tp'] + expected['fp']
        if (not n or expected['tp'] / n < targets['precision']
                or expected['tp'] / positive.sum() < targets['recall']
                or n / len(labels) > targets['maximum_alert_rate']
                or any(info['detected'] / info['positives'] < targets['each_observed_type_recall']
                       for info in reported['subtypes'].values())):
            raise ValueError('Selected joint policy fails frozen targets')
