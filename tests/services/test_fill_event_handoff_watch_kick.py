"""Task #865 watch-event kick filter: classification, shared cap, ladders.

Mirror of the #825 fill kick tests for delivered ``investment_watch_events``
rows.  Every watch event stays bundled exactly as before — only the kick
decision changes — and watch kicks draw from the same ``kick_days`` counter
and ``cooldowns`` map as fill kicks, so the two kinds share one per-market
KST daily cap.  Tests are fixture-only: no live Prefect, no panes, no broker,
no production DB.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.fill_event_handoff import service as handoff_service
from app.services.fill_event_handoff import watch_kick as watch_kick_module
from app.services.fill_event_handoff.service import FillHandoffRunner, HandoffConfig
from app.services.fill_event_handoff.watch_kick import (
    WatchKickCursor,
    classify_watch_for_kick,
    classify_watch_without_action,
    is_tradable_now,
)


def _watch(event_id: int, **overrides: Any) -> dict[str, Any]:
    """One delivered watch-event row as the kick source projects it."""
    event: dict[str, Any] = {
        "event_id": event_id,
        "event_uuid": f"00000000-0000-0000-0000-{event_id:012d}",
        "idempotency_key": f"alert-{event_id}:2026-09-03:t{event_id}",
        "alert_id": event_id,
        "market": "crypto",
        "symbol": "BTC",
        "metric": "price",
        "operator": "above",
        "threshold": "100",
        "threshold_high": None,
        "current_value": "101",
        "outcome": "notified",
        "intent": "sell_review",
        "action_mode": "approval_required",
        "kst_date": "2026-09-03",
        "correlation_id": f"corr-{event_id}",
        "delivered_at": "2026-09-03T00:55:00+00:00",
        "alert_max_action": {"side": "sell", "quantity": "0.1"},
    }
    event.update(overrides)
    return event


# --- pure classifier: eligibility ----------------------------------------------


def test_buy_review_approval_required_tradable_is_kick_eligible() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="buy_review"), None, tradable=True
    )
    assert verdict.eligible is True
    assert verdict.reason == "buy_review"


def test_buy_review_needs_no_max_action_read() -> None:
    # mutant guard: the side rule is an OR, not an AND — a buy_review fire
    # kicks even when the source alert is already gone (max_action=None)
    verdict = classify_watch_for_kick(
        _watch(1, intent="buy_review"), None, tradable=True
    )
    assert verdict.eligible is True


def test_non_buy_review_with_max_action_side_is_kick_eligible() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="sell_review"),
        {"side": "sell", "quantity": "0.1"},
        tradable=True,
    )
    assert verdict.eligible is True
    assert verdict.reason == "action_side"


def test_missing_intent_with_a_present_side_is_still_eligible() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent=None), {"side": "buy"}, tradable=True
    )
    assert verdict.eligible is True
    assert verdict.reason == "action_side"


# --- pure classifier: queue-only gates ------------------------------------------


def test_notify_only_is_never_kick_eligible() -> None:
    # AC1: notify_only stays queue-only even when every other field is ideal
    verdict = classify_watch_for_kick(
        _watch(1, action_mode="notify_only", intent="buy_review"),
        {"side": "buy"},
        tradable=True,
    )
    assert verdict.eligible is False
    assert verdict.reason == "action_mode_notify_only"


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("preview_only", "action_mode_preview_only"),
        ("auto_execute_mock", "action_mode_auto_execute_mock"),
        ("", "action_mode_missing"),
        (None, "action_mode_missing"),
        ("surprise", "action_mode_unknown"),
    ],
)
def test_non_approval_modes_are_queue_only(mode: Any, reason: str) -> None:
    verdict = classify_watch_for_kick(
        _watch(1, action_mode=mode, intent="buy_review"),
        {"side": "buy"},
        tradable=True,
    )
    assert verdict.eligible is False
    assert verdict.reason == reason


def test_market_closed_is_queue_only_even_for_a_perfect_event() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="buy_review"), None, tradable=False
    )
    assert verdict.eligible is False
    assert verdict.reason == "market_closed"


def test_approval_required_without_buy_review_or_side_is_queue_only() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="sell_review"),
        {"side": "  "},
        tradable=True,
    )
    assert verdict.eligible is False
    assert verdict.reason == "no_action_side"


@pytest.mark.parametrize("max_action", [None, "junk", 3, []])
def test_unreadable_max_action_fails_closed(max_action: Any) -> None:
    # never kick on guessed data: an absent/deleted alert cannot prove a side
    verdict = classify_watch_for_kick(
        _watch(1, intent="sell_review"), max_action, tradable=True
    )
    assert verdict.eligible is False
    assert verdict.reason == "max_action_unavailable"


def test_max_action_without_side_is_queue_only() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="risk_review"), {"quantity": "1"}, tradable=True
    )
    assert verdict.eligible is False
    assert verdict.reason == "no_action_side"


@pytest.mark.parametrize("market", ["forex", "index", "", None])
def test_unsupported_markets_are_queue_only(market: Any) -> None:
    verdict = classify_watch_for_kick(
        _watch(1, market=market, intent="buy_review"), None, tradable=True
    )
    assert verdict.eligible is False
    assert verdict.reason == "unsupported_market"


def test_early_classifier_matches_the_full_classifier() -> None:
    # classify_watch_without_action is the no-read fast path of the same rule
    early = classify_watch_without_action(
        _watch(1, action_mode="notify_only"), tradable=True
    )
    assert early is not None and early.reason == "action_mode_notify_only"
    assert (
        classify_watch_without_action(_watch(1, intent="buy_review"), tradable=True)
        is not None
    )
    assert (
        classify_watch_without_action(_watch(1, intent="sell_review"), tradable=True)
        is None
    )


# --- is_tradable_now: exchange-calendar boundaries -------------------------------


def test_crypto_is_always_tradable() -> None:
    for stamp in (
        datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
        datetime(2026, 9, 5, 12, 0, tzinfo=UTC),  # Saturday
        datetime(2026, 9, 3, 23, 59, tzinfo=UTC),
    ):
        assert is_tradable_now("crypto", stamp) is True


@pytest.mark.parametrize(
    "utc_stamp,expected",
    [
        (datetime(2026, 9, 2, 23, 59, tzinfo=UTC), False),  # 08:59 KST
        (datetime(2026, 9, 3, 0, 0, tzinfo=UTC), True),  # 09:00 KST open edge
        (datetime(2026, 9, 3, 6, 29, tzinfo=UTC), True),  # 15:29 KST
        (datetime(2026, 9, 3, 6, 30, tzinfo=UTC), False),  # 15:30 KST close edge
        (datetime(2026, 9, 5, 1, 0, tzinfo=UTC), False),  # Saturday
    ],
)
def test_kr_tradable_hours_follow_the_xkrx_session(
    utc_stamp: datetime, expected: bool
) -> None:
    # mutant guard: open is inclusive, close is exclusive — an off-by-one at
    # either edge fails an assertion
    assert is_tradable_now("kr", utc_stamp) is expected


@pytest.mark.parametrize(
    "utc_stamp,expected",
    [
        (datetime(2026, 9, 3, 13, 29, tzinfo=UTC), False),  # 09:29 ET
        (datetime(2026, 9, 3, 13, 30, tzinfo=UTC), True),  # 09:30 ET open edge
        (datetime(2026, 9, 3, 19, 59, tzinfo=UTC), True),  # 15:59 ET
        (datetime(2026, 9, 3, 20, 0, tzinfo=UTC), False),  # 16:00 ET close edge
    ],
)
def test_us_tradable_hours_follow_the_xnys_session(
    utc_stamp: datetime, expected: bool
) -> None:
    assert is_tradable_now("us", utc_stamp) is expected


@pytest.mark.parametrize("market", ["forex", "", "KOSPI200"])
def test_unknown_markets_are_never_tradable(market: str) -> None:
    assert is_tradable_now(market, datetime(2026, 9, 3, 1, 0, tzinfo=UTC)) is False


def test_unusable_clock_input_fails_closed_instead_of_raising() -> None:
    assert is_tradable_now("kr", "not-a-datetime") is False  # type: ignore[arg-type]


# --- runner integration ----------------------------------------------------------


class _Db:
    async def commit(self) -> None:
        pass


def _fill(ledger_id: int, **overrides: Any) -> dict[str, Any]:
    fill: dict[str, Any] = {
        "ledger_id": ledger_id,
        "event_key": f"execution_ledger:{ledger_id}",
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": "BTC",
        "side": "sell",
        "filled_qty": "0.1",
        "filled_price": "100",
        "filled_notional": "10",
        "currency": "KRW",
        "broker_order_id": "same-order",
        "correlation_id": "c-1",
        "filled_at": "2026-09-03T00:00:00+00:00",
    }
    fill.update(overrides)
    return fill


def _stub_context() -> type:
    class Context:
        appended: list[Any] = []
        kick_results: list[str] = []
        capped: list[int] = []

        def __init__(self, _db: object) -> None:
            pass

        async def get_open_question_for_event_key(self, key: str) -> Any:
            for entry in self.appended:
                if entry.refs.event_key == key:
                    return SimpleNamespace(id=len(self.appended))
            return None

        async def append_entries(self, entries: list[Any]) -> list[Any]:
            self.__class__.appended.extend(entries)
            return [SimpleNamespace(id=len(self.appended))]

        async def append_fill_handoff_kick_result(self, **kwargs: Any) -> None:
            self.__class__.kick_results.append(str(kwargs["flow_run_id"]))

        async def append_fill_handoff_kick_capped(self, **kwargs: Any) -> None:
            self.__class__.capped.append(int(kwargs["entry_id"]))

    return Context


def _safe_event_id(row: dict[str, Any]) -> int:
    try:
        return int(row["event_id"])
    except (KeyError, TypeError, ValueError):
        return -1


def _watch_source_cls(rows: list[dict[str, Any]]) -> type:
    """Cursor-faithful fake for ``DbWatchKickSource``.

    Unparseable ids sort as -1 so a malformed fixture row still reaches the
    runner's own malformed-row handling instead of breaking the fake query.
    """

    def order_key(row: dict[str, Any]) -> tuple[str, int]:
        return str(row["delivered_at"]), _safe_event_id(row)

    class Source:
        def __init__(self, _db: object) -> None:
            pass

        async def high_watermark(self) -> WatchKickCursor:
            if not rows:
                return WatchKickCursor(None, 0)
            row = max(rows, key=order_key)
            return WatchKickCursor(
                datetime.fromisoformat(str(row["delivered_at"])),
                _safe_event_id(row),
            )

        async def list_after(
            self, cursor: WatchKickCursor, *, limit: int
        ) -> list[dict[str, Any]]:
            if cursor.delivered_at is None:
                after = [
                    row
                    for row in rows
                    if _safe_event_id(row) > cursor.event_id
                    or _safe_event_id(row) == -1
                ]
            else:
                marker = (cursor.delivered_at.isoformat(), cursor.event_id)
                after = [
                    row
                    for row in rows
                    if order_key(row) > marker or _safe_event_id(row) == -1
                ]
            return sorted(after, key=order_key)[:limit]

    return Source


def _post_recording(
    calls: list[tuple[str, dict[str, Any]]], *, flow_id: str = "flow-id"
) -> Any:
    async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, body))
        return (
            {"items": [{"id": "deployment-id"}]}
            if url.endswith("/filter")
            else {"id": flow_id}
        )

    return post


# 2026-09-03 01:00 UTC == 10:00 KST: kr/us markets' sessions are irrelevant for
# crypto rows and 10:00 KST sits outside every crypto rep window (02:20/08:20/
# 14:20/20:20 + 30 minutes), so eligible crypto events reach the cap gate.
NOW_OPEN = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    watches: list[dict[str, Any]] | None = None,
    fills: list[Any] | None = None,
    position: Any = ("0.1", 1),
    kick_enabled: bool = True,
    state_extra: dict[str, Any] | None = None,
    now: datetime = NOW_OPEN,
    dry_run: bool = False,
    cap: int = 2,
    cooldown: int = 0,
    write_state: bool = True,
    seed_watch_cursor: bool = True,
    watch_source: type | None = None,
    post: Any = None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]], type]:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> Any:
            return fills or []

        async def position_before_fill(self, **_kwargs: object) -> Any:
            if isinstance(position, Exception):
                raise position
            from decimal import Decimal as _D

            return _D(position[0]), position[1]

        async def max_ledger_id(self) -> int:
            return max((int(row["ledger_id"]) for row in (fills or [])), default=0)

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    context = _stub_context()
    monkeypatch.setattr(handoff_service, "SessionContextService", context)
    monkeypatch.setattr(
        handoff_service,
        "DbWatchKickSource",
        watch_source or _watch_source_cls(list(watches or [])),
    )

    state: dict[str, Any] = {
        "version": 1,
        "watermark": 0,
        "seen": {},
        "cooldowns": {},
    }
    if seed_watch_cursor:
        state["watch_kick_watermark"] = 0
        state["watch_kick_delivered_at"] = None
    if state_extra:
        state.update(state_extra)
    if write_state:
        (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    calls: list[tuple[str, dict[str, Any]]] = []
    runner = FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=kick_enabled,
            kick_cooldown_seconds=cooldown,
            kick_daily_cap=cap,
            prefect_api_url="http://prefect",
            kick_deployments={
                "crypto": "crypto-deployment",
                "kr": "kr-deployment",
            },
            dry_run=dry_run,
        ),
        now=lambda: now,
        http_post=post or _post_recording(calls),
    )
    outcome = asyncio.run(runner.run(_Db()))
    outcome["_context"] = context
    return outcome, calls, context


def _create_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [body for url, body in calls if url.endswith("create_flow_run")]


def test_eligible_watch_event_kicks_and_records_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[_watch(7)])
    assert outcome["watch_kicked"] == 1
    (decision,) = outcome["watch_decisions"]
    assert decision["event_id"] == 7
    assert decision["market"] == "crypto"
    assert decision["symbol"] == "BTC"
    assert decision["action_mode"] == "approval_required"
    assert decision["class"] == "kick"
    assert decision["reason"] == "action_side"
    assert decision["filter"] == "action_side"
    assert decision["flow_run_id"] == "flow-id"
    (create,) = _create_calls(calls)
    assert create["parameters"]["rep"] == "crypto-1420"  # next rep after 10:00 KST
    assert create["parameters"]["date_tag"] == "20260903-watch7"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["kick_days"]["crypto"] == {"date": "20260903", "count": 1}
    assert state["cooldowns"]["crypto"]
    # the kick cursor advanced past the processed event
    assert state["watch_kick_watermark"] == 7


def test_buy_review_watch_event_kicks() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent="buy_review", alert_max_action=None),
        None,
        tradable=True,
    )
    assert verdict.eligible is True


def test_notify_only_watch_event_is_bundled_but_never_kicked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(3, action_mode="notify_only", intent="buy_review")],
    )
    assert outcome["watch_kicked"] == 0
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "action_mode_notify_only"
    assert calls == []
    state = json.loads((tmp_path / "state.json").read_text())
    # the cursor still advanced — the event is resolved, not wedged
    assert state["watch_kick_watermark"] == 3


def test_market_closed_watch_event_is_queue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-09-03 12:00 UTC == 21:00 KST — KR regular session is closed
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(4, market="kr", intent="buy_review")],
        now=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "market_closed"
    assert calls == []


def test_watch_event_inside_a_rep_window_is_queue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 05:25 UTC == 14:25 KST sits inside the crypto-1420 30-minute rep window
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(5)],
        now=datetime(2026, 9, 3, 5, 25, tzinfo=UTC),
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "rep_window"
    assert calls == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state.get("kick_days", {}).get("crypto", {}).get("count", 0) == 0


def test_missing_max_action_falls_back_to_queue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(6, intent="sell_review", alert_id=None, alert_max_action=None)],
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "max_action_unavailable"
    assert calls == []


# --- shared #825 daily cap and cooldown ----------------------------------------


def test_watch_kicks_share_the_fill_daily_cap_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # two fill kicks already consumed today — the third (watch) must cap out
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(8)],
        state_extra={"kick_days": {"crypto": {"date": "20260903", "count": 2}}},
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "capped"
    assert decision["reason"] == "daily_cap"
    assert outcome["watch_kicked"] == 0
    assert calls == []


def test_fill_kick_and_watch_kick_share_one_cap_in_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC2 critical path: cap 1, one kick-eligible fill plus one kick-eligible
    # watch event in the same poll — only the fill may kick; the watch caps
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(11)],
        watches=[_watch(12)],
        cap=1,
    )
    assert outcome["kicked"] == 1
    assert outcome["watch_kicked"] == 0
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "capped"
    assert decision["reason"] == "daily_cap"
    assert len(_create_calls(calls)) == 1  # exactly one flow run was created
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["kick_days"]["crypto"]["count"] == 1


def test_watch_kick_consumes_the_cap_for_later_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # reverse direction: two watch kicks saturate the shared cap, then a fill
    # in the next poll sees count == cap and caps out
    outcome, _, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(20, symbol="AAA"),
            _watch(21, symbol="BBB"),
            _watch(22, symbol="CCC"),
        ],
    )
    assert outcome["watch_kicked"] == 2
    classes = {
        entry["event_id"]: entry["class"] for entry in outcome["watch_decisions"]
    }
    assert classes == {20: "kick", 21: "kick", 22: "capped"}

    # next poll: a fill kick must now cap against the watch-consumed counter
    outcome2, calls2, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(30)],
        watches=[],
        write_state=False,
    )
    assert outcome2["kicked"] == 0
    (decision,) = outcome2["decisions"]
    assert decision["class"] == "capped"
    assert decision["reason"] == "daily_cap"
    assert calls2 == []


def test_watch_kicks_share_the_fill_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(40)],
        cooldown=3600,
        state_extra={"cooldowns": {"crypto": NOW_OPEN.timestamp() - 100}},
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "cooldown"
    assert calls == []


def test_a_watch_kick_cools_down_the_next_fill_kick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=[_watch(50)], cooldown=3600)
    assert outcome["watch_kicked"] == 1
    # a fill arriving within the cooldown window must not kick
    outcome2, calls2, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(51)],
        watches=[],
        cooldown=3600,
        now=datetime(2026, 9, 3, 1, 30, tzinfo=UTC),
        write_state=False,
    )
    assert outcome2["kicked"] == 0
    (decision,) = outcome2["decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "cooldown"
    assert calls2 == []


# --- same-symbol ladder grouping ----------------------------------------------


def test_ladder_fires_of_one_symbol_are_one_kick_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # three ladder-level alerts for BTC fire in the same poll — distinct
    # idempotency keys (distinct bundle lines), one kick candidate
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(60, idempotency_key="a-btc:2026-09-03:1", threshold="100"),
            _watch(61, idempotency_key="a-btc:2026-09-03:2", threshold="105"),
            _watch(62, idempotency_key="a-btc:2026-09-03:3", threshold="110"),
        ],
    )
    assert outcome["watch_kicked"] == 1
    assert len(_create_calls(calls)) == 1
    reasons = {
        entry["event_id"]: (entry["class"], entry["reason"])
        for entry in outcome["watch_decisions"]
    }
    assert reasons[60] == ("kick", "action_side")
    assert reasons[61] == ("queue_only", "ladder_grouped")
    assert reasons[62] == ("queue_only", "ladder_grouped")


def test_ladder_group_uses_the_first_eligible_event_as_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(70, action_mode="notify_only"),
            _watch(71, intent="buy_review", alert_max_action=None),
            _watch(72),
        ],
    )
    assert outcome["watch_kicked"] == 1
    reasons = {
        entry["event_id"]: (entry["class"], entry["reason"])
        for entry in outcome["watch_decisions"]
    }
    assert reasons[70] == ("queue_only", "action_mode_notify_only")
    assert reasons[71] == ("kick", "buy_review")
    assert reasons[72] == ("queue_only", "ladder_grouped")
    assert len(_create_calls(calls)) == 1


def test_ladder_fires_across_polls_stay_bounded_by_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=[_watch(80)], cooldown=3600)
    assert outcome["watch_kicked"] == 1
    # a second ladder rung of the same symbol delivered in the next poll hits
    # the shared market cooldown, not another kick
    outcome2, calls2, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(81, idempotency_key="a-btc:2026-09-03:2")],
        cooldown=3600,
        now=datetime(2026, 9, 3, 1, 30, tzinfo=UTC),
        write_state=False,
    )
    (decision,) = outcome2["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "cooldown"
    assert calls2 == []


def test_different_symbols_each_get_a_candidate_under_the_market_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(90, symbol="BTC"),
            _watch(91, symbol="ETH"),
            _watch(92, symbol="SOL"),
        ],
    )
    # per-market cap 2: BTC and ETH kick, the third candidate caps
    assert outcome["watch_kicked"] == 2
    assert len(_create_calls(calls)) == 2
    reasons = {
        entry["event_id"]: entry["class"] for entry in outcome["watch_decisions"]
    }
    assert reasons == {90: "kick", 91: "kick", 92: "capped"}


def test_market_groups_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10:00 KST — KR open and outside every rep window; crypto always open
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(95, market="kr", symbol="005930"),
            _watch(96, market="crypto", symbol="BTC"),
        ],
        now=datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
    )
    assert outcome["watch_kicked"] == 2
    assert len(_create_calls(calls)) == 2


# --- cursor, seeding, dry-run, default-off -------------------------------------


def test_install_boundary_seeds_ledger_and_watch_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no state.json at all — a fresh install seeds BOTH watermarks to their
    # current high-water marks and returns without processing anything
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(200)],
        watches=[_watch(201)],
        write_state=False,
    )
    assert outcome["watch_kicked"] == 0
    assert outcome["watch_decisions"] == []
    assert outcome["durable"] == 0
    assert calls == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watermark"] == 200
    assert state["watch_kick_watermark"] == 201
    assert state["watch_kick_delivered_at"] == "2026-09-03T00:55:00+00:00"


def test_fresh_state_seeds_the_watch_cursor_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # state without watch_kick_watermark = upgrade onto a pre-kick state file:
    # it seeds to the delivered high-water mark and kicks nothing
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(100), _watch(101)],
        seed_watch_cursor=False,
    )
    assert outcome["watch_kicked"] == 0
    assert outcome["watch_decisions"] == []
    assert calls == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 101
    # the next poll then processes new events normally
    outcome2, _, _ = _run(
        tmp_path, monkeypatch, watches=[_watch(102)], write_state=False
    )
    assert outcome2["watch_kicked"] == 1


def test_watch_cursor_does_not_skip_a_lower_id_delivered_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # event 11 delivered first; event 10 delivered one minute later must still
    # be classified (delivery-order cursor, not id order) — distinct symbols
    # so both are their own kick candidate
    rows = [
        _watch(11, delivered_at="2026-09-03T00:55:00+00:00"),
        _watch(10, symbol="ETH", delivered_at="2026-09-03T00:56:00+00:00"),
    ]
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=rows)
    assert outcome["watch_kicked"] == 2
    state = json.loads((tmp_path / "state.json").read_text())
    # cursor rests at the later delivery position, not the higher id
    assert state["watch_kick_watermark"] == 10
    assert state["watch_kick_delivered_at"] == "2026-09-03T00:56:00+00:00"


def test_dry_run_records_preview_decisions_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(110)],
        dry_run=True,
    )
    assert outcome["watch_kicked"] == 0
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "kick"  # would-be verdict, not a real kick
    assert decision["dry_run"] is True
    assert decision["flow_run_id"] is None
    assert calls == []
    persisted = json.loads(state_path.read_text())
    assert persisted["watch_kick_watermark"] == 0
    assert "kick_days" not in persisted
    assert persisted["cooldowns"] == {}


def test_kick_disabled_leaves_state_and_output_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingSource:
        def __init__(self, _db: object) -> None:
            raise AssertionError("watch source must not even be built")

    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> Any:
            return [_fill(120)]

        async def position_before_fill(self, **_kwargs: object) -> Any:
            raise AssertionError("position read must be skipped when off")

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    context = _stub_context()
    monkeypatch.setattr(handoff_service, "SessionContextService", context)
    monkeypatch.setattr(handoff_service, "DbWatchKickSource", ExplodingSource)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {"version": 1, "watermark": 0, "seen": {}, "cooldowns": {}},
            sort_keys=True,
        )
        + "\n"
    )
    outcome = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(state_dir=tmp_path, kick_enabled=False),
            now=lambda: NOW_OPEN,
        ).run(_Db())
    )
    # AC6: exactly the pre-filter outcome shape, no watch state keys at all
    assert set(outcome) == {"durable", "pushed", "kicked", "duplicate", "fallback"}
    persisted = json.loads(state_path.read_text())
    assert "watch_kick_watermark" not in persisted
    assert "watch_kick_delivered_at" not in persisted
    assert "kick_days" not in persisted


def test_watch_read_failure_never_wedges_the_fill_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingSource:
        def __init__(self, _db: object) -> None:
            pass

        async def list_after(self, *_args: object, **_kwargs: object) -> Any:
            raise RuntimeError("synthetic watch read failure")

    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(130)],
        watch_source=FailingSource,
    )
    assert outcome["watch_errors"] == ["watch_read_failed"]
    assert outcome["watch_decisions"] == []
    # fills still ran their full path — the fill kicked normally
    assert outcome["kicked"] == 1
    assert len(_create_calls(calls)) == 1
    # the watch cursor is untouched so the events retry next poll
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 0


def test_watch_seed_failure_is_recorded_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingWatermark:
        def __init__(self, _db: object) -> None:
            pass

        async def high_watermark(self) -> WatchKickCursor:
            raise RuntimeError("synthetic seed failure")

    outcome, _, _ = _run(
        tmp_path,
        monkeypatch,
        seed_watch_cursor=False,
        watch_source=FailingWatermark,
    )
    assert outcome["watch_errors"] == ["watch_high_watermark_failed"]
    state = json.loads((tmp_path / "state.json").read_text())
    # cursor keys stay absent so the next poll retries the seed
    assert "watch_kick_watermark" not in state


def test_malformed_watch_row_is_recorded_not_wedged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _watch(140)
    bad["event_id"] = "not-an-int"
    outcome, _, _ = _run(
        tmp_path, monkeypatch, watches=[bad, _watch(141, symbol="ETH")]
    )
    assert outcome["watch_errors"] == ["event_malformed"]
    # the well-formed row still processed and the cursor advanced past it
    assert outcome["watch_kicked"] == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 141


def test_malformed_row_at_the_tail_does_not_wedge_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _watch(150, delivered_at="2026-09-03T00:56:00+00:00")
    bad["event_id"] = "not-an-int"
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=[_watch(151), bad])
    assert outcome["watch_errors"] == ["event_malformed"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 151


def test_missing_delivered_at_is_malformed_not_wedged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a row that cannot be cursor-tracked must be dropped, not processed —
    # processing it would stall the cursor and re-gate every later row
    # against the shared cap on every poll
    bad = _watch(165)
    bad["delivered_at"] = None
    outcome, _, _ = _run(
        tmp_path, monkeypatch, watches=[bad, _watch(166, symbol="ETH")]
    )
    assert outcome["watch_errors"] == ["event_malformed"]
    assert outcome["watch_kicked"] == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 166


def test_naive_delivered_at_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _watch(167)
    bad["delivered_at"] = "2026-09-03T00:55:00"  # no tz — unorderable cursor
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=[bad])
    assert outcome["watch_errors"] == ["event_malformed"]
    assert outcome["watch_kicked"] == 0
    assert outcome["watch_decisions"] == []


def test_crash_replay_of_a_kicked_event_cannot_re_kick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a crash between the gate's reservation save and the cursor advance
    # refetches the kicked event; the seen mark dedupes the second attempt —
    # even with the cooldown disabled
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(175)],
        cooldown=0,
        state_extra={"seen": {"watchkick:175": NOW_OPEN.timestamp() - 10}},
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "already_kicked"
    assert outcome["watch_kicked"] == 0
    assert calls == []


def test_classifier_exception_is_queue_only_and_the_row_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_event: Any, _max: Any, *, tradable: bool) -> Any:
        raise RuntimeError("synthetic classifier failure")

    monkeypatch.setattr(handoff_service, "classify_watch_for_kick", boom)
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[_watch(160)])
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "classification_failed"
    assert calls == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["watch_kick_watermark"] == 160


def test_prefect_exception_keeps_the_reserved_kick_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an ambiguous create_flow_run failure keeps the reservation — the kick
    # may exist server-side, so the cap slot must not be released
    async def flaky(url: str, body: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/filter"):
            return {"items": [{"id": "deployment-id"}]}
        raise RuntimeError("prefect create failed")

    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[_watch(170)], post=flaky)
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "kick_error"
    assert outcome["watch_kicked"] == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["kick_days"]["crypto"]["count"] == 1
    assert state["cooldowns"]["crypto"]


def test_prefect_no_run_id_releases_the_reserved_kick_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a definitive response without a flow-run id is the only rollback path
    async def no_id(url: str, body: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/filter"):
            return {"items": [{"id": "deployment-id"}]}
        return {}

    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[_watch(171)], post=no_id)
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "prefect_no_run_id"
    assert outcome["watch_kicked"] == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state.get("kick_days", {}).get("crypto", {}).get("count", 0) == 0
    assert "crypto" not in state["cooldowns"]


def test_missing_deployment_mapping_is_queue_only_config_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> Any:
            return []

        async def max_ledger_id(self) -> int:
            return 0

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    context = _stub_context()
    monkeypatch.setattr(handoff_service, "SessionContextService", context)
    monkeypatch.setattr(
        handoff_service, "DbWatchKickSource", _watch_source_cls([_watch(180)])
    )
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "watermark": 0,
                "seen": {},
                "cooldowns": {},
                "watch_kick_watermark": 0,
                "watch_kick_delivered_at": None,
            }
        )
    )
    outcome = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(
                state_dir=tmp_path,
                kick_enabled=True,
                kick_deployments={"kr": "kr-deployment"},  # no crypto mapping
                prefect_api_url="http://prefect",
            ),
            now=lambda: NOW_OPEN,
            http_post=lambda *_a, **_k: pytest.fail("no HTTP expected"),
        ).run(_Db())
    )
    (decision,) = outcome["watch_decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "deployment_unmapped"


# --- AC4: no new path to the watch re-judgement spawner -------------------------


def test_no_new_path_to_the_repricing_spawner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC4: watch kicks reuse the existing #825 Prefect kickoff surface — the
    # watch_trigger_repricing package must not be imported or called by any
    # path this change adds.
    for module in (handoff_service, watch_kick_module):
        source = inspect.getsource(module)
        assert "watch_trigger_repricing" not in source
    # scrub any entry an earlier test in the session may have imported, so a
    # positive result can only mean this code path re-imported it
    for name in [
        module_name
        for module_name in sys.modules
        if "watch_trigger_repricing" in module_name
    ]:
        monkeypatch.delitem(sys.modules, name)
    outcome, _, _ = _run(tmp_path, monkeypatch, watches=[_watch(190)])
    assert outcome["watch_kicked"] == 1
    assert not any("watch_trigger_repricing" in name for name in sys.modules)


# --- DbWatchKickSource against a real (throwaway) PostgreSQL --------------------
#
# These exercise the actual LEFT JOIN projection and cursor query — the fake
# source above cannot prove the ORM layer returns what the classifier needs.


def _alert_row(*, suffix: str, max_action: Any) -> Any:
    from app.models.investment_reports import InvestmentWatchAlert

    return InvestmentWatchAlert(
        idempotency_key=f"t865-alert-{suffix}",
        market="crypto",
        target_kind="asset",
        symbol="BTC",
        metric="price",
        operator="above",
        threshold=100,
        threshold_key="t1",
        intent="sell_review",
        action_mode="approval_required",
        rationale="t865 fixture",
        max_action=max_action,
        valid_until=datetime(2026, 9, 10, tzinfo=UTC),
        status="active",
    )


def _event_row(*, suffix: str, alert_id: int | None, delivered_at: datetime) -> Any:
    from app.models.investment_reports import InvestmentWatchEvent

    return InvestmentWatchEvent(
        idempotency_key=f"t865-event-{suffix}",
        alert_id=alert_id,
        market="crypto",
        target_kind="asset",
        symbol="BTC",
        metric="price",
        operator="above",
        threshold=100,
        threshold_key="t1",
        intent="sell_review",
        action_mode="approval_required",
        outcome="notified",
        correlation_id="t865-corr",
        kst_date="2026-09-03",
        delivery_status="delivered",
        delivered_at=delivered_at,
    )


@pytest.mark.asyncio
async def test_db_source_joins_alert_max_action(session: Any) -> None:
    alert = _alert_row(suffix="join", max_action={"side": "sell", "quantity": "0.5"})
    session.add(alert)
    await session.flush()
    session.add(
        _event_row(
            suffix="join",
            alert_id=alert.id,
            delivered_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
    )
    await session.flush()

    source = watch_kick_module.DbWatchKickSource(session)
    high = await source.high_watermark()
    assert high.delivered_at is not None
    rows = await source.list_after(WatchKickCursor(None, high.event_id - 1), limit=10)
    (row,) = [row for row in rows if row["idempotency_key"] == "t865-event-join"]
    assert row["alert_max_action"] == {"side": "sell", "quantity": "0.5"}
    # the projection is what the classifier needs to prove a side
    verdict = classify_watch_for_kick(row, row["alert_max_action"], tradable=True)
    assert verdict.eligible is True
    assert verdict.reason == "action_side"


@pytest.mark.asyncio
async def test_db_source_deleted_alert_projects_none_and_fails_closed(
    session: Any,
) -> None:
    # alert_id=None mirrors ON DELETE SET NULL — the event snapshot survives
    # but no side can be proven, so the verdict must be queue-only
    session.add(
        _event_row(
            suffix="orphan",
            alert_id=None,
            delivered_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
    )
    await session.flush()

    source = watch_kick_module.DbWatchKickSource(session)
    high = await source.high_watermark()
    rows = await source.list_after(WatchKickCursor(None, high.event_id - 1), limit=10)
    (row,) = [row for row in rows if row["idempotency_key"] == "t865-event-orphan"]
    assert row["alert_id"] is None
    assert row["alert_max_action"] is None
    verdict = classify_watch_for_kick(row, row["alert_max_action"], tradable=True)
    assert verdict.eligible is False
    assert verdict.reason == "max_action_unavailable"


@pytest.mark.asyncio
async def test_db_source_cursor_excludes_pending_and_respects_delivery_order(
    session: Any,
) -> None:
    pending = _event_row(suffix="pending", alert_id=None, delivered_at=None)
    pending.delivery_status = "pending"
    session.add(pending)
    first = _event_row(
        suffix="first",
        alert_id=None,
        delivered_at=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )
    second = _event_row(
        suffix="second",
        alert_id=None,
        delivered_at=datetime(2026, 9, 3, 0, 1, tzinfo=UTC),
    )
    session.add_all([first, second])
    await session.flush()

    source = watch_kick_module.DbWatchKickSource(session)
    high = await source.high_watermark()
    assert high.event_id == second.id
    # a cursor mid-window sees only the later delivery — never the pending row
    cursor = WatchKickCursor(datetime(2026, 9, 3, 0, 0, 30, tzinfo=UTC), 0)
    keys = [row["idempotency_key"] for row in await source.list_after(cursor, limit=10)]
    assert "t865-event-second" in keys
    assert "t865-event-pending" not in keys
    assert "t865-event-first" not in keys


# --- tester round-1 findings: regression tests --------------------------------
#
# Each test names the t865-verify-r1 counterexample it pins down.


@pytest.mark.parametrize(
    "mode",
    [
        " Approval_Required ",
        "APPROVAL_REQUIRED",
        "approval_required ",
        "Approval_Required",
    ],
)
def test_noncanonical_approval_mode_never_authorizes_a_kick(mode: str) -> None:
    # r1 finding 1: normalization must not launder corrupt action_mode into
    # an authorization — only the canonical spelling kicks.
    verdict = classify_watch_for_kick(
        _watch(1, action_mode=mode, intent="buy_review"),
        {"side": "buy"},
        tradable=True,
    )
    assert verdict.eligible is False
    assert verdict.reason == "action_mode_malformed"


def test_noncanonical_known_mode_spelling_is_malformed_not_named() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, action_mode=" Notify_Only "), {"side": "buy"}, tradable=True
    )
    assert verdict.eligible is False
    assert verdict.reason == "action_mode_malformed"


def test_noncanonical_buy_review_intent_never_authorizes() -> None:
    verdict = classify_watch_for_kick(
        _watch(1, intent=" Buy_Review "), None, tradable=True
    )
    assert verdict.eligible is False
    assert verdict.reason == "max_action_unavailable"


def test_non_datetime_clock_input_is_never_tradable_for_crypto() -> None:
    # r1 finding 5: crypto must fail closed on garbage clock input too.
    assert is_tradable_now("crypto", "not-a-datetime") is False  # type: ignore[arg-type]
    assert is_tradable_now("crypto", None) is False  # type: ignore[arg-type]
    assert is_tradable_now("crypto", 12345) is False  # type: ignore[arg-type]


def test_naive_clock_input_fails_closed() -> None:
    naive = datetime(2026, 9, 3, 1, 0)
    assert is_tradable_now("crypto", naive) is False
    assert is_tradable_now("kr", naive) is False


@pytest.mark.parametrize(
    "state_extra",
    [
        {"watch_kick_watermark": -1, "watch_kick_delivered_at": None},
        {"watch_kick_watermark": "not-an-int", "watch_kick_delivered_at": None},
        {"watch_kick_watermark": 5, "watch_kick_delivered_at": "not-a-date"},
        # naive cursor timestamp is ambiguous — corrupt, not resumable
        {"watch_kick_watermark": 5, "watch_kick_delivered_at": "2026-09-03T00:55:00"},
    ],
)
def test_corrupt_cursor_reseeds_and_never_replays_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_extra: dict[str, Any]
) -> None:
    # r1 finding 2: a corrupt persisted cursor must not be fed to the query —
    # id > -1 would replay the whole delivered backlog as kick candidates.
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(10)],
        state_extra=state_extra,
    )
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "watch_cursor_corrupt" in outcome["watch_errors"]
    persisted = json.loads((tmp_path / "state.json").read_text())
    # reseeded to the delivered high-water mark — backlog stays history
    assert persisted["watch_kick_watermark"] == 10
    assert persisted["watch_kick_delivered_at"] == "2026-09-03T00:55:00+00:00"


def test_stale_event_older_than_dedupe_window_never_kicks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # r1 finding 3: the crash-replay shape — seen mark expired (>24h), cursor
    # never advanced.  The event is now stale and can never kick again.
    old = _watch(30, delivered_at="2026-09-01T23:30:00+00:00")
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[old])
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    (record,) = outcome["watch_decisions"]
    assert record["class"] == "queue_only"
    assert record["reason"] == "stale_event"


def test_delivered_exactly_at_the_dedupe_window_edge_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = _watch(31, delivered_at="2026-09-02T01:00:00+00:00")  # exactly -24h
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[edge])
    assert _create_calls(calls) == []
    assert outcome["watch_decisions"][0]["reason"] == "stale_event"


def test_event_delivered_inside_the_window_still_kicks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = _watch(32, delivered_at="2026-09-02T01:01:00+00:00")  # -23h59m
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[fresh])
    assert outcome["watch_kicked"] == 1
    assert len(_create_calls(calls)) == 1


def test_interleaved_symbols_give_the_slot_to_first_eligible_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # r1 finding 4: grouping must not reorder the kick competition — the
    # first ELIGIBLE row in global delivery order takes the slot.
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[
            _watch(
                40,
                symbol="AAA",
                action_mode="notify_only",
                delivered_at="2026-09-03T00:55:00+00:00",
            ),
            _watch(41, symbol="BBB", delivered_at="2026-09-03T00:56:00+00:00"),
            _watch(42, symbol="AAA", delivered_at="2026-09-03T00:57:00+00:00"),
        ],
        cap=1,
    )
    assert outcome["watch_kicked"] == 1
    (create,) = _create_calls(calls)
    assert create["parameters"]["date_tag"] == "20260903-watch41"
    reasons = {
        entry["event_id"]: (entry["class"], entry["reason"])
        for entry in outcome["watch_decisions"]
    }
    assert reasons[40] == ("queue_only", "action_mode_notify_only")
    assert reasons[41] == ("kick", "action_side")
    assert reasons[42] == ("capped", "daily_cap")


@pytest.mark.parametrize(
    "watermark",
    [0.5, 1.9, True, "0.5", [3], {"x": 1}, -2],
)
def test_noninteger_or_negative_watermark_is_corrupt_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watermark: Any
) -> None:
    # r2 finding 1: a fractional/garbage persisted watermark must not be
    # int()-truncated into a valid replay point — it is corrupt state and
    # reseeds to the delivered high-water mark instead of kicking backlog.
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(10)],
        state_extra={
            "watch_kick_watermark": watermark,
            "watch_kick_delivered_at": None,
        },
    )
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "watch_cursor_corrupt" in outcome["watch_errors"]
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["watch_kick_watermark"] == 10


@pytest.mark.parametrize("event_id", [830.5, 12.0, True])
def test_noninteger_event_id_is_malformed_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_id: Any
) -> None:
    # r2 finding 2: a fractional event id must not truncate into a real row
    # identity — it is dropped as event_malformed and never kicks.
    bad = _watch(900)
    bad["event_id"] = event_id
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[bad])
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "event_malformed" in outcome["watch_errors"]


@pytest.mark.parametrize("watermark", ["²", "٣", 2**63, -(2**63), 2**64])
def test_unparseable_or_out_of_bigint_watermark_is_corrupt_and_reseeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watermark: Any
) -> None:
    # r3 finding 1: str.isdigit() accepts characters such as superscript
    # two that int() cannot parse — the crash escaped the cursor parser and
    # aborted the whole pass (a pending fill is still processed below).
    # r3 finding 2: a watermark outside signed BIGINT range overflows the
    # source query bind — corrupt state reseeds instead of wedging.
    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        fills=[_fill(832)],
        position=("0.1", 0),  # unproven: fill stays queue-only, no prefect call
        watches=[_watch(833)],
        state_extra={
            "watch_kick_watermark": watermark,
            "watch_kick_delivered_at": None,
        },
    )
    assert outcome["durable"] == 1
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "watch_cursor_corrupt" in outcome["watch_errors"]
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["watch_kick_watermark"] == 833


def test_out_of_bigint_watermark_never_reaches_list_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # r3 finding 2 wedge check: the corrupt watermark must be rejected
    # before any list_after attempt — a source that raises on the query
    # must not surface watch_read_failed, and the cursor reseeds to the
    # delivered high-water mark.
    class OverflowingSource:
        def __init__(self, _db: object) -> None:
            pass

        async def high_watermark(self) -> WatchKickCursor:
            return WatchKickCursor(
                datetime.fromisoformat("2026-09-03T00:55:00+00:00"), 834
            )

        async def list_after(self, _cursor: Any, *, limit: int) -> Any:
            raise OverflowError("BIGINT out of range")

    outcome, calls, _ = _run(
        tmp_path,
        monkeypatch,
        watches=[_watch(834)],
        state_extra={
            "watch_kick_watermark": 2**63,
            "watch_kick_delivered_at": None,
        },
        watch_source=OverflowingSource,
    )
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "watch_cursor_corrupt" in outcome["watch_errors"]
    assert "watch_read_failed" not in outcome["watch_errors"]
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["watch_kick_watermark"] == 834


@pytest.mark.parametrize("event_id", [2**63, 2**64, "²"])
def test_unparseable_or_out_of_bigint_event_id_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_id: Any
) -> None:
    # r3 finding 2 (clean filter): an event id outside signed BIGINT range
    # or unparsable by int() is dropped as event_malformed — it must not
    # kick, poison cursor advancement, or wedge the pass.
    bad = _watch(900)
    bad["event_id"] = event_id
    outcome, calls, _ = _run(tmp_path, monkeypatch, watches=[bad])
    assert outcome["watch_kicked"] == 0
    assert _create_calls(calls) == []
    assert "event_malformed" in outcome["watch_errors"]
