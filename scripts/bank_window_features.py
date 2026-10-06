"""Order-independent observations of a closed three-hour transfer bucket.

Includes the scored row itself and same-bucket peers. These inputs are valid
only after the observation window, never as an immediate pre-event score.
Labels, descriptions and event order do not enter any feature.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

from bank_context_features import normalize_transaction
from bank_context_v2 import FEATURE_NAMES as PRIOR_FEATURE_NAMES, account_key

SCHEMA_VERSION = "bank-closed-window-v1"
EXTRA_FEATURE_NAMES = (
    "window_sender_count_log", "window_sender_amount_log", "window_sender_mean_log",
    "window_sender_max_log", "window_sender_min_log", "window_sender_std_log",
    "window_sender_amount_cv", "window_amount_sender_total_share",
    "window_sender_distinct_recipients_log", "window_sender_distinct_amounts_log",
    "window_sender_same_amount_share", "window_pair_count_log", "window_pair_amount_log",
    "window_sender_pair_share", "window_recipient_count_log", "window_recipient_amount_log",
    "window_recipient_mean_log", "window_recipient_std_log",
    "window_recipient_distinct_senders_log", "window_reciprocal_count_log",
)
FEATURE_NAMES = PRIOR_FEATURE_NAMES + EXTRA_FEATURE_NAMES
MAX_WINDOW_ROWS = 20_000


def window_vectors(raw_events):
    if not isinstance(raw_events, list) or not 0 < len(raw_events) <= MAX_WINDOW_ROWS:
        raise ValueError("A window requires 1-20000 complete raw events")
    events = [normalize_transaction(row) for row in raw_events]
    if len({(row["date"], row["거래시간대"]) for row in events}) != 1:
        raise ValueError("A closed window must contain a single date and three-hour bucket")
    senders, recipients, pairs = defaultdict(list), defaultdict(list), defaultdict(list)
    sender_parties, recipient_parties, amount_counts = defaultdict(set), defaultdict(set), defaultdict(Counter)
    for row in events:
        source, recipient = account_key(row, "출금"), account_key(row, "입금")
        amount = row["거래금액"]
        senders[source].append(amount)
        recipients[recipient].append(amount)
        pairs[(source, recipient)].append(amount)
        sender_parties[source].add(recipient)
        recipient_parties[recipient].add(source)
        amount_counts[source][amount] += 1

    def stats(values):
        # Sorted reduction makes every output independent of arrival order.
        ordered = sorted(values)
        total = math.fsum(ordered)
        mean = total / len(values)
        variance = math.fsum((value - mean) ** 2 for value in ordered) / len(values)
        return len(values), total, mean, min(ordered), max(ordered), math.sqrt(variance)

    source_stats = {key: stats(values) for key, values in senders.items()}
    recipient_stats = {key: stats(values) for key, values in recipients.items()}
    pair_stats = {key: (len(values), math.fsum(sorted(values))) for key, values in pairs.items()}
    result = []
    for row in events:
        source, recipient = account_key(row, "출금"), account_key(row, "입금")
        count, total, mean, minimum, maximum, std = source_stats[source]
        r_count, r_total, r_mean, _, _, r_std = recipient_stats[recipient]
        p_count, p_total = pair_stats[(source, recipient)]
        reverse_count = pair_stats.get((recipient, source), (0, 0))[0]
        values = (
            math.log1p(count), math.log1p(total), math.log1p(mean), math.log1p(maximum),
            math.log1p(minimum), math.log1p(std), min(std / mean, 100.0) if mean else 0.0,
            row["거래금액"] / total if total else 0.0, math.log1p(len(sender_parties[source])),
            math.log1p(len(amount_counts[source])), amount_counts[source][row["거래금액"]] / count,
            math.log1p(p_count), math.log1p(p_total), p_count / count,
            math.log1p(r_count), math.log1p(r_total), math.log1p(r_mean), math.log1p(r_std),
            math.log1p(len(recipient_parties[recipient])), math.log1p(reverse_count),
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Nonfinite window observations")
        result.append(values)
    return result
