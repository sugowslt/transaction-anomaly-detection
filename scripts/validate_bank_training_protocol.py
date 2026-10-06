"""Verify a declared temporal protocol against source counts without training."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import train_bank_contextual as base

PROTOCOL = base.ROOT / "reports" / "bank_training_protocol.json"
REPORT = base.ROOT / "reports" / "bank_training_protocol_audit.json"


def validate_boundaries(protocol):
    if protocol["selection_data_end"] != "20231231" or protocol["final_refit"]["sample_rate"] != 1.0:
        raise ValueError("Selection must use pre2024 data with full-past final fitting")
    ids = set()
    for fold in protocol["selection_folds"]:
        if fold["id"] in ids:
            raise ValueError("Duplicate temporal fold")
        ids.add(fold["id"])
        dates = [datetime.strptime(fold[key], "%Y%m%d") for key in ("fit_end", "development_start", "development_end")]
        if not dates[0] < dates[1] <= dates[2] or dates[2].year >= 2024:
            raise ValueError("Temporal fold overlaps training or uses2024 selection labels")
    if not ids:
        raise ValueError("No declared temporal folds")


def main():
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    validate_boundaries(protocol)
    con = sqlite3.connect(f"file:{(base.CACHE / 'stage.sqlite').as_posix()}?mode=ro", uri=True)
    source_rows = con.execute("SELECT COUNT(*) FROM events WHERE event_date<'20240101'").fetchone()[0]
    if source_rows != protocol["final_refit"]["all_pre2024_rows"]:
        raise ValueError("Declared full-past row count differs from source")
    folds = []
    for fold in protocol["selection_folds"]:
        fit_rows = con.execute("SELECT COUNT(*) FROM events WHERE event_date<=?", (fold["fit_end"],)).fetchone()[0]
        dev_rows = con.execute("SELECT COUNT(*) FROM events WHERE event_date BETWEEN ? AND ?", (fold["development_start"], fold["development_end"])).fetchone()[0]
        def positive_types(condition, params):
            return dict(con.execute("SELECT anomaly_type,COUNT(*) FROM events WHERE label=1 AND " + condition + " GROUP BY anomaly_type", params))
        fit_types = positive_types("event_date<=?", (fold["fit_end"],))
        dev_types = positive_types("event_date BETWEEN ? AND ?", (fold["development_start"], fold["development_end"]))
        if any(dev_types.get(kind, 0) != fold[field] for kind, field in (("4.0", "observed_type4_positives"), ("7.0", "observed_type7_positives"))):
            raise ValueError("Declared rare-subtype coverage differs from source")
        folds.append({"id": fold["id"], "fit_rows": fit_rows, "development_rows": dev_rows,
                      "fit_positive_types": fit_types, "development_positive_types": dev_types,
                      "unseen_positive_types": sorted(set(dev_types) - set(fit_types)),
                      "observed_across_past_but_absent_in_development": sorted(set(fit_types) - set(dev_types))})
    con.close()
    result = {"protocol_id": protocol["protocol_id"], "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
              "stage": "boundary_and_source_counts_verified_only", "models_fitted": 0,
              "all_pre2024_rows": source_rows, "folds": folds, "operational_release_ready": False,
              "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
