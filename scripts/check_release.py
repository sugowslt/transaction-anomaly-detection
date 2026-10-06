"""Check that the public demo rows still reflect the packaged models."""

import json
import math
from decimal import Decimal
from pathlib import Path

from bank_context_features import empty_prior_summary
from release_assets import ROOT, verify_contextual_report, verify_graph_report, verify_report_versions, verify_window_report, verify_window_hybrid_report
from serve_model import score_bank_contextual, score_bank_transaction, score_transaction


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
    contextual_report = verify_contextual_report()
    graph_files = [ROOT / "models" / "bank_contextual_v2_candidate.skops", ROOT / "reports" / "bank_contextual_v2_candidate.json"]
    if any(path.exists() for path in graph_files):
        verify_graph_report()
    window_files = [ROOT / "models" / "bank_closed_window.skops", ROOT / "reports" / "bank_closed_window.json"]
    if any(path.exists() for path in window_files):
        verify_window_report()
    if any((ROOT / path).exists() for path in ("models/bank_concurrent_specialist.skops", "reports/bank_closed_window_hybrid.json")):
        verify_window_hybrid_report()
    counts = {kind: verify_demo(ROOT / "reports" / name, field, scorer)
              for kind, name, field, scorer in DEMOS}
    contextual = score_bank_contextual({
        "transaction": {
            "출금계좌일련번호": "DEMO-SENDER", "입금계좌일련번호": "DEMO-RECEIVER",
            "출금금융회사일련번호": "DEMO-BANK-A", "입금금융회사일련번호": "DEMO-BANK-B",
            "거래일자": "20241215", "거래금액": 100000, "거래시간대": 9,
            "자금구분": "0", "매체구분": "2",
        },
        "history": [], "priorSummary": empty_prior_summary(),
    })
    if (contextual["modelVersion"] != contextual_report["model_version"] or
            contextual["contextStatus"] != "cold_start" or
            contextual["historyCount"] != 0 or
            not math.isfinite(contextual["riskScore"]) or
            contextual["alert"] != (contextual["riskScore"] >= contextual["threshold"])):
        raise ValueError("Contextual bank scorer does not match packaged release")
    print(json.dumps({"checked_demo_transactions": counts, "checked_contextual_contract": True}))


if __name__ == "__main__":
    main()
