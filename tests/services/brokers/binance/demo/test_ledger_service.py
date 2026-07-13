"""ROB-298 — BinanceDemoLedgerService state machine + import-boundary tests."""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.errors import (
    BinanceDemoInvalidProduct,
    BinanceDemoInvalidStateTransition,
)
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService


@pytest_asyncio.fixture
async def demo_ledger_service(db_session) -> BinanceDemoLedgerService:
    return BinanceDemoLedgerService(db_session)


@pytest_asyncio.fixture
async def crypto_instrument_btc_id(db_session) -> int:
    """Find-or-create a Binance spot BTCUSDT instrument for the demo ledger.

    Mirrors the testnet fixture's find-or-create semantics — the shared
    ``db_session`` fixture does not roll back between tests, so the unique
    ``(venue, product, venue_symbol)`` row may already exist.
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
        return existing.id
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
    return inst.id


async def _make_row(
    service: BinanceDemoLedgerService,
    *,
    instrument_id: int,
    product: str = "spot",
    side: str = "BUY",
    client_order_id: str | None = None,
) -> str:
    now = dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.UTC)
    coid = (
        client_order_id
        if client_order_id is not None
        else f"demo-test-{product}-{side}-{instrument_id}"
    )
    await service.record_planned(
        instrument_id=instrument_id,
        product=product,
        venue_host="demo-api.binance.com",
        client_order_id=coid,
        side=side,
        order_type="MARKET",
        qty=Decimal("0.001"),
        price=None,
        now=now,
    )
    return coid


@pytest.mark.asyncio
async def test_record_planned_creates_row(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service,
        instrument_id=crypto_instrument_btc_id,
        client_order_id="demo-test-planned-create",
    )
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.product == "spot"
    assert row.lifecycle_state == "planned"
    assert row.venue_host == "demo-api.binance.com"


@pytest.mark.asyncio
async def test_invalid_product_rejected(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    with pytest.raises(BinanceDemoInvalidProduct):
        await _make_row(
            demo_ledger_service,
            instrument_id=crypto_instrument_btc_id,
            product="margin",
            client_order_id="demo-test-invalid-product",
        )


@pytest.mark.asyncio
async def test_independent_boundaries_reject_invalid_product_before_db_work(
    demo_ledger_service: BinanceDemoLedgerService,
) -> None:
    now = dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.UTC)

    with pytest.raises(BinanceDemoInvalidProduct):
        await demo_ledger_service.resolve_or_create_instrument(
            venue="binance",
            product="margin",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        )
    with pytest.raises(BinanceDemoInvalidProduct):
        await demo_ledger_service.reserve_root_planned(
            instrument_id=-1,
            product="margin",
            venue_host="demo-api.binance.com",
            client_order_id="demo-test-invalid-reservation-product",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("1"),
            price=None,
            global_open_root_cap=1,
            now=now,
        )


@pytest.mark.asyncio
async def test_transition_of_missing_row_is_rejected(
    demo_ledger_service: BinanceDemoLedgerService,
) -> None:
    with pytest.raises(BinanceDemoInvalidStateTransition, match="no ledger row"):
        await demo_ledger_service.record_previewed(
            client_order_id="demo-test-row-does-not-exist",
            now=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.UTC),
        )


@pytest.mark.asyncio
async def test_state_transition_planned_to_previewed(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service,
        instrument_id=crypto_instrument_btc_id,
        client_order_id="demo-test-planned-to-previewed",
    )
    now = dt.datetime(2026, 5, 22, 12, 0, 1, tzinfo=dt.UTC)
    await demo_ledger_service.record_previewed(client_order_id=cid, now=now)
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.lifecycle_state == "previewed"
    assert row.previewed_at == now


@pytest.mark.asyncio
async def test_invalid_state_transition_planned_to_filled(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service,
        instrument_id=crypto_instrument_btc_id,
        client_order_id="demo-test-planned-to-filled-illegal",
    )
    now = dt.datetime(2026, 5, 22, 12, 0, 1, tzinfo=dt.UTC)
    with pytest.raises(BinanceDemoInvalidStateTransition):
        await demo_ledger_service.record_filled(client_order_id=cid, now=now)


# ---------------------------------------------------------------------------
# AST import-boundary guard
# ---------------------------------------------------------------------------

_REPO_MODULE = "app.services.brokers.binance.demo.ledger.repository"
_REPO_CLASS = "BinanceDemoLedgerRepository"
_ALLOWED_IMPORTER = pathlib.Path("app/services/brokers/binance/demo/ledger/service.py")


def _repo_root() -> pathlib.Path:
    # tests/services/brokers/binance/demo/test_ledger_service.py
    #   parents[0]=demo  [1]=binance  [2]=brokers  [3]=services
    #   [4]=tests  [5]=repo root
    return pathlib.Path(__file__).resolve().parents[5]


def _imports_repository(tree: ast.AST) -> bool:
    """Return True if the parsed module imports the demo repository
    module or class by any recognised spelling."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _REPO_MODULE:
                return True
            if module == "app.services.brokers.binance.demo.ledger" and any(
                alias.name == "repository" for alias in node.names
            ):
                return True
            if module.startswith("app.services.brokers.binance.demo.ledger") and any(
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
    """AST guard: nothing outside ``app/services/brokers/binance/demo/ledger/``
    imports ``BinanceDemoLedgerRepository``.

    Walks every ``app/**.py`` file with the AST module, looking for
    ``Import``/``ImportFrom`` nodes referencing the repository. Only
    ``ledger/service.py`` may import it; everything else is an offender.

    Tests are intentionally not scanned — fixtures and mocks may exercise
    the repo directly. Broader guards (scripts/, etc.) are handled by
    plan task 11.
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
        f"BinanceDemoLedgerRepository may only be imported within "
        f"`{_ALLOWED_IMPORTER}`. Offending files: "
        f"{[str(p) for p in sorted(offenders)]}. Use "
        f"`BinanceDemoLedgerService` instead."
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
        "ledger architecture changed (then update _ALLOWED_IMPORTER / drop "
        "this test) or someone moved the repo import elsewhere (then the "
        "boundary test above will pass vacuously)."
    )
