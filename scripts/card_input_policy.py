"""Card amount boundary recorded from the same fit split as the packaged model."""

import json
from decimal import Decimal, InvalidOperation

from release_assets import ROOT, model_version


def load_card_amount_limit() -> int:
    report = json.loads((ROOT / "reports" / "card_baseline.json").read_text(encoding="utf-8"))
    if report.get("model_version") != model_version("card"):
        raise ValueError("Card input limit report does not match the packaged model")
    limit = report.get("input_limits", {}).get("max_amount")
    if type(limit) is not int or limit <= 0:
        raise ValueError("Card input limit is missing or invalid")
    return limit


MAX_CARD_AMOUNT = load_card_amount_limit()


def validate_card_amount(value: object) -> int:
    message = f"승인금액은 학습 표본 범위인 0~{MAX_CARD_AMOUNT:,}원 정수만 입력할 수 있습니다."
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(message) from None
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= MAX_CARD_AMOUNT:
        raise ValueError(message)
    return int(amount)
