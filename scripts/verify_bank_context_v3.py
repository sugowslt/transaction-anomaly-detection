"""Independent count/cohort verification of completed common-policy reports."""
import json
import sqlite3
import numpy as np
import experiment_bank_context_v3 as v3
from bank_joint_policy import verify_counts


def main():
    coarse = v3.read(v3.ROOT / 'reports/bank_context_v3_selection.json')
    fine = v3.read(v3.ROOT / 'reports/bank_context_v3_fine_selection.json')
    y = np.concatenate([np.load(v3.old.graph.CACHE / f'y_{s}.npy') for s in ('fit', 'tune')])
    kinds = np.concatenate([np.load(v3.old.graph.CACHE / f'type_{s}.npy') for s in ('fit', 'tune')])
    histories = np.concatenate([np.load(v3.old.window.CACHE / f'x_{s}.npy', mmap_mode='r')[:,23] for s in ('fit','tune')])
    con = sqlite3.connect(f'file:{v3.old.base.CACHE / "stage.sqlite"}?mode=ro', uri=True)
    indices, parts = [], {c: [] for c in ('hgb-context','xgb-weight1','xgb-weight3','fine')}
    for fold in v3.read(v3.old.PROTOCOL)['selection_folds']:
        a = con.execute('SELECT COUNT(*) FROM events WHERE event_date<?',(fold['development_start'],)).fetchone()[0]
        b = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?',(fold['development_end'],)).fetchone()[0]
        indices.append(np.arange(a,b))
        for config in parts:
            path = v3.ROOT / 'reports' / (f"bank_context_v3_fine_{fold['id']}.json" if config=='fine' else f"bank_context_v3_{fold['id']}_{config}.json")
            r = v3.read(path)
            expected_stage = 'completed_fine_unit' if config == 'fine' else 'completed_context_unit'
            if r['stage'] != expected_stage or not r['serialized_predictions_verified']:
                raise ValueError('Incomplete serialized model unit')
            for item in ('artifact','score'):
                if v3.old.digest(v3.ROOT / r[item+'_path']) != r[item+'_sha256']:
                    raise ValueError('Verification artifact changed')
            score = np.load(v3.ROOT / r['score_path'], allow_pickle=False)
            if score.shape != (b-a,) or not np.isfinite(score).all() or not ((score >= 0) & (score <= 1)).all():
                raise ValueError('Invalid verification score population')
            parts[config].append(score)
    con.close(); idx=np.concatenate(indices); y=y[idx]; kinds=kinds[idx]; histories=histories[idx]
    results={}
    targets = v3.read(v3.PLAN)['targets']
    selected_configurations = []
    for config, values in parts.items():
        score=np.concatenate(values)
        r = fine if config=='fine' else coarse['results'][config]
        policy=r['policy'] if config=='fine' else r['precision_policy']
        flags=score>=policy['threshold'] if policy['feasible'] else np.zeros(len(y),dtype=bool)
        counts={'tp':int(((y==1)&flags).sum()),'fp':int(((y==0)&flags).sum()),
                'fn':int(((y==1)&~flags).sum()),'tn':int(((y==0)&~flags).sum())}
        if counts!=r['diagnostic']['confusion'] or r['diagnostic']['rows']!=len(y):
            raise ValueError('Reported common policy counts differ')
        verify_counts(y, kinds, flags, r['diagnostic'])
        search = v3.eligible_policy(y, kinds, score, targets)
        if search != r['search']:
            raise ValueError('Reported eligible policy search differs')
        if config == 'fine' and fine['selected_policy'] != search['selected']:
            raise ValueError('Fine selected policy differs')
        if search['selected'] is not None:
            selected_flags = score >= search['selected']['threshold']
            verify_counts(y, kinds, selected_flags, v3.old.evaluate(y, kinds, selected_flags), targets)
            if config != 'fine':
                selected_configurations.append({'configuration': config, **search['selected']})
        for kind, info in r['diagnostic']['subtypes'].items():
            mask=(y==1)&(kinds==kind.encode('ascii'))
            if int(mask.sum())!=info['positives'] or int((mask&flags).sum())!=info['detected']:
                raise ValueError('Reported type counts differ')
        cold=(histories==0)&(y==1)
        results[config]={'confusion_verified':counts,'cold_start_positives':int(cold.sum()),
                         'cold_start_detected':int((cold&flags).sum()),'cold_start_missed':int((cold&~flags).sum()),
                         'eligible_policy_search_verified': True, 'observed_type_set_verified': True}
    chosen = min(selected_configurations, key=lambda p: (-p['tp'], p['fp'], p['configuration'])) if selected_configurations else None
    if coarse['selected_policy'] != chosen:
        raise ValueError('Coarse selected policy differs')
    report={'stage':'stage27_counts_verified','rows':len(y),'positives':int(y.sum()),'models_verified':16,
            'configurations':results,'runtime_model_changed':False}
    v3.old.write_json(v3.ROOT/'data/work-state/stage27-verification.json',report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
