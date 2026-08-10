"""End-to-end cycle behaviour for both lanes (no network, no venue)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.crypto import shadow, sidecar
from scripts.b0x.cycle import (
    SHADOW_KILL_CURRENCY_MISMATCH_REASON,
    run_shadow_cycle,
    run_sidecar_cycle,
)
from scripts.b0x.envelope import CRYPTO_SHADOW_ENVELOPE, CRYPTO_SIDECAR_ENVELOPE
from scripts.b0x.labels import (
    SHADOW_SYNTHETIC_FILL,
    SHARED_ACCOUNT_HISTORY,
    TRUST_LABELS,
)
from scripts.b0x.ledger import ObservationLedger
from tests.scripts.b0x._table_fixtures import (
    make_payload,
    make_row,
    write_stale_marker,
    write_table,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def table_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            rows=[
                make_row(
                    symbol="KRW-BTC",
                    previous_close="100000000",
                    buy_l1="97000000",
                    sell_r1="105000000",
                    sell_r2="110000000",
                ),
                make_row(symbol="KRW-ETH", previous_close="3000000", buy_l1="2910000"),
            ],
            generated_at=NOW - dt.timedelta(hours=2),
        ),
    )
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "observations"


@pytest.fixture(autouse=True)
def _no_real_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shadow lane must never reach the network in these tests."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("shadow cycle attempted a network candle fetch")

    monkeypatch.setattr(shadow, "fetch_ohlcv", _boom)


@pytest.fixture
def _shadow_currency_misaligned_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reintroduce the pre-SHADOW-ALIGN defect on purpose (mutant ①).

    Production wiring (``run_shadow_cycle`` loading
    ``envelope.CRYPTO_SHADOW_ENVELOPE``, ``quote_currency="KRW"``) does not
    do this any more. This fixture exists only for
    ``test_shadow_cycle_currency_mismatch_backstop_still_cancels_a_resting_book``
    below, which proves the ``CurrencyMismatchKill`` fail-closed backstop
    (#1822/ROB-1233) still fires — and still cancels a resting book — if a
    future edit ever hands this lane a mismatched envelope again.
    """

    monkeypatch.setattr(
        shadow, "QUOTE_CURRENCY", CRYPTO_SIDECAR_ENVELOPE.quote_currency
    )


# ---------------------------------------------------------------------------
# Shadow lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_shadow_cycle_derives_and_records(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)

    assert outcome.zero_order_reason is None
    assert outcome.order_count == 2  # one L1 per symbol, no L2 in the fixture
    assert outcome.record["real_orders"] == 0
    assert outcome.record["live_contact"] == 0

    # All three inherited trust labels present, verbatim.
    assert all(label in outcome.record["labels"] for label in TRUST_LABELS)
    assert SHADOW_SYNTHETIC_FILL not in TRUST_LABELS
    assert any("SHADOW_SYNTHETIC_FILL" in label for label in outcome.record["labels"])

    # The touch rule is stated in the artifact, not left implicit.
    assert outcome.record["touch_rule"]["id"] == shadow.TOUCH_RULE_ID
    assert "터치≠체결" in outcome.record["touch_rule"]["statement"]

    ledger = ObservationLedger(lane=shadow.LANE, root=out_dir)
    assert len(ledger.read_cycles()) == 1
    assert outcome.artifact_path is not None and outcome.artifact_path.exists()
    assert (ledger.lane_dir / "portfolio.json").exists()


