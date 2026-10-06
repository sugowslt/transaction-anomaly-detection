"""Local model service for the Kotlin application."""

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import skops.io as sio

from bank_context_features import (
    FEATURE_NAMES as BANK_CONTEXT_FEATURE_NAMES, HISTORY_DAYS, MAX_HISTORY_ROWS,
    SCHEMA_VERSION as BANK_CONTEXT_SCHEMA_VERSION, feature_snapshot as contextual_feature_snapshot,
    normalize_transaction as normalize_contextual_transaction,
    observed_context, summarize_history, validate_prior_summary, vector as contextual_vector,
    window_start,
)
from card_input_policy import MAX_CARD_AMOUNT, validate_card_amount
from bank_context_v2 import (
    FEATURE_NAMES as BANK_GRAPH_FEATURE_NAMES, SCHEMA_VERSION as BANK_GRAPH_SCHEMA_VERSION,
    feature_snapshot as graph_feature_snapshot,
)
from model_artifact import TRUSTED_TYPES, load_artifact, model_version
from model_score_explanation import explain_score
from bank_window_features import FEATURE_NAMES as WINDOW_FEATURE_NAMES, EXTRA_FEATURE_NAMES as WINDOW_EXTRA_FIELDS, window_vectors
from release_assets import ROOT, verify_report_versions, verify_window_report, verify_window_hybrid_report
from train_bank_baseline import HOUR_CODES, features as bank_features
from train_card_baseline import SOURCE_FEATURE_FIELDS, features


verify_report_versions()
ARTIFACT = load_artifact()
BANK_ARTIFACT = load_artifact("bank")
BANK_FIELDS = {"거래금액", "거래시간대", "자금구분", "매체구분"}
MODEL_VERSIONS = {kind: model_version(kind) for kind in ("card", "bank")}
CONTEXT_MODEL_PATH = ROOT / "models" / "bank_contextual_v1.skops"
CONTEXT_REPORT_PATH = ROOT / "reports" / "bank_contextual_v1.json"
CONTEXT_BODY_LIMIT = 4 * 1024 * 1024


