"""Contract v1.5 ① — the §4 caps bind *across* cycles, from the broker.

Why this file exists, in one sentence: **a single cycle cannot tell a working
cap from a broken one.** The defect v1.5 ① fixes was invisible to every
one-cycle test in this suite, because in a one-cycle test the cap fires exactly
as intended — and that firing reads as proof. The caps were fed from
``attributed_book.json``, a file with read paths and no write path, so every
cycle restarted both counters at zero and a per-UTC-day ceiling of 2 became an
effective 12 on a lane that runs six cycles a day.

So the load-bearing tests here run **two consecutive cycles against one fixed
broker fixture** and assert on the second. The fixture is a stateful in-memory
Demo account: a submitted order becomes a resting order, which is the single
behaviour a real venue has and a phantom state file does not. Zero network,
zero real broker calls.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.broker_truth import (
    OWN_PENDING_ORDER_EXISTS,
    OWN_PENDING_UNREADABLE,
    BrokerTruth,
    OwnPendingResubmitBlocked,
    PendingUnreadable,
    assert_resubmit_allowed,
)
from scripts.b0x.crypto import sidecar
from scripts.b0x.cycle import run_sidecar_cycle
from scripts.b0x.envelope import CRYPTO_SIDECAR_ENVELOPE
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
#: The sidecar table is 4h; six cycles fit in one UTC day. That ratio is the
#: whole point — the old daily cap of 2 was really 2 × 6.
NEXT_CYCLE = NOW + dt.timedelta(hours=4)


# ---------------------------------------------------------------------------
# Fixed broker fixture — stateful, in-memory, no HTTP.
# ---------------------------------------------------------------------------


class _StatefulDemoAccount:
    """A Demo account where a submitted order actually rests.

    Deliberately more faithful than the flat fake used elsewhere: the defect
    under test only appears once cycle N's submissions are visible to cycle
    N+1, which is exactly what the deleted state file never delivered.
    """

    def __init__(self) -> None:
        self.open_orders: dict[str, list[Any]] = {
            "BTCUSDT": [],
            "ETHUSDT": [],
            "SOLUSDT": [],
        }
        self.submit_calls: list[dict[str, Any]] = []

    async def get_asset_balance(self, *, asset: str) -> Any:
        from app.services.brokers.binance.spot_demo.dto import SpotDemoAssetBalance

        free = Decimal("1000") if asset == "USDT" else Decimal("0")
        return SpotDemoAssetBalance(asset=asset, free=free, locked=Decimal("0"))

    async def get_open_orders(self, *, symbol: str) -> Any:
        from app.services.brokers.binance.spot_demo.dto import SpotDemoOpenOrdersResult

        return SpotDemoOpenOrdersResult(orders=list(self.open_orders[symbol]))

    async def submit_order(self, **kwargs: Any) -> Any:
        from app.services.brokers.binance.spot_demo.dto import SpotDemoOpenOrder
        from app.services.brokers.binance.spot_demo.execution_client import (
            SpotDemoDryRunResult,
        )

        assert kwargs["confirm"] is False, "test must never confirm"
        self.submit_calls.append(kwargs)
        self.open_orders[kwargs["symbol"]].append(
            SpotDemoOpenOrder(
                client_order_id=kwargs["client_order_id"],
                broker_order_id=str(len(self.submit_calls)),
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                qty=kwargs["qty"],
                status="NEW",
            )
        )
        return SpotDemoDryRunResult(
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            qty=kwargs["qty"],
            client_order_id=kwargs["client_order_id"],
        )

    async def aclose(self) -> None:  # pragma: no cover - cycle owns its client
        pass


@pytest.fixture
def three_symbol_table(tmp_path: Path) -> Path:
    """All three authorized sidecar symbols, each with a buy_l1 rung."""

    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            rows=[
                make_row(
                    symbol="KRW-BTC", previous_close="100000000", buy_l1="97000000"
                ),
                make_row(symbol="KRW-ETH", previous_close="3000000", buy_l1="2910000"),
                make_row(symbol="KRW-SOL", previous_close="200000", buy_l1="194000"),
            ],
            generated_at=NOW - dt.timedelta(hours=1),
        ),
    )
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "observations"


@pytest.fixture
def _sidecar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("B0X_SIDECAR_ENABLED", "true")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_BASE_URL", raising=False)

    async def _filters(*, base_url: str, symbol: str) -> sidecar.SymbolFilters:
        return sidecar.SymbolFilters(
            step_size=Decimal("0.00001"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )

    async def _price(*, base_url: str, symbol: str) -> Decimal:
        return Decimal("1000")

    monkeypatch.setattr(sidecar, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(sidecar, "fetch_reference_price", _price)


# ---------------------------------------------------------------------------
# 🔴 The two-cycle simulations. One cycle proves nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_new_entry_cap_does_not_reset_on_the_next_cycle(
    three_symbol_table: Path, out_dir: Path, _sidecar_env: None
) -> None:
    """일일 신규 ≤ 2 binds per UTC day, not per cycle.

    Cycle 1 spends the whole daily allowance on two symbols. Cycle 2, four
    hours later on the same UTC day, sees those two as resting orders in the
    broker's own answer and admits **nothing** — neither the two it already
    entered (동일 심볼 재제출 금지) nor the third (cap already saturated).

    Under the deleted state file, cycle 2 derived two more, and so did cycles
    3 through 6.
    """

    account = _StatefulDemoAccount()
    assert CRYPTO_SIDECAR_ENVELOPE.max_new_entries_per_utc_day == 2

    first = await run_sidecar_cycle(
        now=NOW,
        table_dir=three_symbol_table,
        out_dir=out_dir,
        confirm=False,
        client=account,
    )
    # Lexicographic scarcity tie-break: BTC and ETH, never SOL.
    assert [order["symbol"] for order in first.record["orders"]] == [
        "KRW-BTC",
        "KRW-ETH",
    ]
    assert len(first.record["submitted"]) == 2
    assert (
        sorted(account.open_orders)
        and sum(len(orders) for orders in account.open_orders.values()) == 2
    )

    second = await run_sidecar_cycle(
        now=NEXT_CYCLE,
        table_dir=three_symbol_table,
        out_dir=out_dir,
        confirm=False,
        client=account,
    )

    # 🔴 The assertion this whole job exists for.
    assert second.record["orders"] == []
    assert second.record["submitted"] == []
    assert len(account.submit_calls) == 2, "cycle 2 must not re-send anything"

    reasons: dict[str, set[str]] = {}
    for skip in second.record["skipped"]:
        reasons.setdefault(skip["symbol"], set()).add(skip["reason"])
    # The two entered symbols are refused as whole rows, before any leg.
    assert reasons["KRW-BTC"] == {OWN_PENDING_ORDER_EXISTS}
    assert reasons["KRW-ETH"] == {OWN_PENDING_ORDER_EXISTS}
    # The third has no resting order of its own, so it reaches the caps — and
    # the daily allowance is already spent by the other two.
    assert "daily_new_entry_cap_reached" in reasons["KRW-SOL"]

    # And the cap inputs on the record trace to the broker, not to a file.
    assert second.record["broker_truth"] == {
        "position_symbols": [],
        "own_pending": ["KRW-BTC", "KRW-ETH"],
        "own_pending_readable": True,
    }


@pytest.mark.asyncio
async def test_the_same_symbol_and_level_is_never_stacked_across_cycles(
    tmp_path: Path, out_dir: Path, _sidecar_env: None
) -> None:
    """계약 v1.5 ① 동일 심볼 재제출 금지, end to end.

    A single-symbol table run twice: identical table, identical levels. Before
    v1.5 ① the second cycle re-derived and re-submitted the same rung, because
    ``submit_planned`` consulted ``contaminated`` and never the account's own
    resting book.
    """

    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            rows=[
                make_row(
                    symbol="KRW-BTC", previous_close="100000000", buy_l1="97000000"
                )
            ],
            generated_at=NOW - dt.timedelta(hours=1),
        ),
    )
    account = _StatefulDemoAccount()

    first = await run_sidecar_cycle(
        now=NOW, table_dir=directory, out_dir=out_dir, confirm=False, client=account
    )
    assert len(first.record["submitted"]) == 1
    resting = account.open_orders["BTCUSDT"][0].client_order_id
    assert resting.startswith(sidecar.CLIENT_ORDER_ID_PREFIX)

    second = await run_sidecar_cycle(
        now=NEXT_CYCLE,
        table_dir=directory,
        out_dir=out_dir,
        confirm=False,
        client=account,
    )
    assert second.record["orders"] == []
    assert second.record["planned"] == []
    assert len(account.open_orders["BTCUSDT"]) == 1, "one rung, not a stack"
    assert [skip["reason"] for skip in second.record["skipped"]] == [
        OWN_PENDING_ORDER_EXISTS
    ]


@pytest.mark.asyncio
async def test_a_filled_order_frees_the_symbol_but_consumes_a_position_slot(
    tmp_path: Path, out_dir: Path, _sidecar_env: None
) -> None:
    """The rule tracks the broker, in both directions.

    When the resting order is gone but a sellable balance appeared in its
    place, the symbol is no longer blocked as pending — it is blocked as a
    position, and it still occupies one of the 동시 포지션 ≤ 3 slots and one
    member of the daily-new distinct set. Nothing here is remembered; it is all
    re-read.
    """

    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            rows=[
                make_row(
                    symbol="KRW-BTC", previous_close="100000000", buy_l1="97000000"
                )
            ],
            generated_at=NOW - dt.timedelta(hours=1),
        ),
    )

    class _Filled(_StatefulDemoAccount):
        async def get_asset_balance(self, *, asset: str) -> Any:
            from app.services.brokers.binance.spot_demo.dto import SpotDemoAssetBalance

            if asset == "BTC":
                # Well above the 0.00001 minQty in the fixture filters.
                return SpotDemoAssetBalance(
                    asset=asset, free=Decimal("0.01"), locked=Decimal("0")
                )
            return await super().get_asset_balance(asset=asset)

    outcome = await run_sidecar_cycle(
        now=NOW, table_dir=directory, out_dir=out_dir, confirm=False, client=_Filled()
    )
    truth = outcome.record["broker_truth"]
    assert truth["own_pending"] == []
    assert truth["position_symbols"] == ["KRW-BTC"]
    # The balance is also unattributed, so submission is blocked as contaminated
    # (v1 attribution) — the position slot is consumed either way.
    assert outcome.record["submitted"] == []


# ---------------------------------------------------------------------------
# The submission boundary — the place the stacking actually happened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_planned_refuses_a_symbol_with_an_own_resting_order(
    _sidecar_env: None,
) -> None:
    """Re-checked immediately before dispatch, not trusted from derivation."""

    planned = sidecar.SidecarPlannedOrder(
        order_key="abc123",
        client_order_id="b0xc-abc123",
        table_symbol="KRW-BTC",
        symbol="BTCUSDT",
        side="buy",
        leg="buy_l1",
        price=Decimal("970"),
        qty=Decimal("0.01"),
        notional=Decimal("9.7"),
        reference_price=Decimal("1000"),
        price_ratio=Decimal("0.97"),
    )
    fresh = sidecar.FreshTruth(
        quote_free=Decimal("1000"),
        quote_locked=Decimal("0"),
        base_balances={
            asset: (Decimal("0"), Decimal("0")) for asset in ("BTC", "ETH", "SOL")
        },
        open_orders={"BTCUSDT": [], "ETHUSDT": [], "SOLUSDT": []},
        foreign_open_orders=(),
        foreign_base_assets=(),
        own_open_order_symbols=("BTCUSDT",),
    )

    class _NeverCalled:
        async def submit_order(self, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("submission must not reach the venue")

    with pytest.raises(OwnPendingResubmitBlocked):
        await sidecar.submit_planned(
            _NeverCalled(),
            [planned],
            envelope=CRYPTO_SIDECAR_ENVELOPE,
            fresh_truth=fresh,
            confirm=False,
        )


# ---------------------------------------------------------------------------
# BrokerTruth itself — the three literal definitions.
# ---------------------------------------------------------------------------


def test_concurrent_position_count_is_the_non_dust_sellable_balance_count() -> None:
    truth = BrokerTruth(position_symbols=("KRW-ETH", "KRW-BTC"), own_pending=())
    assert truth.concurrent_position_count == 2
    # Normalized: sorted and de-duplicated, so the hashed record is stable.
    assert truth.position_symbols == ("KRW-BTC", "KRW-ETH")


def test_daily_new_entry_seed_is_the_union_of_pending_and_positions() -> None:
    truth = BrokerTruth(
        position_symbols=("KRW-BTC", "KRW-ETH"), own_pending=("KRW-ETH", "KRW-SOL")
    )
    assert truth.daily_new_entry_seed() == {"KRW-BTC", "KRW-ETH", "KRW-SOL"}


def test_unreadable_pending_blocks_every_symbol_and_is_not_an_empty_tuple() -> None:
    """The tri-state, pinned: unreadable ≠ empty."""

    unreadable = BrokerTruth(
        position_symbols=(),
        own_pending=PendingUnreadable(reason="no_tr", detail="venue cannot answer"),
    )
    empty = BrokerTruth(position_symbols=(), own_pending=())

    assert empty.resubmit_block("KRW-BTC") is None
    assert unreadable.resubmit_block("KRW-BTC")[0] == OWN_PENDING_UNREADABLE
    assert unreadable.resubmit_block("anything-at-all") is not None
    # They must not be confusable on the record either.
    assert empty.canonical()["own_pending_readable"] is True
    assert unreadable.canonical()["own_pending_readable"] is False

    assert_resubmit_allowed(empty, symbol="KRW-BTC", lane="test")
    with pytest.raises(OwnPendingResubmitBlocked):
        assert_resubmit_allowed(unreadable, symbol="KRW-BTC", lane="test")


# ---------------------------------------------------------------------------
# 🔴 No state file may come back.
# ---------------------------------------------------------------------------

#: The only JSON file any B0-X lane persists: the Upbit shadow lane's virtual
#: book, which is *actually written* every cycle (``store_json_state``) and is
#: the lane's ledger rather than a cap input. ``".json"`` is the constant part
#: of ``table_source``'s ``f"latest-{market}.json"`` — a read-only input the
#: table generator owns.
_ALLOWED_JSON_LITERALS = frozenset({"portfolio.json", ".json"})

#: Persistence helpers may only be called from the two shadow-lane call sites.
#: Keyed by repo-relative path, never by basename — ``scripts/b0x/cycle.py`` and
#: ``scripts/b0x/kr/cycle.py`` share a basename, and allowing one by name would
#: silently allow the other.
_STATE_HELPERS = frozenset({"load_json_state", "store_json_state"})
_STATE_HELPER_ALLOWED_PATHS = frozenset(
    {"scripts/b0x/cycle.py", "scripts/b0x/ledger.py", "scripts/run_b0x_cycle.py"}
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _b0x_sources() -> list[Path]:
    files = sorted((_REPO_ROOT / "scripts" / "b0x").rglob("*.py"))
    files += [
        _REPO_ROOT / "scripts" / "run_b0x_cycle.py",
        _REPO_ROOT / "scripts" / "run_b0x_kr_cycle.py",
        _REPO_ROOT / "scripts" / "run_b0x_cancel.py",
    ]
    assert len(files) > 10, "source discovery found suspiciously little to scan"
    return files


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def test_no_lane_state_file_is_reintroduced() -> None:
    """Contract v1.5 ①: 상태 파일 신설 금지.

    ``attributed_book.json`` was not merely wrong, it was a *shape* of wrong:
    a cap input that a lane owns, persists, and can silently fail to write.
    This guard refuses the shape, not the name — any new ``*.json`` state path
    in the package fails here even if it is called something else entirely.
    """

    offenders: list[str] = []
    for path in _b0x_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.endswith(".json")
                and node.value not in _ALLOWED_JSON_LITERALS
            ):
                offenders.append(f"{_rel(path)}:{node.lineno} {node.value!r}")
    assert offenders == [], (
        "a new persisted-state path appeared in the B0-X package — contract "
        f"v1.5 ① forbids one: {offenders}"
    )


def test_state_persistence_helpers_stay_confined_to_the_shadow_lane() -> None:
    """The other half of the shape guard: who may persist at all.

    Restricting the filename alone would let a lane re-persist its caps under
    an allowed name. Only the shadow lane's two virtual-book call sites may
    touch the persistence helpers.
    """

    offenders: list[str] = []
    scanned = 0
    for path in _b0x_sources():
        if _rel(path) in _STATE_HELPER_ALLOWED_PATHS:
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in _STATE_HELPERS:
                    offenders.append(f"{_rel(path)}:{node.lineno} {name}")
    # The allowlist must not have swallowed the KR lane by basename collision.
    assert "scripts/b0x/kr/cycle.py" not in _STATE_HELPER_ALLOWED_PATHS
    assert scanned >= 8, f"allowlist excluded too much to be meaningful: {scanned}"
    assert offenders == [], (
        "a B0-X module outside the shadow lane persisted lane state — that is "
        f"the ``attributed_book.json`` shape returning: {offenders}"
    )


def test_cap_inputs_cannot_default_to_empty() -> None:
    """``broker_truth`` is a required field, structurally.

    The original defect did not need a bug to fire: a missing file quietly
    produced empty counters. A required constructor argument turns "nobody
    supplied the cap inputs" into an error at construction instead.
    """

    import dataclasses

    from scripts.b0x.state import LaneAccountState

    field = next(
        f for f in dataclasses.fields(LaneAccountState) if f.name == "broker_truth"
    )
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING

    with pytest.raises(TypeError):
        LaneAccountState(lane="x", quote_currency="USDT", cash=Decimal("0"))
