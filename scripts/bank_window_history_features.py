"""Research inputs: closed-window activity relative to ALL earlier windows.

No current or later bucket enters the prior distribution. Account identifiers
are grouping keys only. Every earlier observed window is retained in exact
moment and rank statistics, regardless of its label (labels are rejected here).
Inactive windows are not inferred. These are closed-bucket, not pre-event inputs.
"""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from bank_context_v3_features import _normalize
from bank_context_v2 import account_key, bucket_key

FEATURE_NAMES = tuple(
    f'v4_{scope}_{name}'
    for scope in ('all_windows', 'same_hour_windows')
    for name in ('cold', 'history_count_log', *(
        f'{metric}_{stat}' for metric in ('count_log', 'amount_log', 'recipients_log')
        for stat in ('mean', 'std', 'zscore', 'percentile'))))


@dataclass
class _Distribution:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    ranks: Counter = field(default_factory=Counter)

    def add(self, value):
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)
        self.ranks[value] += 1

    def compare(self, value):
        if not self.n:
            return (0.0, 0.0, 0.0, 0.5)
        std = math.sqrt(max(0.0, self.m2 / self.n))
        # Fixed log-unit scale floor avoids division by zero without clipping.
        z = (value - self.mean) / math.sqrt(std * std + 0.25)
        less = sum(count for x, count in self.ranks.items() if x < value)
        percentile = (less + 0.5 * self.ranks[value] + 0.5) / (self.n + 1)
        return (self.mean, std, z, percentile)


def _state():
    return tuple(_Distribution() for _ in range(3))


class WindowHistoryState:
    def __init__(self):
        self.all_windows, self.same_hour_windows = {}, {}
        self.last_added_bucket = None

    def _window(self, raw):
        if not isinstance(raw, list) or not 0 < len(raw) <= 20000:
            raise ValueError('Complete closed window requires 1-20000 rows; no truncation')
        events = [_normalize(row) for row in raw]
        keys = {bucket_key(row) for row in events}
        if len(keys) != 1:
            raise ValueError('One closed three-hour bucket required')
        key = next(iter(keys))
        if self.last_added_bucket is not None and key <= self.last_added_bucket:
            raise ValueError('Prior windows must be strictly earlier than current bucket')
        groups = defaultdict(list)
        for row in events:
            groups[account_key(row, '출금')].append(row)
        metrics = {}
        for sender, rows in groups.items():
            metrics[sender] = (math.log1p(len(rows)),
                               math.log1p(math.fsum(sorted(row['거래금액'] for row in rows))),
                               math.log1p(len({account_key(row, '입금') for row in rows})))
        return events, key, metrics

    def snapshot(self, raw):
        events, _, metrics = self._window(raw)
        output = {}
        for sender, values in metrics.items():
            hour = events[0]['거래시간대']
            vector = []
            for distribution in (self.all_windows.get(sender, _state()),
                                 self.same_hour_windows.get((sender, hour), _state())):
                n = distribution[0].n
                vector.extend((float(n == 0), math.log1p(n)))
                for item, value in zip(distribution, values, strict=True):
                    vector.extend(item.compare(value))
            if len(vector) != len(FEATURE_NAMES) or not all(math.isfinite(v) for v in vector):
                raise ValueError('Invalid window-history feature vector')
            output[sender] = tuple(vector)
        return [output[account_key(row, '출금')] for row in events]

    def add(self, raw):
        events, key, metrics = self._window(raw)
        hour = events[0]['거래시간대']
        for sender, values in metrics.items():
            for distribution in (self.all_windows.setdefault(sender, _state()),
                                 self.same_hour_windows.setdefault((sender, hour), _state())):
                for item, value in zip(distribution, values, strict=True):
                    item.add(value)
        self.last_added_bucket = key
