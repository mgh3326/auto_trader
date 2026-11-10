"""AT-3846: derived values that cannot fit their Numeric(p, s) column become None.

The weekly US fundamentals build died on a bulk upsert (PostgreSQL "numeric field
overflow") because ``payout_ratio`` (Numeric(10, 6), < 10**4) was computed as
``dividends_paid / net_income * 100`` for a tiny positive net income. Pure unit tests.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal

import pytest

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots import builder_us
from app.services.financial_fundamentals_snapshots.builder_us import (
    build_us_fundamentals_for_symbols,
    parse_us_annual_income_periods,
)
from app.services.financial_fundamentals_snapshots.numeric_bounds import (
    bound_numeric_fields,
    column_limit,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsUpsert,
)

_COLLECTED = dt.datetime(2026, 6, 5, tzinfo=dt.UTC)
_PAYOUT_LIMIT = Decimal(10) ** 4  # Numeric(10, 6): |value| < 10**4


def _annual(net_income: str) -> dict:
    return {"2024-12-31": {"Total Revenue": "1000000", "Net Income": net_income}}


async def _fetch_tiny_ni(*, symbol, collected_at):  # noqa: ANN001
    return parse_us_annual_income_periods(
        symbol=symbol, data=_annual("0.5"), collected_at=collected_at
    )


async def _fetch_div(*, symbol):  # noqa: ANN001
    return {2024: Decimal("0.25")}, {2024: Decimal("1000000")}


@pytest.mark.unit
def test_tiny_net_income_payout_exceeds_the_column() -> None:
    """Repro of the failure: the raw derivation does not fit Numeric(10, 6)."""
    periods = parse_us_annual_income_periods(
        symbol="TINY", data=_annual("0.5"), collected_at=_COLLECTED
    )
    enriched = builder_us.enrich_annual_with_dividends(
        periods,
        dps_by_year={2024: Decimal("0.25")},
        dividends_paid_by_year={2024: Decimal("1000000")},
    )
    assert enriched[0].payout_ratio is not None
    assert abs(enriched[0].payout_ratio) >= _PAYOUT_LIMIT
    assert column_limit("payout_ratio") == _PAYOUT_LIMIT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overflowing_payout_reaches_upsert_as_none(monkeypatch) -> None:
    captured: dict = {}

    class _StubRepo:
        def __init__(self, session):  # noqa: ANN001
            pass

        async def upsert(self, payloads):  # noqa: ANN001
            captured["rows"] = list(payloads)
            return len(captured["rows"])

    class _FakeCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):  # noqa: ANN002
            return False

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.financial_fundamentals_snapshots.repository."
        "FinancialFundamentalsSnapshotsRepository",
        _StubRepo,
    )
    result = await build_us_fundamentals_for_symbols(
        ["TINY"],
        commit=True,
        fetcher=_fetch_tiny_ni,
        include_dividends=True,
        dividend_fetcher=_fetch_div,
    )
    (row,) = captured["rows"]
    assert row.payout_ratio is None
    assert row.dividend_per_share == Decimal("0.25")  # the row is still written
    assert row.net_income == Decimal("0.5")
    assert result.committed is True
    assert result.snapshots_built == 1
    assert result.numeric_overflow_counts == {"payout_ratio": 1}
    assert any("payout_ratio" in w and "numeric overflow" in w for w in result.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_bad_symbol_does_not_drop_the_others() -> None:
    async def _fetch(*, symbol, collected_at):  # noqa: ANN001
        ni = "0.5" if symbol == "BAD" else "100"
        return parse_us_annual_income_periods(
            symbol=symbol, data=_annual(ni), collected_at=collected_at
        )

    async def _div(*, symbol):  # noqa: ANN001
        paid = "1000000" if symbol == "BAD" else "30"
        return {2024: Decimal("0.25")}, {2024: Decimal(paid)}

    result = await build_us_fundamentals_for_symbols(
        ["BAD", "GOOD"],
        fetcher=_fetch,
        include_dividends=True,
        dividend_fetcher=_div,
    )
    assert result.snapshots_built == 2
    assert result.numeric_overflow_counts == {"payout_ratio": 1}


def _row(**kw) -> FinancialFundamentalsUpsert:
    return FinancialFundamentalsUpsert(
        market="us",
        symbol="X",
        fiscal_period="2024A",
        period_type="annual",
        period_end_date=dt.date(2024, 12, 31),
        source="yfinance",
        source_collected_at=_COLLECTED,
        **kw,
    )


@pytest.mark.unit
def test_every_numeric_column_is_bounded_by_its_declared_precision() -> None:
    limits = {
        "revenue": Decimal(10) ** 28,
        "net_income": Decimal(10) ** 28,
        "gross_profit": Decimal(10) ** 28,
        "cost_of_sales": Decimal(10) ** 28,
        "discrete_revenue": Decimal(10) ** 28,
        "discrete_net_income": Decimal(10) ** 28,
        "roe": Decimal(10) ** 16,
        "payout_ratio": Decimal(10) ** 4,
        "dividend_per_share": Decimal(10) ** 16,
    }
    for name, limit in limits.items():
        assert column_limit(name) == limit
        ok = _row(**{name: limit - 1})
        bad = _row(**{name: limit})
        _, ok_counts = bound_numeric_fields(ok)
        fixed, bad_counts = bound_numeric_fields(bad)
        assert ok_counts == {}, name
        assert bad_counts == {name: 1}, name
        assert getattr(fixed, name) is None
    # every Numeric column of the model is covered by the helper
    numeric = {
        c.name
        for c in FinancialFundamentalsSnapshot.__table__.columns
        if type(c.type).__name__ == "Numeric"
    }
    assert numeric == set(limits)


@pytest.mark.unit
def test_rounding_up_to_the_limit_counts_as_overflow() -> None:
    # 9999.9999996 rounds to 10000.000000 at scale 6 → PostgreSQL overflow.
    fixed, counts = bound_numeric_fields(_row(payout_ratio=Decimal("9999.9999996")))
    assert fixed.payout_ratio is None
    assert counts == {"payout_ratio": 1}
    # negative side and a value that fits are untouched
    fixed, counts = bound_numeric_fields(_row(payout_ratio=Decimal("-10000")))
    assert fixed.payout_ratio is None
    keep, counts = bound_numeric_fields(_row(payout_ratio=Decimal("9999.999999")))
    assert keep.payout_ratio == Decimal("9999.999999")
    assert counts == {}


@pytest.mark.unit
def test_cli_prints_overflow_counts_in_summary(capsys) -> None:
    from scripts.build_us_fundamentals_snapshots import _print_result

    result = builder_us.UsFundamentalsBuildResult(
        symbols_resolved=1,
        snapshots_built=1,
        committed=False,
        numeric_overflow_counts={"payout_ratio": 3},
    )
    _print_result(result)
    assert "numeric_overflow payout_ratio=3" in capsys.readouterr().out


@pytest.mark.unit
def test_failure_last_stderr_line_names_exception_class_and_first_line(
    monkeypatch, capsys
) -> None:
    """A forced DB exception: the LAST stderr line is a one-line summary."""
    import asyncio

    from sqlalchemy.exc import DBAPIError

    import scripts.build_us_fundamentals_snapshots as cli

    class _Orig(Exception):
        pass

    async def _boom(args):  # noqa: ANN001
        raise DBAPIError(
            "INSERT INTO financial_fundamentals_snapshots ...",
            {},
            _Orig("numeric field overflow\nDETAIL: A field with precision 10"),
        )

    monkeypatch.setattr(cli, "run", _boom)
    monkeypatch.setattr(
        sys, "argv", ["build_us_fundamentals_snapshots", "--symbol", "A"]
    )
    monkeypatch.setattr(
        "app.core.cli.setup_logging_and_sentry", lambda **kw: None, raising=True
    )
    rc = asyncio.run(cli.main())
    assert rc == 1
    err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    last = err_lines[-1]
    assert "\n" not in last
    assert "DBAPIError" in last
    assert "numeric field overflow" in last
    assert "DETAIL" not in last  # first message line only


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_builder_excludes_overflowing_payout_with_warning() -> None:
    """The same overflow applies to KR (DART 현금배당성향 is passed through)."""
    import pandas as pd

    from app.services.financial_fundamentals_snapshots.builder import (
        RawAnnualFiling,
        RawFundamentalsBundle,
        build_financial_fundamentals_for_symbols,
    )

    is_frame = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "2,000,000",
            }
        ]
    )
    div_frame = pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": "1,000"},
            {"se": "현금배당성향(%)", "thstrm": "123,456.7"},
        ]
    )

    async def _fetcher(symbol, *, include_quarterly):  # noqa: ANN001
        return RawFundamentalsBundle(
            symbol=symbol,
            currency="KRW",
            annual=(
                RawAnnualFiling(
                    bsns_year=2024,
                    rcept_no="1",
                    income_statement=is_frame,
                    dividend=div_frame,
                ),
            ),
            quarterly=(),
            filing_dates={"1": dt.date(2025, 3, 18)},
        )

    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=_COLLECTED,
        fetcher=_fetcher,
    )
    (p,) = result.payloads
    assert p.payout_ratio is None
    assert p.dividend_per_share == Decimal("1000")
    assert any("field=payout_ratio count=1" in w for w in result.warnings)
