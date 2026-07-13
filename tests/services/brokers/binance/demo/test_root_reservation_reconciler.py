"""ROB-844 — broker-truth release of abandoned root reservations."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderStatusResult,
)

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_NOW = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)
_PLANNED_AT = _NOW - dt.timedelta(hours=2)
_STALE_BEFORE = _NOW - dt.timedelta(hours=1)
_PREFIX = "rob844-reconcile-"
_CREDENTIAL_FINGERPRINT = "sha256:" + "84" * 32


class _CredentialBoundClient:
    credential_fingerprint = _CREDENTIAL_FINGERPRINT


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    async def _clean() -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id.like(f"{_PREFIX}%")
                )
            )
            await db.commit()

    await _clean()
    yield
    await _clean()


async def _seed(
    *,
    product: str,
    suffix: str,
    planned_at: dt.datetime = _PLANNED_AT,
    credential_fingerprint: str | None = _CREDENTIAL_FINGERPRINT,
) -> tuple[str, str]:
    cid = f"{_PREFIX}{suffix}"
    host = "demo-api.binance.com" if product == "spot" else "demo-fapi.binance.com"
    symbol = f"R844REC{suffix.upper()}USDT"
    async with AsyncSessionLocal() as db:
        service = BinanceDemoLedgerService(db)
        instrument_id = await service.resolve_or_create_instrument(
            venue="binance",
            product=product,
            venue_symbol=symbol,
            base_asset=f"R844REC{suffix.upper()}",
            quote_asset="USDT",
        )
        result = await service.reserve_root_planned(
            instrument_id=instrument_id,
            product=product,
            venue_host=host,
            client_order_id=cid,
            side="BUY",
            order_type="MARKET",
            qty=Decimal("1"),
            price=None,
            extra_metadata=(
                {}
                if credential_fingerprint is None
                else {"credential_fingerprint": credential_fingerprint}
            ),
            global_open_root_cap=1_000_000,
            now=planned_at,
        )
    assert result.status == "reserved"
    return cid, symbol


async def _row(cid: str) -> BinanceDemoOrderLedger:
    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id == cid
            )
        )
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_explicit_not_found_releases_abandoned_spot_reservation() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _symbol = await _seed(product="spot", suffix="notfound")

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            raise BinanceDemoOrderNotFound(cid)

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": _Spot()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["released"] == 1
    assert result["kept"] == 0
    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_transport_failure_keeps_reservation_blocking() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, _symbol = await _seed(product="spot", suffix="timeout")

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            raise TimeoutError("broker unavailable")

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": _Spot()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["released"] == 0
    assert result["kept"] == 1
    assert result["outcomes"][0]["reason"] == "broker_lookup_failed"
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_candidate_for_unconfigured_product_stays_client_unavailable() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, _ = await _seed(product="usdm_futures", suffix="nolaneclient")
    result = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=False,
    )

    assert result["outcomes"] == [
        {
            "client_order_id": cid,
            "action": "kept",
            "reason": "client_unavailable",
        }
    ]
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "executed_qty", "released"),
    [("CANCELED", "0", True), ("PARTIALLY_FILLED", "0.1", False)],
)
async def test_spot_status_requires_terminal_zero_fill(
    status: str, executed_qty: str, released: bool
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(product="spot", suffix=f"spot{status.lower()}")

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            return {
                "clientOrderId": cid,
                "symbol": symbol,
                "status": status,
                "executedQty": executed_qty,
                "orderId": 84410,
            }

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": _Spot()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert (result["released"] == 1) is released
    assert (await _row(cid)).lifecycle_state == (
        "reconciled" if released else "planned"
    )


@pytest.mark.asyncio
async def test_futures_dto_terminal_zero_fill_releases() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(product="usdm_futures", suffix="futuresrejected")

    class _Futures(_CredentialBoundClient):
        async def get_order(self, **_kwargs):
            return FuturesDemoOrderStatusResult(
                client_order_id=cid,
                broker_order_id="futures-order",
                symbol=symbol,
                side="BUY",
                order_type="MARKET",
                status="REJECTED",
                orig_qty=Decimal("1"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
                reduce_only=False,
                raw_response_redacted={
                    "clientOrderId": cid,
                    "orderId": 84411,
                    "symbol": symbol,
                    "status": "REJECTED",
                    "executedQty": "0",
                },
            )

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"usdm_futures": _Futures()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["released"] == 1
    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_recent_reservation_is_not_looked_up_or_released() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _symbol = await _seed(product="spot", suffix="recent", planned_at=_NOW)

    class _Spot(_CredentialBoundClient):
        calls = 0

        async def get_order_status(self, **_kwargs):
            self.calls += 1
            raise BinanceDemoOrderNotFound(cid)

    client = _Spot()
    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": client},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["scanned"] == 0
    assert client.calls == 0
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_inflight_executor_row_lock_wins_over_reconciler() -> None:
    """A broker-submitted transaction is skipped, never stale-overwritten."""
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _symbol = await _seed(product="spot", suffix="rowlock")

    class _Spot(_CredentialBoundClient):
        calls = 0

        async def get_order_status(self, **_kwargs):
            self.calls += 1
            raise BinanceDemoOrderNotFound(cid)

    client = _Spot()
    async with AsyncSessionLocal() as executor_db:
        ledger = BinanceDemoLedgerService(executor_db)
        await ledger.record_previewed(client_order_id=cid, now=_NOW)
        await ledger.record_validated(client_order_id=cid, now=_NOW)
        await ledger.record_submitted(
            client_order_id=cid,
            broker_order_id="row-lock-broker-order",
            now=_NOW,
        )

        async with AsyncSessionLocal() as reconcile_db:
            result = await reconcile_binance_demo_root_reservations(
                AsyncSessionLocal,
                clients={"spot": client},
                now=_NOW,
                stale_before=_STALE_BEFORE,
                dry_run=True,
            )
            await reconcile_db.commit()

        assert result["scanned"] == 0
        assert client.calls == 0
        await executor_db.commit()

    assert (await _row(cid)).lifecycle_state == "submitted"


@pytest.mark.asyncio
async def test_reconciler_row_lock_prevents_executor_from_resuming_to_submit() -> None:
    """If reconcile locks first, the executor re-reads terminal truth and stops."""
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import (
        BinanceDemoInvalidStateTransition,
        BinanceDemoOrderNotFound,
    )

    cid, _symbol = await _seed(product="spot", suffix="reconcilewins")
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            lookup_started.set()
            await release_lookup.wait()
            raise BinanceDemoOrderNotFound(cid)

    async def _reconcile() -> None:
        async with AsyncSessionLocal() as db:
            await reconcile_binance_demo_root_reservations(
                AsyncSessionLocal,
                clients={"spot": _Spot()},
                now=_NOW,
                stale_before=_STALE_BEFORE,
                dry_run=False,
            )
            await db.commit()

    reconcile_task = asyncio.create_task(_reconcile())
    await lookup_started.wait()

    async with AsyncSessionLocal() as executor_db:
        ledger = BinanceDemoLedgerService(executor_db)
        transition_task = asyncio.create_task(
            ledger.record_previewed(client_order_id=cid, now=_NOW)
        )
        # Let the competing transaction reach its row-lock wait before the
        # reconciler commits its terminal release.
        await asyncio.sleep(0.1)
        release_lookup.set()
        await reconcile_task
        with pytest.raises(BinanceDemoInvalidStateTransition):
            await transition_task
        await executor_db.rollback()

    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_reconciler_does_not_lock_later_candidate_during_broker_lookup() -> None:
    """Broker I/O for candidate one must not hold candidate two's row lock."""
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    first_cid, _ = await _seed(
        product="spot",
        suffix="shorttxfirst",
        planned_at=_PLANNED_AT - dt.timedelta(minutes=1),
    )
    second_cid, _ = await _seed(product="spot", suffix="shorttxsecond")
    first_lookup_started = asyncio.Event()
    release_first_lookup = asyncio.Event()

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, *, client_order_id: str, **_kwargs):
            if client_order_id == first_cid:
                first_lookup_started.set()
                await release_first_lookup.wait()
            raise BinanceDemoOrderNotFound(client_order_id)

    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            result = await reconcile_binance_demo_root_reservations(
                AsyncSessionLocal,
                clients={"spot": _Spot()},
                now=_NOW,
                stale_before=_STALE_BEFORE,
                dry_run=False,
            )
            await db.commit()
            return result

    task = asyncio.create_task(_run())
    await first_lookup_started.wait()
    lock_error: Exception | None = None
    try:
        async with AsyncSessionLocal() as probe:
            try:
                locked = await probe.scalar(
                    select(BinanceDemoOrderLedger)
                    .where(BinanceDemoOrderLedger.client_order_id == second_cid)
                    .with_for_update(nowait=True)
                )
                assert locked is not None
            except Exception as exc:  # noqa: BLE001 - asserted below after cleanup
                lock_error = exc
            finally:
                await probe.rollback()
    finally:
        release_first_lookup.set()
    result = await task

    assert lock_error is None
    assert result["released"] == 2


