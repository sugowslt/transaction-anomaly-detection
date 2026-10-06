"""Install local Python dependencies and run both demo services."""

import argparse
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

from release_assets import verify_contextual_report, verify_graph_report, verify_report_versions, verify_window_report, verify_window_hybrid_report


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
GRADLE = ROOT / "server" / ("gradlew.bat" if os.name == "nt" else "gradlew")


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--model-port", type=int, default=8001)
    parser.add_argument("--experimental-bank-v2", action="store_true", help="Enable graph-context research candidate; this does not certify release readiness")
    args = parser.parse_args()
    for required in (
        ROOT / "models" / "card_baseline.skops",
        ROOT / "models" / "bank_baseline.skops",
        ROOT / "models" / "bank_contextual_v1.skops",
        ROOT / "reports" / "card_baseline.json",
        ROOT / "reports" / "bank_baseline.json",
        ROOT / "reports" / "bank_contextual_v1.json",
        ROOT / "reports" / "card_baseline_audit.json",
        ROOT / "reports" / "bank_baseline_audit.json",
        ROOT / "reports" / "threshold_tradeoff.json",
        ROOT / "reports" / "demo_transactions.json",
        ROOT / "reports" / "bank_demo_transactions.json",
    ):
        if not required.is_file():
            raise FileNotFoundError(f"Required demo file is missing: {required}")
    verify_report_versions()
    verify_contextual_report()
    graph_files = [ROOT / "models" / "bank_contextual_v2_candidate.skops", ROOT / "reports" / "bank_contextual_v2_candidate.json"]
    if any(path.exists() for path in graph_files) or args.experimental_bank_v2:
        verify_graph_report()
    window_files = [ROOT / "models" / "bank_closed_window.skops", ROOT / "reports" / "bank_closed_window.json"]
    if any(path.exists() for path in window_files) or args.experimental_bank_v2:
        verify_window_report()
    if any((ROOT / path).exists() for path in ("models/bank_concurrent_specialist.skops", "reports/bank_closed_window_hybrid.json")):
        verify_window_hybrid_report()
    if not PYTHON.is_file():
        venv.create(VENV, with_pip=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run([str(PYTHON), str(ROOT / "scripts" / "check_release.py")], check=True)
    model = None
    server = None
    try:
        model = subprocess.Popen(
            [str(PYTHON), str(ROOT / "scripts" / "serve_model.py"), "--port", str(args.model_port)],
            cwd=ROOT,
        )
        time.sleep(1)
        if model.poll() is not None:
            raise RuntimeError(f"Model service exited with code {model.returncode}")
        print(f"Open http://127.0.0.1:{args.web_port} when Spring Boot is ready.", flush=True)
        environment = os.environ.copy()
        environment["SERVER_PORT"] = str(args.web_port)
        environment["FRAUD_MODEL_URL"] = f"http://127.0.0.1:{args.model_port}"
        if args.experimental_bank_v2:
            environment["FRAUD_BANK_V2_CANDIDATE_ENABLED"] = "true"
        server = subprocess.Popen([str(GRADLE), "bootRun"], cwd=ROOT / "server", env=environment)
        return server.wait()
    finally:
        stop(server)
        stop(model)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
