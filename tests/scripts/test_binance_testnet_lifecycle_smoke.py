"""ROB-294 — Lifecycle smoke CLI tests.

Mirrors ``tests/scripts/test_binance_testnet_scalper_smoke.py`` for the
ROB-294 lifecycle CLI:

  * default-disabled gate (no env → exit 0, no side effects);
  * opted-in but missing ``--symbol`` → exit 1;
  * opted-in but ``--symbol`` outside MVP set → exit 1;
  * credentialed dry-run with a deterministic Entry snapshot → ledger
    walks planned → previewed → validated and STOPS (zero submitted
    rows); single broker open_orders signed read is allowed.

The confirmed-single-cycle path is NOT exercised here because it
requires real testnet credentials + operator approval. The runbook
documents that path; the unit tests cover the runner-level outcomes
that the CLI orchestrates (see
``tests/services/scalping/test_runner_lifecycle_outcomes_rob294.py``).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for var in (
        "BINANCE_TESTNET_ENABLED",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_TESTNET_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_lifecycle_smoke_disabled_by_default_no_side_effects(
    caplog, httpx_mock
) -> None:
    """No env, no flags → exit 0 with the 'disabled' log line and 0 HTTP."""
    from scripts.binance_testnet_lifecycle_smoke import main

    with caplog.at_level(logging.INFO):
        exit_code = main(argv=[])
    assert exit_code == 0
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "scalper disabled" in messages
    assert "BINANCE_TESTNET_ENABLED=true" in messages
    # No HTTP at all — httpx_mock has no registered responses; any leak
    # would have errored out.
    assert httpx_mock.get_requests() == []


def test_lifecycle_smoke_disabled_emits_evidence_json(tmp_path: Path, caplog) -> None:
    """Even on the disabled path, --evidence-json writes a usable JSON file.

    Locks the evidence contract: every CLI invocation produces an
    operator-facing summary, regardless of outcome. The disabled-stage
    file should contain ``mode='default-disabled'`` and zero side-effect
    counters.
    """
    from scripts.binance_testnet_lifecycle_smoke import main

    out = tmp_path / "evidence.json"
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=["--evidence-json", str(out)])
    assert exit_code == 0
    assert out.exists(), f"evidence-json file not written: {out}"
    payload = json.loads(out.read_text())
    assert payload["mode"] == "default-disabled"
    assert payload["exit_code"] == 0
    assert payload["env_binance_enabled_present"] is False
    # No secret values ever — sanity-grep the JSON.
    text = out.read_text()
    assert "API_KEY" not in text.upper() or "API_KEY_PRESENT" in text.upper()


def test_lifecycle_smoke_missing_symbol_returns_nonzero(monkeypatch, caplog) -> None:
    """Opted in but --symbol absent → exit 1 (operator misconfig)."""
    from scripts.binance_testnet_lifecycle_smoke import main

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=[])
    assert exit_code == 1
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "--symbol" in messages


def test_lifecycle_smoke_rejects_symbol_outside_mvp_set(monkeypatch, caplog) -> None:
    """Symbol must be in the locked MVP triplet — DOGEUSDT must be rejected."""
    from scripts.binance_testnet_lifecycle_smoke import main

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with caplog.at_level(logging.INFO):
        exit_code = main(argv=["--symbol", "DOGEUSDT"])
    assert exit_code == 1
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "MVP" in messages or "BTCUSDT" in messages


def test_lifecycle_smoke_rejects_excessive_max_notional(monkeypatch, caplog) -> None:
    """``--max-notional`` is bounded; anything above the ceiling is rejected."""
    from scripts.binance_testnet_lifecycle_smoke import main

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with caplog.at_level(logging.INFO):
        exit_code = main(
            argv=[
                "--symbol",
                "BTCUSDT",
                "--max-notional",
                "100000",  # absurdly large; ceiling is 25
            ]
        )
    assert exit_code == 1
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "ceiling" in messages or "max-notional" in messages


@pytest.mark.asyncio
async def test_lifecycle_smoke_dry_run_walks_to_validated(
    monkeypatch, caplog, db_session, httpx_mock, tmp_path: Path
) -> None:
    """Opt-in dry-run with an Entry-resolving snapshot:

    - reconcile_on_start issues one signed GET (open_orders, stubbed);
    - tick resolves to Entry(BUY);
    - ledger walks planned → previewed → validated (no submitted rows);
    - evidence JSON is written and contains the expected shape.

    Uses the global ``db_session`` so the CLI's own ``AsyncSessionLocal``
    reaches the same DB. Cleanup removes any rows the CLI wrote.
    """

    from sqlalchemy import delete, select

    from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger
    from app.models.crypto_instruments import CryptoInstrument

    # Reconcile pass's open_orders read for BTCUSDT.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/openOrders\?.*$"),
        json=[],
        status_code=200,
        is_reusable=True,
    )

    existing_result = await db_session.execute(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == "BTCUSDT",
        )
    )
    existing = existing_result.scalar_one_or_none()
    seeded_here = False
    if existing is None:
        new_row = CryptoInstrument(
            venue="binance",
            product="spot",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            status="active",
        )
        db_session.add(new_row)
        await db_session.commit()
        await db_session.refresh(new_row)
        instrument_id = new_row.id
        seeded_here = True
    else:
        instrument_id = existing.id

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")

    out = tmp_path / "evidence.json"
    try:
        with caplog.at_level(logging.INFO):
            # Invoke ``_run_lifecycle`` directly so we share the test's
            # asyncio loop (mirrors the ROB-286 smoke test pattern).
            from scripts.binance_testnet_lifecycle_smoke import (
                LifecycleEvidence,
                _run_lifecycle,
            )

            class _Args:
                symbol = "BTCUSDT"
                simulate_price = "50000"
                simulate_rsi = 20.0  # oversold → Entry
                simulate_ema20 = "49600"
                simulate_ema50 = "49000"
                simulate_instrument_health = "healthy"
                max_notional = None
                dry_run = True
                confirm = False
                cancel_pending_on_exit = False
                evidence_json = str(out)

            evidence = LifecycleEvidence(
                mode="dry-run",
                symbol="BTCUSDT",
                started_at="2026-05-22T00:00:00+00:00",
            )
            exit_code = await _run_lifecycle(args=_Args(), evidence=evidence)
        assert exit_code == 0

        result = await db_session.execute(
            select(BinanceTestnetOrderLedger).where(
                BinanceTestnetOrderLedger.instrument_id == instrument_id
            )
        )
        rows = list(result.scalars().all())
        states = [r.lifecycle_state for r in rows]
        # Entry decision → planned → previewed → validated. Exactly one row.
        assert len(rows) == 1, (
            f"dry-run produced {len(rows)} ledger rows; expected 1 "
            f"(planned→previewed→validated). states={states}"
        )
        assert rows[0].lifecycle_state == "validated"

        # Evidence dataclass: filled-in shape we expect.
        assert evidence.tick_action == "entry"
        assert evidence.tick_submitted is False
        assert evidence.tick_dry_run is True
        assert len(evidence.client_order_ids_created) == 1
        assert evidence.final_lifecycle_states == {
            evidence.client_order_ids_created[0]: "validated"
        }
        assert evidence.anomaly_client_order_ids == []
        # Ledger count is monotonic.
        assert evidence.ledger_rows_after == evidence.ledger_rows_before + 1
    finally:
        # Cleanup state the CLI wrote through its own session.
        await db_session.execute(
            delete(BinanceTestnetOrderLedger).where(
                BinanceTestnetOrderLedger.instrument_id == instrument_id
            )
        )
        if seeded_here:
            await db_session.execute(
                delete(CryptoInstrument).where(CryptoInstrument.id == instrument_id)
            )
        await db_session.commit()
