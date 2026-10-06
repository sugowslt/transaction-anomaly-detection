"""As-of features for the AI Hub synthetic electronic-transfer task.

Only events strictly before the transaction date may enter the history.  The
source has three-hour buckets but no reliable order within a day, so all
same-day events are deliberately excluded.  The same functions are used by
offline training and the local scoring service.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta


SCHEMA_VERSION = "bank-context-v1"
HISTORY_DAYS = 90
MAX_HISTORY_ROWS = 20_000
MAX_AMOUNT = 100_000_000_000_000_000
HOUR_CODES = frozenset(range(0, 24, 3))
SOURCE_FIELDS = frozenset({
    "출금계좌일련번호", "입금계좌일련번호", "출금금융회사일련번호", "입금금융회사일련번호",
    "자금구분", "거래금액", "거래시간대", "매체구분", "거래일자",
})
CATEGORICAL_FIELDS = ("자금구분", "매체구분")
FEATURE_NAMES = (
    "amount_log", "time_bucket", "fund_code", "channel_code", "same_institution",
    "day_of_week", "sender_7d_count_log", "sender_30d_count_log", "sender_90d_count_log",
    "sender_30d_mean_amount_log", "sender_90d_mean_amount_log", "sender_90d_max_amount_log",
    "amount_to_30d_mean", "amount_to_90d_mean", "amount_to_90d_max",
    "sender_90d_distinct_recipients_log", "recipient_90d_count_log",
    "recipient_bank_90d_count_log", "same_channel_90d_share", "same_fund_90d_share",
    "same_time_90d_share", "days_since_sender", "days_since_recipient",
    "sender_all_count_log", "sender_all_mean_amount_log", "sender_all_max_amount_log",
    "amount_to_all_mean", "amount_to_all_max", "sender_all_distinct_recipients_log",
    "recipient_all_count_log", "recipient_bank_all_count_log",
    "same_channel_all_share", "same_fund_all_share",
    "days_since_first_sender", "days_since_last_sender_all", "days_since_last_recipient_all",
)
CATEGORICAL_INDICES = (2, 3)
PRIOR_SUMMARY_FIELDS = frozenset({
    "senderCount", "senderAmountSum", "senderAmountMax", "distinctRecipients",
    "recipientCount", "recipientBankCount", "sameChannelCount", "sameFundCount",
    "firstDate", "lastDate", "lastRecipientDate",
})


def empty_prior_summary() -> dict:
    return {
        "senderCount": 0, "senderAmountSum": 0.0, "senderAmountMax": 0.0,
        "distinctRecipients": 0, "recipientCount": 0, "recipientBankCount": 0,
        "sameChannelCount": 0, "sameFundCount": 0,
        "firstDate": None, "lastDate": None, "lastRecipientDate": None,
    }


class LifetimeState:
    """Incremental all-time statistics updated only after each date is scored."""

    __slots__ = ("count", "amount_sum", "amount_max", "recipients", "recipient_last",
                 "banks", "channels", "funds", "first_date", "last_date", "scoped_institutions")

    def __init__(self, *, scoped_institutions=False):
        self.scoped_institutions = scoped_institutions
        self.count = 0
        self.amount_sum = 0.0
        self.amount_max = 0.0
        self.recipients = Counter()
        self.recipient_last = {}
        self.banks = Counter()
        self.channels = Counter()
        self.funds = Counter()
        self.first_date = None
        self.last_date = None

    def add(self, event: dict) -> None:
        self.count += 1
        self.amount_sum += event["거래금액"]
        self.amount_max = max(self.amount_max, event["거래금액"])
        recipient = ((event["입금금융회사일련번호"], event["입금계좌일련번호"])
                     if self.scoped_institutions else event["입금계좌일련번호"])
        self.recipients[recipient] += 1
        self.recipient_last[recipient] = event["date"]
        self.banks[event["입금금융회사일련번호"]] += 1
        self.channels[event["매체구분"]] += 1
        self.funds[event["자금구분"]] += 1
        self.first_date = min(self.first_date, event["date"]) if self.first_date else event["date"]
        self.last_date = max(self.last_date, event["date"]) if self.last_date else event["date"]

    def for_transaction(self, transaction: dict) -> dict:
        if self.count == 0:
            return empty_prior_summary()
        recipient = ((transaction["입금금융회사일련번호"], transaction["입금계좌일련번호"])
                     if self.scoped_institutions else transaction["입금계좌일련번호"])
        last_recipient = self.recipient_last.get(recipient)
        return {
            "senderCount": self.count, "senderAmountSum": self.amount_sum,
            "senderAmountMax": self.amount_max, "distinctRecipients": len(self.recipients),
            "recipientCount": self.recipients[recipient],
            "recipientBankCount": self.banks[transaction["입금금융회사일련번호"]],
            "sameChannelCount": self.channels[transaction["매체구분"]],
            "sameFundCount": self.funds[transaction["자금구분"]],
            "firstDate": self.first_date.strftime("%Y%m%d"),
            "lastDate": self.last_date.strftime("%Y%m%d"),
            "lastRecipientDate": last_recipient.strftime("%Y%m%d") if last_recipient else None,
        }


def validate_prior_summary(transaction: dict, summary: dict, history: list[dict], *, scoped_institutions=False) -> dict:
    """Reject malformed or inconsistent server-side lifetime aggregates."""
    if not isinstance(summary, dict) or set(summary) != PRIOR_SUMMARY_FIELDS:
        raise ValueError("priorSummary must have the documented eleven fields")
    count_names = ("senderCount", "distinctRecipients", "recipientCount", "recipientBankCount",
                   "sameChannelCount", "sameFundCount")
    for name in count_names:
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9_223_372_036_854_775_807:
            raise ValueError(f"Invalid priorSummary.{name}")
    for name in ("senderAmountSum", "senderAmountMax"):
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Invalid priorSummary.{name}")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"Invalid priorSummary.{name}")
    count = summary["senderCount"]
    if any(summary[name] > count for name in count_names[1:]):
        raise ValueError("priorSummary count exceeds senderCount")
    if summary["senderAmountMax"] > summary["senderAmountSum"] and not math.isclose(
        summary["senderAmountMax"], summary["senderAmountSum"], rel_tol=1e-12, abs_tol=1e-6
    ):
        raise ValueError("priorSummary maximum exceeds total amount")
    if count > 0 and summary["distinctRecipients"] == 0:
        raise ValueError("Non-empty priorSummary requires at least one recipient")
    dates = {}
    for name in ("firstDate", "lastDate", "lastRecipientDate"):
        raw = summary[name]
        if raw is None:
            dates[name] = None
            continue
        if not isinstance(raw, str) or len(raw) != 8 or not raw.isascii() or not raw.isdigit():
            raise ValueError(f"Invalid priorSummary.{name}")
        try:
            dates[name] = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid priorSummary.{name}") from exc
        if dates[name] >= transaction["date"]:
            raise ValueError("priorSummary dates must precede the transaction")
    if count == 0:
        if any(summary[name] != 0 for name in count_names[1:] + ("senderAmountSum", "senderAmountMax")) or any(dates.values()):
            raise ValueError("Empty priorSummary must contain only zeros and null dates")
    elif dates["firstDate"] is None or dates["lastDate"] is None or dates["firstDate"] > dates["lastDate"]:
        raise ValueError("Non-empty priorSummary requires valid first and last dates")
    if (summary["recipientCount"] == 0) != (dates["lastRecipientDate"] is None):
        raise ValueError("lastRecipientDate must match recipientCount")
    if dates["lastRecipientDate"] and not dates["firstDate"] <= dates["lastRecipientDate"] <= dates["lastDate"]:
        raise ValueError("lastRecipientDate lies outside observed sender history")
    if count < len(history):
        raise ValueError("priorSummary.senderCount is smaller than detailed history")
    if history:
        raw_sum = sum(event["거래금액"] for event in history)
        if summary["senderAmountSum"] + 1e-6 < raw_sum or summary["senderAmountMax"] + 1e-6 < max(event["거래금액"] for event in history):
            raise ValueError("priorSummary amount is inconsistent with detailed history")
        if dates["firstDate"] > min(event["date"] for event in history) or dates["lastDate"] < max(event["date"] for event in history):
            raise ValueError("priorSummary date range excludes detailed history")
        recent_counts = {
            "distinctRecipients": len({(event["입금금융회사일련번호"], event["입금계좌일련번호"])
                                       if scoped_institutions else event["입금계좌일련번호"] for event in history}),
            "recipientCount": sum(event["입금계좌일련번호"] == transaction["입금계좌일련번호"] and
                                  (not scoped_institutions or event["입금금융회사일련번호"] == transaction["입금금융회사일련번호"])
                                  for event in history),
            "recipientBankCount": sum(event["입금금융회사일련번호"] == transaction["입금금융회사일련번호"] for event in history),
            "sameChannelCount": sum(event["매체구분"] == transaction["매체구분"] for event in history),
            "sameFundCount": sum(event["자금구분"] == transaction["자금구분"] for event in history),
        }
        if any(summary[name] < observed for name, observed in recent_counts.items()):
            raise ValueError("priorSummary category counts exclude detailed history")
        if count == len(history):
            if any(summary[name] != observed for name, observed in recent_counts.items()) or not math.isclose(
                summary["senderAmountSum"], raw_sum, rel_tol=1e-12, abs_tol=1e-6
            ):
                raise ValueError("priorSummary differs from its complete detailed history")
    return summary


def normalize_transaction(row: dict, *, exact_fields: bool = True) -> dict:
    """Validate a source/runtime event without ever retaining labels."""
    if not isinstance(row, dict) or (set(row) != SOURCE_FIELDS if exact_fields else not SOURCE_FIELDS.issubset(row)):
        raise ValueError("Contextual bank transaction must contain exactly nine source fields")
    normalized = {}
    for name in ("출금계좌일련번호", "입금계좌일련번호", "출금금융회사일련번호", "입금금융회사일련번호"):
        value = row[name]
        if not isinstance(value, str) or not 0 < len(value) <= 64 or value.strip() != value:
            raise ValueError(f"Invalid {name}")
        normalized[name] = value
    raw_date = row["거래일자"]
    if not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isascii() or not raw_date.isdigit():
        raise ValueError("거래일자 must be YYYYMMDD")
    try:
        parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("거래일자 must be a real date") from exc
    normalized["거래일자"] = raw_date
    normalized["date"] = parsed_date
    raw_amount = row["거래금액"]
    if isinstance(raw_amount, bool):
        raise ValueError("거래금액 must be a non-negative number")
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("거래금액 must be a non-negative number") from exc
    if not math.isfinite(amount) or not 0 <= amount < MAX_AMOUNT:
        raise ValueError("거래금액 must be below the supported maximum")
    normalized["거래금액"] = amount
    hour = row["거래시간대"]
    if isinstance(hour, bool) or not isinstance(hour, (int, float, str)):
        raise ValueError("Invalid 거래시간대")
    try:
        hour_num = int(hour)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid 거래시간대") from exc
    if str(hour_num) != str(hour) and not (isinstance(hour, float) and hour.is_integer()):
        raise ValueError("Invalid 거래시간대")
    if hour_num not in HOUR_CODES:
        raise ValueError("Invalid 거래시간대")
    normalized["거래시간대"] = hour_num
    for name in CATEGORICAL_FIELDS:
        value = row[name]
        if not isinstance(value, str) or not value or len(value) > 16:
            raise ValueError(f"Invalid {name}")
        normalized[name] = value
    return normalized


def history_base(transaction: dict, history, *, scoped_institutions=False) -> dict:
    """Build reusable same-sender history statistics for one calendar day."""
    day = transaction["date"]
    lower = day - timedelta(days=HISTORY_DAYS)
    sender = transaction["출금계좌일련번호"]
    base = {
        "count_7": 0, "count_30": 0, "count_90": 0,
        "sum_30": 0.0, "sum_90": 0.0, "max_90": 0.0,
        "recipients": Counter(), "recipient_last_age": {}, "banks": Counter(),
        "channels": Counter(), "funds": Counter(), "hours": Counter(),
        "last_sender_age": 91,
    }
    if scoped_institutions:
        base["scoped_institutions"] = True
    for previous in history:
        prior_date = previous["date"]
        if (previous["출금계좌일련번호"] != sender or not lower <= prior_date < day or
                scoped_institutions and previous["출금금융회사일련번호"] != transaction["출금금융회사일련번호"]):
            raise ValueError("History must contain only same-sender events from the prior 90 days")
        age = (day - prior_date).days
        amount = previous["거래금액"]
        base["count_90"] += 1
        base["sum_90"] += amount
        base["max_90"] = max(base["max_90"], amount)
        recipient = ((previous["입금금융회사일련번호"], previous["입금계좌일련번호"])
                     if scoped_institutions else previous["입금계좌일련번호"])
        base["recipients"][recipient] += 1
        base["recipient_last_age"][recipient] = min(base["recipient_last_age"].get(recipient, 91), age)
        base["banks"][previous["입금금융회사일련번호"]] += 1
        base["channels"][previous["매체구분"]] += 1
        base["funds"][previous["자금구분"]] += 1
        base["hours"][previous["거래시간대"]] += 1
        base["last_sender_age"] = min(base["last_sender_age"], age)
        if age <= 30:
            base["count_30"] += 1
            base["sum_30"] += amount
        if age <= 7:
            base["count_7"] += 1
    return base


def summary_from_base(transaction: dict, base: dict) -> dict:
    recipient = ((transaction["입금금융회사일련번호"], transaction["입금계좌일련번호"])
                 if base.get("scoped_institutions") else transaction["입금계좌일련번호"])
    return {
        **{name: base[name] for name in (
            "count_7", "count_30", "count_90", "sum_30", "sum_90", "max_90", "last_sender_age"
        )},
        "distinct_recipients_90": len(base["recipients"]),
        "pair_count_90": base["recipients"][recipient],
        "last_recipient_age": base["recipient_last_age"].get(recipient, 91),
        "bank_count_90": base["banks"][transaction["입금금융회사일련번호"]],
        "channel_count_90": base["channels"][transaction["매체구분"]],
        "fund_count_90": base["funds"][transaction["자금구분"]],
        "hour_count_90": base["hours"][transaction["거래시간대"]],
    }


def summarize_history(transaction: dict, history, *, scoped_institutions=False) -> dict:
    """Summarize same-sender prior 90 days; input events are normalized."""
    return summary_from_base(transaction, history_base(transaction, history, scoped_institutions=scoped_institutions))


def feature_snapshot(transaction: dict, summary: dict, mappings: dict, *, fit: bool,
                     prior_summary: dict | None = None) -> dict[str, float]:
    """Return the exact model inputs in stable order (also suitable for audit)."""
    amount = transaction["거래금액"]
    count_30 = summary["count_30"]
    count_90 = summary["count_90"]
    mean_30 = summary["sum_30"] / count_30 if count_30 else 0.0
    mean_90 = summary["sum_90"] / count_90 if count_90 else 0.0
    max_90 = summary["max_90"]
    prior = prior_summary if prior_summary is not None else empty_prior_summary()
    all_count = prior["senderCount"]
    all_mean = prior["senderAmountSum"] / all_count if all_count else 0.0
    all_max = prior["senderAmountMax"]

    def ratio(denominator: float) -> float:
        return min(amount / denominator, 1000.0) if denominator > 0 else 0.0

    def age(field: str) -> float:
        raw = prior[field]
        return float((transaction["date"] - datetime.strptime(raw, "%Y%m%d").date()).days) if raw else 3651.0

    codes = {}
    for name in CATEGORICAL_FIELDS:
        value = transaction[name]
        mapping = mappings[name]
        if fit and value not in mapping:
            mapping[value] = len(mapping)
        codes[name] = float(mapping.get(value, -1))
    values = (
        math.log1p(amount), float(transaction["거래시간대"]), codes["자금구분"], codes["매체구분"],
        float(transaction["출금금융회사일련번호"] == transaction["입금금융회사일련번호"]),
        float(transaction["date"].weekday()),
        math.log1p(summary["count_7"]), math.log1p(count_30), math.log1p(count_90),
        math.log1p(mean_30), math.log1p(mean_90), math.log1p(max_90),
        ratio(mean_30), ratio(mean_90), ratio(max_90),
        math.log1p(summary["distinct_recipients_90"]), math.log1p(summary["pair_count_90"]),
        math.log1p(summary["bank_count_90"]),
        summary["channel_count_90"] / count_90 if count_90 else 0.0,
        summary["fund_count_90"] / count_90 if count_90 else 0.0,
        summary["hour_count_90"] / count_90 if count_90 else 0.0,
        float(summary["last_sender_age"]), float(summary["last_recipient_age"]),
        math.log1p(all_count), math.log1p(all_mean), math.log1p(all_max),
        ratio(all_mean), ratio(all_max), math.log1p(prior["distinctRecipients"]),
        math.log1p(prior["recipientCount"]), math.log1p(prior["recipientBankCount"]),
        prior["sameChannelCount"] / all_count if all_count else 0.0,
        prior["sameFundCount"] / all_count if all_count else 0.0,
        age("firstDate"), age("lastDate"), age("lastRecipientDate"),
    )
    if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(value) for value in values):
        raise ValueError("Invalid contextual features")
    return dict(zip(FEATURE_NAMES, values))


def vector(snapshot: dict[str, float]) -> list[float]:
    return [snapshot[name] for name in FEATURE_NAMES]


def observed_context(summary: dict, prior_summary: dict) -> dict[str, float | int]:
    """Human-readable observations, not causal attributions or account IDs."""
    count_30 = summary["count_30"]
    count_90 = summary["count_90"]
    lifetime = prior_summary["senderCount"]
    return {
        "sender7dCount": summary["count_7"],
        "sender30dCount": count_30,
        "sender90dCount": count_90,
        "sender30dMeanAmount": summary["sum_30"] / count_30 if count_30 else 0.0,
        "sender90dMeanAmount": summary["sum_90"] / count_90 if count_90 else 0.0,
        "sender90dMaxAmount": summary["max_90"],
        "distinctRecipients90d": summary["distinct_recipients_90"],
        "recipient90dCount": summary["pair_count_90"],
        "recipientBank90dCount": summary["bank_count_90"],
        "senderLifetimeCount": lifetime,
        "senderLifetimeMeanAmount": prior_summary["senderAmountSum"] / lifetime if lifetime else 0.0,
        "senderLifetimeMaxAmount": prior_summary["senderAmountMax"],
        "distinctRecipientsLifetime": prior_summary["distinctRecipients"],
        "recipientLifetimeCount": prior_summary["recipientCount"],
        "daysSinceLastSender": summary["last_sender_age"],
        "daysSinceLastRecipient": summary["last_recipient_age"],
    }


def window_start(transaction: dict) -> str:
    return (transaction["date"] - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
