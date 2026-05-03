"""Tests for PaperApprovalPacket schema and verifiers (ROB-91).

Covers:
- Schema validation: required fields, frozen model, extra-field rejection,
  timezone-aware expiry, signal/execution mismatch, exactly-one max guard,
  lifecycle step constraint.
- verify_packet_freshness: stale, fresh, naive now.
- verify_packet_idempotency: no prior execution (pass), existing row (fail).
- verify_sell_packet_source: buy skips check, sell scenarios (pass/fail).
- TestRob91AcceptanceScenarios: named acceptance test class.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
_PAST = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_packet(**overrides: Any):
    from app.services.paper_approval_packet import PaperApprovalPacket

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
        "lifecycle_correlation_id": "corr-test-001",
        "client_order_id": "buy-test-001",
        "expires_at": _FUTURE,
    }
    defaults.update(overrides)
    return PaperApprovalPacket(**defaults)


def _make_ledger_row(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "id": 1,
        "client_order_id": "buy-test-001",
        "lifecycle_correlation_id": "corr-test-001",
        "record_kind": "execution",
        "lifecycle_state": "position_reconciled",
        "execution_symbol": "BTC/USD",
        "side": "buy",
        "filled_qty": Decimal("0.001"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_ledger(
    *,
    find_executed_result: Any = None,
    list_by_correlation_result: list[Any] | None = None,
) -> Any:
    ledger = MagicMock()
    ledger.find_executed_by_client_order_id = AsyncMock(
        return_value=find_executed_result
    )
    ledger.list_by_correlation_id = AsyncMock(
        return_value=list_by_correlation_result or []
    )
    return ledger


# ---------------------------------------------------------------------------
# Schema: basic construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_packet_constructs_with_max_notional():
    packet = _make_packet()
    assert packet.signal_symbol == "KRW-BTC"
    assert packet.execution_symbol == "BTC/USD"
    assert packet.max_notional == Decimal("10")
    assert packet.max_qty is None


@pytest.mark.unit
def test_packet_constructs_with_max_qty():
    packet = _make_packet(max_notional=None, max_qty=Decimal("0.001"))
    assert packet.max_qty == Decimal("0.001")
    assert packet.max_notional is None


@pytest.mark.unit
def test_packet_is_frozen():
    from pydantic import ValidationError

    packet = _make_packet()
    with pytest.raises((ValidationError, TypeError)):
        packet.side = "sell"  # type: ignore[misc]


@pytest.mark.unit
def test_packet_forbids_extra_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="extra"):
        _make_packet(unknown_field="bad")


# ---------------------------------------------------------------------------
# Schema: expires_at must be timezone-aware
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_packet_rejects_naive_expires_at():
    from pydantic import ValidationError

    naive = datetime(2030, 1, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValidationError, match="timezone-aware"):
        _make_packet(expires_at=naive)


@pytest.mark.unit
def test_packet_accepts_non_utc_aware_expires_at():
    tz_plus9 = timezone(timedelta(hours=9))
    aware = datetime(2030, 1, 1, 21, 0, 0, tzinfo=tz_plus9)
    packet = _make_packet(expires_at=aware)
    assert packet.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Schema: exactly-one max guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_packet_rejects_both_max_notional_and_max_qty():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not both"):
        _make_packet(max_notional=Decimal("10"), max_qty=Decimal("0.001"))


@pytest.mark.unit
def test_packet_rejects_neither_max_guard():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_packet(max_notional=None, max_qty=None)


@pytest.mark.unit
def test_packet_rejects_zero_max_notional():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="positive"):
        _make_packet(max_notional=Decimal("0"))


@pytest.mark.unit
def test_packet_rejects_negative_max_qty():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="positive"):
        _make_packet(max_notional=None, max_qty=Decimal("-1"))


# ---------------------------------------------------------------------------
# Schema: expected_lifecycle_step constraint
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("step", ["planned", "previewed", "validated", "submitted"])
def test_packet_accepts_valid_lifecycle_steps(step: str):
    packet = _make_packet(expected_lifecycle_step=step)
    assert packet.expected_lifecycle_step == step


@pytest.mark.unit
@pytest.mark.parametrize(
    "step", ["filled", "closed", "anomaly", "final_reconciled", "unknown"]
)
def test_packet_rejects_invalid_lifecycle_steps(step: str):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="expected_lifecycle_step"):
        _make_packet(expected_lifecycle_step=step)


# ---------------------------------------------------------------------------
# Schema: signal/execution symbol mapping for crypto
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_packet_rejects_unsupported_upbit_signal_symbol():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not supported"):
        _make_packet(signal_symbol="KRW-DOGE", execution_symbol="DOGE/USD")


@pytest.mark.unit
def test_packet_rejects_wrong_execution_symbol_for_known_signal():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="does not match"):
        _make_packet(signal_symbol="KRW-BTC", execution_symbol="ETH/USD")


@pytest.mark.unit
@pytest.mark.parametrize(
    "signal,execution",
    [
        ("KRW-BTC", "BTC/USD"),
        ("KRW-ETH", "ETH/USD"),
        ("KRW-SOL", "SOL/USD"),
    ],
)
def test_packet_accepts_valid_crypto_mappings(signal: str, execution: str):
    packet = _make_packet(signal_symbol=signal, execution_symbol=execution)
    assert packet.signal_symbol == signal
    assert packet.execution_symbol == execution


# ---------------------------------------------------------------------------
# verify_packet_freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_freshness_passes_when_now_before_expires_at():
    from app.services.paper_approval_packet import verify_packet_freshness

    packet = _make_packet(expires_at=_FUTURE)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    verify_packet_freshness(packet, now=now)  # must not raise


@pytest.mark.unit
def test_freshness_raises_stale_when_now_equals_expires_at():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_packet_freshness,
    )

    expires = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    packet = _make_packet(expires_at=expires)
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        verify_packet_freshness(packet, now=expires)
    assert exc_info.value.code == "stale_packet"


@pytest.mark.unit
def test_freshness_raises_stale_when_now_after_expires_at():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_packet_freshness,
    )

    expires = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 4, 13, 0, 0, tzinfo=UTC)
    packet = _make_packet(expires_at=expires)
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        verify_packet_freshness(packet, now=now)
    assert exc_info.value.code == "stale_packet"


@pytest.mark.unit
def test_freshness_raises_naive_now():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_packet_freshness,
    )

    packet = _make_packet(expires_at=_FUTURE)
    naive_now = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        verify_packet_freshness(packet, now=naive_now)
    assert exc_info.value.code == "naive_now"


# ---------------------------------------------------------------------------
# verify_packet_idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_idempotency_passes_when_no_execution_row():
    from app.services.paper_approval_packet import verify_packet_idempotency

    packet = _make_packet()
    ledger = _mock_ledger(find_executed_result=None)
    await verify_packet_idempotency(packet, ledger=ledger)  # must not raise
    ledger.find_executed_by_client_order_id.assert_called_once_with(
        packet.client_order_id
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_idempotency_raises_duplicate_when_execution_exists():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_packet_idempotency,
    )

    existing = _make_ledger_row(lifecycle_state="filled")
    ledger = _mock_ledger(find_executed_result=existing)
    packet = _make_packet()
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_packet_idempotency(packet, ledger=ledger)
    assert exc_info.value.code == "duplicate_client_order_id"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_idempotency_raises_duplicate_for_submitted_state():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_packet_idempotency,
    )

    existing = _make_ledger_row(lifecycle_state="submitted")
    ledger = _mock_ledger(find_executed_result=existing)
    packet = _make_packet()
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_packet_idempotency(packet, ledger=ledger)
    assert exc_info.value.code == "duplicate_client_order_id"


# ---------------------------------------------------------------------------
# verify_sell_packet_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_passes_for_buy_packet():
    """Buy packets must skip source check entirely."""
    from app.services.paper_approval_packet import verify_sell_packet_source

    packet = _make_packet(side="buy")
    ledger = _mock_ledger()
    await verify_sell_packet_source(packet, ledger=ledger)  # must not raise
    ledger.list_by_correlation_id.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_passes_with_valid_reconciled_buy():
    """A sell packet with exactly one reconciled buy source must pass."""
    from app.services.paper_approval_packet import verify_sell_packet_source

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.002"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=None,
        max_qty=Decimal("0.001"),
        qty_source="ledger_filled_qty",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    await verify_sell_packet_source(packet, ledger=ledger)  # must not raise


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_invalid_qty_source():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="manual",  # not allowed for sell
    )
    ledger = _mock_ledger()
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "invalid_qty_source"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_missing_source_when_no_rows():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="ledger_filled_qty",
    )
    ledger = _mock_ledger(list_by_correlation_result=[])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "missing_source_order"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_missing_source_when_only_sell_rows():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    sell_row = _make_ledger_row(
        side="sell",
        record_kind="execution",
        lifecycle_state="closed",
    )
    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="ledger_filled_qty",
    )
    ledger = _mock_ledger(list_by_correlation_result=[sell_row])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "missing_source_order"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_multiple_source_orders():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    buy1 = _make_ledger_row(
        id=1,
        client_order_id="buy-001",
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.001"),
    )
    buy2 = _make_ledger_row(
        id=2,
        client_order_id="buy-002",
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.001"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="ledger_filled_qty",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy1, buy2])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "multiple_source_orders"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_source_not_reconciled():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="submitted",  # not reconciled
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.001"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="ledger_filled_qty",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "source_not_reconciled"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_wrong_symbol():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="ETH/USD",  # different from packet
        filled_qty=Decimal("0.5"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=Decimal("10"),
        qty_source="ledger_filled_qty",
        signal_symbol="KRW-BTC",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "wrong_symbol"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_raises_qty_exceeds_source():
    from app.services.paper_approval_packet import (
        PaperApprovalPacketError,
        verify_sell_packet_source,
    )

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.001"),  # source only has 0.001
    )
    packet = _make_packet(
        side="sell",
        max_notional=None,
        max_qty=Decimal("0.005"),  # requesting more than available
        qty_source="ledger_filled_qty",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    with pytest.raises(PaperApprovalPacketError) as exc_info:
        await verify_sell_packet_source(packet, ledger=ledger)
    assert exc_info.value.code == "qty_exceeds_source"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_passes_with_notional_no_qty_check():
    """max_notional sell packets skip qty_exceeds_source check."""
    from app.services.paper_approval_packet import verify_sell_packet_source

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="position_reconciled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.001"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=Decimal("500"),  # notional, not qty
        qty_source="ledger_filled_qty",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    await verify_sell_packet_source(packet, ledger=ledger)  # must not raise


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sell_source_passes_with_filled_state_buy():
    """filled state is in reconciled buy states as a conservative inclusion."""
    from app.services.paper_approval_packet import verify_sell_packet_source

    buy_row = _make_ledger_row(
        side="buy",
        record_kind="execution",
        lifecycle_state="filled",
        execution_symbol="BTC/USD",
        filled_qty=Decimal("0.002"),
    )
    packet = _make_packet(
        side="sell",
        max_notional=None,
        max_qty=Decimal("0.001"),
        qty_source="reconcile_filled_qty",
        execution_symbol="BTC/USD",
    )
    ledger = _mock_ledger(list_by_correlation_result=[buy_row])
    await verify_sell_packet_source(packet, ledger=ledger)  # must not raise


# ---------------------------------------------------------------------------
# PaperApprovalPacketError repr and code access
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_packet_error_has_code_attribute():
    from app.services.paper_approval_packet import PaperApprovalPacketError

    err = PaperApprovalPacketError(code="stale_packet", message="expired")
    assert err.code == "stale_packet"
    assert str(err) == "expired"
    assert isinstance(err, ValueError)


@pytest.mark.unit
def test_packet_error_repr():
    from app.services.paper_approval_packet import PaperApprovalPacketError

    err = PaperApprovalPacketError(code="duplicate_client_order_id", message="dup")
    assert "duplicate_client_order_id" in repr(err)


# ---------------------------------------------------------------------------
# Module-level static safety checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_paper_approval_packet_module_has_no_broker_calls():
    """The module must contain no broker submit/cancel/modify calls or DB mutations."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/paper_approval_packet.py"
    ).read_text()

    # String-based checks for terms that must not appear anywhere (even in strings)
    string_forbidden = [
        "submit_order",
        "cancel_order",
        "place_order",
        "modify_order",
        "INSERT",
        "UPDATE",
        "DELETE",
        "commit()",
    ]
    for term in string_forbidden:
        assert term not in source, f"Forbidden term found in packet module: {term!r}"

    # AST-based check: datetime.now() must not be called in any function body
    tree = ast.parse(source)
    datetime_now_calls: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # skip string constants (docstrings)
        if isinstance(node, ast.Call):
            # datetime.now(...) or datetime.now()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "now"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "datetime"
            ):
                datetime_now_calls.append(getattr(node, "lineno", 0))

    assert not datetime_now_calls, (
        f"datetime.now() call(s) found in verifier module at lines: {datetime_now_calls}"
    )


