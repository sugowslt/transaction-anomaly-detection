"""Check that the public demo rows still reflect the packaged models."""

import json
import math
from decimal import Decimal
from pathlib import Path

from release_assets import ROOT, verify_report_versions
from serve_model import score_bank_transaction, score_transaction


DEMOS = (
    ("card", "demo_transactions.json", "통합승인금액", score_transaction),
    ("bank", "bank_demo_transactions.json", "거래금액", score_bank_transaction),
)


def verify_demo(path: Path, amount_field: str, scorer) -> int:
    examples = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(examples, list) or not examples:
        raise ValueError(f"Demo file has no transactions: {path}")
    identifiers = set()
    for example in examples:
        identifier = example["id"]
        if identifier in identifiers:
            raise ValueError(f"Duplicate demo transaction ID in {path}: {identifier}")
        identifiers.add(identifier)
        transaction = example["transaction"]
        result = scorer(transaction)
        if (
            Decimal(str(example["amount"])) != Decimal(str(transaction[amount_field]))
            or not math.isclose(example["riskScore"], result["riskScore"], rel_tol=1e-9, abs_tol=1e-12)
            or example["alert"] != result["alert"]
        ):
            raise ValueError(f"Demo transaction no longer matches its model: {path} ({identifier})")
    return len(examples)


def main() -> None:
    verify_report_versions()
    counts = {kind: verify_demo(ROOT / "reports" / name, field, scorer)
              for kind, name, field, scorer in DEMOS}
    print(json.dumps({"checked_demo_transactions": counts}))


if __name__ == "__main__":
    main()
