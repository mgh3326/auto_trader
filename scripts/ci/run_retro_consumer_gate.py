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
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
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


@contextmanager
def acquire_consumer_checkout(
    consumer_info: dict[str, Any], explicit_dir: str | None
) -> Iterator[Path]:
    target_commit = consumer_info["commit"]

    # 1. Resolve or clone base repository
    if explicit_dir:
        base_repo = Path(explicit_dir).resolve()
        if not base_repo.exists():
            raise RuntimeError(f"explicit consumer dir {explicit_dir} does not exist")
    else:
        local_default = Path.home() / "work" / consumer_info["name"]
        if local_default.exists() and (local_default / ".git").exists():
            base_repo = local_default
        else:
            cache_dir = REPO_ROOT / ".cache" / "consumers" / consumer_info["name"]
            if not cache_dir.exists():
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                print(f"[gate] Cloning {consumer_info['repo_url']} to {cache_dir}...")
                subprocess.run(
                    ["git", "clone", consumer_info["repo_url"], str(cache_dir)],
                    check=True,
                )
            base_repo = cache_dir

    # 2. Check if base_repo is a git repository
    if not (base_repo / ".git").exists():
        if explicit_dir:
            raise RuntimeError(
                f"explicit consumer dir {explicit_dir} is not a valid git repository"
            )
        cache_dir = REPO_ROOT / ".cache" / "consumers" / consumer_info["name"]
        if base_repo == cache_dir:
            print(f"[gate] Corrupted cache {cache_dir}; re-cloning...")
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", consumer_info["repo_url"], str(cache_dir)],
                check=True,
            )
            base_repo = cache_dir
        else:
            raise RuntimeError(
                f"consumer repository at {base_repo} is not a valid git repository"
            )

    if not (base_repo / ".git").exists():
        raise RuntimeError(
            f"unable to establish a valid git repository for consumer at {base_repo}"
        )

    # Ensure target_commit is present
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{target_commit}^{{commit}}"],
        cwd=str(base_repo),
        capture_output=True,
    )
    if verify.returncode != 0:
        print(f"[gate] Fetching commit {target_commit[:10]} in {base_repo}...")
        subprocess.run(
            ["git", "fetch", "origin", target_commit],
            cwd=str(base_repo),
            check=True,
            capture_output=True,
        )

    # Create an isolated temporary git worktree at the pinned commit
    temp_parent = Path(tempfile.mkdtemp(prefix=f"{consumer_info['name']}-gate-"))
    temp_worktree = temp_parent / "checkout"
    try:
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(temp_worktree),
                target_commit,
            ],
            cwd=str(base_repo),
            check=True,
            capture_output=True,
        )
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(temp_worktree), text=True
        ).strip()
        print(
            f"[gate] Prepared isolated worktree at {temp_worktree} (HEAD: {head[:10]})"
        )
        yield temp_worktree
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(temp_worktree)],
            cwd=str(base_repo),
            check=False,
            capture_output=True,
        )
        shutil.rmtree(temp_parent, ignore_errors=True)