@pytest.mark.unit
def test_paper_approval_packet_module_is_valid_python():
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/paper_approval_packet.py"
    ).read_text()
    tree = ast.parse(source)
    assert tree is not None


# ---------------------------------------------------------------------------
# TestRob91AcceptanceScenarios — named acceptance criteria
# ---------------------------------------------------------------------------


class TestRob91AcceptanceScenarios:
    """Named acceptance tests covering the four ROB-91 acceptance criteria."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_duplicate_client_order_id_cannot_execute_twice(self):
        """AC: A client_order_id that already executed must be rejected before broker call."""
        from app.services.paper_approval_packet import (
            PaperApprovalPacketError,
            verify_packet_idempotency,
        )

        existing_row = _make_ledger_row(lifecycle_state="filled")
        ledger = _mock_ledger(find_executed_result=existing_row)

        packet = _make_packet(client_order_id="dup-order-001")
        with pytest.raises(PaperApprovalPacketError) as exc_info:
            await verify_packet_idempotency(packet, ledger=ledger)

        assert exc_info.value.code == "duplicate_client_order_id"
        assert "dup-order-001" in str(exc_info.value)

    @pytest.mark.unit
    def test_stale_packet_fails_closed(self):
        """AC: A packet whose expires_at is in the past must be rejected."""
        from app.services.paper_approval_packet import (
            PaperApprovalPacketError,
            verify_packet_freshness,
        )

        past_expiry = datetime(2020, 6, 1, 0, 0, 0, tzinfo=UTC)
        packet = _make_packet(expires_at=past_expiry)
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)

        with pytest.raises(PaperApprovalPacketError) as exc_info:
            verify_packet_freshness(packet, now=now)

        assert exc_info.value.code == "stale_packet"

    @pytest.mark.unit
    def test_wrong_symbol_payload_fails_closed(self):
        """AC: A packet whose execution_symbol does not match the signal mapping must be rejected at schema construction."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            _make_packet(
                signal_symbol="KRW-BTC",
                execution_symbol="SOL/USD",  # wrong symbol for BTC signal
            )

        error_text = str(exc_info.value)
        assert "SOL/USD" in error_text or "does not match" in error_text

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_sell_missing_source_order_fails_closed(self):
        """AC: A sell packet with no prior buy source in the ledger must be rejected."""
        from app.services.paper_approval_packet import (
            PaperApprovalPacketError,
            verify_sell_packet_source,
        )

        packet = _make_packet(
            side="sell",
            max_notional=Decimal("10"),
            qty_source="ledger_filled_qty",
            lifecycle_correlation_id="corr-no-buy",
        )
        # Empty ledger — no buy rows at all
        ledger = _mock_ledger(list_by_correlation_result=[])

        with pytest.raises(PaperApprovalPacketError) as exc_info:
            await verify_sell_packet_source(packet, ledger=ledger)

        assert exc_info.value.code == "missing_source_order"
        assert "corr-no-buy" in str(exc_info.value)