def load_context_artifact():
    if not CONTEXT_MODEL_PATH.is_file() or not CONTEXT_REPORT_PATH.is_file():
        return None, None
    report = json.loads(CONTEXT_REPORT_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(CONTEXT_MODEL_PATH.read_bytes()).hexdigest()
    version = f"bank-context-v1-{digest[:12]}"
    if report.get("artifact_sha256") != digest or report.get("model_version") != version:
        raise ValueError("Contextual bank model/report digest mismatch")
    untrusted = set(sio.get_untrusted_types(file=CONTEXT_MODEL_PATH))
    unexpected = untrusted - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected contextual model types: {sorted(unexpected)}")
    artifact = sio.load(CONTEXT_MODEL_PATH, trusted=TRUSTED_TYPES)
    if (tuple(artifact.get("feature_names", ())) != BANK_CONTEXT_FEATURE_NAMES or
            artifact.get("feature_schema_version") != BANK_CONTEXT_SCHEMA_VERSION or
            artifact.get("history_days") != HISTORY_DAYS or
            artifact.get("threshold") != report.get("threshold_policy", {}).get("threshold")):
        raise ValueError("Contextual bank model feature contract does not match report")
    return artifact, version


CONTEXT_ARTIFACT, CONTEXT_MODEL_VERSION = load_context_artifact()
if CONTEXT_MODEL_VERSION:
    MODEL_VERSIONS["bank_contextual"] = CONTEXT_MODEL_VERSION


def load_graph_artifact():
    path = ROOT / "models" / "bank_contextual_v2_candidate.skops"
    report_path = ROOT / "reports" / "bank_contextual_v2_candidate.json"
    if not path.exists() or not report_path.exists():
        return None, None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    version = f"bank-context-v2-candidate-{digest[:12]}"
    if report.get("artifact_sha256") != digest or report.get("model_version") != version:
        raise ValueError("Graph bank model/report digest mismatch")
    unexpected = set(sio.get_untrusted_types(file=path)) - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected graph model types: {sorted(unexpected)}")
    artifact = sio.load(path, trusted=TRUSTED_TYPES)
    if (tuple(artifact.get("feature_names", ())) != BANK_GRAPH_FEATURE_NAMES or
            artifact.get("feature_schema_version") != BANK_GRAPH_SCHEMA_VERSION or
            artifact.get("threshold") != report.get("threshold_policy", {}).get("threshold") or
            artifact.get("explanation_reference") != report.get("explanation_reference") or
            tuple(artifact.get("explanation_reference", {}).get("feature_names", ())) != BANK_GRAPH_FEATURE_NAMES):
        raise ValueError("Graph bank feature contract does not match report")
    support = report.get("input_support", {})
    if not (isinstance(support.get("fit_amount_min"), (int, float)) and
            isinstance(support.get("fit_amount_max"), (int, float)) and
            0 <= support["fit_amount_min"] <= support["fit_amount_max"]):
        raise ValueError("Graph bank input support metadata is invalid")
    artifact["input_support"] = support
    return artifact, version


GRAPH_ARTIFACT, GRAPH_MODEL_VERSION = load_graph_artifact()
if GRAPH_MODEL_VERSION:
    MODEL_VERSIONS["bank_contextual_v2"] = GRAPH_MODEL_VERSION


def load_window_artifact():
    path, report_path = ROOT / "models" / "bank_closed_window.skops", ROOT / "reports" / "bank_closed_window.json"
    if not path.exists() and not report_path.exists():
        return None, None
    report = verify_window_report()
    version = report["model_version"]
    unexpected = set(sio.get_untrusted_types(file=path)) - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected closed-window model types: {sorted(unexpected)}")
    artifact = sio.load(path, trusted=TRUSTED_TYPES)
    if (tuple(artifact.get("feature_names", ())) != WINDOW_FEATURE_NAMES or
            artifact.get("feature_schema_version") != "bank-closed-window-v1" or
            artifact.get("threshold") != report.get("threshold_policy", {}).get("threshold")):
        raise ValueError("Closed-window feature/threshold contract mismatch")
    return artifact, version


WINDOW_ARTIFACT, WINDOW_MODEL_VERSION = load_window_artifact()
if WINDOW_MODEL_VERSION:
    MODEL_VERSIONS["bank_closed_window"] = WINDOW_MODEL_VERSION


def load_window_hybrid_artifact():
    path = ROOT / "models" / "bank_concurrent_specialist.skops"
    report_path = ROOT / "reports" / "bank_closed_window_hybrid.json"
    if not path.exists() and not report_path.exists():
        return None, None
    report = verify_window_hybrid_report()
    unexpected = set(sio.get_untrusted_types(file=path)) - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected specialist model types: {sorted(unexpected)}")
    artifact = sio.load(path, trusted=TRUSTED_TYPES)
    if (list(artifact.get("feature_names", ())) != report["specialist_feature_names"] or
            artifact.get("threshold") != report["specialist_threshold"] or
            artifact.get("target") != "synthetic anomaly type4, closed-window only"):
        raise ValueError("Window specialist feature or target contract mismatch")
    return artifact, report


WINDOW_SPECIALIST, WINDOW_HYBRID_REPORT = load_window_hybrid_artifact()
if WINDOW_HYBRID_REPORT:
    MODEL_VERSIONS["bank_closed_window_hybrid"] = WINDOW_HYBRID_REPORT["model_version"]


def score_bank_window(request):
    if WINDOW_ARTIFACT is None:
        raise RuntimeError("Closed-window research model is unavailable")
    if not isinstance(request, dict) or set(request) != {"events", "priorSnapshots"}:
        raise ValueError("Closed-window request requires events and priorSnapshots")
    extra = window_vectors(request["events"])
    snapshots = request["priorSnapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != len(extra):
        raise ValueError("Every observed event requires its immutable prior snapshot")
    rows = []
    for prior, current in zip(snapshots, extra):
        if not isinstance(prior, dict) or set(prior) != set(BANK_GRAPH_FEATURE_NAMES) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) for value in prior.values()
        ):
            raise ValueError("Invalid prior feature snapshot")
        rows.append([prior[name] for name in BANK_GRAPH_FEATURE_NAMES] + list(current))
    matrix = np.asarray(rows, dtype=np.float32)
    scores = WINDOW_ARTIFACT["model"].predict_proba(matrix)[:, 1]
    threshold = float(WINDOW_ARTIFACT["threshold"])
    result = {"modelVersion": WINDOW_MODEL_VERSION, "featureSchemaVersion": "bank-closed-window-v1", "threshold": threshold,
            "observationMode": "closed_three_hour_bucket_including_self_and_peers",
            "items": [{"index": index, "riskScore": float(score), "alert": bool(score >= threshold),
                       "windowSnapshot": dict(zip(WINDOW_EXTRA_FIELDS, values))} for index, (score, values) in enumerate(zip(scores, extra))]}
    if WINDOW_SPECIALIST is not None:
        report = WINDOW_HYBRID_REPORT
        indices = [WINDOW_FEATURE_NAMES.index(name) for name in report["specialist_feature_names"]]
        concurrent_scores = WINDOW_SPECIALIST["model"].predict_proba(matrix[:, indices])[:, 1]
        result.update(modelVersion=report["model_version"], generalModelVersion=WINDOW_MODEL_VERSION,
                      specialistModelVersion=report["specialist_model_version"], specialistThreshold=report["specialist_threshold"],
                      decisionPolicy=report["decision_policy"])
        for item, concurrent_score in zip(result["items"], concurrent_scores):
            item.update(generalAlert=item["alert"], concurrentScore=float(concurrent_score),
                        concurrentAlert=bool(concurrent_score >= report["specialist_threshold"]))
            item["alert"] = item["generalAlert"] or item["concurrentAlert"]
    return result


