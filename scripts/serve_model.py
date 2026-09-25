"""Local model service for the Kotlin application."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from model_artifact import load_artifact, model_version
from release_assets import verify_report_versions
from train_bank_baseline import HOUR_CODES, features as bank_features
from train_card_baseline import SOURCE_FEATURE_FIELDS, features


verify_report_versions()
ARTIFACT = load_artifact()
BANK_ARTIFACT = load_artifact("bank")
BANK_FIELDS = {"거래금액", "거래시간대", "자금구분", "매체구분"}
MODEL_VERSIONS = {kind: model_version(kind) for kind in ("card", "bank")}


def score_transaction(transaction: dict) -> dict:
    if not isinstance(transaction, dict) or set(transaction) != SOURCE_FEATURE_FIELDS:
        raise ValueError("Card transaction must contain exactly the model input fields")
    vector = np.asarray([features(transaction, ARTIFACT["mappings"], fit=False)], dtype=np.float32)
    probability = float(ARTIFACT["model"].predict_proba(vector)[0, 1])
    return {
        "riskScore": probability,
        "alert": probability >= ARTIFACT["threshold"],
        "threshold": ARTIFACT["threshold"],
        "modelVersion": MODEL_VERSIONS["card"],
    }


def score_bank_transaction(transaction: dict) -> dict:
    if not isinstance(transaction, dict) or set(transaction) != BANK_FIELDS:
        raise ValueError("Bank transaction must contain exactly four fields: 거래금액, 거래시간대, 자금구분, 매체구분")
    if any(isinstance(transaction[name], bool) or not isinstance(transaction[name], (int, float)) for name in ("거래금액", "거래시간대")):
        raise ValueError("Amount and hour must be JSON numbers")
    if transaction["거래시간대"] not in HOUR_CODES:
        raise ValueError("Time bucket must be one of 0, 3, 6, 9, 12, 15, 18, 21")
    for name in ("자금구분", "매체구분"):
        if not isinstance(transaction[name], str) or transaction[name] not in BANK_ARTIFACT["mappings"][name]:
            raise ValueError(f"Unknown {name} code")
    vector = np.asarray([bank_features(transaction, BANK_ARTIFACT["mappings"], fit=False)], dtype=np.float32)
    probability = float(BANK_ARTIFACT["model"].predict_proba(vector)[0, 1])
    return {
        "riskScore": probability,
        "alert": probability >= BANK_ARTIFACT["threshold"],
        "threshold": BANK_ARTIFACT["threshold"],
        "modelVersion": MODEL_VERSIONS["bank"],
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "models": MODEL_VERSIONS})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path not in ("/score", "/score/bank"):
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65_536:
                raise ValueError("Request body size must be 1–65536 bytes")
            transaction = json.loads(self.rfile.read(length))
            scorer = score_bank_transaction if self.path == "/score/bank" else score_transaction
            self.send_json(200, scorer(transaction))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Model service listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
