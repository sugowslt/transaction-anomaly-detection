"""Local model service for the Kotlin application."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from model_artifact import load_artifact
from train_card_baseline import features


ARTIFACT = load_artifact()


def score_transaction(transaction: dict) -> dict:
    if not isinstance(transaction, dict):
        raise ValueError("Transaction must be a JSON object")
    vector = np.asarray([features(transaction, ARTIFACT["mappings"], fit=False)], dtype=np.float32)
    probability = float(ARTIFACT["model"].predict_proba(vector)[0, 1])
    return {
        "riskScore": probability,
        "alert": probability >= ARTIFACT["threshold"],
        "threshold": ARTIFACT["threshold"],
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
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/score":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65_536:
                raise ValueError("Request body size must be 1–65536 bytes")
            transaction = json.loads(self.rfile.read(length))
            self.send_json(200, score_transaction(transaction))
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
