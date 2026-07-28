#!/usr/bin/env python3
"""Read-only KR-B1 P0 preparation smoke.

This validates frozen local artifacts only.  It does not call a broker,
database, network, scheduler, or deployment surface and does not execute a
cost probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.krb1_p0_journal import (
    SEALED_INITIAL_HEAD,
    JournalError,
    verify_journal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO_ROOT / "docs/research/krb1"
MANIFEST_PATH = ARTIFACT_ROOT / "p0-prep-artifact-manifest.json"
EXPECTED_SEALED_SHA256 = (
    "d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1"
)
EXPECTED_DATES = [
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
]
EXPECTED_RETROSPECTIVE_FIELDS = {
    "DV20_pct",
    "M60",
    "actual_execution_price",
    "actual_exit_date",
    "base_net",
    "correlation_id",
    "delay",
    "fees",
    "gross",
    "hash",
    "planned_exit_date",
    "planned_limit",
    "quantity",
    "rank",
    "record_type",
    "stress_net",
    "study_id",
    "taxes",
    "tick_bp",
}


class SmokeError(ValueError):
    """One preparation artifact failed closed."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot load {path}: {exc}") from exc
    if type(value) is not dict:
        raise SmokeError(f"{path} must contain one JSON object")
    return value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SmokeError(f"cannot hash {path}: {exc}") from exc


def _verify_artifact_hashes(manifest: dict[str, Any]) -> None:
    artifacts = manifest.get("artifacts")
    if type(artifacts) is not dict or not artifacts:
        raise SmokeError("artifact manifest has no artifacts")
    for relative_name, expected in artifacts.items():
        if type(relative_name) is not str or type(expected) is not str:
            raise SmokeError("artifact manifest names and hashes must be strings")
        actual = _sha256(ARTIFACT_ROOT / relative_name)
        if actual != expected:
            raise SmokeError(
                f"artifact hash mismatch for {relative_name}: {actual} != {expected}"
            )


def _verify_seal_and_scaffold(sealed_path: Path) -> None:
    actual_seal_hash = _sha256(sealed_path)
    if actual_seal_hash != EXPECTED_SEALED_SHA256:
        raise SmokeError(
            f"sealed canonical hash mismatch: {actual_seal_hash} "
            f"!= {EXPECTED_SEALED_SHA256}"
        )
    sealed = _load_object(sealed_path)
    initial_rows = sealed.get("anchor_ledger", {}).get("initial_rows")
    if type(initial_rows) is not list or len(initial_rows) != 1:
        raise SmokeError("sealed canonical must contain exactly one initial row")

    scaffold_path = ARTIFACT_ROOT / "p0-anchor-ledger.initial.jsonl"
    try:
        scaffold = json.loads(scaffold_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot load journal scaffold: {exc}") from exc
    if scaffold != initial_rows[0]:
        raise SmokeError("journal scaffold differs from sealed initial row")
    try:
        head = verify_journal(scaffold_path)
    except JournalError as exc:
        raise SmokeError(f"journal scaffold failed verification: {exc}") from exc
    if head.row_count != 1 or head.chain_hash != SEALED_INITIAL_HEAD:
        raise SmokeError("journal scaffold initial head/count mismatch")


def _parse(value: Any, *, field: str) -> datetime:
    if type(value) is not str:
        raise SmokeError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SmokeError(f"{field} is not an ISO timestamp") from exc
    if parsed.utcoffset() != timedelta(hours=9):
        raise SmokeError(f"{field} must use +09:00")
    return parsed


def _verify_calendar() -> None:
    calendar = _load_object(ARTIFACT_ROOT / "krx-calendar-2026-07-30-p0.json")
    if calendar.get("authority") != "THIS_FROZEN_SNAPSHOT":
        raise SmokeError("calendar snapshot is not the declared authority")
    if calendar.get("half_days_within_span") != []:
        raise SmokeError("frozen P0 window must have no half-days")
    sessions = calendar.get("sessions")
    if type(sessions) is not list or len(sessions) != 10:
        raise SmokeError("calendar must contain exactly 10 sessions")
    dates = [session.get("trade_date") for session in sessions]
    if dates != EXPECTED_DATES:
        raise SmokeError(f"calendar session dates changed: {dates!r}")

    for expected_index, session in enumerate(sessions, start=1):
        if type(session) is not dict:
            raise SmokeError(f"calendar session {expected_index} must be an object")
        if session.get("p0_session") != expected_index:
            raise SmokeError(f"calendar session index {expected_index} changed")
        opened = _parse(session.get("official_open"), field="official_open")
        closed = _parse(session.get("official_close"), field="official_close")
        start = _parse(
            session.get("entry_submit_start_inclusive"),
            field="entry_submit_start_inclusive",
        )
        end = _parse(
            session.get("entry_submit_end_exclusive"),
            field="entry_submit_end_exclusive",
        )
        exit_at = _parse(session.get("exit_submit_at"), field="exit_submit_at")
        reconcile = _parse(session.get("reconcile_at"), field="reconcile_at")
        if opened.date().isoformat() != session["trade_date"]:
            raise SmokeError(f"calendar session {expected_index} date/open mismatch")
        if (
            start != opened - timedelta(minutes=10)
            or end != opened - timedelta(minutes=5)
            or closed - opened != timedelta(hours=6, minutes=30)
            or exit_at != closed - timedelta(minutes=20)
            or reconcile != closed + timedelta(minutes=5)
        ):
            raise SmokeError(f"calendar session {expected_index} timing changed")


def _verify_retrospective_schema() -> None:
    schema = _load_object(ARTIFACT_ROOT / "p0-retrospective-row.schema.json")
    required = schema.get("required")
    properties = schema.get("properties")
    if type(required) is not list or set(required) != EXPECTED_RETROSPECTIVE_FIELDS:
        raise SmokeError("retrospective required fields differ from §6 mapping")
    if type(properties) is not dict or set(properties) != EXPECTED_RETROSPECTIVE_FIELDS:
        raise SmokeError("retrospective properties differ from §6 mapping")
    if schema.get("additionalProperties") is not False:
        raise SmokeError("retrospective schema must reject undeclared fields")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only KR-B1 P0 preparation smoke")
    parser.add_argument(
        "--sealed-json",
        type=Path,
        required=True,
        help="path to krb1-combined-canonical-2026-07-28.json",
    )
    args = parser.parse_args()

    try:
        manifest = _load_object(MANIFEST_PATH)
        if manifest.get("sealed_canonical_sha256") != EXPECTED_SEALED_SHA256:
            raise SmokeError("artifact manifest seal hash changed")
        if manifest.get("cost_probe_state") != "NOT_EXECUTED":
            raise SmokeError("cost probe must remain NOT_EXECUTED before P0")
        if manifest.get("p0_state") != "NOT_STARTED":
            raise SmokeError("preparation manifest must remain NOT_STARTED")
        _verify_artifact_hashes(manifest)
        _verify_seal_and_scaffold(args.sealed_json.expanduser())
        _verify_calendar()
        _verify_retrospective_schema()
    except SmokeError as exc:
        parser.exit(1, f"KR-B1 P0 PREP SMOKE FAIL: {exc}\n")

    print(
        json.dumps(
            {
                "calendar_first_session": EXPECTED_DATES[0],
                "calendar_last_session": EXPECTED_DATES[-1],
                "calendar_sessions": len(EXPECTED_DATES),
                "canonical_sha256": EXPECTED_SEALED_SHA256,
                "cost_probe_state": "NOT_EXECUTED",
                "journal_initial_head": SEALED_INITIAL_HEAD,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