def build_gate_session():
    from tests.services.retro_v1.support import (
        FakeSession,
        bar_row,
        forecast,
        kr_day_session,
    )

    now = datetime.now(UTC)
    stale_run = SimpleNamespace(
        run_id="00000000-0000-0000-0000-000000000001",
        broker="kis",
        window_start=now - timedelta(days=1),
        window_end=now,
        started_at=now - timedelta(hours=5, minutes=1),
        finished_at=now - timedelta(hours=5),
        dry_run=False,
        would_insert=0,
        would_update=0,
        unchanged=0,
        committed_insert=0,
        committed_update=0,
        error_summary=None,
        notes=None,
    )
    experiment = SimpleNamespace(
        strategy_id="S_ALPHA",
        venue="alpaca_paper",
        total=4,
        sent=3,
        filled=2,
        unresolved=1,
        closed_count=2,
        closed_unpriced=0,
        pnl_sum=Decimal("12.50"),
        last_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    account_id = "upbit:upbit:" + "a" * 64
    snapshots = [
        SimpleNamespace(
            account=account_id,
            market="account",
            day=date(2026, 1, 1) + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
            observed_at=datetime(2026, 1, 8, 9, 30, tzinfo=UTC),
        )
        for i in range(8)
    ]
    bars = [
        bar_row(
            "005930",
            datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
            "71000",
            partition="KRX",
            adj_close=None,
            value=Decimal("71000000"),
        ),
        *[
            bar_row(
                "005930",
                datetime(2026, 1, 6, tzinfo=UTC) + timedelta(days=i),
                str(72000 + i),
                partition="KRX",
                adj_close=None,
                value=Decimal("72000000"),
            )
            for i in range(25)
        ],
    ]

    def account_snapshots_route(sql: str):
        if "max(" in sql:
            return [datetime(2026, 1, 8, 9, 30, tzinfo=UTC)]
        if "DISTINCT" in sql or "distinct" in sql:
            return [(account_id, "account")]
        return snapshots

    def trade_forecast_route(sql: str):
        if "max(" in sql or "min(" in sql:
            return [None]
        return [forecast(2, "000660", datetime(2026, 1, 5, 0, 40, tzinfo=UTC))]

    routes = {
        "SHOW transaction_read_only": ["on"],
        "FROM review.execution_ledger_reconcile_runs": [stale_run],
        "execution_ledger_reconcile_runs": [stale_run],
        "krx_after_market_eligibility": [(0, None, None)],
        "investment_report_items": [None],
        "FROM review.mock_strategy_orders": [experiment],
        "FROM strategy_sleeves": [],
        "max(account_value_snapshots.observed_at)": [
            datetime(2026, 1, 8, 9, 30, tzinfo=UTC)
        ],
        "account_value_snapshots": account_snapshots_route,
        "account_external_flows": [],
        "FROM public.kr_candles_1d": bars,
    }

    base_session = FakeSession()
    kr_session = kr_day_session()
    full_routes = dict(routes)
    for k, v in kr_session.routes.items():
        if k == "FROM review.execution_ledger":
            full_routes["FROM review.execution_ledger "] = v
        elif k == "FROM review.trade_forecasts":
            full_routes[k] = trade_forecast_route
        elif k == "FROM public.kr_candles_1d":
            pass
        else:
            full_routes[k] = v
    base_session.routes = full_routes
    return base_session


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
        self.original_retro_day = None

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

            from app.core.db import get_db
            from app.middleware.auth import AuthMiddleware
            from app.routers import retro_v1

            app = FastAPI()

            # Dependency override to inject deterministic test DB session
            async def get_test_db():
                yield build_gate_session()

            app.dependency_overrides[get_db] = get_test_db

            # Real readers are used directly without static mocking.
            # Mutants wrap real readers rather than replacing them with static fixtures.
            if self.mutant == "delete_field":
                real_retro_day = retro_v1.READERS["retro_day"]
                self.original_retro_day = real_retro_day

                async def mutated_retro_day(session, **query):
                    resp = await real_retro_day(session, **query)
                    if "data" in resp and isinstance(resp["data"], list):
                        for row in resp["data"]:
                            row.pop("event_ref", None)
                    return resp

                retro_v1.READERS["retro_day"] = mutated_retro_day

            elif self.mutant == "float_integer":
                real_retro_day = retro_v1.READERS["retro_day"]
                self.original_retro_day = real_retro_day

                async def mutated_retro_day(session, **query):
                    resp = await real_retro_day(session, **query)
                    if "data" in resp and isinstance(resp["data"], list):
                        for row in resp["data"]:
                            row["price"] = 100.5
                    return resp

                retro_v1.READERS["retro_day"] = mutated_retro_day

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
        if self.original_retro_day is not None:
            from app.routers import retro_v1

            retro_v1.READERS["retro_day"] = self.original_retro_day
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

    # Copy / stage tools/retro-consumer-check/main.go into isolated consumer workspace
    harness_src = REPO_ROOT / "tools" / "retro-consumer-check" / "main.go"
    if not harness_src.exists():
        print(f"error: harness {harness_src} not found", file=sys.stderr)
        return 2

    with acquire_consumer_checkout(consumer_info, args.consumer_dir) as consumer_dir:
        staged_cmd_dir = consumer_dir / "cmd" / "retro-consumer-check"
        cmd_dir_existed_before = staged_cmd_dir.exists()
        staged_cmd_dir.mkdir(parents=True, exist_ok=True)
        staged_main = staged_cmd_dir / "main.go"
        main_existed_before = staged_main.exists()
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
                    "error: candidate server did not start within timeout",
                    file=sys.stderr,
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
            env = {**os.environ, "GOTOOLCHAIN": "auto"}
            res = subprocess.run(cmd, cwd=str(consumer_dir), env=env)
            return res.returncode
        finally:
            server_thread.stop()
            # Clean up staged files without deleting pre-existing consumer files
            if not main_existed_before and staged_main.exists():
                staged_main.unlink(missing_ok=True)
            if not cmd_dir_existed_before and staged_cmd_dir.exists():
                shutil.rmtree(staged_cmd_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