def score_transaction(transaction: dict) -> dict:
    if not isinstance(transaction, dict) or set(transaction) != SOURCE_FEATURE_FIELDS:
        raise ValueError("Card transaction must contain exactly the model input fields")
    validate_card_amount(transaction["통합승인금액"])
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


def contextual_snapshot(request: dict, *, scoped_institutions=False) -> tuple[dict, dict, dict, int, str]:
    if not isinstance(request, dict) or set(request) != {"transaction", "history", "priorSummary"}:
        raise ValueError("Contextual request requires transaction, history, and priorSummary")
    history_raw = request["history"]
    if not isinstance(history_raw, list) or len(history_raw) > MAX_HISTORY_ROWS:
        raise ValueError(f"history must contain at most {MAX_HISTORY_ROWS} events")
    for row in (request["transaction"], *history_raw):
        if not isinstance(row, dict):
            raise ValueError("Contextual events must be objects")
        if any(isinstance(row.get(name), bool) or not isinstance(row.get(name), (int, float))
               for name in ("거래금액", "거래시간대")):
            raise ValueError("거래금액 and 거래시간대 must be JSON numbers")
    transaction = normalize_contextual_transaction(request["transaction"])
    history = [normalize_contextual_transaction(row) for row in history_raw]
    prior = validate_prior_summary(transaction, request["priorSummary"], history, scoped_institutions=scoped_institutions)
    summary = summarize_history(transaction, history, scoped_institutions=scoped_institutions)
    snapshot = contextual_feature_snapshot(
        transaction, summary, CONTEXT_ARTIFACT["mappings"], fit=False, prior_summary=prior,
    )
    if snapshot["fund_code"] < 0 or snapshot["channel_code"] < 0:
        raise ValueError("Unknown fund or channel code")
    return snapshot, summary, prior, len(history), window_start(transaction)


def score_bank_contextual(request: dict) -> dict:
    if CONTEXT_ARTIFACT is None:
        raise RuntimeError("Contextual bank model is not packaged")
    snapshot, summary, prior, history_count, history_start = contextual_snapshot(request, scoped_institutions=True)
    score = float(CONTEXT_ARTIFACT["model"].predict_proba(
        np.asarray([contextual_vector(snapshot)], dtype=np.float32)
    )[0, 1])
    threshold = float(CONTEXT_ARTIFACT["threshold"])
    return {
        "riskScore": score, "alert": score >= threshold, "threshold": threshold,
        "modelVersion": CONTEXT_MODEL_VERSION,
        "featureSchemaVersion": BANK_CONTEXT_SCHEMA_VERSION,
        "featureSnapshot": snapshot,
        "observedContext": observed_context(summary, prior),
        "historyCount": history_count,
        "historyWindowStart": history_start,
        "contextStatus": "cold_start" if prior["senderCount"] == 0 else "history_provided",
        "evidenceType": "observed transaction history; not a causal explanation",
    }