@pytest.mark.asyncio
async def test_shadow_cycle_is_idempotent_in_its_derivation(
    table_dir: Path, out_dir: Path
) -> None:
    """Same table + same book → same cycle_id, orders, and derivation hash.

    "Same book" now includes the lane's resting orders, because contract v1.5 ①
    made them a derivation *input* (동일 심볼 재제출 금지). So the replay is
    staged explicitly: snapshot the book, run a cycle that changes it, restore,
    and re-run. A test that simply ran two cycles back to back would be
    asserting that placing orders does not change the account — which is the
    assumption the phantom ``attributed_book.json`` encoded.
    """

    portfolio_path = (
        ObservationLedger(lane=shadow.LANE, root=out_dir).lane_dir / "portfolio.json"
    )
    first = await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)
    book_after_first = portfolio_path.read_text(encoding="utf-8")

    second = await run_shadow_cycle(
        now=NOW + dt.timedelta(minutes=1), table_dir=table_dir, out_dir=out_dir
    )
    assert first.derivation is not None and second.derivation is not None

    # Restore the book the second cycle started from, then replay it.
    portfolio_path.write_text(book_after_first, encoding="utf-8")
    replay = await run_shadow_cycle(
        now=NOW + dt.timedelta(minutes=2), table_dir=table_dir, out_dir=out_dir
    )
    assert replay.derivation is not None
    assert replay.derivation.cycle_id == second.derivation.cycle_id
    assert replay.derivation.derivation_hash() == second.derivation.derivation_hash()
    assert replay.derivation.canonical_bytes() == second.derivation.canonical_bytes()

    # Three cycles recorded — the ledger appends, never rewrites.
    assert len(ObservationLedger(lane=shadow.LANE, root=out_dir).read_cycles()) == 3


@pytest.mark.asyncio
async def test_stale_table_yields_zero_orders_and_cancels_the_book(
    table_dir: Path, out_dir: Path
) -> None:
    await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)
    portfolio_path = (
        ObservationLedger(lane=shadow.LANE, root=out_dir).lane_dir / "portfolio.json"
    )
    assert json.loads(portfolio_path.read_text())["open_orders"]

    write_stale_marker(table_dir)
    outcome = await run_shadow_cycle(
        now=NOW + dt.timedelta(hours=1), table_dir=table_dir, out_dir=out_dir
    )

    assert outcome.zero_order_reason == "stale_marker_present"
    assert outcome.order_count == 0
    assert outcome.record["cancelled_stale_orders"] == 2
    # §2-2: no silent reuse — the book is empty, not carrying the old intent.
    assert json.loads(portfolio_path.read_text())["open_orders"] == []


@pytest.mark.asyncio
async def test_missing_table_yields_zero_orders(tmp_path: Path, out_dir: Path) -> None:
    outcome = await run_shadow_cycle(
        now=NOW, table_dir=tmp_path / "nothing-here", out_dir=out_dir
    )
    assert outcome.zero_order_reason == "table_missing"
    assert outcome.order_count == 0
    assert outcome.artifact_path is not None


@pytest.mark.asyncio
async def test_shadow_lane_uses_b0_sizing_not_the_usdt_envelope(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)
    assert outcome.record["envelope_application"].startswith("envelope_not_applied")
    assert outcome.record["orders"], "fixture must actually derive orders"
    for order in outcome.record["orders"]:
        assert order["notional"] == "10000"  # sizing.new_entry_notional_krw