@pytest.mark.asyncio
async def test_concurrent_reconcile_workers_classify_locked_candidate_once() -> None:
    """SKIP LOCKED gives one overlapping worker ownership of a candidate."""
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _ = await _seed(product="spot", suffix="concurrentworkers")
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    class _Spot(_CredentialBoundClient):
        calls = 0

        async def get_order_status(self, **_kwargs):
            self.calls += 1
            lookup_started.set()
            await release_lookup.wait()
            raise BinanceDemoOrderNotFound(cid)

    client = _Spot()
    first = asyncio.create_task(
        reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": client},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
    )
    await lookup_started.wait()
    second = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={"spot": client},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=False,
    )
    release_lookup.set()
    first_result = await first

    assert client.calls == 1
    assert sorted([first_result["scanned"], second["scanned"]]) == [0, 1]
    assert sum(result["released"] for result in (first_result, second)) == 1
    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_dry_run_classifies_but_mutates_no_ledger_state() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _ = await _seed(product="spot", suffix="dryrunnomutation")

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            raise BinanceDemoOrderNotFound(cid)

    before = await _row(cid)
    result = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={"spot": _Spot()},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=True,
    )
    after = await _row(cid)

    assert result["released"] == 0
    assert result["outcomes"][0]["action"] == "would_release"
    assert after.lifecycle_state == before.lifecycle_state == "planned"
    assert after.cancelled_at is None
    assert after.reconciled_at is None
    assert after.extra_metadata == before.extra_metadata


