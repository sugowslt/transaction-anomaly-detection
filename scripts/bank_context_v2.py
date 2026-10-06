"""Candidate graph and prior-bucket context, shared by training and scoring.

The official construction guide v1.4 (2026-07-30) defines bank buckets as
00-02, 03-05, ..., 21-23 hours. Earlier buckets are therefore past events;
order within the current bucket remains unknown and is excluded. Hour-based
windows use bucket starts, not unavailable exact transaction timestamps.
The v1 prior-day inputs remain unchanged.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict, deque

from bank_context_features import FEATURE_NAMES as V1_FEATURE_NAMES


SCHEMA_VERSION = "bank-context-v2-candidate"
ORDERING_EVIDENCE = {
    "source_url": "https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71925",
    "document": "25년 활용 가이드라인_이상 판별을 위한 금융거래 정보 및 사용자 패턴 합성데이터_v1.4(26.07.30)",
    "definition": "Bank 0/3/6/9/12/15/18/21 correspond to 00-02/03-05/06-08/09-11/12-14/15-17/18-20/21-23 hours.",
    "policy": "Only strictly earlier (date,bucket) enters graph context; exclude every event in the current bucket.",
    "window_limit": "Rolling windows use bucket start times, with up to three-hour timestamp uncertainty at the boundary.",
}
GRAPH_FIELDS = (
    "senderDayCount", "senderDayAmount", "senderDayDistinctRecipients", "senderPairDayCount",
    "sender24hCount", "sender24hAmount", "senderInflow7dCount", "senderInflow30dCount",
    "senderInflow90dCount", "senderInflow90dAmount", "senderInflow90dDistinctSources",
    "senderDayInflowCount", "senderDayInflowAmount", "recipientInflow7dCount",
    "recipientInflow30dCount", "recipientInflow90dCount", "recipientInflow90dAmount",
    "recipientInflow90dDistinctSources", "recipientDayInflowCount", "recipientDayInflowAmount",
    "recipientOutflow90dCount", "recipientOutflow90dAmount", "recipientDayOutflowCount",
    "recipientDayOutflowAmount",
)
EXTRA_FEATURE_NAMES = tuple(f"graph_{name}_log" for name in GRAPH_FIELDS) + (
    "graph_amount_to_sender_day_mean", "graph_amount_to_sender_inflow_90d_mean",
    "graph_amount_to_sender_day_inflow", "graph_recipient_net_90d_signed_log",
    "graph_sender_net_day_signed_log", "graph_sender_pair_day_share",
)
FEATURE_NAMES = V1_FEATURE_NAMES + EXTRA_FEATURE_NAMES


def bucket_key(event: dict) -> int:
    return event["date"].toordinal() * 24 + event["거래시간대"]


def account_key(event: dict, direction: str) -> tuple[str, str]:
    return event[f"{direction}금융회사일련번호"], event[f"{direction}계좌일련번호"]


class Window:
    """Exact sliding counts/sums/distinct partners without rescanning all events."""

    __slots__ = ("hours", "events", "amount", "partners")

    def __init__(self, hours: int):
        self.hours = hours
        self.events = deque()
        self.amount = 0.0
        self.partners = Counter()

    def add(self, item: tuple[int, float, str]) -> None:
        self.events.append(item)
        self.amount += item[1]
        self.partners[item[2]] += 1

    def expire(self, now: int) -> None:
        lower = now - self.hours
        while self.events and self.events[0][0] < lower:
            _, amount, partner = self.events.popleft()
            self.amount -= amount
            self.partners[partner] -= 1
            if self.partners[partner] == 0:
                del self.partners[partner]
        if not self.events:
            self.amount = 0.0

    def stats(self, now: int) -> tuple[int, float, int]:
        self.expire(now)
        return len(self.events), max(0.0, self.amount), len(self.partners)


class Node:
    __slots__ = ("out90", "out24", "in7", "in30", "in90", "day", "out_day", "in_day",
                 "out_day_amount", "in_day_amount")

    def __init__(self):
        self.out90 = Window(90 * 24)
        self.out24 = Window(24)
        self.in7 = Window(7 * 24)
        self.in30 = Window(30 * 24)
        self.in90 = Window(90 * 24)
        self.day = None
        self.out_day = Counter()
        self.in_day = Counter()
        self.out_day_amount = self.in_day_amount = 0.0

    def at_day(self, day) -> None:
        if self.day != day:
            self.day = day
            self.out_day.clear()
            self.in_day.clear()
            self.out_day_amount = self.in_day_amount = 0.0


class GraphState:
    """Consume chronological buckets after scoring all events in each bucket."""

    def __init__(self):
        self.nodes = defaultdict(Node)
        self.last_added = None

    def add(self, event: dict) -> None:
        key = bucket_key(event)
        if self.last_added is not None and key < self.last_added:
            raise ValueError("Graph history must be in nondecreasing bucket order")
        self.last_added = key
        sender = account_key(event, "출금")
        recipient = account_key(event, "입금")
        amount = event["거래금액"]
        outgoing = (key, amount, recipient)
        incoming = (key, amount, sender)
        sender_node = self.nodes[sender]
        recipient_node = self.nodes[recipient]
        sender_node.at_day(event["date"])
        recipient_node.at_day(event["date"])
        sender_node.out90.expire(key)
        sender_node.out24.expire(key)
        sender_node.out90.add(outgoing)
        sender_node.out24.add(outgoing)
        for window in (recipient_node.in7, recipient_node.in30, recipient_node.in90):
            window.expire(key)
            window.add(incoming)
        sender_node.out_day[recipient] += 1
        sender_node.out_day_amount += amount
        recipient_node.in_day[sender] += 1
        recipient_node.in_day_amount += amount

    def context(self, event: dict) -> dict:
        now = bucket_key(event)
        if self.last_added is not None and self.last_added >= now:
            raise ValueError("Current and future buckets cannot enter graph context")
        sender = self.nodes[account_key(event, "출금")]
        recipient_key = account_key(event, "입금")
        recipient = self.nodes[recipient_key]
        sender.at_day(event["date"])
        recipient.at_day(event["date"])
        out24 = sender.out24.stats(now)
        sin7, sin30, sin90 = [window.stats(now) for window in (sender.in7, sender.in30, sender.in90)]
        rin7, rin30, rin90 = [window.stats(now) for window in (recipient.in7, recipient.in30, recipient.in90)]
        rout90 = recipient.out90.stats(now)
        values = (
            sum(sender.out_day.values()), sender.out_day_amount, len(sender.out_day),
            sender.out_day[recipient_key], out24[0], out24[1],
            sin7[0], sin30[0], sin90[0], sin90[1], sin90[2],
            sum(sender.in_day.values()), sender.in_day_amount,
            rin7[0], rin30[0], rin90[0], rin90[1], rin90[2],
            sum(recipient.in_day.values()), recipient.in_day_amount,
            rout90[0], rout90[1], sum(recipient.out_day.values()), recipient.out_day_amount,
        )
        return {"asOfDate": event["거래일자"], "asOfHour": event["거래시간대"], **dict(zip(GRAPH_FIELDS, values))}


def validate_context(event: dict, context: dict) -> dict:
    if not isinstance(context, dict) or set(context) != {*GRAPH_FIELDS, "asOfDate", "asOfHour"}:
        raise ValueError("graphContext requires exactly the documented 26 fields")
    if (context["asOfDate"] != event["거래일자"] or isinstance(context["asOfHour"], bool) or
            not isinstance(context["asOfHour"], int) or context["asOfHour"] != event["거래시간대"]):
        raise ValueError("graphContext as-of bucket does not match the transaction")
    for name in GRAPH_FIELDS:
        value = context[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Invalid graphContext.{name}")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or ("Amount" not in name and (not isinstance(value, int) or value > 9_223_372_036_854_775_807)):
            raise ValueError(f"Invalid graphContext.{name}")
    for prefix in ("sender", "recipient"):
        if not context[f"{prefix}Inflow7dCount"] <= context[f"{prefix}Inflow30dCount"] <= context[f"{prefix}Inflow90dCount"]:
            raise ValueError("graphContext inflow windows are inconsistent")
        if context[f"{prefix}Inflow90dDistinctSources"] > context[f"{prefix}Inflow90dCount"]:
            raise ValueError("graphContext distinct sources exceed incoming events")
        if context[f"{prefix}DayInflowCount"] > context[f"{prefix}Inflow7dCount"]:
            raise ValueError("graphContext day inflow exceeds seven-day inflow")
        if context[f"{prefix}DayInflowAmount"] > context[f"{prefix}Inflow90dAmount"]:
            raise ValueError("graphContext day inflow amount exceeds ninety-day inflow")
    if not context["senderPairDayCount"] <= context["senderDayCount"] <= context["sender24hCount"]:
        raise ValueError("graphContext sender-day counts are inconsistent")
    if context["senderDayDistinctRecipients"] > context["senderDayCount"]:
        raise ValueError("graphContext sender-day distinct recipients exceed event count")
    if context["recipientDayOutflowCount"] > context["recipientOutflow90dCount"]:
        raise ValueError("graphContext recipient-day outflow exceeds ninety-day outflow")
    for count_name, amount_name in (
        ("senderDayCount", "senderDayAmount"), ("sender24hCount", "sender24hAmount"),
        ("senderInflow90dCount", "senderInflow90dAmount"), ("senderDayInflowCount", "senderDayInflowAmount"),
        ("recipientInflow90dCount", "recipientInflow90dAmount"), ("recipientDayInflowCount", "recipientDayInflowAmount"),
        ("recipientOutflow90dCount", "recipientOutflow90dAmount"), ("recipientDayOutflowCount", "recipientDayOutflowAmount"),
    ):
        if context[count_name] == 0 and context[amount_name] != 0:
            raise ValueError("graphContext empty count has a nonzero amount")
    return context


def extra_snapshot(event: dict, context: dict) -> dict:
    """The exact same transformation accepts offline and server-side aggregates."""
    validate_context(event, context)
    amount = event["거래금액"]

    def ratio(denominator):
        return min(amount / denominator, 1000.0) if denominator > 0 else 0.0

    def signed_log(value):
        return math.copysign(math.log1p(abs(value)), value)

    day_mean = context["senderDayAmount"] / context["senderDayCount"] if context["senderDayCount"] else 0.0
    incoming_mean = context["senderInflow90dAmount"] / context["senderInflow90dCount"] if context["senderInflow90dCount"] else 0.0
    values = tuple(math.log1p(context[name]) for name in GRAPH_FIELDS) + (
        ratio(day_mean), ratio(incoming_mean), ratio(context["senderDayInflowAmount"]),
        signed_log(context["recipientInflow90dAmount"] - context["recipientOutflow90dAmount"]),
        signed_log(context["senderDayInflowAmount"] - context["senderDayAmount"]),
        context["senderPairDayCount"] / context["senderDayCount"] if context["senderDayCount"] else 0.0,
    )
    return dict(zip(EXTRA_FEATURE_NAMES, values))


def feature_snapshot(v1_snapshot: dict, event: dict, graph_context: dict) -> dict:
    if tuple(v1_snapshot) != V1_FEATURE_NAMES:
        raise ValueError("v2 requires the unchanged, ordered v1 feature snapshot")
    return {**v1_snapshot, **extra_snapshot(event, graph_context)}
