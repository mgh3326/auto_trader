#!/usr/bin/env python3
"""Run the retro consumer contract gate.

Spins up the candidate retro provider (the PR's FastAPI route and middleware
with synthetic contract examples or test DB) and runs the Go harness
(tools/retro-consumer-check) using the pinned factortrail consumer client.

Supports testing mutants:
  --mutant delete_field: deletes a required field in retro_day
  --mutant float_integer: serializes a price decimal as a float number
  --mutant bypass_token: bypasses token authentication
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import uvicorn
import yaml
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PIN_FILE = REPO_ROOT / "contracts/retro/v1/consumers.lock"
SPEC_FILE = REPO_ROOT / "contracts/retro/v1/openapi.yaml"
EXAMPLES_DIR = REPO_ROOT / "contracts/retro/v1/examples"


DUMMY_ENV = {
    "KIS_APP_KEY": "DUMMY_KIS_APP_KEY",
    "KIS_APP_SECRET": "DUMMY_KIS_APP_SECRET",
    "OPENDART_API_KEY": "DUMMY_OPENDART_API_KEY",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:5432/test_db",
    "UPBIT_ACCESS_KEY": "DUMMY_UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY": "DUMMY_UPBIT_SECRET_KEY",
    "SECRET_KEY": "Synthetic_Secret_Key_For_Retro_Gate_12345678",
    "ENVIRONMENT": "test",
    "RETRO_API_ENABLED": "true",
}


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_pin(pin_file: Path) -> dict[str, Any]:
    data = json.loads(pin_file.read_text(encoding="utf-8"))
    consumers = data.get("consumers", [])
    if not consumers:
        raise RuntimeError(f"no consumers defined in {pin_file}")
    return consumers[0]


def ensure_consumer_repo(
    consumer_info: dict[str, Any], explicit_dir: str | None
) -> Path:
    if explicit_dir:
        path = Path(explicit_dir).resolve()
        if path.exists():
            return path
        raise RuntimeError(f"explicit consumer dir {explicit_dir} does not exist")

    # Check common local path first
    local_default = Path.home() / "work" / consumer_info["name"]
    if local_default.exists() and (local_default / ".git").exists():
        return local_default

    # Otherwise clone to .cache/consumers/<name>
    cache_dir = REPO_ROOT / ".cache" / "consumers" / consumer_info["name"]
    target_commit = consumer_info["commit"]
    if not cache_dir.exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {consumer_info['repo_url']} to {cache_dir}...")
        subprocess.run(
            ["git", "clone", consumer_info["repo_url"], str(cache_dir)],
            check=True,
        )
    # Check out pinned commit
    subprocess.run(
        ["git", "checkout", target_commit],
        cwd=str(cache_dir),
        check=True,
        capture_output=True,
    )
    return cache_dir


class CandidateServerThread(threading.Thread):
    def __init__(
        self,
        port: int,
        read_token: str,
        mutant: str | None = None,
    ):
        super().__init__(daemon=True)
        self.port = port
        self.read_token = read_token
        self.mutant = mutant
        self.server: uvicorn.Server | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            for k, v in DUMMY_ENV.items():
                os.environ.setdefault(k, v)
            os.environ["SECRET_KEY"] = DUMMY_ENV["SECRET_KEY"]
            os.environ["RETRO_API_ENABLED"] = "true"
            os.environ["RETRO_API_READ_TOKEN"] = self.read_token

            # Set environment variables for candidate router/middleware
            from app.core.config import settings

            settings.RETRO_API_ENABLED = True
            settings.RETRO_API_READ_TOKEN = self.read_token

            from app.middleware.auth import AuthMiddleware
            from app.routers import retro_v1

            # Load contract examples
            examples = {}
            for p in EXAMPLES_DIR.glob("*.yaml"):
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                if doc.get("status") == 200:
                    examples[doc["operation"]] = doc["response"]

            app = FastAPI()

            # Install mock readers for candidate endpoints
            for op in retro_v1.OPERATIONS:
                resp_data = copy.deepcopy(
                    examples.get(
                        op,
                        {
                            "data": [],
                            "as_of": "2026-11-06T00:00:00Z",
                            "cutoff_date": "2026-11-06",
                            "next_cursor": None,
                            "timezone": "KST",
                            "total": 0,
                            "unknowns": [],
                        },
                    )
                )

                if self.mutant == "delete_field" and op == "retro_day":
                    # Mutant: delete required field 'event_ref' from data rows
                    if "data" in resp_data and isinstance(resp_data["data"], list):
                        for row in resp_data["data"]:
                            row.pop("event_ref", None)

                if self.mutant == "float_integer" and op == "retro_day":
                    # Mutant: serialize decimal price as a float number
                    if "data" in resp_data and isinstance(resp_data["data"], list):
                        for row in resp_data["data"]:
                            row["price"] = 100.5

                mock = AsyncMock(return_value=resp_data)
                retro_v1.READERS[op] = mock

            app.include_router(retro_v1.router)

            if self.mutant != "bypass_token":
                app.add_middleware(AuthMiddleware)

            config = uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                access_log=False,
            )
            self.server = uvicorn.Server(config)
            self.server.run()
        except Exception as exc:
            self.error = exc

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True


def wait_for_server(port: int, token: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    import urllib.request

    url = f"http://127.0.0.1:{port}/trading/api/retro/v1/data_gaps"
    req = urllib.request.Request(url, headers={"X-Retro-Read-Token": token})
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Retro consumer contract gate runner")
    parser.add_argument(
        "--pin-file", default=str(DEFAULT_PIN_FILE), help="Path to consumers.lock"
    )
    parser.add_argument(
        "--consumer-dir", default=None, help="Path to consumer repo checkout"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="Port for candidate server (0 for random)"
    )
    parser.add_argument(
        "--mutant",
        choices=["delete_field", "float_integer", "bypass_token"],
        default=None,
        help="Inject mutant for gate testing",
    )
    parser.add_argument(
        "--read-token", default="candidate-retro-token-secret-2026", help="Secret token"
    )
    args = parser.parse_args()

    pin_file = Path(args.pin_file).resolve()
    if not pin_file.exists():
        print(f"error: pin file {pin_file} not found", file=sys.stderr)
        return 2

    consumer_info = load_pin(pin_file)
    print(f"[gate] Consumer: {consumer_info['name']} @ {consumer_info['commit'][:10]}")

    consumer_dir = ensure_consumer_repo(consumer_info, args.consumer_dir)
    print(f"[gate] Consumer repo at: {consumer_dir}")

    # Copy / stage tools/retro-consumer-check/main.go into consumer tree
    harness_src = REPO_ROOT / "tools" / "retro-consumer-check" / "main.go"
    if not harness_src.exists():
        print(f"error: harness {harness_src} not found", file=sys.stderr)
        return 2

    staged_cmd_dir = consumer_dir / "cmd" / "retro-consumer-check"
    staged_cmd_dir.mkdir(parents=True, exist_ok=True)
    staged_main = staged_cmd_dir / "main.go"
    shutil.copyfile(harness_src, staged_main)

    port = args.port or find_free_port()
    print(
        f"[gate] Starting candidate provider on 127.0.0.1:{port} (mutant={args.mutant})..."
    )

    server_thread = CandidateServerThread(
        port=port, read_token=args.read_token, mutant=args.mutant
    )
    server_thread.start()

    try:
        if not wait_for_server(port, args.read_token, timeout=10.0):
            print(
                "error: candidate server did not start within timeout", file=sys.stderr
            )
            if server_thread.error:
                print(f"server error: {server_thread.error}", file=sys.stderr)
            return 1
        print(f"[gate] Candidate provider responsive on http://127.0.0.1:{port}")

        # Run Go harness inside consumer directory
        consumer_lock_path = consumer_dir / consumer_info.get(
            "contract_lock_path", "contracts.lock.json"
        )
        cmd = [
            "go",
            "run",
            "./cmd/retro-consumer-check",
            "--base-url",
            f"http://127.0.0.1:{port}",
            "--token",
            args.read_token,
            "--candidate-spec",
            str(SPEC_FILE),
            "--candidate-examples",
            str(EXAMPLES_DIR),
            "--consumer-lock",
            str(consumer_lock_path),
        ]
        print(f"[gate] Executing: {' '.join(cmd)}")
        res = subprocess.run(cmd, cwd=str(consumer_dir))
        return res.returncode
    finally:
        server_thread.stop()
        # Clean up staged tool file in consumer tree
        if staged_cmd_dir.exists():
            shutil.rmtree(staged_cmd_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
