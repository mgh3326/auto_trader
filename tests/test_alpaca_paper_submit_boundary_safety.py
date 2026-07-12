"""ROB-842 safety guards for the unified Alpaca paper submit boundary.

- paper-host pin preserved; no live endpoint / live-credential import or reference
  in the new application-service or packet modules (AST/text scan);
- no new idempotency store/table/column/migration — the existing native
  ``review.alpaca_paper_order_ledger`` remains the sole lifecycle/result source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBMIT_SERVICE = REPO_ROOT / "app/services/alpaca_paper_submit_service.py"
PACKET = REPO_ROOT / "app/services/paper_approval_packet.py"
LEDGER = REPO_ROOT / "app/services/alpaca_paper_ledger_service.py"
AUTOMATED_TOOLS = REPO_ROOT / "app/mcp_server/tooling/alpaca_paper_automated_orders.py"

_LIVE_MARKERS = (
    "api.alpaca.markets",
    "LIVE_TRADING_BASE_URL",
    "alpaca_live",
)


# ---------------------------------------------------------------------------
# Paper-host pin / no live endpoint or credential reference
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("path", [SUBMIT_SERVICE, PACKET, AUTOMATED_TOOLS])
def test_new_modules_have_no_live_endpoint_or_credential_reference(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for marker in _LIVE_MARKERS:
        assert marker not in text, (
            f"{path.name} references live marker {marker!r}; paper boundary only"
        )


@pytest.mark.unit
def test_submit_service_only_broker_surface_is_paper_service() -> None:
    """The only broker service the coordinator can name is the paper-pinned one."""
    text = SUBMIT_SERVICE.read_text(encoding="utf-8")
    assert "AlpacaPaperBrokerService" in text
    # No live service/class name variants.
    assert "AlpacaLiveBrokerService" not in text
    assert "LiveBrokerService" not in text


@pytest.mark.unit
def test_paper_broker_service_still_rejects_live_base_url() -> None:
    """The injected broker remains paper-host-pinned (fail-closed on live)."""
    from app.services.brokers.alpaca.config import AlpacaPaperSettings
    from app.services.brokers.alpaca.endpoints import LIVE_TRADING_BASE_URL
    from app.services.brokers.alpaca.exceptions import AlpacaPaperEndpointError
    from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

    with pytest.raises(AlpacaPaperEndpointError):
        AlpacaPaperBrokerService(
            settings=AlpacaPaperSettings(
                api_key="pk", api_secret="sk", base_url=LIVE_TRADING_BASE_URL
            )
        )


@pytest.mark.unit
def test_coordinator_never_builds_broker_on_rejected_packet() -> None:
    """A rejected packet must not even construct the broker (no host touch)."""
    import asyncio
    import uuid
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.services.alpaca_paper_submit_service import (
        AlpacaPaperSubmitCoordinator,
        build_canonical_payload,
        canonical_hash,
        derive_automated_key,
    )
    from app.services.paper_approval_packet import PaperApprovalPacket

    canonical = build_canonical_payload(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        time_in_force="gtc",
        qty=None,
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )

    def _explode():
        raise AssertionError("broker factory must not be called on a rejected packet")

    coord = AlpacaPaperSubmitCoordinator(
        ledger=_UnusedLedger(),
        broker_factory=_explode,
        now_fn=lambda: datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC),
    )
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
        client_order_id=derive_automated_key(
            correlation_id="corr", snapshot_id="snap", canonical=canonical
        ),
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        account_mode="alpaca_live",  # <- rejection trigger (after key/order checks)
        origin="automated",
        market_data_asof=datetime(2026, 7, 12, 11, 59, 50, tzinfo=UTC),
        market_data_source="upbit_ticker",
        preview_payload_hash=canonical_hash(canonical),
        snapshot_id="snap",
        execution_order_type="limit",
        execution_time_in_force="gtc",
    )
    outcome = asyncio.run(coord.submit(packet, submit_canonical=canonical))
    assert outcome.status == "rejected"
    assert outcome.reason_code == "account_mode_mismatch"
    assert outcome.broker_called is False


class _UnusedLedger:
    """Ledger stub whose methods must never be reached for a rejected packet."""

    @property
    def session(self):  # pragma: no cover - defensive
        raise AssertionError("ledger must not be touched on a rejected packet")


# ---------------------------------------------------------------------------
# No new idempotency store / table / column / migration
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_no_idempotency_table_in_metadata() -> None:
    from app.models.base import Base

    names = set(Base.metadata.tables.keys())
    offenders = [n for n in names if "idempotency" in n.lower()]
    assert not offenders, f"unexpected idempotency table(s): {offenders}"


@pytest.mark.unit
def test_alpaca_paper_ledger_gained_no_new_columns() -> None:
    """The hash/idempotency key live on the packet, never as a new ledger column."""
    from app.models.review import AlpacaPaperOrderLedger

    cols = {c.name for c in AlpacaPaperOrderLedger.__table__.columns}
    forbidden = {
        "idempotency_key",
        "preview_payload_hash",
        "submit_hash",
        "claim_key",
        "approval_hash",
    }
    assert forbidden.isdisjoint(cols), (
        f"ROB-842 must add no ledger columns; found {forbidden & cols}"
    )


@pytest.mark.unit
def test_submit_service_defines_no_schema_and_no_raw_writes() -> None:
    """The application service owns no schema and issues no direct table writes."""
    tree = ast.parse(SUBMIT_SERVICE.read_text(encoding="utf-8"))
    banned_calls = {"create_table", "add_column", "pg_insert", "insert"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = getattr(base, "id", getattr(base, "attr", ""))
                assert base_name != "Base", "submit service must not define ORM models"
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            assert name not in banned_calls, (
                f"submit service must delegate persistence, not call {name!r}"
            )


@pytest.mark.unit
def test_atomic_claim_targets_existing_ledger_table_only() -> None:
    """claim_submit writes only the existing alpaca_paper_order_ledger table."""
    from app.models.review import AlpacaPaperOrderLedger
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    assert AlpacaPaperOrderLedger.__tablename__ == "alpaca_paper_order_ledger"
    assert hasattr(AlpacaPaperLedgerService, "claim_submit")
