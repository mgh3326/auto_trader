"""ROB-286 — Smoke CLI behaviour: default-disabled + dry-run no-submit.

Matrix rows T31, T32.
"""

from __future__ import annotations

import logging

import pytest

from scripts.binance_testnet_scalper_smoke import main


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for var in (
        "BINANCE_TESTNET_ENABLED",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_TESTNET_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_smoke_disabled_by_default_no_side_effects(caplog, httpx_mock) -> None:
    """T31 — Default exit 0 with the 'disabled' log line and zero side effects.

    httpx_mock is wired in, so any HTTP call would error out with
    "no response registered". We don't register any responses; the
    default-disabled path must not perform any HTTP at all.
    """
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=[])
    assert exit_code == 0
    # Single recognizable log line.
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "scalper disabled" in messages
    assert "BINANCE_TESTNET_ENABLED=true" in messages
    # No HTTP performed (httpx_mock is empty; if a request had leaked it
    # would have errored out before we got here).
    assert httpx_mock.get_requests() == []


def test_smoke_disabled_when_env_explicit_false(monkeypatch, caplog) -> None:
    """Explicit BINANCE_TESTNET_ENABLED=false is still treated as disabled."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "false")
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=[])
    assert exit_code == 0
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "scalper disabled" in messages


def test_smoke_missing_credentials_returns_nonzero(monkeypatch, caplog) -> None:
    """When opted in but credentials are missing, main() returns 1.

    Verifies BinanceMissingCredentials is caught at the top level and
    converted to a non-zero exit code rather than crashing.
    """
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    # Credentials intentionally absent.
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=[])
    assert exit_code == 1


@pytest.mark.asyncio
async def test_smoke_dryrun_creates_no_submitted_rows(
    monkeypatch, caplog, db_session, httpx_mock
) -> None:
    """T32 — Opt-in dry-run produces zero 'submitted' ledger rows.

    Uses the global session test_db so the smoke CLI's own
    ``AsyncSessionLocal`` reaches the same DB. We seed an instrument row
    here, run main(--duration 0 --dry-run), then count rows in the table
    whose lifecycle_state == 'submitted' (must be zero).

    httpx_mock is registered with a single GET stub for open_orders
    (the reconcile pass calls this; the smoke CLI doesn't gate on it).
    Any *other* HTTP call would error out.
    """
    import re

    from sqlalchemy import select

    from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger
    from app.models.crypto_instruments import CryptoInstrument

    # Allow the reconcile pass's open_orders read for BTCUSDT.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/openOrders\?.*$"),
        json=[],
        status_code=200,
        is_reusable=True,
    )

    existing = await db_session.execute(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == "BTCUSDT",
        )
    )
    if existing.scalar_one_or_none() is None:
        db_session.add(
            CryptoInstrument(
                venue="binance",
                product="spot",
                venue_symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                status="active",
            )
        )
        await db_session.commit()

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with caplog.at_level(logging.INFO):
        # --duration 0 = single tick. Run synchronously inside the same
        # event loop the test owns (main()'s asyncio.run would fight us;
        # invoke _run_smoke directly instead).
        from scripts.binance_testnet_scalper_smoke import _run_smoke

        exit_code = await _run_smoke(dry_run=True, confirm=False, duration_s=0)
    assert exit_code == 0

    result = await db_session.execute(
        select(BinanceTestnetOrderLedger).where(
            BinanceTestnetOrderLedger.lifecycle_state == "submitted"
        )
    )
    submitted_count = len(list(result.scalars().all()))
    assert submitted_count == 0, (
        f"smoke CLI dry-run produced {submitted_count} 'submitted' ledger rows; "
        "expected 0. Hard invariant #8 (operator gate) compromised."
    )
