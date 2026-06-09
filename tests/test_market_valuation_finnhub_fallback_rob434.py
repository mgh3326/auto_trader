"""ROB-434: US market_valuation Finnhub fallback (field-fill).

When yahoo .info leaves valuation fields null (or fails), backfill the missing
fields from Finnhub company_basic_financials. source stays 'yahoo'; per-field
provenance in raw_payload['_field_provenance']; default-off, inert without key.
"""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.mark.unit
def test_settings_flag_defaults_off() -> None:
    from app.core.config import settings

    assert settings.market_valuation_finnhub_fallback_enabled is False


@pytest.mark.unit
def test_resolve_raw_value_priority_and_truthiness() -> None:
    from app.services.market_valuation_snapshots.builder import _resolve_raw_value

    # canonical lowercase key wins over the yahoo key
    assert _resolve_raw_value({"roe": 22.0, "ROE": 9.9}, "roe") == 22.0
    # falls back to the yahoo key when canonical absent
    assert _resolve_raw_value({"marketCap": 1234}, "market_cap") == 1234
    # 0/None/absent → None (truthiness, matches _payload_from_raw's or-chain)
    assert _resolve_raw_value({"per": 0.0, "PER": 0}, "per") is None
    assert _resolve_raw_value({}, "high_52w_date") is None


@pytest.mark.unit
def test_map_finnhub_metrics_unit_traps() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {
            "roeTTM": 22.0,  # already percent → NOT ×100
            "peTTM": 8.0,
            "pbAnnual": 0.9,
            "dividendYieldIndicatedAnnual": 3.0,  # percent → ÷100 ratio
            "marketCapitalization": 1500.0,  # millions → ×1e6 absolute
            "52WeekHigh": 110.0,
            "52WeekLow": 80.0,
            "52WeekHighDate": "2026-03-14",
        }
    )
    assert out["roe"] == 22.0  # critical: not 2200
    assert out["per"] == 8.0
    assert out["pbr"] == 0.9
    assert out["dividend_yield"] == 0.03
    assert out["market_cap"] == 1_500_000_000.0
    assert out["high_52w"] == 110.0
    assert out["low_52w"] == 80.0
    assert out["high_52w_date"] == "2026-03-14"  # iso str (JSON-safe, parsed later)


@pytest.mark.unit
def test_map_finnhub_metrics_fail_closed_on_missing_and_nonfinite() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {"roeTTM": None, "peTTM": "n/a", "marketCapitalization": float("inf")}
    )
    assert out == {}  # nothing fabricated; non-finite/None/unparseable dropped


@pytest.mark.unit
def test_map_finnhub_metrics_bad_date_dropped() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": ""})
    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": None})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_disabled_is_noop(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: False)
    calls = {"n": 0}

    async def _never(symbol):  # noqa: ANN001
        calls["n"] += 1
        return {"roe": 22.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _never)
    raw = {"PER": 8.0}  # roe missing
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out == {"PER": 8.0}  # untouched
    assert calls["n"] == 0  # finnhub never called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_fills_only_missing_fields(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 99.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    # yahoo gave PER (8.0) but no ROE/market_cap → fill ROE + market_cap, keep PER
    raw = {"PER": 8.0}
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out["roe"] == 18.0
    assert out["market_cap"] == 2_000_000_000.0
    assert "per" not in out  # PER already present (yahoo) → NOT overwritten
    assert out["_field_provenance"] == {"roe": "finnhub", "market_cap": "finnhub"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_total_yahoo_failure_fills_all(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 8.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    out = await fb.apply_valuation_fallback("AAPL", {}, yahoo_failed=True)
    assert out["roe"] == 18.0 and out["per"] == 8.0
    assert out["_field_provenance"]["per"] == "finnhub"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_finnhub_error_is_fail_closed(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _boom(symbol):  # noqa: ANN001
        raise RuntimeError("finnhub rate limited")

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _boom)
    raw = {"PER": 8.0}
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out == {"PER": 8.0}  # unchanged, no crash


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_no_gap_skips_finnhub(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)
    calls = {"n": 0}

    async def _metrics(symbol):  # noqa: ANN001
        calls["n"] += 1
        return {"roe": 1.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    # every field present → no finnhub call
    raw = {
        "ROE": 15.0,
        "PER": 8.0,
        "PBR": 0.9,
        "Dividend Yield": 0.02,
        "marketCap": 3e9,
        "yearHigh": 100.0,
        "yearLow": 80.0,
        "high_52w_date": "2026-01-01",
    }
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert calls["n"] == 0
    assert "_field_provenance" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_backfills_roe_when_yahoo_null(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _fund(sym):  # noqa: ANN001
        return {"PER": 8.0, "ROE": None, "marketCap": 3_000_000_000}  # ROE missing

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.fetch_fundamental_info", _fund
    )
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)

    raw = await builder.default_valuation_fetcher("AAPL", "us")
    assert raw["roe"] == 18.0  # backfilled
    assert raw["PER"] == 8.0  # yahoo preserved
    assert raw["_field_provenance"] == {"roe": "finnhub"}
    # source unchanged downstream:
    payload = builder._payload_from_raw(
        market="us", symbol="AAPL", snapshot_date=dt.date(2026, 6, 9), raw=raw
    )
    assert payload.source == "yahoo"
    assert payload.roe == __import__("decimal").Decimal("18.0")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_recovers_total_yahoo_failure(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _boom(sym):  # noqa: ANN001
        raise RuntimeError("Invalid Crumb / Session is closed")

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.fetch_fundamental_info", _boom
    )
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 8.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)

    raw = await builder.default_valuation_fetcher("AAPL", "us")  # no raise
    assert raw["roe"] == 18.0 and raw["per"] == 8.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_total_failure_reraises_when_disabled(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _boom(sym):  # noqa: ANN001
        raise RuntimeError("Invalid Crumb")

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.fetch_fundamental_info", _boom
    )
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: False)  # disabled

    with pytest.raises(RuntimeError, match="Invalid Crumb"):
        await builder.default_valuation_fetcher("AAPL", "us")