@pytest.mark.asyncio
async def test_shadow_cycle_is_currency_aligned_by_default_after_shadow_align(
    table_dir: Path, out_dir: Path
) -> None:
    """SHADOW-ALIGN regression: the real, unpatched wiring used to hit
    ``kill_switch_currency_mismatch`` on *every* shadow cycle (shadow's book
    is KRW, the shared crypto sidecar envelope's kill threshold was USDT —
    see ``test_shadow_cycle_currency_mismatch_backstop_still_cancels_a_resting_book``
    below for proof the fail-closed guard itself still works). ``run_shadow_cycle``
    now loads ``envelope.CRYPTO_SHADOW_ENVELOPE`` (KRW), which matches
    ``shadow.QUOTE_CURRENCY`` — so a normal cycle must derive orders, not zero
    out on a currency reason.
    """

    outcome = await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)

    assert outcome.zero_order_reason is None
    assert outcome.zero_order_reason != SHADOW_KILL_CURRENCY_MISMATCH_REASON
    assert outcome.order_count == 2
    assert outcome.record["orders"] != []

    kill_switch = outcome.record["kill_switch"]
    assert kill_switch["state_quote_currency"] == "KRW"
    assert kill_switch["envelope_quote_currency"] == "KRW"
    assert kill_switch["allow_new_orders"] is True

    assert outcome.record["envelope"]["market"] == CRYPTO_SHADOW_ENVELOPE.market
    assert outcome.record["envelope"]["quote_currency"] == "KRW"

    portfolio_path = (
        ObservationLedger(lane=shadow.LANE, root=out_dir).lane_dir / "portfolio.json"
    )
    assert json.loads(portfolio_path.read_text())["open_orders"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_shadow_currency_misaligned_mutant")
async def test_shadow_cycle_currency_mismatch_backstop_still_cancels_a_resting_book(
    table_dir: Path, out_dir: Path
) -> None:
    """Mutant ① made permanent: if a future edit ever hands this lane a
    mismatched envelope again (here simulated by patching
    ``shadow.QUOTE_CURRENCY`` back to USDT, reproducing the pre-SHADOW-ALIGN
    wiring), ``CurrencyMismatchKill`` (#1822/ROB-1233) must still fire, the
    cycle must still degrade to a recorded zero-order cycle rather than
    crash or silently miscompare, and a resting virtual book from an earlier,
    genuinely-aligned cycle must still be cancelled rather than carried
    forward.
    """

    portfolio_path = (
        ObservationLedger(lane=shadow.LANE, root=out_dir).lane_dir / "portfolio.json"
    )
    with pytest.MonkeyPatch.context() as aligned:
        aligned.setattr(shadow, "QUOTE_CURRENCY", CRYPTO_SHADOW_ENVELOPE.quote_currency)
        seeded = await run_shadow_cycle(now=NOW, table_dir=table_dir, out_dir=out_dir)
    assert seeded.order_count == 2
    assert json.loads(portfolio_path.read_text())["open_orders"]

    # Fixture is active again here (outside the aligned context) — back to
    # the mismatched wiring for this second cycle.
    outcome = await run_shadow_cycle(
        now=NOW + dt.timedelta(minutes=1), table_dir=table_dir, out_dir=out_dir
    )

    assert outcome.zero_order_reason == SHADOW_KILL_CURRENCY_MISMATCH_REASON
    assert "KRW" in outcome.record["zero_order_detail"]
    assert "USDT" in outcome.record["zero_order_detail"]
    assert outcome.record["cancelled_stale_orders"] == 2
    assert json.loads(portfolio_path.read_text())["open_orders"] == []


# ---------------------------------------------------------------------------
# Sidecar lane
# ---------------------------------------------------------------------------


class _FakeSpotDemoClient:
    """Flat, uncontaminated Demo account."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.closed = False

    async def get_asset_balance(self, *, asset: str) -> Any:
        from app.services.brokers.binance.spot_demo.dto import SpotDemoAssetBalance

        free = Decimal("1000") if asset == "USDT" else Decimal("0")
        return SpotDemoAssetBalance(asset=asset, free=free, locked=Decimal("0"))

    async def get_open_orders(self, *, symbol: str) -> Any:
        from app.services.brokers.binance.spot_demo.dto import SpotDemoOpenOrdersResult

        return SpotDemoOpenOrdersResult(orders=[])

    async def submit_order(self, **kwargs: Any) -> Any:
        from app.services.brokers.binance.spot_demo.execution_client import (
            SpotDemoDryRunResult,
        )

        self.submitted.append(kwargs)
        assert kwargs["confirm"] is False, "test must never confirm"
        return SpotDemoDryRunResult(
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            qty=kwargs["qty"],
            client_order_id=kwargs["client_order_id"],
        )

    async def aclose(self) -> None:
        self.closed = True


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


@pytest.mark.asyncio
async def test_sidecar_dry_run_plans_but_dispatches_nothing(
    table_dir: Path, out_dir: Path, _sidecar_env: None
) -> None:
    client = _FakeSpotDemoClient()
    outcome = await run_sidecar_cycle(
        now=NOW, table_dir=table_dir, out_dir=out_dir, confirm=False, client=client
    )

    assert outcome.record["confirm"] is False
    assert outcome.record["fresh_truth"]["flat"] is True
    assert outcome.record["fresh_truth"]["contaminated"] is False
    assert outcome.record["planned"], "expected planned orders for BTC/ETH"
    assert all(row["dispatched"] is False for row in outcome.record["submitted"])
    assert all(call["confirm"] is False for call in client.submitted)
    assert outcome.record["base_url_host"] == "demo-api.binance.com"
    assert SHARED_ACCOUNT_HISTORY in outcome.record["labels"]
    artifact_text = outcome.artifact_path.read_text(encoding="utf-8")
    assert f"> {SHARED_ACCOUNT_HISTORY}" in artifact_text

    # Only the three authorized symbols can appear.
    for row in outcome.record["planned"]:
        assert row["symbol"] in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        assert row["client_order_id"].startswith("b0xc-")
        assert Decimal(row["notional"]) <= Decimal("10")


@pytest.mark.asyncio
async def test_sidecar_is_bound_by_the_usdt_envelope(
    table_dir: Path, out_dir: Path, _sidecar_env: None
) -> None:
    outcome = await run_sidecar_cycle(
        now=NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeSpotDemoClient(),
    )
    assert outcome.record["envelope"] == {
        "market": "crypto",
        "quote_currency": "USDT",
        "per_order_notional": "10",
        "per_symbol_total_notional": "50",
        "max_concurrent_positions": 3,
        "max_new_entries_per_utc_day": 2,
        "daily_loss_kill": "5",
        "daily_loss_kill_basis": "absolute",
    }
    for order in outcome.record["orders"]:
        assert order["notional"] == "10"


@pytest.mark.asyncio
async def test_sidecar_refuses_to_submit_on_a_contaminated_account(
    table_dir: Path, out_dir: Path, _sidecar_env: None
) -> None:
    class _Contaminated(_FakeSpotDemoClient):
        async def get_open_orders(self, *, symbol: str) -> Any:
            from app.services.brokers.binance.spot_demo.dto import (
                SpotDemoOpenOrder,
                SpotDemoOpenOrdersResult,
            )

            if symbol != "BTCUSDT":
                return SpotDemoOpenOrdersResult(orders=[])
            return SpotDemoOpenOrdersResult(
                orders=[
                    SpotDemoOpenOrder(
                        client_order_id="someone-elses-bot",
                        broker_order_id="1",
                        symbol=symbol,
                        side="BUY",
                        qty=Decimal("1"),
                        status="NEW",
                    )
                ]
            )

    outcome = await run_sidecar_cycle(
        now=NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_Contaminated(),
    )
    assert outcome.contaminated is True
    assert outcome.record["contaminated"] is True
    assert outcome.record["submitted"] == []
    assert "CONTAMINATED" in outcome.record["submission_blocked"]
    # Derivation still happened — 관측은 계속하되 오염 구간 분리 (§2-3).
    assert outcome.record["orders"]


@pytest.mark.asyncio
async def test_sidecar_disabled_by_default(
    table_dir: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("B0X_SIDECAR_ENABLED", raising=False)
    with pytest.raises(sidecar.SidecarDisabled):
        await run_sidecar_cycle(
            now=NOW, table_dir=table_dir, out_dir=out_dir, client=_FakeSpotDemoClient()
        )


@pytest.mark.asyncio
async def test_sidecar_stale_table_yields_zero_orders_before_any_planning(
    table_dir: Path, out_dir: Path, _sidecar_env: None
) -> None:
    write_stale_marker(table_dir)
    client = _FakeSpotDemoClient()
    outcome = await run_sidecar_cycle(
        now=NOW, table_dir=table_dir, out_dir=out_dir, confirm=False, client=client
    )
    assert outcome.zero_order_reason == "stale_marker_present"
    assert outcome.record["submitted"] == []
    assert client.submitted == []


@pytest.mark.asyncio
async def test_sidecar_records_a_distinct_policy_hash(
    table_dir: Path, out_dir: Path, _sidecar_env: None
) -> None:
    from app.services.brokers.binance import paper_adapter

    outcome = await run_sidecar_cycle(
        now=NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeSpotDemoClient(),
    )
    assert outcome.record["policy"]["version"] == "b0x-binance-spot-demo-v1"
    assert outcome.record["policy"]["policy_hash"] != paper_adapter._POLICY_HASH
