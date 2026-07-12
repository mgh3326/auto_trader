"""ROB-842 real public-MCP-handler integration tests for the automated boundary.

Post-3rd-round: the public preview takes ONLY order intent + an opaque, trusted
``quote_snapshot_id``. Identity / market provenance / ceiling are server-owned
(loaded from ``market_quote_snapshots`` + hard-cap policy); the caller cannot
supply correlation, snapshot, market-data, ceiling, origin, or client_order_id.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling import alpaca_paper_automated_orders as auto_mod
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    alpaca_paper_automated_preview_order,
    alpaca_paper_automated_submit_order,
    reset_alpaca_paper_automated_factories,
    set_alpaca_paper_automated_factories,
)
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.review import AlpacaPaperOrderLedger
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import Order

pytestmark = [pytest.mark.asyncio]

_CORR = "rob842dec-"  # server-derived correlation prefix


class CountingBroker:
    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.submit_calls: list[Any] = []
        self._delay_s = delay_s

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return Order(
            id=f"paper-{len(self.submit_calls)}",
            client_order_id=getattr(request, "client_order_id", None),
            symbol=getattr(request, "symbol", "BTC/USD"),
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type=getattr(request, "type", "limit"),
            time_in_force=getattr(request, "time_in_force", "gtc"),
            status="accepted",
            limit_price=getattr(request, "limit_price", None),
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        return None

    async def get_position(self, symbol: str) -> Any:
        return None


@pytest_asyncio.fixture
async def broker(monkeypatch) -> CountingBroker:
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", True)
    b = CountingBroker(delay_s=0.02)
    set_alpaca_paper_automated_factories(
        session_factory=lambda: AsyncSessionLocal, broker_factory=lambda: b
    )
    yield b
    reset_alpaca_paper_automated_factories()


@pytest_asyncio.fixture(autouse=True)
async def _clean():
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
            )
        )
        await db.execute(
            delete(MarketQuoteSnapshot).where(
                MarketQuoteSnapshot.symbol.in_(["KRW-BTC", "AAPL"])
            )
        )
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
            )
        )
        await db.execute(
            delete(MarketQuoteSnapshot).where(
                MarketQuoteSnapshot.symbol.in_(["KRW-BTC", "AAPL"])
            )
        )
        await db.commit()


async def _seed_snapshot(
    *, market="crypto", symbol="KRW-BTC", source="upbit", age_s=10, price="50000"
) -> int:
    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market=market,
            symbol=symbol,
            source=source,
            snapshot_at=datetime.now(UTC) - timedelta(seconds=age_s),
            price=Decimal(price),
        )
        db.add(row)
        await db.commit()
        return row.id


def _crypto_intent(snapshot_id: int, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "quote_snapshot_id": snapshot_id,
        "notional": Decimal("10"),
        "limit_price": Decimal("50000"),
        "time_in_force": "gtc",
        "asset_class": "crypto",
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Default-disabled
# ---------------------------------------------------------------------------
async def test_automated_preview_disabled_by_default() -> None:
    result = await alpaca_paper_automated_preview_order(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        quote_snapshot_id=1,
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        time_in_force="gtc",
        asset_class="crypto",
    )
    assert result["success"] is False
    assert result["reason_code"] == "automated_submit_disabled"


async def test_automated_submit_disabled_by_default() -> None:
    result = await alpaca_paper_automated_submit_order("any", confirm=True)
    assert result["success"] is False
    assert result["reason_code"] == "automated_submit_disabled"


# ---------------------------------------------------------------------------
# Public-handler exactly-one broker submit
# ---------------------------------------------------------------------------
async def test_public_handler_sequential_duplicate_submits_once(broker):
    sid = await _seed_snapshot()
    preview = await alpaca_paper_automated_preview_order(**_crypto_intent(sid))
    assert preview["success"] is True
    token = preview["approval_token"]

    first = await alpaca_paper_automated_submit_order(token, confirm=True)
    second = await alpaca_paper_automated_submit_order(token, confirm=True)

    assert first["status"] == "submitted"
    assert second["status"] in {"replayed", "recovered"}
    assert second["broker_called"] is False
    assert len(broker.submit_calls) == 1


async def test_public_handler_parallel_duplicate_submits_once(broker):
    sid = await _seed_snapshot()
    token = (await alpaca_paper_automated_preview_order(**_crypto_intent(sid)))[
        "approval_token"
    ]
    results = await asyncio.gather(
        alpaca_paper_automated_submit_order(token, confirm=True),
        alpaca_paper_automated_submit_order(token, confirm=True),
    )
    assert len(broker.submit_calls) == 1
    assert sorted(r["status"] for r in results).count("submitted") == 1


async def test_confirm_false_is_dry_run_no_post(broker):
    sid = await _seed_snapshot()
    token = (await alpaca_paper_automated_preview_order(**_crypto_intent(sid)))[
        "approval_token"
    ]
    dry = await alpaca_paper_automated_submit_order(token, confirm=False)
    assert dry["submitted"] is False
    assert dry["blocked_reason"] == "confirmation_required"
    assert broker.submit_calls == []


async def test_submit_without_persisted_preview_rejected(broker):
    result = await alpaca_paper_automated_submit_order(
        "rob842a-crypto-nope", confirm=True
    )
    assert result["reason_code"] == "no_preview_for_token"
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# Trusted-snapshot provenance fail-close (B2) — no persistence, no broker
# ---------------------------------------------------------------------------
async def test_preview_missing_snapshot_fails_close(broker):
    result = await alpaca_paper_automated_preview_order(**_crypto_intent(999999))
    assert result["success"] is False
    assert result["reason_code"] == "no_trusted_snapshot"


async def test_preview_stale_snapshot_fails_close(broker):
    sid = await _seed_snapshot(age_s=3600)  # 1h old
    result = await alpaca_paper_automated_preview_order(**_crypto_intent(sid))
    assert result["success"] is False
    assert result["reason_code"] == "stale_trusted_snapshot"


async def test_preview_symbol_mismatch_fails_close(broker):
    # snapshot maps to BTC/USD but the order is for a different pair
    sid = await _seed_snapshot(symbol="KRW-BTC")
    result = await alpaca_paper_automated_preview_order(
        **_crypto_intent(sid, symbol="ETH/USD", limit_price=Decimal("3000"))
    )
    assert result["success"] is False
    assert result["reason_code"] == "snapshot_symbol_mismatch"


async def test_preview_order_exceeding_hard_cap_fails_close(broker):
    # us_equity notional above the $1000 server hard cap — caller cannot raise it
    sid = await _seed_snapshot(market="us", symbol="AAPL", source="yahoo", price="150")
    result = await alpaca_paper_automated_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        quote_snapshot_id=sid,
        notional=Decimal("1500"),
        asset_class="us_equity",
    )
    assert result["success"] is False
    assert result["reason_code"] == "notional_exceeds_max"


async def test_same_snapshot_same_key_different_snapshot_different_key(broker):
    sid1 = await _seed_snapshot()
    p1a = await alpaca_paper_automated_preview_order(**_crypto_intent(sid1))
    p1b = await alpaca_paper_automated_preview_order(**_crypto_intent(sid1))
    assert p1a["approval_token"] == p1b["approval_token"]  # same trusted decision

    sid2 = await _seed_snapshot()  # a distinct trusted observation
    p2 = await alpaca_paper_automated_preview_order(**_crypto_intent(sid2))
    assert p2["approval_token"] != p1a["approval_token"]


async def test_preview_records_provenance_hashes(broker):
    sid = await _seed_snapshot()
    preview = await alpaca_paper_automated_preview_order(**_crypto_intent(sid))
    prov = preview["provenance"]
    assert prov["quote_snapshot_id"] == sid
    assert prov["snapshot_content_hash"]
    assert prov["packet_hash"]
    assert prov["policy_max_notional"] == "50"  # crypto hard cap


# ---------------------------------------------------------------------------
# No caller-owned identity / provenance / ceiling / origin (B2/B4)
# ---------------------------------------------------------------------------
async def test_preview_signature_exposes_no_caller_owned_trust_fields() -> None:
    params = set(inspect.signature(alpaca_paper_automated_preview_order).parameters)
    forbidden = {
        "correlation_id",
        "snapshot_id",
        "market_data_asof",
        "market_data_source",
        "max_notional",
        "max_qty",
        "qty_source",
        "origin",
        "client_order_id",
        "signal_venue",
    }
    assert forbidden.isdisjoint(params), (
        f"caller-owned trust fields present: {forbidden & params}"
    )
    assert "quote_snapshot_id" in params  # only an opaque server-issued reference


async def test_submit_signature_is_token_and_confirm_only() -> None:
    params = set(inspect.signature(alpaca_paper_automated_submit_order).parameters)
    assert params == {"approval_token", "confirm"}


async def test_manual_submit_tool_has_no_origin_param() -> None:
    from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_submit_order

    params = set(inspect.signature(alpaca_paper_submit_order).parameters)
    assert "origin" not in params
    assert "client_order_id" not in params


async def test_module_exposes_gate_and_factory_controls() -> None:
    assert callable(auto_mod.set_alpaca_paper_automated_factories)
    assert auto_mod.ALPACA_PAPER_AUTOMATED_TOOL_NAMES == {
        "alpaca_paper_automated_preview_order",
        "alpaca_paper_automated_submit_order",
    }


# ---------------------------------------------------------------------------
# F4 — trusted price × qty must not bypass the $1,000 hard cap
# ---------------------------------------------------------------------------
async def test_preview_market_qty_bypass_of_hard_cap_fails_close(broker):
    # equity MARKET qty=5 at trusted price 100,000 => $500,000 implied notional.
    sid = await _seed_snapshot(
        market="us", symbol="AAPL", source="yahoo", price="100000"
    )
    result = await alpaca_paper_automated_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        quote_snapshot_id=sid,
        qty=Decimal("5"),
        asset_class="us_equity",
    )
    assert result["success"] is False
    assert result["reason_code"] == "notional_exceeds_max"


async def test_preview_rejects_non_finite_snapshot_price(broker):
    # A snapshot with a non-positive/zero price is not usable evidence.
    sid = await _seed_snapshot(market="us", symbol="AAPL", source="yahoo", price="0")
    result = await alpaca_paper_automated_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        quote_snapshot_id=sid,
        qty=Decimal("1"),
        asset_class="us_equity",
    )
    assert result["success"] is False
    assert result["reason_code"] == "invalid_snapshot_price"


# ---------------------------------------------------------------------------
# F6 — automated sell is explicitly disabled until ROB-845
# ---------------------------------------------------------------------------
async def test_automated_sell_is_explicitly_disabled(broker):
    sid = await _seed_snapshot()
    result = await alpaca_paper_automated_preview_order(
        symbol="BTC/USD",
        side="sell",
        type="limit",
        quote_snapshot_id=sid,
        qty=Decimal("0.0001"),
        limit_price=Decimal("50000"),
        time_in_force="gtc",
        asset_class="crypto",
    )
    assert result["success"] is False
    assert result["reason_code"] == "automated_sell_disabled"


# ---------------------------------------------------------------------------
# F7 — a duplicate-token preview answers from the persisted packet
# ---------------------------------------------------------------------------
async def test_duplicate_preview_returns_persisted_expiry(broker):
    sid = await _seed_snapshot()
    first = await alpaca_paper_automated_preview_order(
        **_crypto_intent(sid, valid_for_seconds=300)
    )
    # A second preview of the SAME trusted decision must echo the ORIGINAL
    # persisted expiry/hash, not a locally rebuilt one.
    second = await alpaca_paper_automated_preview_order(
        **_crypto_intent(sid, valid_for_seconds=999)
    )
    assert first["approval_token"] == second["approval_token"]
    assert second["expires_at"] == first["expires_at"]
    assert second["provenance"]["packet_hash"] == first["provenance"]["packet_hash"]


# ---------------------------------------------------------------------------
# F3 — public success contract at the handler (422 => success=false, replay)
# ---------------------------------------------------------------------------
class _RaisingBroker(CountingBroker):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def submit_order(self, request):  # type: ignore[override]
        self.submit_calls.append(request)
        raise self._exc


async def test_handler_http_422_success_false_and_terminal_replay(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", True)
    raising = _RaisingBroker(AlpacaPaperRequestError("bad", status_code=422))
    set_alpaca_paper_automated_factories(
        session_factory=lambda: AsyncSessionLocal, broker_factory=lambda: raising
    )
    try:
        sid = await _seed_snapshot()
        token = (await alpaca_paper_automated_preview_order(**_crypto_intent(sid)))[
            "approval_token"
        ]
        first = await alpaca_paper_automated_submit_order(token, confirm=True)
        assert first["status"] == "failed"
        assert first["success"] is False  # failed is NOT success

        second = await alpaca_paper_automated_submit_order(token, confirm=True)
        assert second["status"] == "failed"
        assert second["success"] is False
        assert second["reason_code"] == "broker_rejected_replayed"
        assert len(raising.submit_calls) == 1  # terminal — no re-POST
    finally:
        reset_alpaca_paper_automated_factories()
