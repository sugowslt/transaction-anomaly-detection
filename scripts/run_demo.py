"""Install local Python dependencies and run both demo services."""

import argparse
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

from release_assets import verify_report_versions


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
    args = parser.parse_args()
    for required in (
        ROOT / "models" / "card_baseline.skops",
        ROOT / "models" / "bank_baseline.skops",
        ROOT / "reports" / "card_baseline.json",
        ROOT / "reports" / "bank_baseline.json",
        ROOT / "reports" / "demo_transactions.json",
        ROOT / "reports" / "bank_demo_transactions.json",
    ):
        if not required.is_file():
            raise FileNotFoundError(f"Required demo file is missing: {required}")
    verify_report_versions()
    if not PYTHON.is_file():
        venv.create(VENV, with_pip=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
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
