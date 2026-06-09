from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
    build_financial_fundamentals_for_symbols,
)


async def _nan_fetcher(
    symbol: str, *, include_quarterly: bool
) -> RawFundamentalsBundle:
    # DART finstate_all frames carry non-finite numeric cells for missing
    # divisions/periods; DataFrame.to_dict(orient="records") preserves them as
    # float('nan')/float('inf'), which Postgres jsonb rejects.
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "1,000",
                "frgn_amount": float("nan"),
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "100",
                "frgn_amount": float("inf"),
            },
        ]
    )
    return RawFundamentalsBundle(
        symbol=symbol,
        currency="KRW",
        annual=(RawAnnualFiling(bsns_year=2024, rcept_no="r1", income_statement=df),),
        quarterly=(),
        filing_dates={"r1": dt.date(2025, 3, 20)},
    )


@pytest.mark.asyncio
async def test_raw_payload_is_strict_json_safe_when_frame_has_non_finite():
    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        fetcher=_nan_fetcher,
    )
    assert result.payloads
    for p in result.payloads:
        # Postgres-jsonb / JS JSON.parse contract: rejects NaN/Infinity.
        json.dumps(p.raw_payload, allow_nan=False)
        records = p.raw_payload["income_statement"]
        assert any("frgn_amount" in r for r in records)
        assert all(r.get("frgn_amount") is None for r in records)
