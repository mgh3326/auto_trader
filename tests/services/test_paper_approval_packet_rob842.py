"""ROB-842 packet-contract verifiers: market-data, account-mode, hash, server key.

Pure/side-effect-free tests for the Alpaca-complete additions to
``app.services.paper_approval_packet``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.alpaca_paper_submit_service import (
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
    derive_client_order_id,
)
from app.services.paper_approval_packet import (
    PaperApprovalPacket,
    PaperApprovalPacketError,
    verify_order_within_packet,
    verify_packet_account_mode,
    verify_packet_market_data,
    verify_preview_submit_hash,
    verify_server_derived_key,
)

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
_FUTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)

_CANONICAL = build_canonical_payload(
    symbol="BTC/USD",
    side="buy",
    type="limit",
    time_in_force="gtc",
    qty=None,
    notional=Decimal("10"),
    limit_price=Decimal("50000"),
    asset_class="crypto",
)
_COID = derive_client_order_id(_CANONICAL)
_HASH = canonical_hash(_CANONICAL)


def _make_packet(**overrides: Any) -> PaperApprovalPacket:
    defaults: dict[str, Any] = {
        "signal_source": "test_signal",
        "artifact_id": uuid.uuid4(),
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTC/USD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "side": "buy",
        "max_notional": Decimal("10"),
        "qty_source": "notional_estimate",
        "expected_lifecycle_step": "previewed",
        "lifecycle_correlation_id": "corr-842",
        "client_order_id": _COID,
        "expires_at": _FUTURE,
        "account_mode": "alpaca_paper",
        "origin": "automated",
        "market_data_asof": _NOW - timedelta(seconds=30),
        "market_data_source": "upbit_ticker",
        "preview_payload_hash": _HASH,
    }
    defaults.update(overrides)
    return PaperApprovalPacket(**defaults)


# ---------------------------------------------------------------------------
# Backward-compat: ROB-91 producers without the new fields still validate.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_new_fields_are_optional_backward_compatible() -> None:
    packet = PaperApprovalPacket(
        signal_source="s",
        artifact_id=uuid.uuid4(),
        signal_symbol="KRW-BTC",
        signal_venue="upbit",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        side="buy",
        max_notional=Decimal("10"),
        qty_source="notional_estimate",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id="corr",
        client_order_id="buy-1",
        expires_at=_FUTURE,
    )
    assert packet.account_mode == "alpaca_paper"
    assert packet.origin == "manual"
    assert packet.market_data_asof is None
    assert packet.preview_payload_hash is None


# ---------------------------------------------------------------------------
# Market-data source freshness
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_market_data_fresh_passes() -> None:
    verify_packet_market_data(_make_packet(), now=_NOW, max_age=timedelta(minutes=5))


@pytest.mark.unit
def test_market_data_missing_source_timestamp() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(market_data_asof=None), now=_NOW, max_age=timedelta(minutes=5)
        )
    assert exc.value.code == "missing_source_timestamp"


@pytest.mark.unit
def test_market_data_naive_source_timestamp() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(market_data_asof=datetime(2026, 7, 12, 12, 0, 0)),
            now=_NOW,
            max_age=timedelta(minutes=5),
        )
    assert exc.value.code == "naive_source_timestamp"


@pytest.mark.unit
def test_market_data_stale_quote() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(market_data_asof=_NOW - timedelta(minutes=30)),
            now=_NOW,
            max_age=timedelta(minutes=5),
        )
    assert exc.value.code == "stale_quote"


@pytest.mark.unit
def test_market_data_naive_now_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(),
            now=datetime(2026, 7, 12, 12, 0, 0),
            max_age=timedelta(minutes=5),
        )
    assert exc.value.code == "naive_now"


# ---------------------------------------------------------------------------
# Account mode
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_account_mode_match_passes() -> None:
    verify_packet_account_mode(_make_packet(), expected="alpaca_paper")


@pytest.mark.unit
def test_account_mode_mismatch_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_account_mode(
            _make_packet(account_mode="alpaca_live"), expected="alpaca_paper"
        )
    assert exc.value.code == "account_mode_mismatch"


# ---------------------------------------------------------------------------
# Preview↔submit hash
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_preview_hash_match_passes() -> None:
    verify_preview_submit_hash(_make_packet(), submit_hash=_HASH)


@pytest.mark.unit
def test_preview_hash_mismatch_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_preview_submit_hash(_make_packet(), submit_hash="deadbeef")
    assert exc.value.code == "preview_hash_mismatch"


@pytest.mark.unit
def test_preview_hash_missing_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_preview_submit_hash(
            _make_packet(preview_payload_hash=None), submit_hash=_HASH
        )
    assert exc.value.code == "missing_preview_hash"


# ---------------------------------------------------------------------------
# Server-derived key / caller-id bypass guard
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_server_key_match_passes() -> None:
    verify_server_derived_key(_make_packet(), server_key=_COID)


@pytest.mark.unit
def test_server_key_mismatch_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_server_derived_key(
            _make_packet(client_order_id="rob74-crypto-tampered0000000"),
            server_key=_COID,
        )
    assert exc.value.code == "server_key_mismatch"


@pytest.mark.unit
def test_caller_supplied_id_matching_server_key_passes() -> None:
    verify_server_derived_key(
        _make_packet(), server_key=_COID, caller_client_order_id=_COID
    )


@pytest.mark.unit
def test_caller_supplied_id_bypass_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_server_derived_key(
            _make_packet(),
            server_key=_COID,
            caller_client_order_id="attacker-chosen-id",
        )
    assert exc.value.code == "caller_id_mismatch"


# ---------------------------------------------------------------------------
# Market-data future timestamp / missing source (blocker 3)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_market_data_future_source_timestamp_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(market_data_asof=_NOW + timedelta(minutes=1)),
            now=_NOW,
            max_age=timedelta(minutes=5),
        )
    assert exc.value.code == "future_source_timestamp"


@pytest.mark.unit
def test_market_data_missing_source_label_rejected() -> None:
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_packet_market_data(
            _make_packet(market_data_source=None),
            now=_NOW,
            max_age=timedelta(minutes=5),
        )
    assert exc.value.code == "missing_market_data_source"


# ---------------------------------------------------------------------------
# Order-within-packet authority binding (blocker 3)
# ---------------------------------------------------------------------------
def _bound_packet(**overrides: Any) -> PaperApprovalPacket:
    return _make_packet(
        execution_order_type="limit",
        execution_time_in_force="gtc",
        **overrides,
    )


@pytest.mark.unit
def test_order_within_packet_passes_for_matching_order() -> None:
    verify_order_within_packet(_bound_packet(), _CANONICAL)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ({"symbol": "ETH/USD"}, "order_symbol_mismatch"),
        ({"side": "sell"}, "order_side_mismatch"),
        ({"asset_class": "us_equity"}, "order_asset_class_mismatch"),
        ({"type": "market"}, "order_type_mismatch"),
        ({"time_in_force": "ioc"}, "order_tif_mismatch"),
    ],
)
def test_order_within_packet_field_mismatches(bad: dict[str, Any], code: str) -> None:
    canonical = dict(_CANONICAL)
    canonical.update(bad)
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_order_within_packet(_bound_packet(), canonical)
    assert exc.value.code == code


@pytest.mark.unit
def test_order_within_packet_notional_exceeds_max() -> None:
    canonical = dict(_CANONICAL)
    canonical["notional"] = "50"
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_order_within_packet(_bound_packet(max_notional=Decimal("10")), canonical)
    assert exc.value.code == "notional_exceeds_max"


@pytest.mark.unit
def test_order_within_packet_market_qty_bounded_by_reference_price() -> None:
    # A market qty order (no limit price) is bounded via the packet's trusted
    # reference price so it cannot bypass max_notional (ROB-842 F4).
    canonical = build_canonical_payload(
        symbol="BTC/USD",
        side="buy",
        type="market",
        time_in_force="gtc",
        qty=Decimal("5"),
        notional=None,
        limit_price=None,
        asset_class="crypto",
    )
    packet = _make_packet(
        execution_order_type="market",
        execution_time_in_force="gtc",
        max_notional=Decimal("1000"),
        max_qty=None,
        reference_price=Decimal("100000"),  # 5 * 100000 = 500000 > 1000
    )
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_order_within_packet(packet, canonical)
    assert exc.value.code == "notional_exceeds_max"


@pytest.mark.unit
def test_order_within_packet_qty_exceeds_max() -> None:
    canonical = build_canonical_payload(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        time_in_force="gtc",
        qty=Decimal("2"),
        notional=None,
        limit_price=Decimal("5"),
        asset_class="crypto",
    )
    packet = _bound_packet(max_notional=None, max_qty=Decimal("1"))
    with pytest.raises(PaperApprovalPacketError) as exc:
        verify_order_within_packet(packet, canonical)
    assert exc.value.code == "qty_exceeds_max"


# ---------------------------------------------------------------------------
# Server-owned idempotency scope (blocker 4)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_automated_key_stable_for_same_intent() -> None:
    a = derive_automated_key(
        correlation_id="run-1", snapshot_id="s1", canonical=_CANONICAL
    )
    b = derive_automated_key(
        correlation_id="run-1", snapshot_id="s1", canonical=_CANONICAL
    )
    assert a == b


@pytest.mark.unit
def test_automated_key_differs_across_runs_with_identical_economics() -> None:
    a = derive_automated_key(
        correlation_id="run-1", snapshot_id="s1", canonical=_CANONICAL
    )
    b = derive_automated_key(
        correlation_id="run-2", snapshot_id="s1", canonical=_CANONICAL
    )
    c = derive_automated_key(
        correlation_id="run-1", snapshot_id="s2", canonical=_CANONICAL
    )
    assert a != b
    assert a != c
    # economics-only manual key would have collided:
    assert derive_client_order_id(_CANONICAL) == derive_client_order_id(_CANONICAL)


# ---------------------------------------------------------------------------
# Sell source fail-close: only a confirmed real holding may back a sell (blocker 5)
# ---------------------------------------------------------------------------
def _sell_packet(**overrides: Any) -> PaperApprovalPacket:
    return _make_packet(
        side="sell",
        max_notional=None,
        max_qty=Decimal("0.001"),
        qty_source="ledger_filled_qty",
        **overrides,
    )


def _sell_ledger(source_row: Any) -> Any:
    ledger = MagicMock()
    ledger.list_by_correlation_id = AsyncMock(return_value=[source_row])
    return ledger


def _reconciled_buy(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "side": "buy",
        "record_kind": "execution",
        "lifecycle_state": "position_reconciled",
        "execution_symbol": "BTC/USD",
        "filled_qty": Decimal("0.002"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_source_missing_filled_qty_fails_close() -> None:
    from app.services.paper_approval_packet import verify_sell_packet_source

    ledger = _sell_ledger(_reconciled_buy(filled_qty=None))
    with pytest.raises(PaperApprovalPacketError) as exc:
        await verify_sell_packet_source(_sell_packet(), ledger=ledger)
    assert exc.value.code == "source_filled_qty_unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_source_unparseable_filled_qty_fails_close() -> None:
    from app.services.paper_approval_packet import verify_sell_packet_source

    ledger = _sell_ledger(_reconciled_buy(filled_qty="not-a-number"))
    with pytest.raises(PaperApprovalPacketError) as exc:
        await verify_sell_packet_source(_sell_packet(), ledger=ledger)
    assert exc.value.code == "source_filled_qty_unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_source_nonpositive_filled_qty_fails_close() -> None:
    from app.services.paper_approval_packet import verify_sell_packet_source

    ledger = _sell_ledger(_reconciled_buy(filled_qty=Decimal("0")))
    with pytest.raises(PaperApprovalPacketError) as exc:
        await verify_sell_packet_source(_sell_packet(), ledger=ledger)
    assert exc.value.code == "source_filled_qty_unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_source_confirmed_holding_passes() -> None:
    from app.services.paper_approval_packet import verify_sell_packet_source

    ledger = _sell_ledger(_reconciled_buy(filled_qty=Decimal("0.002")))
    await verify_sell_packet_source(_sell_packet(), ledger=ledger)  # must not raise
