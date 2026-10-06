"""Prepare signed all-past window-history inputs; never train/promote a model."""
import hashlib
import itertools
import json
import sqlite3
from collections import defaultdict
import numpy as np
import experiment_bank_context_v3 as v3
import bank_window_history_features as features

ROOT = v3.ROOT
PLAN = ROOT / 'reports' / 'bank_window_history_protocol.json'
CACHE = v3.old.base.DATA / '.bank-window-history-cache'


def prepare():
    plan = v3.read(PLAN); base = v3.prepare(); prior = v3.read(v3.PLAN)
    if (plan['parent_context_protocol_sha256'] != v3.old.digest(v3.PLAN)
            or plan['targets'] != prior['targets'] or plan['past_end'] != prior['past_end']
            or plan['past_end'] >= '20240101' or plan['features'] != list(features.FEATURE_NAMES)):
        raise ValueError('Frozen past-only window-history contract changed')
    stage = v3.old.base.CACHE / 'stage.sqlite'
    signature = {'plan_sha256': v3.old.digest(PLAN), 'builder_sha256': v3.old.digest(__file__),
                 'feature_code_sha256': v3.old.digest(features.__file__), 'base_context': base,
                 'source_sha256': v3.old.digest(stage),
                 'normalization_code_sha256': v3.old.digest(__import__('bank_context_features').__file__),
                 'scoping_code_sha256': v3.old.digest(__import__('bank_context_v2').__file__),
                 'strict_raw_code_sha256': v3.old.digest(__import__('bank_context_v3_features').__file__)}
    CACHE.mkdir(exist_ok=True); done = CACHE / 'complete.json'
    if done.exists():
        meta = v3.read(done)
        if meta['signature'] != signature or v3.old.digest(CACHE / 'extra.npy') != meta['extra_sha256']:
            raise ValueError('Existing window-history cache differs; do not overwrite evidence')
        return meta
    con = sqlite3.connect(f'file:{stage}?mode=ro', uri=True); con.row_factory = sqlite3.Row
    count = con.execute('SELECT COUNT(*) FROM events WHERE event_date<=?', (plan['past_end'],)).fetchone()[0]
    labels = np.concatenate([np.load(v3.old.graph.CACHE / f'y_{n}.npy') for n in ('fit', 'tune')])
    kinds = np.concatenate([np.load(v3.old.graph.CACHE / f'type_{n}.npy') for n in ('fit', 'tune')])
    if count != base['rows'] or len(labels) != count or len(kinds) != count:
        raise ValueError('Window-history source population differs')
    matrix = np.lib.format.open_memmap(CACHE / 'extra.tmp.npy', mode='w+', dtype=np.float32,
                                      shape=(count, len(features.FEATURE_NAMES)))
    state = features.WindowHistoryState(); digest = hashlib.sha256(); index = 0; windows = 0
    cursor = con.execute('SELECT rowid,* FROM events WHERE event_date<=? ORDER BY event_date,rowid', (plan['past_end'],))
    for day, daily in itertools.groupby(cursor, key=lambda row: row['event_date']):
        rows = [dict(row) for row in daily]; buckets, senders, extra = defaultdict(list), defaultdict(list), {}
        for row in rows:
            buckets[row['hour']].append(row); senders[(row['sender_bank'], row['sender'])].append(row)
        for hour in sorted(buckets):
            bucket = buckets[hour]; raw = [v3.old.base.row_event(row) for row in bucket]
            vectors = state.snapshot(raw)
            for row, vector in zip(bucket, vectors, strict=True):
                extra[row['rowid']] = vector
            # Update only AFTER every row of this complete bucket was observed.
            state.add(raw); windows += 1
        for sender_rows in senders.values():
            for row in sender_rows:
                if labels[index] != row['label'] or kinds[index] != row['anomaly_type'].encode('ascii'):
                    raise ValueError('Window-history source target ordering mismatch')
                matrix[index] = extra[row['rowid']]
                digest.update(np.asarray([row['rowid']], dtype='<i8').tobytes()); index += 1
        if day.endswith('01'):
            print(json.dumps({'stage': 'window_history_features', 'day': day, 'rows': index}), flush=True)
    con.close()
    if index != count or digest.hexdigest() != base['ordered_source_identity_sha256'] or not np.isfinite(matrix).all():
        raise ValueError('Incomplete, nonfinite or misaligned window-history inputs')
    matrix.flush(); del matrix
    (CACHE / 'extra.tmp.npy').replace(CACHE / 'extra.npy')
    meta = {'signature': signature, 'rows': count, 'shape': [count, len(features.FEATURE_NAMES)],
            'ordered_source_identity_sha256': digest.hexdigest(), 'extra_sha256': v3.old.digest(CACHE / 'extra.npy'),
            'complete_windows': windows, 'all_available_past_used': True, 'no_2024_rows_read': True,
            'runtime_model_changed': False, 'operational_release_ready': False}
    v3.old.write_json(done, meta)
    print(json.dumps({'stage': 'window_history_prepared', 'rows': count, 'shape': meta['shape']}), flush=True)
    return meta


if __name__ == '__main__':
    try:
        prepare()
    except KeyboardInterrupt:
        print(json.dumps({'stage': 'window_history_interrupted',
                          'note': 'No completion marker; incomplete temporary cache must be rebuilt.'}), flush=True)
        raise SystemExit(130)
