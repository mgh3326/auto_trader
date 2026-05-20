"""ROB-286 — BinanceTestnetLedgerService tests.

Matrix rows T24-T27.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.testnet.errors import (
    BinanceInvalidStateTransition,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)


@pytest_asyncio.fixture
async def instrument(db_session) -> CryptoInstrument:
    """Find-or-create a Binance spot BTCUSDT instrument for the ledger to reference.

    The shared ``db_session`` fixture does not roll back between tests, so the
    unique ``(venue, product, venue_symbol)`` row may already exist from a
    prior test. Use find-or-create semantics to stay idempotent.
    """
    from sqlalchemy import select

    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == "BTCUSDT",
        )
    )
    if existing is not None:
        return existing
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst


@pytest.mark.asyncio
async def test_record_plan_creates_row(
    db_session, instrument: CryptoInstrument
) -> None:
    svc = BinanceTestnetLedgerService(session=db_session)
    row = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-1",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.001"),
        price=Decimal("50000"),
        notional_usdt=Decimal("5"),
    )
    assert row.lifecycle_state == "planned"
    assert row.planned_at is not None
    assert row.client_order_id == "ledger-test-1"


@pytest.mark.asyncio
async def test_record_plan_is_idempotent(
    db_session, instrument: CryptoInstrument
) -> None:
    """T24 — Re-recording the same plan is a no-op (returns existing row)."""
    svc = BinanceTestnetLedgerService(session=db_session)
    row1 = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-2",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    row2 = await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-2",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    assert row1.id == row2.id
    assert row1.lifecycle_state == row2.lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_record_submit_idempotent(
    db_session, instrument: CryptoInstrument
) -> None:
    """T24 — Re-recording the same submit is a no-op."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-3",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id="ledger-test-3")
    await svc.record_validation(client_order_id="ledger-test-3")
    row1 = await svc.record_submit(
        client_order_id="ledger-test-3",
        broker_order_id="binance-1",
    )
    row2 = await svc.record_submit(
        client_order_id="ledger-test-3",
        broker_order_id="binance-1",
    )
    assert row1.id == row2.id
    assert row1.lifecycle_state == row2.lifecycle_state == "submitted"
    assert row1.broker_order_id == "binance-1"


@pytest.mark.asyncio
async def test_invalid_transition_raises(
    db_session, instrument: CryptoInstrument
) -> None:
    """T25 — Jumping states (planned → filled) is refused."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-4",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    with pytest.raises(BinanceInvalidStateTransition):
        # planned → filled is illegal; must go via previewed → validated → submitted.
        await svc.record_fill(client_order_id="ledger-test-4")


@pytest.mark.asyncio
async def test_transition_on_missing_row_raises(db_session) -> None:
    svc = BinanceTestnetLedgerService(session=db_session)
    with pytest.raises(BinanceInvalidStateTransition):
        await svc.record_submit(
            client_order_id="never-seen", broker_order_id="binance-x"
        )


@pytest.mark.asyncio
async def test_full_happy_path_lifecycle(
    db_session, instrument: CryptoInstrument
) -> None:
    """End-to-end: planned → previewed → validated → submitted → filled →
    tp_sl_armed → tp_sl_triggered → closed → reconciled."""
    svc = BinanceTestnetLedgerService(session=db_session)
    cid = "ledger-test-happy"
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id=cid)
    await svc.record_validation(client_order_id=cid)
    await svc.record_submit(client_order_id=cid, broker_order_id="binance-z")
    await svc.record_fill(client_order_id=cid)
    await svc.record_tp_sl_armed(client_order_id=cid)
    await svc.record_tp_sl_triggered(client_order_id=cid)
    await svc.record_closed(client_order_id=cid)
    row = await svc.record_reconciled(client_order_id=cid)
    assert row.lifecycle_state == "reconciled"
    assert row.reconciled_at is not None
    assert row.last_reconciled_at is not None


@pytest.mark.asyncio
async def test_anomaly_emits_sentry(
    db_session, instrument: CryptoInstrument, mocker
) -> None:
    """T26 — Recording an anomaly emits a Sentry message."""
    svc = BinanceTestnetLedgerService(session=db_session)
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id="ledger-test-anomaly",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    # Patch sentry_sdk.capture_message at import location.
    import sentry_sdk

    mock_capture = mocker.patch.object(sentry_sdk, "capture_message")
    row = await svc.record_anomaly(
        client_order_id="ledger-test-anomaly",
        reason="reconcile_drift",
    )
    assert row.lifecycle_state == "anomaly"
    assert row.anomaly_reason == "reconcile_drift"
    # Anomaly emits.
    assert mock_capture.called
    call_kwargs = mock_capture.call_args
    # First positional arg is the message.
    assert "anomaly" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_first_fill_after_submit_emits_sentry(
    db_session, instrument: CryptoInstrument, mocker
) -> None:
    """Open item #4 lean — first fill after submit triggers sanity event."""
    svc = BinanceTestnetLedgerService(session=db_session)
    cid = "ledger-test-firstfill"
    await svc.record_plan(
        instrument_id=instrument.id,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
    )
    await svc.record_preview(client_order_id=cid)
    await svc.record_validation(client_order_id=cid)
    await svc.record_submit(client_order_id=cid, broker_order_id="binance-ff")
    import sentry_sdk

    mock_capture = mocker.patch.object(sentry_sdk, "capture_message")
    await svc.record_fill(client_order_id=cid)
    assert mock_capture.called


