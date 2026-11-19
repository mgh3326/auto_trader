#!/usr/bin/env python3
"""Fill projection CLI of the hk 3795 mandate (ticket T4b).

Reads plan state, the plan's bound proposals and the authoritative ledger fills, then
appends the plan events those facts prove (``tranche_filled`` / ``tranche_partially_filled``
/ ``tranche_unfilled``, ``first_tranche_settled``, ``exhausted``, ``closed``,
``orders_settled``, ``holdings_disposed``, ``released``) as ``system:mandate_projection``.

Default is a preview: the same store checks run, then the plan is rolled back. Events
are written only with ``--commit``, one transaction per plan. The ledger and the order
tables are read, never written; no broker is called.

Holdings are input, not a broker read: ``--holdings FILE`` is a JSON object
``{"readings": [{"account_ref", "symbol", "quantity", "checked_at"}]}`` an operator wrote
from a broker read. A ``zero_holdings`` disposal accepts only readings whose
``account_ref`` equals one of the plan's ``scope_account_refs``, every one of them,
newer than the plan's last fill and no older than ``--max-holdings-age-hours``.

The database is named explicitly: ``--database-url-env NAME`` (preferred) or
``--database-url URL``. The URL is never printed.

Runbook: ``docs/runbooks/mandate-cli.md`` §6. Exit codes: 0 ok, 1 input, refusal or database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.mandate import projection as proj
from app.services.mandate import store as ms
from app.services.mandate.projection_rules import (
    DEFAULT_MAX_HOLDINGS_AGE,
    HoldingsReading,
)
from app.services.strategy_sleeves import store as sleeve_store
from scripts.mandate import (
    EXIT_ERROR,
    EXIT_OK,
    MandateCliError,
    _aware_datetime,
    _database_url,
    _positive_int,
)

READING_KEYS = frozenset({"account_ref", "symbol", "quantity", "checked_at"})


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


def read_holdings(path: str | None) -> tuple[HoldingsReading, ...]:
    """Operator holdings readings from a closed-key JSON file (none if no file)."""
    if path is None:
        return ()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), parse_float=str)
    except (OSError, ValueError) as exc:
        raise MandateCliError(
            f"보유 파일 {path!r}을(를) 읽을 수 없거나 JSON이 아닙니다"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {"readings"}:
        raise MandateCliError('보유 파일은 {"readings": [...]} 객체여야 합니다')
    items = raw["readings"]
    if not isinstance(items, list):
        raise MandateCliError("readings는 목록이어야 합니다")
    readings: list[HoldingsReading] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != READING_KEYS:
            raise MandateCliError(
                f"보유 항목의 키는 정확히 {sorted(READING_KEYS)}여야 합니다"
            )
        try:
            quantity = Decimal(str(item["quantity"]))
            checked_at = _aware_datetime(str(item["checked_at"]))
        except (InvalidOperation, argparse.ArgumentTypeError) as exc:
            raise MandateCliError(
                "보유 항목의 quantity는 10진 문자열, checked_at은 시간대 포함 ISO 8601이어야 합니다"
            ) from exc
        if not quantity.is_finite():
            raise MandateCliError("보유 수량은 유한한 수여야 합니다")
        for key in ("account_ref", "symbol"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise MandateCliError(
                    f"보유 항목의 {key}는 비어 있지 않은 문자열이어야 합니다"
                )
        readings.append(
            HoldingsReading(
                account_ref=item["account_ref"].strip(),
                symbol=item["symbol"].strip(),
                quantity=quantity,
                checked_at=checked_at,
            )
        )
    return tuple(readings)


def report_json(report: proj.PlanReport) -> dict[str, Any]:
    return {
        "plan_id": report.plan_id,
        "symbol": report.symbol,
        "authority": report.authority,
        "cleanup": report.cleanup,
        "final_authority": report.final_authority,
        "final_cleanup": report.final_cleanup,
        "events": [
            {
                "event": e.event,
                "tranche_index": e.fields.get("tranche_index"),
                "proposal_id": str(e.fields["proposal_id"])
                if e.fields.get("proposal_id")
                else None,
                "fill_amount": _json_value(e.fields.get("fill_amount")),
                "fill_quantity": _json_value(e.fields.get("fill_quantity")),
                "first_fill_amount": _json_value(e.fields.get("first_fill_amount")),
                "first_fill_quantity": _json_value(e.fields.get("first_fill_quantity")),
                "evidence": e.evidence,
            }
            for e in report.planned
        ],
        "holds": [
            {
                "code": h.code.value,
                "proposal_id": str(h.proposal_id) if h.proposal_id else None,
                "tranche_index": h.tranche_index,
                "detail": h.detail,
            }
            for h in report.holds
        ],
        "written": report.written,
        "appended_seqs": list(report.appended_seqs),
        "error": report.error,
    }


async def _selected_plan_ids(
    session: AsyncSession, args: argparse.Namespace
) -> list[int]:
    if args.plan_id:
        return list(dict.fromkeys(args.plan_id))
    sleeve = await sleeve_store.get_sleeve_by_key(session, args.sleeve_key)
    if sleeve is None:
        raise MandateCliError(f"sleeve {args.sleeve_key!r}가 없습니다")
    return await proj.unreleased_plan_ids(session, sleeve_id=sleeve.id)


async def _dispatch(
    session: AsyncSession,
    args: argparse.Namespace,
    holdings: Sequence[HoldingsReading],
    now: datetime,
) -> dict[str, Any]:
    plan_ids = await _selected_plan_ids(session, args)
    await session.rollback()
    max_age = timedelta(hours=args.max_holdings_age_hours)
    reports: list[dict[str, Any]] = []
    failed = False
    for plan_id in plan_ids:
        try:
            report = await proj.project_plan(
                session,
                plan_id,
                holdings=holdings,
                now=now,
                max_holdings_age=max_age,
                write=args.commit,
            )
        except (ms.MandateError, ms.MandateIntegrityError) as exc:
            await session.rollback()
            reports.append({"plan_id": plan_id, "error": str(exc)})
            failed = True
            continue
        if report.error is not None:
            await session.rollback()
            failed = True
            reports.append(report_json(report))
            continue
        if args.commit and report.planned:
            await session.commit()
            report = replace(report, written=True)
        else:
            await session.rollback()
        reports.append(report_json(report))
    return {
        "mode": "commit" if args.commit else "preview",
        "plans": reports,
        "failed": failed,
    }


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one projection. ``session_factory`` is injected by tests; the CLI builds it."""
    holdings = read_holdings(args.holdings)
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        async with session_factory() as session:
            payload = await _dispatch(session, args, holdings, now or datetime.now(UTC))
            return (EXIT_ERROR if payload["failed"] else EXIT_OK), payload
    finally:
        if engine is not None:
            await engine.dispose()


def _hours(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("시간 수여야 합니다") from exc
    if not 0 < value <= 24 * 7:
        raise argparse.ArgumentTypeError("0시간 초과 168시간 이하여야 합니다")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hk 3795 위임장 체결 투영 CLI")
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="명시적 PostgreSQL URL")
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help="PostgreSQL URL을 담은 환경 변수 이름(값은 출력하지 않음)",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--plan-id", type=_positive_int, action="append", help="계획 id(반복 가능)"
    )
    target.add_argument("--sleeve-key", help="그 sleeve의 미해제 계획 전부")
    parser.add_argument(
        "--holdings", help='운영자가 쓴 보유 조회 JSON({"readings": [...]})'
    )
    parser.add_argument(
        "--max-holdings-age-hours",
        type=_hours,
        default=DEFAULT_MAX_HOLDINGS_AGE.total_seconds() / 3600,
    )
    parser.add_argument("--commit", action="store_true", help="이벤트를 실제로 쓴다")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (
        MandateCliError,
        ms.MandateError,
        ms.MandateIntegrityError,
        ValueError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"database_error:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