@pytest.mark.unit
def test_aggregate_reporting_from_payloads() -> None:
    from decimal import Decimal

    from app.jobs.market_valuation_snapshots import _aggregate_report
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotUpsert,
    )

    p1 = MarketValuationSnapshotUpsert(
        market="us",
        symbol="AAA",
        snapshot_date=dt.date(2026, 6, 9),
        source="yahoo",
        per=Decimal("8"),
        roe=Decimal("18"),
        market_cap=Decimal("3e9"),
        raw_payload={"_field_provenance": {"roe": "finnhub"}},
    )
    p2 = MarketValuationSnapshotUpsert(
        market="us",
        symbol="BBB",
        snapshot_date=dt.date(2026, 6, 9),
        source="yahoo",
        per=Decimal("9"),
        roe=None,
        market_cap=Decimal("5e9"),
        raw_payload={},
    )
    backfill, coverage = _aggregate_report([p1, p2])
    assert backfill == {"roe": 1}  # only p1's roe was finnhub
    assert coverage["per"] == 2
    assert coverage["roe"] == 1  # p2 roe is None
    assert coverage["market_cap"] == 2
    assert coverage["pbr"] == 0


import sqlalchemy as sa  # noqa: E402


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfilled_row_upserts_and_passes_quality_guard(db_session) -> None:
    from decimal import Decimal

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.us_quality_guards import (
        apply_us_valuation_quality_guards,
    )
    from app.services.market_valuation_snapshots.builder import (
        build_valuation_snapshots_for_market,
    )
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotsRepository,
    )

    snapshot_date = dt.date(2026, 6, 9)
    sym = "ZZQ434"

    # Simulate the merged raw a yahoo-partial + finnhub-backfill produced:
    async def fake_fetcher(symbol: str, market: str) -> dict[str, object]:
        assert market == "us"
        return {
            "PER": "8",  # yahoo
            "roe": 18.0,  # finnhub-filled (≤300 guard)
            "market_cap": 3_000_000_000.0,  # finnhub-filled (≥$100M guard)
            "_field_provenance": {"roe": "finnhub", "market_cap": "finnhub"},
        }

    result = await build_valuation_snapshots_for_market(
        market="us", symbols=[sym], snapshot_date=snapshot_date, fetcher=fake_fetcher
    )
    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.source == "yahoo"
    assert payload.roe == Decimal("18.0")
    assert payload.raw_payload["_field_provenance"] == {
        "roe": "finnhub",
        "market_cap": "finnhub",
    }

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()
    assert (
        await MarketValuationSnapshotsRepository(db_session).upsert(result.payloads)
        == 1
    )
    await db_session.commit()

    # The backfilled row survives the read-time quality guard (mcap≥$100M, roe≤300%).
    stmt = apply_us_valuation_quality_guards(
        sa.select(MarketValuationSnapshot.symbol).where(
            MarketValuationSnapshot.symbol == sym,
            MarketValuationSnapshot.snapshot_date == snapshot_date,
        ),
        uses_roe=True,
    )
    rows = (await db_session.execute(stmt)).all()
    assert len(rows) == 1  # passes the guard

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()