@pytest.mark.asyncio
async def test_preloaded_executor_cannot_overwrite_reconciled_terminal_state() -> None:
    """FOR UPDATE must refresh stale identity-map attributes before transition."""
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import (
        BinanceDemoInvalidStateTransition,
        BinanceDemoOrderNotFound,
    )

    cid, _ = await _seed(product="spot", suffix="stalepreload")

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            raise BinanceDemoOrderNotFound(cid)

    async with AsyncSessionLocal() as executor_db:
        preloaded = await executor_db.scalar(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id == cid
            )
        )
        assert preloaded is not None
        assert preloaded.lifecycle_state == "planned"

        async with AsyncSessionLocal() as reconcile_db:
            result = await reconcile_binance_demo_root_reservations(
                AsyncSessionLocal,
                clients={"spot": _Spot()},
                now=_NOW,
                stale_before=_STALE_BEFORE,
                dry_run=False,
            )
            await reconcile_db.commit()
        assert result["released"] == 1

        ledger = BinanceDemoLedgerService(executor_db)
        with pytest.raises(BinanceDemoInvalidStateTransition):
            await ledger.record_previewed(client_order_id=cid, now=_NOW)
        await executor_db.rollback()

    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "expected_action", "expected_reason"),
    [
        (
            dt.timedelta(days=89) - dt.timedelta(microseconds=1),
            "released",
            "broker_order_not_found",
        ),
        (dt.timedelta(days=89), "kept", "broker_lookup_retention_exceeded"),
        (dt.timedelta(days=90), "kept", "broker_lookup_retention_exceeded"),
    ],
)
async def test_not_found_release_is_bounded_by_broker_lookup_retention(
    age: dt.timedelta, expected_action: str, expected_reason: str
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    suffix = f"retention{int(age.total_seconds())}"
    cid, _ = await _seed(product="spot", suffix=suffix, planned_at=_NOW - age)

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            raise BinanceDemoOrderNotFound(cid)

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": _Spot()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["outcomes"] == [
        {
            "client_order_id": cid,
            "action": expected_action,
            "reason": expected_reason,
        }
    ]
    assert (await _row(cid)).lifecycle_state == (
        "reconciled" if expected_action == "released" else "planned"
    )


