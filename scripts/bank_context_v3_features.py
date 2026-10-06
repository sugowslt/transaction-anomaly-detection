"""28 label-free candidate inputs, separate from every released feature schema.

Prior inputs use strictly earlier calendar dates. Call snapshot for ALL rows of
a date before adding ANY row of that date. Closed inputs include self and peers
only in one explicitly completed three-hour bucket. Account/bank identifiers
are grouping keys, never returned as model values. Raw rows must have exactly
SOURCE_FIELDS: labels, descriptions and extra metadata are rejected.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from bank_context_features import SOURCE_FIELDS, normalize_transaction
from bank_context_v2 import account_key

SCHEMA_VERSION = "bank-context-v3-research-candidate"
PRIOR_FEATURE_NAMES = (
    "v3_sender_cold_start", "v3_pair_cold_start",
    "v3_sender_log_amount_mean", "v3_sender_log_amount_std",
    "v3_amount_log_zscore", "v3_amount_log_hist_percentile",
    "v3_sender_hour_entropy", "v3_hour_surprisal", "v3_hour_circular_distance",
    "v3_sender_channel_entropy", "v3_sender_fund_entropy", "v3_sender_recipient_entropy",
    "v3_sender_recipient_concentration", "v3_channel_surprisal", "v3_fund_surprisal",
    "v3_recipient_surprisal",
)
WINDOW_FEATURE_NAMES = (
    "v3_window_sender_recipient_entropy", "v3_window_sender_recipient_concentration",
    "v3_window_sender_amount_concentration", "v3_window_sender_amount_percentile",
    "v3_window_sender_channel_entropy", "v3_window_sender_fund_entropy",
    "v3_window_sender_inflow_count_log", "v3_window_sender_inflow_amount_log",
    "v3_window_sender_net_flow_share", "v3_window_recipient_outflow_count_log",
    "v3_window_recipient_outflow_amount_log", "v3_window_recipient_net_flow_share",
)
FEATURE_NAMES = PRIOR_FEATURE_NAMES + WINDOW_FEATURE_NAMES
MAX_WINDOW_ROWS = 20_000


def _normalize(raw):
    if not isinstance(raw, dict) or set(raw) not in (SOURCE_FIELDS, SOURCE_FIELDS | {"date"}):
        raise ValueError("v3 accepts source fields and optional normalized date only")
    event = normalize_transaction({key: raw[key] for key in SOURCE_FIELDS})
    if "date" in raw and raw["date"] != event["date"]:
        raise ValueError("v3 normalized date differs from source date")
    return event


def _entropy(counts):
    total = sum(counts.values())
    return -math.fsum((n / total) * math.log(n / total) for n in sorted(counts.values()) if n) if total else 0.0


def _concentration(counts):
    total = sum(counts.values())
    return math.fsum((n / total) ** 2 for n in sorted(counts.values())) if total else 0.0


def _surprisal(counts, key, total=None):
    # Add-one probability with one reserved unseen category; no fitted labels.
    return -math.log((counts.get(key, 0) + 1) / ((sum(counts.values()) if total is None else total) + len(counts) + 1))


def _checked(names, values):
    if len(values) != len(names) or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid v3 finite feature vector")
    return dict(zip(names, values))


@dataclass
class _Sender:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    histogram: Counter = field(default_factory=Counter)
    hours: Counter = field(default_factory=Counter)
    channels: Counter = field(default_factory=Counter)
    funds: Counter = field(default_factory=Counter)
    recipients: Counter = field(default_factory=Counter)
    recipient_stats: tuple | None = None


class PastContextState:
    """All-date state; callers retain full days, without row sampling/truncation.

    Population log-amount variance uses Welford. The fixed histogram has width
    2 in log1p(amount), and percentile uses half the current bin's mass (ties).
    Z scores are clipped to [-20,20]; empty/constant history has z=0.
    Entropies are natural-log units; hour distance is circular hours / 12.
    """
    def __init__(self):
        self.senders = defaultdict(_Sender)
        self.last_added_date = None

    def snapshot(self, raw):
        event = _normalize(raw)
        if self.last_added_date is not None and event["date"] <= self.last_added_date:
            raise ValueError("v3 prior snapshots require a strictly later date than added history")
        node = self.senders.get(account_key(event, "출금"), _Sender())
        if node.recipient_stats is None:
            node.recipient_stats = (_entropy(node.recipients), _concentration(node.recipients))
        recipient = account_key(event, "입금")
        amount = math.log1p(event["거래금액"])
        std = math.sqrt(max(0.0, node.m2 / node.count)) if node.count else 0.0
        hist_bin = int(amount // 2)
        percentile = (sum(n for b, n in node.histogram.items() if b < hist_bin) +
                      0.5 * node.histogram[hist_bin]) / node.count if node.count else 0.5
        hour = event["거래시간대"]
        distance = math.fsum(n * min(abs(hour - h), 24 - abs(hour - h)) / 12
                             for h, n in sorted(node.hours.items())) / node.count if node.count else 0.0
        values = (
            float(node.count == 0), float(node.recipients[recipient] == 0), node.mean, std,
            max(-20.0, min(20.0, (amount - node.mean) / std)) if std > 1e-12 else 0.0,
            percentile, _entropy(node.hours), -math.log((node.hours[hour] + 1) / (node.count + 8)), distance,
            _entropy(node.channels), _entropy(node.funds), *node.recipient_stats,
            _surprisal(node.channels, event["매체구분"], node.count),
            _surprisal(node.funds, event["자금구분"], node.count), _surprisal(node.recipients, recipient, node.count),
        )
        return tuple(_checked(PRIOR_FEATURE_NAMES, values).values())

    def add(self, raw):
        event = _normalize(raw)
        if self.last_added_date is not None and event["date"] < self.last_added_date:
            raise ValueError("v3 history must be added in nondecreasing date order")
        self.last_added_date = event["date"]
        node = self.senders[account_key(event, "출금")]
        value = math.log1p(event["거래금액"])
        node.count += 1
        delta = value - node.mean
        node.mean += delta / node.count
        node.m2 += delta * (value - node.mean)
        node.histogram[int(value // 2)] += 1
        node.hours[event["거래시간대"]] += 1
        node.channels[event["매체구분"]] += 1
        node.funds[event["자금구분"]] += 1
        node.recipients[account_key(event, "입금")] += 1
        node.recipient_stats = None


def window_vectors(raw_events):
    """Return finite tuples in WINDOW_FEATURE_NAMES order aligned with rows.

    Amount percentile is exact midrank: (#less + .5 * #equal) / count.
    Net flow share is (in-out)/(in+out), or zero when both are zero.
    """
    if not isinstance(raw_events, list) or not 0 < len(raw_events) <= MAX_WINDOW_ROWS:
        raise ValueError("A v3 closed window requires 1-20000 complete raw events")
    events = [_normalize(row) for row in raw_events]
    if len({(e["date"], e["거래시간대"]) for e in events}) != 1:
        raise ValueError("A v3 closed window requires one date and three-hour bucket")
    outgoing, incoming = defaultdict(list), defaultdict(list)
    partners, channels, funds = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for e in events:
        sender, recipient = account_key(e, "출금"), account_key(e, "입금")
        outgoing[sender].append(e["거래금액"])
        incoming[recipient].append(e["거래금액"])
        partners[sender][recipient] += 1
        channels[sender][e["매체구분"]] += 1
        funds[sender][e["자금구분"]] += 1
    ordered = {key: sorted(values) for key, values in outgoing.items()}
    out_totals = {key: math.fsum(values) for key, values in ordered.items()}
    in_totals = {key: math.fsum(sorted(values)) for key, values in incoming.items()}
    sender_stats = {
        key: (_entropy(partners[key]), _concentration(partners[key]),
              math.fsum((v / out_totals[key]) ** 2 for v in values) if out_totals[key] else 0.0,
              _entropy(channels[key]), _entropy(funds[key]))
        for key, values in ordered.items()
    }
    def net(key):
        inflow, outflow = in_totals.get(key, 0.0), out_totals.get(key, 0.0)
        return (inflow - outflow) / (inflow + outflow) if inflow + outflow else 0.0
    result = []
    for e in events:
        sender, recipient = account_key(e, "출금"), account_key(e, "입금")
        values = ordered[sender]
        total = out_totals[sender]
        amount = e["거래금액"]
        recipient_entropy, recipient_concentration, amount_concentration, channel_entropy, fund_entropy = sender_stats[sender]
        features = (
            recipient_entropy, recipient_concentration, amount_concentration,
            (bisect_left(values, amount) + 0.5 * (bisect_right(values, amount) - bisect_left(values, amount))) / len(values),
            channel_entropy, fund_entropy,
            math.log1p(len(incoming[sender])), math.log1p(in_totals.get(sender, 0.0)), net(sender),
            math.log1p(len(outgoing[recipient])), math.log1p(out_totals.get(recipient, 0.0)), net(recipient),
        )
        result.append(tuple(_checked(WINDOW_FEATURE_NAMES, features).values()))
    return result
