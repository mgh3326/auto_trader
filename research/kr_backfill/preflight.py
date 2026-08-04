"""Kiwoom-only Stage-B preflight with no broker HTTP and no database DML."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from collect import UPSERT_SQL, dsn
from equality_gate import build_clients
from sources import (
    Pacer,
    assert_fetch_window_open,
    fetch_kiwoom_minutes,
)

REQUIRED_ENV = (
    "DATABASE_URL",
    "KIWOOM_MOCK_APP_KEY",
    "KIWOOM_MOCK_APP_SECRET",
    "REDIS_URL",
)
TARGET_TABLE = "research.kr_candles_1m"
LATENCY_TABLE = "public.kr_candles_1m"
ALLOWED_HOST = "mockapi.kiwoom.com"


@dataclass
class PreflightReport:
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_dependencies: list[str] = field(default_factory=list)

    def passed(self, name: str, **details: Any) -> None:
        self.checks[name] = {"status": "PASS", **details}

    def failed(
        self, name: str, *, dependency: str, exc: Exception | None = None
    ) -> None:
        self.missing_dependencies.append(dependency)
        record: dict[str, Any] = {"status": "FAIL", "dependency": dependency}
        if exc is not None:
            record["error_type"] = type(exc).__name__
        self.checks[name] = record

    def render(self) -> dict[str, Any]:
        missing = sorted(set(self.missing_dependencies))
        return {
            "status": "PASS" if not missing else "FAIL",
            "missing_dependencies": missing,
            "checks": self.checks,
            "broker_http_requests": 0,
            "database_dml_statements": 0,
            "allowed_host": ALLOWED_HOST,
        }


def _nonempty_env(name: str) -> bool:
    return bool(str(os.getenv(name, "")).strip())


def _validate_inputs(
    report: PreflightReport, *, split_csv: Path, job_dir: Path
) -> None:
    try:
        with split_csv.open() as fh:
            kiwoom_symbols = [
                row.get("ticker", "")
                for row in csv.DictReader(fh)
                if row.get("source") == "kiwoom" and row.get("ticker")
            ]
        if not kiwoom_symbols:
            raise ValueError("no Kiwoom symbols")
        report.passed("split_csv", kiwoom_symbols=len(kiwoom_symbols))
    except Exception as exc:  # noqa: BLE001
        report.failed("split_csv", dependency="KIWOOM_SPLIT_INPUT", exc=exc)

    events_dir = job_dir / "events"
    if events_dir.is_dir() and os.access(events_dir, os.W_OK):
        report.passed("job_events_directory")
    else:
        report.failed(
            "job_events_directory", dependency="WRITABLE_JOB_EVENTS_DIRECTORY"
        )


async def _check_offline_fetch(report: PreflightReport, client: Any) -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        seen_hosts.append(host)
        return httpx.Response(
            200,
            json={
                "return_code": 0,
                "stk_min_pole_chart_qry": [
                    {
                        "cntr_tm": "20260801153000",
                        "open_pric": "100",
                        "high_pric": "101",
                        "low_pric": "99",
                        "cur_prc": "100",
                        "trde_qty": "10",
                    }
                ],
            },
            request=request,
        )

    try:
        client.set_transport_for_test(
            httpx.MockTransport(handler), token="preflight-offline-token"
        )
        pacer = Pacer("kiwoom")
        pacer.interval = 0.0
        rows, meta = await fetch_kiwoom_minutes(
            client=client,
            symbol="005930",
            pacer=pacer,
            max_pages=1,
            base_dt="20260801",
        )
        only_row = next(iter(rows.values()), {})
        if (
            len(rows) != 1
            or only_row.get("value") != 1000.0
            or meta.get("pages") != 1
            or seen_hosts != [ALLOWED_HOST]
        ):
            raise RuntimeError("offline fetch contract mismatch")
        report.passed("offline_first_fetch", resolved_hosts=seen_hosts)
    except Exception as exc:  # noqa: BLE001
        report.failed("offline_first_fetch", dependency="KIWOOM_FETCH_PATH", exc=exc)


async def _check_redis(report: PreflightReport) -> None:
    try:
        from app.services.brokers.kiwoom.auth import _get_redis_client

        redis_client = await _get_redis_client()
        await redis_client.ping()
        report.passed("redis_connectivity")
    except Exception as exc:  # noqa: BLE001
        report.failed("redis_connectivity", dependency="REDIS_CONNECTIVITY", exc=exc)

    if "app.core.config" in sys.modules:
        report.failed(
            "global_settings_isolation", dependency="GLOBAL_SETTINGS_ISOLATION"
        )
    else:
        report.passed("global_settings_isolation")


async def _check_database(report: PreflightReport) -> None:
    pool: asyncpg.Pool | None = None
    try:
        pool = await asyncpg.create_pool(dsn(), min_size=1, max_size=2)
        async with pool.acquire() as conn:
            target_exists = await conn.fetchval("SELECT to_regclass($1)", TARGET_TABLE)
            latency_exists = await conn.fetchval(
                "SELECT to_regclass($1)", LATENCY_TABLE
            )
            if target_exists is None:
                report.failed("target_table", dependency=TARGET_TABLE)
            else:
                report.passed("target_table")
            if latency_exists is None:
                report.failed("latency_table", dependency=LATENCY_TABLE)
            else:
                report.passed("latency_table")

            can_insert = await conn.fetchval(
                "SELECT has_table_privilege(current_user, $1, 'INSERT')",
                TARGET_TABLE,
            )
            if can_insert:
                report.passed("research_insert_privilege")
            else:
                report.failed(
                    "research_insert_privilege",
                    dependency="RESEARCH_INSERT_PRIVILEGE",
                )

            can_read_latency = await conn.fetchval(
                "SELECT has_table_privilege(current_user, $1, 'SELECT')",
                LATENCY_TABLE,
            )
            if can_read_latency:
                report.passed("public_latency_read_privilege")
            else:
                report.failed(
                    "public_latency_read_privilege",
                    dependency="PUBLIC_LATENCY_SELECT_PRIVILEGE",
                )

            public_write = await conn.fetchval(
                """
                SELECT has_table_privilege(current_user, $1, 'INSERT')
                    OR has_table_privilege(current_user, $1, 'UPDATE')
                    OR has_table_privilege(current_user, $1, 'DELETE')
                """,
                LATENCY_TABLE,
            )
            if public_write:
                report.failed(
                    "public_write_denied", dependency="PUBLIC_WRITE_DENY_GRANT"
                )
            else:
                report.passed("public_write_denied")

            try:
                await conn.prepare(UPSERT_SQL)
                report.passed("upsert_prepare")
            except Exception as exc:  # noqa: BLE001
                report.failed(
                    "upsert_prepare", dependency="UPSERT_SQL_PREPARE", exc=exc
                )

            try:
                await conn.fetch(
                    "SELECT time, close FROM public.kr_candles_1m "
                    "WHERE symbol=$1 AND time >= $2 ORDER BY time DESC LIMIT 1",
                    "005930",
                    datetime.now().astimezone(),
                )
                report.passed("latency_query_read")
            except Exception as exc:  # noqa: BLE001
                report.failed(
                    "latency_query_read", dependency="LATENCY_QUERY_READ", exc=exc
                )
    except Exception as exc:  # noqa: BLE001
        report.failed(
            "database_connectivity", dependency="DATABASE_CONNECTIVITY", exc=exc
        )
    else:
        report.passed("database_connectivity")
    finally:
        if pool is not None:
            await pool.close()


async def run_preflight(
    *, split_csv: Path, job_dir: Path
) -> tuple[int, dict[str, Any]]:
    report = PreflightReport()
    for name in REQUIRED_ENV:
        if _nonempty_env(name):
            report.passed(f"env:{name}")
        else:
            report.failed(f"env:{name}", dependency=name)

    try:
        assert_fetch_window_open()
        report.passed("fetch_window")
    except Exception as exc:  # noqa: BLE001
        report.failed("fetch_window", dependency="BACKFILL_DAYTIME_APPROVED", exc=exc)

    _validate_inputs(report, split_csv=split_csv, job_dir=job_dir)

    client: Any | None = None
    try:
        clients = await build_clients(["kiwoom"])
        client = clients["kiwoom"]
        report.passed("kiwoom_client_build")
    except Exception as exc:  # noqa: BLE001
        report.failed("kiwoom_client_build", dependency="KIWOOM_CLIENT_BUILD", exc=exc)

    if client is not None:
        await _check_offline_fetch(report, client)
    else:
        report.failed("offline_first_fetch", dependency="KIWOOM_FETCH_PATH")

    await _check_redis(report)
    await _check_database(report)

    rendered = report.render()
    return (0 if rendered["status"] == "PASS" else 2), rendered


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-csv", required=True, type=Path)
    parser.add_argument("--job-dir", required=True, type=Path)
    args = parser.parse_args()

    exit_code, report = await run_preflight(
        split_csv=args.split_csv, job_dir=args.job_dir
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
