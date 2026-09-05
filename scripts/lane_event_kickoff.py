"""Emit one idempotent kickoff notification to a resident operator lane."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services.lane_events import (
    LANE_EVENT_TEXT_LIMIT,
    LANE_PATTERN,
    emit_lane_event,
    lane_event_config_from_env,
    sanitize_lane_event_text,
)

KST = ZoneInfo("Asia/Seoul")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAYBOOK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.md$")


@dataclass(frozen=True)
class KickoffSlot:
    oncalendar: str
    playbook: str
    weekdays_only: bool


KICKOFF_SLOTS: Mapping[str, KickoffSlot] = {
    "crypto-0220": KickoffSlot("02:20", "prompts/crypto-session-trade.md", False),
    "crypto-0820": KickoffSlot("08:20", "prompts/crypto-session-trade.md", False),
    "crypto-1420": KickoffSlot("14:20", "prompts/crypto-session-trade.md", False),
    "crypto-2020": KickoffSlot("20:20", "prompts/crypto-session-trade.md", False),
    "nxt-prep": KickoffSlot("07:15", "prompts/kr-nxt-prep.md", True),
    "nxt-open": KickoffSlot("07:55", "prompts/kr-nxt-open.md", True),
    "0905": KickoffSlot("09:05", "prompts/kr-open-trade.md", True),
    "1130": KickoffSlot("11:30", "prompts/kr-open-trade.md", True),
    "1430": KickoffSlot("14:30", "prompts/kr-open-trade.md", True),
    "nxt-eve": KickoffSlot("15:50", "prompts/kr-open-trade.md", True),
    "us-2235": KickoffSlot("22:35", "prompts/us-open-trade.md", True),
}


def kst_trading_date(now: datetime) -> str:
    """Return the KST calendar date for an injected instant."""
    return now.astimezone(KST).date().isoformat()


def kickoff_event_id(slot: str, trading_date: str) -> str:
    return f"kickoff-{slot}-{trading_date}"


def _date_arg(value: str) -> str:
    if not _DATE_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a calendar date") from exc
    return value


def _playbook_arg(value: str) -> str:
    if (
        not _PLAYBOOK_PATTERN.fullmatch(value)
        or value.startswith("/")
        or ".." in value.split("/")
    ):
        raise argparse.ArgumentTypeError("playbook must be a safe relative .md path")
    return value


def _lane_arg(value: str) -> str:
    if not LANE_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("lane must be a valid lane name")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True, type=_lane_arg)
    parser.add_argument("--slot", required=True, choices=tuple(KICKOFF_SLOTS))
    parser.add_argument("--playbook", required=True, type=_playbook_arg)
    parser.add_argument("--date", type=_date_arg)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _output(
    *,
    lane: str,
    slot: str,
    playbook: str,
    trading_date: str,
    enabled: bool,
    dry_run: bool,
    emitted: bool,
    duplicate: bool,
    reason: str | None = None,
) -> None:
    result: dict[str, object] = {
        "date": trading_date,
        "dry_run": dry_run,
        "duplicate": duplicate,
        "emitted": emitted,
        "enabled": enabled,
        "event_id": kickoff_event_id(slot, trading_date),
        "lane": lane,
        "playbook": playbook,
        "slot": slot,
    }
    if reason is not None:
        result["reason"] = reason
    print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = parse_args(argv)
    trading_date = args.date or kst_trading_date(now or datetime.now(KST))
    raw_text = f"[kickoff] {args.slot} {args.playbook} date={trading_date}"
    text = sanitize_lane_event_text(raw_text)
    if (
        len(raw_text.encode("utf-8")) > LANE_EVENT_TEXT_LIMIT
        or len(text.encode("utf-8")) > LANE_EVENT_TEXT_LIMIT
    ):
        _output(
            lane=args.lane,
            slot=args.slot,
            playbook=args.playbook,
            trading_date=trading_date,
            enabled=False,
            dry_run=False,
            emitted=False,
            duplicate=False,
            reason="text_too_long",
        )
        return 2

    enabled = os.getenv("LANE_EVENT_KICKOFF_ENABLED", "").strip().lower() == "true"
    effective_dry_run = args.dry_run or not enabled
    if effective_dry_run:
        _output(
            lane=args.lane,
            slot=args.slot,
            playbook=args.playbook,
            trading_date=trading_date,
            enabled=enabled,
            dry_run=True,
            emitted=False,
            duplicate=False,
        )
        return 0

    try:
        config = lane_event_config_from_env(
            prefix_fallbacks=("LANE_EVENT_EMIT",),
            pane_none_values=frozenset({"", "-"}),
        )
    except ValueError:
        _output(
            lane=args.lane,
            slot=args.slot,
            playbook=args.playbook,
            trading_date=trading_date,
            enabled=enabled,
            dry_run=False,
            emitted=False,
            duplicate=False,
            reason="invalid_config",
        )
        return 2

    result = emit_lane_event(
        args.lane,
        event_id=kickoff_event_id(args.slot, trading_date),
        text=text,
        config=config,
    )
    if result.outcome == "emitted":
        _output(
            lane=args.lane,
            slot=args.slot,
            playbook=args.playbook,
            trading_date=trading_date,
            enabled=enabled,
            dry_run=False,
            emitted=True,
            duplicate=False,
        )
        return 0
    if result.outcome == "duplicate":
        _output(
            lane=args.lane,
            slot=args.slot,
            playbook=args.playbook,
            trading_date=trading_date,
            enabled=enabled,
            dry_run=False,
            emitted=False,
            duplicate=True,
        )
        return 0
    _output(
        lane=args.lane,
        slot=args.slot,
        playbook=args.playbook,
        trading_date=trading_date,
        enabled=enabled,
        dry_run=False,
        emitted=False,
        duplicate=False,
        reason=result.reason or "os_error",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