@pytest.mark.asyncio
async def test_terminal_zero_fill_payload_releases_beyond_lookup_retention() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(
        product="spot", suffix="oldterminal", planned_at=_NOW - dt.timedelta(days=90)
    )

    class _Spot(_CredentialBoundClient):
        async def get_order_status(self, **_kwargs):
            return {
                "clientOrderId": cid,
                "symbol": symbol,
                "status": "CANCELED",
                "executedQty": "0",
                "orderId": 84490,
            }

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={"spot": _Spot()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["released"] == 1
    assert result["outcomes"][0]["reason"] == "terminal_zero_fill"
    assert (await _row(cid)).lifecycle_state == "reconciled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "status"),
    [("spot", "CANCELLED"), ("usdm_futures", "EXPIRED_IN_MATCH")],
)
async def test_undocumented_or_cross_product_terminal_status_stays_blocking(
    product: str, status: str
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(product=product, suffix=f"status{product}{status}")
    payload = {
        "clientOrderId": cid,
        "symbol": symbol,
        "status": status,
        "executedQty": "0",
        "orderId": 84491,
    }
    if product == "spot":

        class _Client(_CredentialBoundClient):
            async def get_order_status(self, **_kwargs):
                return payload

    else:

        class _Client(_CredentialBoundClient):
            async def get_order(self, **_kwargs):
                return FuturesDemoOrderStatusResult(
                    client_order_id=cid,
                    broker_order_id="84491",
                    symbol=symbol,
                    side="BUY",
                    order_type="MARKET",
                    status=status,
                    orig_qty=Decimal("1"),
                    executed_qty=Decimal("0"),
                    avg_price=Decimal("0"),
                    reduce_only=False,
                    raw_response_redacted=payload,
                )

    result = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={product: _Client()},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=False,
    )

    assert result["outcomes"][0]["reason"] == "broker_exposure_not_disproven"
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_fingerprint", "client_fingerprint", "expected_reason"),
    [
        (None, _CREDENTIAL_FINGERPRINT, "credential_fingerprint_missing"),
        (
            _CREDENTIAL_FINGERPRINT,
            "sha256:" + "99" * 32,
            "credential_fingerprint_mismatch",
        ),
        (
            _CREDENTIAL_FINGERPRINT,
            None,
            "client_credential_fingerprint_unavailable",
        ),
    ],
)
async def test_account_identity_uncertainty_keeps_without_broker_lookup(
    persisted_fingerprint: str | None,
    client_fingerprint: str | None,
    expected_reason: str,
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )
    from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound

    cid, _ = await _seed(
        product="spot",
        suffix=f"accountidentity{expected_reason}",
        credential_fingerprint=persisted_fingerprint,
    )

    class _Spot:
        calls = 0

        @property
        def credential_fingerprint(self):
            return client_fingerprint

        async def get_order_status(self, **_kwargs):
            self.calls += 1
            raise BinanceDemoOrderNotFound(cid)

    client = _Spot()
    result = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={"spot": client},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=False,
    )

    assert result["released"] == 0
    assert result["outcomes"][0]["reason"] == expected_reason
    assert client.calls == 0
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_persisted_venue_host_mismatch_keeps_without_broker_lookup() -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, _ = await _seed(product="spot", suffix="hostmismatch")
    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id == cid
            )
        )
        assert row is not None
        row.venue_host = "demo-fapi.binance.com"
        await db.commit()

    class _Spot(_CredentialBoundClient):
        calls = 0

        async def get_order_status(self, **_kwargs):
            self.calls += 1
            raise AssertionError("host mismatch must block before broker GET")

    client = _Spot()
    result = await reconcile_binance_demo_root_reservations(
        AsyncSessionLocal,
        clients={"spot": client},
        now=_NOW,
        stale_before=_STALE_BEFORE,
        dry_run=False,
    )

    assert result["outcomes"][0]["reason"] == "venue_host_mismatch"
    assert client.calls == 0
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["clientOrderId", "symbol", "status", "executedQty", "orderId"],
)
@pytest.mark.parametrize("product", ["spot", "usdm_futures"])
async def test_missing_required_truth_is_kept_malformed(
    product: str,
    missing_field: str,
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(
        product=product, suffix=f"{product}missing{missing_field}"
    )
    payload = {
        "clientOrderId": cid,
        "symbol": symbol,
        "status": "CANCELED",
        "executedQty": "0",
        "orderId": 84401,
    }
    payload.pop(missing_field)

    if product == "spot":

        class _Client(_CredentialBoundClient):
            async def get_order_status(self, **_kwargs):
                return payload

    else:

        class _Client(_CredentialBoundClient):
            async def get_order(self, **_kwargs):
                return FuturesDemoOrderStatusResult(
                    client_order_id=str(payload.get("clientOrderId", cid)),
                    broker_order_id=str(payload.get("orderId", "")),
                    symbol=str(payload.get("symbol", symbol)),
                    side="BUY",
                    order_type="MARKET",
                    status=str(payload.get("status", "UNKNOWN")),
                    orig_qty=Decimal("1"),
                    executed_qty=Decimal("0"),
                    avg_price=Decimal("0"),
                    reduce_only=False,
                    raw_response_redacted=payload,
                )

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={product: _Client()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["outcomes"][0]["reason"] == "malformed_broker_truth"
    assert (await _row(cid)).lifecycle_state == "planned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("clientOrderId", " "),
        ("symbol", ""),
        ("status", None),
        ("executedQty", "NaN"),
        ("orderId", " "),
        ("orderId", "invalid"),
        ("orderId", -1),
    ],
)
@pytest.mark.parametrize("product", ["spot", "usdm_futures"])
async def test_invalid_required_truth_is_kept_malformed(
    product: str, field: str, invalid: object
) -> None:
    from app.jobs.binance_demo_root_reservation_reconciliation import (
        reconcile_binance_demo_root_reservations,
    )

    cid, symbol = await _seed(
        product=product, suffix=f"invalid{product}{field}{str(invalid)}"
    )
    payload = {
        "clientOrderId": cid,
        "symbol": symbol,
        "status": "CANCELED",
        "executedQty": "0",
        "orderId": 84402,
    }
    payload[field] = invalid

    if product == "spot":

        class _Client(_CredentialBoundClient):
            async def get_order_status(self, **_kwargs):
                return payload

    else:

        class _Client(_CredentialBoundClient):
            async def get_order(self, **_kwargs):
                return FuturesDemoOrderStatusResult(
                    client_order_id=str(payload.get("clientOrderId", cid)),
                    broker_order_id=str(payload.get("orderId", "")),
                    symbol=str(payload.get("symbol", symbol)),
                    side="BUY",
                    order_type="MARKET",
                    status=str(payload.get("status", "UNKNOWN")),
                    orig_qty=Decimal("1"),
                    executed_qty=Decimal("0"),
                    avg_price=Decimal("0"),
                    reduce_only=False,
                    raw_response_redacted=payload,
                )

    async with AsyncSessionLocal() as db:
        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients={product: _Client()},
            now=_NOW,
            stale_before=_STALE_BEFORE,
            dry_run=False,
        )
        await db.commit()

    assert result["outcomes"][0]["reason"] == "malformed_broker_truth"
    assert (await _row(cid)).lifecycle_state == "planned"