def score_bank_graph(request: dict) -> dict:
    if GRAPH_ARTIFACT is None:
        raise RuntimeError("Graph-context bank candidate is not packaged")
    if not isinstance(request, dict) or set(request) != {"transaction", "history", "priorSummary", "graphContext"}:
        raise ValueError("Graph request requires transaction, history, priorSummary, and graphContext")
    base_request = {name: request[name] for name in ("transaction", "history", "priorSummary")}
    snapshot, summary, prior, count, start = contextual_snapshot(base_request, scoped_institutions=True)
    transaction = normalize_contextual_transaction(request["transaction"])
    extended = graph_feature_snapshot(snapshot, transaction, request["graphContext"])
    score = float(GRAPH_ARTIFACT["model"].predict_proba(
        np.asarray([[extended[name] for name in BANK_GRAPH_FEATURE_NAMES]], dtype=np.float32)
    )[0, 1])
    threshold = float(GRAPH_ARTIFACT["threshold"])
    explanation = explain_score(GRAPH_ARTIFACT["model"], extended,
                                GRAPH_ARTIFACT["explanation_reference"], expected_score=score)
    context_available = prior["senderCount"] > 0 or any(
        value > 0 for name, value in request["graphContext"].items() if name.endswith("Count"))
    support = GRAPH_ARTIFACT["input_support"]
    within_amount = support["fit_amount_min"] <= transaction["거래금액"] <= support["fit_amount_max"]
    within_period = transaction["date"].year == 2024
    limitations = ["synthetic_labels_only", "local_history_completeness_unverified", "research_candidate"]
    if not within_amount:
        limitations.append("amount_outside_fit_range")
    if not context_available:
        limitations.append("cold_context")
    if not within_period:
        limitations.append("date_outside_retrospective_evaluation")
    status = ("outside_fit_support" if not within_amount else "insufficient_context" if not context_available
              else "outside_temporal_validation" if not within_period else "within_observed_support")
    support_assessment = {
        "status": status, "reasons": limitations,
        "fitAmountMin": support["fit_amount_min"], "fitAmountMax": support["fit_amount_max"],
        "validationYear": 2024,
    }
    return {
        "riskScore": score, "alert": score >= threshold, "threshold": threshold,
        "modelVersion": GRAPH_MODEL_VERSION, "featureSchemaVersion": BANK_GRAPH_SCHEMA_VERSION,
        "featureSnapshot": extended, "observedContext": observed_context(summary, prior),
        "historyCount": count, "historyWindowStart": start,
        "contextStatus": "history_provided" if context_available else "cold_start",
        "graphContext": request["graphContext"],
        "modelExplanation": explanation,
        "supportAssessment": support_assessment,
        "evidenceType": "observed transaction history; not a causal explanation",
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
            self.send_json(200, {"status": "ok", "models": MODEL_VERSIONS,
                                 "inputLimits": {"card": {"maxAmount": MAX_CARD_AMOUNT},
                                                 "bank_contextual": {"historyDays": HISTORY_DAYS,
                                                                     "maxHistoryRows": MAX_HISTORY_ROWS,
                                                                     "maxBodyBytes": CONTEXT_BODY_LIMIT}}})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path not in ("/score", "/score/bank", "/score/bank/contextual", "/score/bank/contextual-v2", "/score/bank/window"):
            self.send_json(404, {"error": "Not found"})
            return
        if self.path == "/score/bank/contextual" and CONTEXT_ARTIFACT is None:
            self.send_json(503, {"error": "Contextual bank model is unavailable"})
            return
        if self.path == "/score/bank/contextual-v2" and GRAPH_ARTIFACT is None:
            self.send_json(503, {"error": "Graph-context bank candidate is unavailable"})
            return
        if self.path == "/score/bank/window" and WINDOW_ARTIFACT is None:
            self.send_json(503, {"error": "Closed-window research model is unavailable"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 16 * 1024 * 1024 if self.path == "/score/bank/window" else CONTEXT_BODY_LIMIT if self.path in ("/score/bank/contextual", "/score/bank/contextual-v2") else 65_536
            if not 0 < length <= limit:
                raise ValueError(f"Request body size must be 1–{limit} bytes")
            transaction = json.loads(self.rfile.read(length))
            scorer = (score_bank_window if self.path == "/score/bank/window" else score_bank_graph if self.path == "/score/bank/contextual-v2" else
                      score_bank_contextual if self.path == "/score/bank/contextual" else
                      score_bank_transaction if self.path == "/score/bank" else score_transaction)
            self.send_json(200, scorer(transaction))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})


def main() -> None:
    if CONTEXT_ARTIFACT is None:
        raise RuntimeError("Contextual bank model and matching report are required to start the model service")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Model service listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
