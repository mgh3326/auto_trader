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
    additional_oncalendars: tuple[str, ...] = ()
    source: str = "resident"
    disposition: str = "cycle_kickoff"
    tick_scoped: bool = False


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
    # B0X portability sources mirror the six Prefect records exactly.  They
    # are independently gated (default off) and do not replace the original
    # eleven resident sources above until the documented single-owner cutover.
    "b0x-table-kr": KickoffSlot(
        "07:45",
        "docs/runbooks/b0x-policy-table-build.md",
        True,
        source="b0x",
        disposition="policy_table_build",
    ),
    "b0x-table-us": KickoffSlot(
        "22:00",
        "docs/runbooks/b0x-policy-table-build.md",
        True,
        source="b0x",
        disposition="policy_table_build",
    ),
    "b0x-nudge-kr": KickoffSlot(
        "09:05",
        "docs/runbooks/b0x-kr-cycle.md",
        True,
        source="b0x",
    ),
    "b0x-nudge-us": KickoffSlot(
        "22:35",
        "docs/runbooks/b0x-us-cycle.md",
        True,
        source="b0x",
    ),
    "b0x-nudge-crypto": KickoffSlot(
        "01:00",
        "docs/runbooks/b0x-crypto-cycle.md",
        False,
        additional_oncalendars=("05:00", "09:00", "13:00", "17:00", "21:00"),
        source="b0x",
        tick_scoped=True,
    ),
    "b0x-harvest": KickoffSlot(
        "00:13",
        "docs/runbooks/b0x-harvest.md",
        False,
        additional_oncalendars=tuple(
            f"{hour:02d}:{minute:02d}"
            for hour in range(24)
            for minute in (13, 43)
            if (hour, minute) != (0, 13)
        ),
        source="b0x",
        disposition="observe_only_harvest",
        tick_scoped=True,
    ),
}


def kst_trading_date(now: datetime) -> str:
    """Return the KST calendar date for an injected instant."""
    return now.astimezone(KST).date().isoformat()


def kickoff_event_id(slot: str, trading_date: str, tick: str | None = None) -> str:
    suffix = f"-T{tick}" if tick is not None else ""
    return f"kickoff-{slot}-{trading_date}{suffix}"


def _scheduled_ticks(slot: KickoffSlot) -> frozenset[str]:
    return frozenset(
        value.replace(":", "")
        for value in (slot.oncalendar, *slot.additional_oncalendars)
    )


def _tick_arg(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3])[0-5]\d", value):
        raise argparse.ArgumentTypeError("tick must use HHMM in KST")
    return value


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
    parser.add_argument("--tick", type=_tick_arg)
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
    tick: str | None = None,
    reason: str | None = None,
) -> None:
    result: dict[str, object] = {
        "date": trading_date,
        "dry_run": dry_run,
        "duplicate": duplicate,
        "emitted": emitted,
        "enabled": enabled,
        "event_id": kickoff_event_id(slot, trading_date, tick),
        "lane": lane,
        "playbook": playbook,
        "slot": slot,
        # A producer acknowledgement is deliberately not consumer evidence.
        "transport_acknowledged": emitted or duplicate,
        "consumer_execution_evidence": None,
    }
    if reason is not None:
        result["reason"] = reason
    print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = parse_args(argv)
    instant = (now or datetime.now(KST)).astimezone(KST)
    trading_date = args.date or kst_trading_date(instant)
    slot = KICKOFF_SLOTS[args.slot]
    if (
        slot.source == "b0x"
        and slot.weekdays_only
        and date.fromisoformat(trading_date).weekday() >= 5
    ):
        raise ValueError(f"source slot {args.slot!r} is weekdays-only")
    if slot.source == "b0x" and args.playbook != slot.playbook:
        raise ValueError(
            f"playbook for B0X source slot {args.slot!r} must be {slot.playbook!r}"
        )
    tick = args.tick or (instant.strftime("%H%M") if slot.tick_scoped else None)
    if slot.tick_scoped and tick not in _scheduled_ticks(slot):
        raise ValueError(
            f"tick {tick!r} is not scheduled for source slot {args.slot!r}"
        )
    if not slot.tick_scoped and args.tick is not None:
        raise ValueError(f"source slot {args.slot!r} is date-scoped, not tick-scoped")
    enabled = os.getenv("LANE_EVENT_KICKOFF_ENABLED", "").strip().lower() == "true"
    if slot.source == "b0x":
        enabled = enabled and (
            os.getenv("LANE_EVENT_KICKOFF_B0X_ENABLED", "").strip().lower() == "true"
        )
        raw_text = json.dumps(
            {
                "date": trading_date,
                "disposition": slot.disposition,
                "playbook": args.playbook,
                "source": "b0x",
                "slot": args.slot,
                "tick": tick,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
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
            enabled=enabled,
            dry_run=False,
            emitted=False,
            duplicate=False,
            tick=tick,
            reason="text_too_long",
        )
        return 2

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
            tick=tick,
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
            tick=tick,
            reason="invalid_config",
        )
        return 2

    result = emit_lane_event(
        args.lane,
        event_id=kickoff_event_id(args.slot, trading_date, tick),
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
            tick=tick,
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
            tick=tick,
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
        tick=tick,
        reason=result.reason or "os_error",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
