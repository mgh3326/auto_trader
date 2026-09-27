"""Task #825 kick-priority filter: classification, cap, and fallback tests.

Every fill must stay queued as a ``session_context`` open question; only the
Prefect kick decision changes.  Tests here are fixture-only — no real Prefect,
no panes, no broker — and each boundary assertion is written so the matching
one-sided mutant (``>`` vs ``>=``, off-by-one cap, wrong day boundary) fails.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.fill_event_handoff import service as handoff_service
from app.services.fill_event_handoff.kick_filter import (
    FillPositionFacts,
    KickVerdict,
    classify_fill_for_kick,
    classify_without_position,
)
from app.services.fill_event_handoff.service import FillHandoffRunner, HandoffConfig
from app.services.fill_event_handoff.state import HandoffState


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


def _facts(qty_before: str, rows_before: int = 1) -> FillPositionFacts:
    return FillPositionFacts(qty_before=Decimal(qty_before), rows_before=rows_before)


# --- pure classifier: kick classes -------------------------------------------


def test_sell_that_empties_the_position_is_a_full_exit_kick() -> None:
    verdict = classify_fill_for_kick(_fill(1), _facts("0.1"))
    assert verdict.eligible is True
    assert verdict.reason == "sell_full_exit"
    assert verdict.position_before == Decimal("0.1")
    assert verdict.position_after == Decimal("0")


def test_sell_overshooting_the_position_is_still_a_full_exit() -> None:
    verdict = classify_fill_for_kick(_fill(1, filled_qty="0.2"), _facts("0.1"))
    assert verdict.eligible is True
    assert verdict.reason == "sell_full_exit"
    assert verdict.position_after == Decimal("-0.1")


def test_sell_short_of_full_exit_is_queue_only() -> None:
    # mutant guard: ``position_after <= 0`` must not become ``< 0`` — a sell
    # leaving any positive residual is not a full exit
    verdict = classify_fill_for_kick(_fill(1, filled_qty="0.01"), _facts("0.1"))
    assert verdict.eligible is False
    assert verdict.reason == "sell_partial_below_25pct"
    assert verdict.position_after == Decimal("0.09")


def test_buy_after_a_proven_flat_position_is_a_new_position_kick() -> None:
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_qty="0.05", filled_notional="60000"),
        _facts("0", rows_before=3),
    )
    assert verdict.eligible is True
    assert verdict.reason == "buy_new_position"
    assert verdict.position_after == Decimal("0.05")


def test_buy_at_exactly_25_percent_of_position_kicks() -> None:
    # mutant guard: ``qty >= qty_before * fraction`` must not become ``>``
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_qty="25", filled_notional="2500000"),
        _facts("100"),
    )
    assert verdict.eligible is True
    assert verdict.reason == "partial_fill_ge_25pct"


def test_buy_just_below_25_percent_is_queue_only() -> None:
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_qty="24.9", filled_notional="2490000"),
        _facts("100"),
    )
    assert verdict.eligible is False
    assert verdict.reason == "buy_add_below_25pct"


def test_sell_at_exactly_25_percent_that_leaves_position_is_a_partial_kick() -> None:
    # mutant guard: a 25% partial sell is eligible even though the position
    # does not reach zero
    verdict = classify_fill_for_kick(
        _fill(1, side="sell", filled_qty="25"),
        _facts("100"),
    )
    assert verdict.eligible is True
    assert verdict.reason == "partial_fill_ge_25pct"
    assert verdict.position_after == Decimal("75")


def test_sell_partial_below_25_percent_is_queue_only() -> None:
    verdict = classify_fill_for_kick(
        _fill(1, side="sell", filled_qty="24.9"),
        _facts("100"),
    )
    assert verdict.eligible is False
    assert verdict.reason == "sell_partial_below_25pct"


# --- pure classifier: position-free queue-only classes ------------------------


@pytest.mark.parametrize("symbol", ["SGOV", "BIL", "459580", "357870", "sgov"])
def test_parking_symbols_are_queue_only_without_a_position_read(symbol) -> None:
    verdict = classify_without_position(
        _fill(1, side="buy", symbol=symbol, filled_notional="999999999")
    )
    assert verdict is not None
    assert verdict.reason == "parking_etf"
    assert verdict.eligible is False


def test_small_dca_buy_below_the_currency_floor_is_queue_only() -> None:
    verdict = classify_without_position(
        _fill(1, side="buy", filled_notional="4999", currency="KRW")
    )
    assert verdict is not None
    assert verdict.reason == "small_dca_buy"


def test_buy_at_exactly_the_floor_is_not_small_dca() -> None:
    # mutant guard: ``notional < threshold`` must not become ``<=``
    verdict = classify_without_position(
        _fill(1, side="buy", filled_notional="5000", currency="KRW")
    )
    assert verdict is None


def test_small_dca_floor_is_per_currency() -> None:
    verdict = classify_without_position(
        _fill(1, side="buy", filled_notional="4.99", currency="USD")
    )
    assert verdict is not None
    assert verdict.reason == "small_dca_buy"
    assert (
        classify_without_position(
            _fill(1, side="buy", filled_notional="5", currency="USD")
        )
        is None
    )


def test_unmapped_currency_never_trips_the_small_dca_floor() -> None:
    assert (
        classify_without_position(
            _fill(1, side="buy", filled_notional="0.0001", currency="JPY")
        )
        is None
    )


# --- pure classifier: fail-closed classes -------------------------------------


def test_zero_prior_rows_is_unproven_not_flat() -> None:
    # mutant guard: rows_before == 0 must not be treated as a proven zero
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_notional="60000"), _facts("0", rows_before=0)
    )
    assert verdict.eligible is False
    assert verdict.reason == "position_unproven"


def test_missing_facts_fail_closed() -> None:
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_notional="60000"), None
    )
    assert verdict.eligible is False
    assert verdict.reason == "position_read_failed"


def test_sell_with_negative_ledger_position_is_unproven() -> None:
    verdict = classify_fill_for_kick(
        _fill(1, side="sell", filled_qty="0.1"), _facts("-0.5")
    )
    assert verdict.eligible is False
    assert verdict.reason == "position_unproven"


@pytest.mark.parametrize(
    "override",
    [
        {"market": "forex"},
        {"market": None},
        {"instrument_type": "forex"},
        {"instrument_type": "index"},
        {"instrument_type": "alien"},
        {"instrument_type": None},
    ],
)
def test_unsupported_markets_and_instruments_are_queue_only(override) -> None:
    verdict = classify_fill_for_kick(_fill(1, **override), _facts("0.1"))
    assert verdict.eligible is False
    assert verdict.reason == "unsupported_market"


def test_buy_into_a_proven_negative_position_is_unproven() -> None:
    # a ledger-proven negative balance is inconsistent, not flat — fail closed
    verdict = classify_fill_for_kick(
        _fill(1, side="buy", filled_notional="60000"), _facts("-0.5", 2)
    )
    assert verdict.eligible is False
    assert verdict.reason == "position_unproven"
    assert verdict.position_before == Decimal("-0.5")
    assert verdict.position_after == Decimal("-0.4")


def test_degenerate_quantity_magnitude_is_malformed_not_an_exception() -> None:
    # 1e9999999 overflows the Decimal context during position arithmetic —
    # the classifier must fail closed, not raise out of run()
    verdict = classify_fill_for_kick(_fill(1, filled_qty="1e9999999"), _facts("0.1"))
    assert verdict.eligible is False
    assert verdict.reason == "fill_malformed"


@pytest.mark.parametrize(
    "override",
    [
        {"side": "hold"},
        {"side": ""},
        {"filled_qty": "0"},
        {"filled_qty": "-0.1"},
        {"filled_qty": "not-a-number"},
        {"filled_qty": "NaN"},
        {"filled_qty": "Infinity"},
        {"side": "buy", "filled_notional": None},
        {"side": "buy", "filled_notional": "not-a-number"},
        {"side": "buy", "filled_notional": "NaN"},
        {"side": "buy", "filled_notional": "Infinity"},
    ],
)
def test_malformed_fills_are_queue_only(override) -> None:
    verdict = classify_fill_for_kick(_fill(1, **override), _facts("0.1"))
    assert verdict.eligible is False
    assert verdict.reason == "fill_malformed"


# --- _kick: daily cap, KST reset, cooldown interplay --------------------------


def _post_recording(calls: list[tuple[str, dict[str, Any]]]):
    async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, body))
        return (
            {"items": [{"id": "deployment-id"}]}
            if url.endswith("/filter")
            else {"id": f"flow-id-{len(calls)}"}
        )

    return post


def _kick_runner(
    tmp_path: Path,
    now: Any,
    *,
    cap: int = 2,
    cooldown: int = 0,
) -> FillHandoffRunner:
    return FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=True,
            kick_cooldown_seconds=cooldown,
            kick_daily_cap=cap,
            prefect_api_url="http://prefect",
            kick_deployments={"crypto": "crypto-deployment"},
        ),
        now=now,
        http_post=_post_recording([]),
    )


def test_daily_cap_allows_two_kicks_then_caps(tmp_path: Path) -> None:
    # 2026-09-03 01:00 UTC == 10:00 KST — outside every crypto rep window
    runner = _kick_runner(tmp_path, lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC))
    verdict = KickVerdict(True, "sell_full_exit")
    with HandoffState(tmp_path) as locked:
        first = asyncio.run(runner._kick(_fill(1), locked, verdict))
        second = asyncio.run(runner._kick(_fill(2), locked, verdict))
        third = asyncio.run(runner._kick(_fill(3), locked, verdict))
        state = locked.data
    assert first.klass == "kick" and first.flow_run_id
    assert second.klass == "kick" and second.flow_run_id
    # mutant guard: ``count >= cap`` must not become ``>`` — the third kick
    # at cap 2 is the exact boundary
    assert third.klass == "capped"
    assert third.reason == "daily_cap"
    assert third.flow_run_id is None
    assert state["kick_days"]["crypto"] == {"date": "20260903", "count": 2}


def test_daily_cap_resets_at_kst_midnight(tmp_path: Path) -> None:
    moments = iter(
        [
            datetime(2026, 9, 3, 14, 30, tzinfo=UTC),  # 23:30 KST, Sep 3
            datetime(2026, 9, 3, 15, 30, tzinfo=UTC),  # 00:30 KST, Sep 4
        ]
    )
    runner = _kick_runner(tmp_path, lambda: next(moments), cap=1)
    verdict = KickVerdict(True, "sell_full_exit")
    with HandoffState(tmp_path) as locked:
        first = asyncio.run(runner._kick(_fill(1), locked, verdict))
        assert first.klass == "kick"
        assert locked.data["kick_days"]["crypto"] == {"date": "20260903", "count": 1}
        second = asyncio.run(runner._kick(_fill(2), locked, verdict))
        # mutant guard: comparing KST dates must not reuse the prior UTC day
        assert second.klass == "kick"
        assert locked.data["kick_days"]["crypto"] == {"date": "20260904", "count": 1}


def test_zero_cap_caps_every_eligible_fill(tmp_path: Path) -> None:
    runner = _kick_runner(
        tmp_path, lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC), cap=0
    )
    with HandoffState(tmp_path) as locked:
        decision = asyncio.run(
            runner._kick(_fill(1), locked, KickVerdict(True, "sell_full_exit"))
        )
    assert decision.klass == "capped"


def test_cooldown_blocks_without_consuming_daily_cap(tmp_path: Path) -> None:
    moments = iter(
        [
            datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 3, 1, 30, tzinfo=UTC),  # inside 3600 s cooldown
            datetime(2026, 9, 3, 2, 0, 1, tzinfo=UTC),  # cooldown elapsed
        ]
    )
    runner = _kick_runner(tmp_path, lambda: next(moments), cooldown=3600)
    verdict = KickVerdict(True, "sell_full_exit")
    with HandoffState(tmp_path) as locked:
        assert asyncio.run(runner._kick(_fill(1), locked, verdict)).klass == "kick"
        cooled = asyncio.run(runner._kick(_fill(2), locked, verdict))
        assert cooled.klass == "queue_only"
        assert cooled.reason == "cooldown"
        # a cooldown deferral is not a cap consumption
        assert locked.data["kick_days"]["crypto"]["count"] == 1
        assert asyncio.run(runner._kick(_fill(3), locked, verdict)).klass == "kick"


def test_rep_window_defers_before_the_cap_is_touched(tmp_path: Path) -> None:
    # 2026-09-03 05:25 UTC == 14:25 KST — inside the crypto-1420 rep window
    runner = _kick_runner(
        tmp_path, lambda: datetime(2026, 9, 3, 5, 25, tzinfo=UTC), cap=1
    )
    with HandoffState(tmp_path) as locked:
        decision = asyncio.run(
            runner._kick(_fill(1), locked, KickVerdict(True, "sell_full_exit"))
        )
        assert decision.klass == "queue_only"
        assert decision.reason == "rep_window"
        assert (
            "kick_days" not in locked.data
            or locked.data["kick_days"]["crypto"]["count"] == 0
        )


def test_kick_count_is_persisted_before_the_prefect_call(tmp_path: Path) -> None:
    # BLOCKER regression: the reservation must be durable before issue so a
    # crash can over-count but never let the cap be exceeded across processes.
    runner = _kick_runner(tmp_path, lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC))
    verdict = KickVerdict(True, "sell_full_exit")
    with HandoffState(tmp_path) as locked:
        first = asyncio.run(runner._kick(_fill(1), locked, verdict))
        assert first.klass == "kick"
        # state.json must already hold the count — no later save is needed
        persisted = json.loads((tmp_path / "state.json").read_text())
        assert persisted["kick_days"]["crypto"]["count"] == 1
    # a new process re-reads the persisted count and hits the cap boundary
    with HandoffState(tmp_path) as locked:
        second = asyncio.run(runner._kick(_fill(2), locked, verdict))
        third = asyncio.run(runner._kick(_fill(3), locked, verdict))
        assert second.klass == "kick"
        assert third.klass == "capped"
        assert locked.data["kick_days"]["crypto"]["count"] == 2


def test_kick_without_flow_run_id_releases_the_reservation(tmp_path: Path) -> None:
    async def post(url: str, _body: dict[str, Any]) -> dict[str, Any]:
        return (
            {"items": [{"id": "deployment-id"}]}
            if url.endswith("/filter")
            else {"id": ""}
        )

    runner = FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=True,
            kick_cooldown_seconds=3600,
            kick_daily_cap=2,
            prefect_api_url="http://prefect",
            kick_deployments={"crypto": "crypto-deployment"},
        ),
        now=lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
        http_post=post,
    )
    with HandoffState(tmp_path) as locked:
        decision = asyncio.run(
            runner._kick(_fill(1), locked, KickVerdict(True, "sell_full_exit"))
        )
        assert decision.klass == "queue_only"
        assert decision.reason == "prefect_no_run_id"
        assert locked.data["kick_days"]["crypto"]["count"] == 0
        assert locked.data["cooldowns"] == {}
        persisted = json.loads((tmp_path / "state.json").read_text())
        assert persisted["kick_days"]["crypto"]["count"] == 0


def test_kick_ambiguous_failure_keeps_the_reservation(tmp_path: Path) -> None:
    async def post(url: str, _body: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/filter"):
            return {"items": [{"id": "deployment-id"}]}
        raise TimeoutError("response lost — the kick may exist server-side")

    runner = FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=True,
            kick_cooldown_seconds=3600,
            kick_daily_cap=2,
            prefect_api_url="http://prefect",
            kick_deployments={"crypto": "crypto-deployment"},
        ),
        now=lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
        http_post=post,
    )
    with HandoffState(tmp_path) as locked:
        with pytest.raises(TimeoutError):
            asyncio.run(
                runner._kick(_fill(1), locked, KickVerdict(True, "sell_full_exit"))
            )
        # the ambiguous call stays counted so a retry cannot exceed the cap
        assert locked.data["kick_days"]["crypto"]["count"] == 1


# --- _classify_fill: timestamp and read guards -------------------------------


@pytest.mark.parametrize(
    "filled_at",
    ["2026-09-03T00:00:00", "not-a-timestamp"],
)
def test_classify_fill_rejects_unusable_timestamps_without_a_read(
    tmp_path: Path, filled_at: str
) -> None:
    calls: list[dict[str, Any]] = []

    class Repo:
        async def position_before_fill(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return Decimal("0"), 2

    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path, kick_enabled=True))
    verdict = asyncio.run(runner._classify_fill(_fill(1, filled_at=filled_at), Repo()))
    assert verdict.eligible is False
    assert verdict.reason == "fill_malformed"
    # a naive or unparsable timestamp must never be coerced into a query
    assert calls == []


def test_classify_fill_survives_a_classifier_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Repo:
        async def position_before_fill(self, **_kwargs: Any) -> Any:
            return Decimal("0"), 2

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(handoff_service, "classify_fill_for_kick", boom)
    runner = FillHandoffRunner(HandoffConfig(state_dir=tmp_path, kick_enabled=True))
    verdict = asyncio.run(runner._classify_fill(_fill(1), Repo()))
    assert verdict.eligible is False
    assert verdict.reason == "classification_failed"


# --- run() integration: classification flows through the handoff -------------


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


class _Db:
    async def commit(self) -> None:
        pass


def _orm_row(**overrides: Any) -> Any:
    """An ORM-shaped ledger row for runs through the real sanitize_fill."""
    row = SimpleNamespace(
        id=1,
        broker="upbit",
        account_mode="live",
        venue="upbit_krw",
        instrument_type="crypto",
        symbol="BTC",
        raw_symbol="BTC",
        side="sell",
        filled_qty=Decimal("0.1"),
        filled_price=Decimal("100"),
        filled_notional=Decimal("10"),
        currency="KRW",
        broker_order_id="o-1",
        fill_seq=0,
        correlation_id=None,
        source="websocket",
        filled_at=datetime(2026, 9, 3, tzinfo=UTC),
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _run_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    position: tuple[str, int] | Exception | None,
    kick_enabled: bool = True,
    kick_days: dict[str, Any] | None = None,
    real_sanitize: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> list[Any]:
            return rows

        async def position_before_fill(self, **_kwargs: object) -> Any:
            if isinstance(position, Exception):
                raise position
            if position is None:
                raise AssertionError("position read must be skipped")
            from decimal import Decimal as _D

            return _D(position[0]), position[1]

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    context = _stub_context()
    monkeypatch.setattr(handoff_service, "SessionContextService", context)
    if not real_sanitize:
        monkeypatch.setattr(handoff_service, "sanitize_fill", lambda row: row)
    state = {"version": 1, "watermark": 0, "seen": {}, "cooldowns": {}}
    if kick_days is not None:
        state["kick_days"] = kick_days
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return (
            {"items": [{"id": "deployment-id"}]}
            if url.endswith("/filter")
            else {"id": "flow-id"}
        )

    runner = FillHandoffRunner(
        HandoffConfig(
            state_dir=tmp_path,
            kick_enabled=kick_enabled,
            kick_cooldown_seconds=0,
            prefect_api_url="http://prefect",
            kick_deployments={"crypto": "crypto-deployment"},
            dry_run=dry_run,
        ),
        now=lambda: datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
        http_post=post,
    )
    outcome = asyncio.run(runner.run(_Db()))
    outcome["_context"] = context
    return outcome


def test_run_kicks_an_eligible_fill_and_records_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(tmp_path, monkeypatch, [_fill(1)], position=("0.1", 1))
    assert outcome["durable"] == 1
    assert outcome["kicked"] == 1
    (decision,) = outcome["decisions"]
    assert decision["class"] == "kick"
    assert decision["reason"] == "sell_full_exit"
    assert decision["flow_run_id"] == "flow-id"
    refs = outcome["_context"].appended[0].refs
    assert refs.kick_filter_class == "kick"
    assert refs.kick_filter_reason == "sell_full_exit"
    assert refs.position_before == "0.1"
    assert refs.position_after == "0.0"
    assert outcome["_context"].kick_results == ["flow-id"]


def test_run_over_cap_records_capped_and_annotates_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        [_fill(1)],
        position=("0.1", 1),
        kick_days={"crypto": {"date": "20260903", "count": 2}},
    )
    assert outcome["durable"] == 1
    assert outcome["kicked"] == 0
    (decision,) = outcome["decisions"]
    assert decision["class"] == "capped"
    assert decision["reason"] == "daily_cap"
    assert outcome["_context"].capped == [1]


def test_run_failed_position_read_is_queue_only_and_still_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(
        tmp_path, monkeypatch, [_fill(1)], position=RuntimeError("db gone")
    )
    assert outcome["durable"] == 1
    assert outcome["kicked"] == 0
    (decision,) = outcome["decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "position_read_failed"
    refs = outcome["_context"].appended[0].refs
    assert refs.kick_filter_class == "queue_only"
    assert refs.kick_filter_reason == "position_read_failed"


def test_run_overflowing_quantity_is_malformed_and_still_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BLOCKER regression: classification failure must never precede-and-skip
    # the durable open_question append
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        [_fill(1, filled_qty="1e9999999")],
        position=("0.1", 1),
    )
    assert outcome["durable"] == 1
    (decision,) = outcome["decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "fill_malformed"
    refs = outcome["_context"].appended[0].refs
    assert refs.kick_filter_reason == "fill_malformed"


def test_run_poison_and_unsupported_rows_skip_without_wedging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BLOCKER regression: a forex row cannot be an open_question (the schema
    # Literal has no such market) and a row that crashes sanitize_fill must not
    # abort the batch — both are recorded, the watermark still advances, and
    # the following valid row is still queued.
    rows = [
        _orm_row(id=1, instrument_type="forex"),
        _orm_row(id=2, filled_at="not-a-timestamp"),
        _orm_row(id=3, filled_qty=Decimal("0.01")),
    ]
    outcome = _run_outcome(
        tmp_path, monkeypatch, rows, position=("0.1", 1), real_sanitize=True
    )
    assert outcome["durable"] == 1
    assert outcome["skipped"] == [
        {"ledger_id": 1, "market": "forex", "reason": "unsupported_market"},
        {"ledger_id": 2, "reason": "sanitize_failed"},
    ]
    reasons = [entry["reason"] for entry in outcome["decisions"]]
    assert reasons == [
        "unsupported_market",
        "sanitize_failed",
        "sell_partial_below_25pct",
    ]
    # the watermark advanced past every row — no poison row can wedge the poll
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["watermark"] == 3


def test_run_skipped_rows_without_kick_flag_have_no_extra_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        [_orm_row(id=1, instrument_type="forex")],
        position=None,
        kick_enabled=False,
        real_sanitize=True,
    )
    assert outcome["durable"] == 0
    assert outcome["skipped"] == [
        {"ledger_id": 1, "market": "forex", "reason": "unsupported_market"}
    ]
    assert "decisions" not in outcome


def test_run_dry_run_never_advances_watermark_on_skipped_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _orm_row(id=1, instrument_type="forex"),
        _orm_row(id=2, filled_at="not-a-timestamp"),
    ]
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        rows,
        position=None,
        real_sanitize=True,
        dry_run=True,
    )
    assert outcome["durable"] == 0
    assert outcome["skipped"] == [
        {"ledger_id": 1, "market": "forex", "reason": "unsupported_market"},
        {"ledger_id": 2, "reason": "sanitize_failed"},
    ]
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["watermark"] == 0


def test_run_dry_run_with_only_poison_rows_leaves_state_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Round-3 regression: the seen-prune plus the trailing save must not
    # touch state.json under dry_run, even when every row is skipped.
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
    before = {
        "version": 1,
        "watermark": 0,
        "seen": {"expired": now.timestamp() - 90000},
        "cooldowns": {},
    }
    state_path.write_text(json.dumps(before, sort_keys=True) + "\n")
    before_bytes = state_path.read_bytes()

    class Repo:
        def __init__(self, _db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **_kwargs: object) -> Any:
            return [_orm_row(id=1, filled_at="not-a-timestamp")]

    monkeypatch.setattr(handoff_service, "ExecutionLedgerRepository", Repo)
    outcome = asyncio.run(
        FillHandoffRunner(
            HandoffConfig(state_dir=tmp_path, kick_enabled=True, dry_run=True),
            now=lambda: now,
        ).run(_Db())
    )
    assert outcome["skipped"] == [{"ledger_id": 1, "reason": "sanitize_failed"}]
    assert state_path.read_bytes() == before_bytes


def test_run_unproven_position_is_queue_only_and_still_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        [_fill(1, side="buy", filled_notional="60000")],
        position=("0", 0),
    )
    assert outcome["durable"] == 1
    assert outcome["kicked"] == 0
    (decision,) = outcome["decisions"]
    assert decision["class"] == "queue_only"
    assert decision["reason"] == "position_unproven"


def test_run_with_kick_disabled_is_byte_identical_to_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run_outcome(
        tmp_path,
        monkeypatch,
        [_fill(1)],
        position=None,  # the read must not even happen
        kick_enabled=False,
    )
    assert outcome["durable"] == 1
    # AC6 regression: the outcome shape is exactly the pre-filter keys
    assert set(outcome) - {"_context"} == {
        "durable",
        "pushed",
        "kicked",
        "duplicate",
        "fallback",
    }
    refs = outcome["_context"].appended[0].refs
    assert refs.kick_filter_class is None
    assert refs.kick_filter_reason is None
    assert refs.position_before is None
    assert refs.position_after is None
    assert refs.fill_handoff == "v1"


# --- CLI env parsing ----------------------------------------------------------


def test_cli_parses_numeric_notional_map_and_kick_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import fill_event_handoff as handoff_cli

    captured: list[HandoffConfig] = []

    class Runner:
        def __init__(self, config: HandoffConfig, **_kwargs: object) -> None:
            captured.append(config)

        async def run(self, _db: object) -> dict[str, Any]:
            return {"ok": True}

    class Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(handoff_cli, "FillHandoffRunner", Runner)
    monkeypatch.setattr(handoff_cli, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setenv("FILL_HANDOFF_LANES", "")
    monkeypatch.setenv("FILL_HANDOFF_KICK_DAILY_CAP", "3")
    # the documented numeric JSON shape must not crash the CLI
    monkeypatch.setenv("FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL", '{"KRW":5000,"USD":5}')
    monkeypatch.setenv("FILL_HANDOFF_KICK_PARKING_SYMBOLS", "sgov, TPLUS ")
    monkeypatch.setenv("FILL_HANDOFF_KICK_MIN_POSITION_FRACTION", "0.3")
    assert asyncio.run(handoff_cli.main_async()) == {"ok": True}
    config = captured[0]
    assert config.kick_daily_cap == 3
    assert config.kick_small_buy_notional == {
        "KRW": Decimal("5000"),
        "USD": Decimal("5"),
    }
    assert config.kick_parking_symbols == frozenset({"SGOV", "TPLUS"})
    assert config.kick_min_position_fraction == Decimal("0.3")


def test_cli_rejects_non_numeric_notional_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import fill_event_handoff as handoff_cli

    monkeypatch.setenv("FILL_HANDOFF_KICK_SMALL_BUY_NOTIONAL", '{"KRW":true}')
    with pytest.raises(ValueError, match="numeric"):
        asyncio.run(handoff_cli.main_async())