_REPO_MODULE = "app.services.brokers.binance.testnet.ledger.repository"
_REPO_CLASS = "BinanceTestnetLedgerRepository"
_ALLOWED_IMPORTER = pathlib.Path(
    "app/services/brokers/binance/testnet/ledger/service.py"
)


def _repo_root() -> pathlib.Path:
    # tests/services/brokers/binance/testnet/test_ledger_service.py
    #   parents[0]=testnet  [1]=binance  [2]=brokers  [3]=services
    #   [4]=tests  [5]=repo root
    return pathlib.Path(__file__).resolve().parents[5]


def _imports_repository(tree: ast.AST) -> bool:
    """Return True if the parsed module imports the repository module
    or class by any of the recognised spellings."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _REPO_MODULE:
                return True
            # ``from app.services.brokers.binance.testnet.ledger import repository``
            if module == "app.services.brokers.binance.testnet.ledger" and any(
                alias.name == "repository" for alias in node.names
            ):
                return True
            # ``from app.services.brokers.binance.testnet.ledger import
            # BinanceTestnetLedgerRepository`` (re-export shape).
            if module.startswith("app.services.brokers.binance.testnet.ledger") and any(
                alias.name == _REPO_CLASS for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _REPO_MODULE or alias.name.startswith(
                    _REPO_MODULE + "."
                ):
                    return True
    return False


def test_repository_import_boundary_enforced() -> None:
    """T27 — Repository import-boundary guard.

    The ``BinanceTestnetLedgerRepository`` is the service-internal DB
    boundary for ``binance_testnet_order_ledger``. Production code
    outside ``app/services/brokers/binance/testnet/ledger/service.py``
    must NOT import the repository module or class; the service is the
    only public write surface.

    Walks every ``app/**.py`` file with the AST module, looks for
    ``Import``/``ImportFrom`` nodes referencing
    ``app.services.brokers.binance.testnet.ledger.repository`` or the
    ``BinanceTestnetLedgerRepository`` symbol, and fails if any file
    other than ``ledger/service.py`` imports them.

    Tests are intentionally not scanned — they may exercise the repo
    through mocks or fixtures.
    """
    repo_root = _repo_root()
    app_dir = repo_root / "app"
    assert app_dir.exists(), f"app/ dir missing under {repo_root}"

    offenders: list[pathlib.Path] = []
    for py_file in app_dir.rglob("*.py"):
        rel = py_file.relative_to(repo_root)
        if rel == _ALLOWED_IMPORTER:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        if _imports_repository(tree):
            offenders.append(rel)

    assert not offenders, (
        f"Repository import-boundary violated. ROB-286 acceptance criterion: "
        f"`{_REPO_MODULE}` and `{_REPO_CLASS}` are service-internal — only "
        f"`{_ALLOWED_IMPORTER}` may import them. Offending files: "
        f"{[str(p) for p in sorted(offenders)]}. Use "
        f"`BinanceTestnetLedgerService` instead, or move the repository "
        f"shape entirely if a different consumer is legitimate."
    )


def test_repository_allowed_importer_is_actually_using_it() -> None:
    """Sanity counterpart to ``test_repository_import_boundary_enforced``.

    Verifies the allowed importer (``ledger/service.py``) actually
    references the repository — guards against the guard test silently
    passing because the only legitimate user stopped importing it.
    """
    repo_root = _repo_root()
    service_file = repo_root / _ALLOWED_IMPORTER
    assert service_file.exists(), f"missing allowed importer: {service_file}"
    tree = ast.parse(service_file.read_text(encoding="utf-8"))
    assert _imports_repository(tree), (
        f"{_ALLOWED_IMPORTER} no longer imports the repository — either the "
        "ledger architecture changed (then update _ALLOWED_IMPORTER / drop this "
        "test) or someone moved the repo import elsewhere (then the boundary "
        "test above will pass vacuously)."
    )
