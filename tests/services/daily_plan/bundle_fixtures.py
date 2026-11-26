"""One fully proven set of builder rows for plan day 2026-11-27 (no database)."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.daily_plan import bundle_builder as builder

KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 11, 27)
PREVIOUS = date(2026, 11, 26)
FLOW_SESSIONS = (
    date(2026, 11, 20),
    date(2026, 11, 23),
    date(2026, 11, 24),
    date(2026, 11, 25),
    PREVIOUS,
)
CLOSED_AT = datetime(2026, 11, 27, 8, 20, tzinfo=KST)
CUTOFF_AT = datetime(2026, 11, 27, 8, 30, tzinfo=KST)
LATE = datetime(2026, 11, 27, 9, 0, tzinfo=KST)
SYMBOL = "005930"
OTHER = "000660"
KIS = "kis:kis_live:" + "a" * 64
TOSS = "toss:toss_live:" + "b" * 64
KIS_REF = "acct_" + "1" * 32
TOSS_REF = "acct_" + "2" * 32
BINDINGS = {
    KIS: builder.AccountBinding(KIS_REF, "kis_live"),
    TOSS: builder.AccountBinding(TOSS_REF, "toss_live"),
}


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def sessions(count: int) -> list[date]:
    """Weekdays ending at the previous session; the calendar is faked."""
    found, cursor = [], PREVIOUS
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    return found[::-1]


BAR_DAYS = sessions(80)


def facts() -> builder.SessionFacts:
    return builder.SessionFacts(
        session_date=DAY,
        previous_session=PREVIOUS,
        flow_sessions=FLOW_SESSIONS,
        closes={day: at(day, 15, 30) for day in BAR_DAYS},
    )


def bars(base: int = 70000) -> list[builder.CandleBar]:
    """A deterministic zigzag, so RSI and support levels both exist."""
    rows = []
    for index, day in enumerate(BAR_DAYS):
        close = base + ((index * 7) % 23 - 11) * 300 + index * 20
        rows.append(
            builder.CandleBar(
                session=day,
                open=Decimal(close - 100),
                high=Decimal(close + 400),
                low=Decimal(close - 500),
                close=Decimal(close),
                volume=Decimal(1000000 + (index % 9) * 10000),
                source="kis",
                # The morning run rewrites recent bars before the cutoff.
                ingested_at=at(DAY, 7, 10),
            )
        )
    return rows


def screener(row_id: int, symbol: str, turnover: str, **changes) -> builder.ScreenerRow:
    row = builder.ScreenerRow(
        row_id=row_id,
        symbol=symbol,
        snapshot_date=PREVIOUS,
        daily_turnover=Decimal(turnover),
        change_rate=Decimal("1.5000"),
        consecutive_up_days=3,
        dist_to_support_pct=None,
        created_at=at(PREVIOUS, 20, 25),
        updated_at=at(PREVIOUS, 20, 25),
    )
    return replace(row, **changes)


def valuation(row_id: int = 11, pbr: str = "1.2500") -> builder.ValuationRow:
    return builder.ValuationRow(
        row_id=row_id,
        snapshot_date=PREVIOUS,
        pbr=Decimal(pbr),
        created_at=at(PREVIOUS, 16, 30),
        updated_at=at(PREVIOUS, 16, 30),
    )


def flows(base: int = 1000) -> list[builder.FlowRow]:
    return [
        builder.FlowRow(
            row_id=base + index,
            snapshot_date=day,
            foreign_net=1000 * (index + 1) - 2500,
            created_at=at(day, 18, 10),
            updated_at=at(day, 18, 10),
        )
        for index, day in enumerate(FLOW_SESSIONS)
    ]


def news(article_id: int = 21) -> builder.NewsRow:
    return builder.NewsRow(
        article_id=article_id,
        link_first_seen_at=at(PREVIOUS, 18, 1),
        status="pending",
        judged_at=None,
        published_at=at(PREVIOUS, 18, 0),
        published_precision="minute",
        first_received_at=at(PREVIOUS, 18, 1),
        revised_at=None,
    )


def disclosure(event_id: int = 31) -> builder.DisclosureRow:
    return builder.DisclosureRow(
        event_id=event_id,
        source="naver_disclosure",
        event_date=PREVIOUS,
        published_at=None,
        published_precision=None,
        provider_created_at=at(PREVIOUS, 17, 0),
        receipt_text=None,
        first_received_at=at(PREVIOUS, 17, 5),
        revised_at=None,
        created_at=at(PREVIOUS, 17, 5),
        updated_at=at(PREVIOUS, 17, 5),
    )


def inputs(symbol: str = SYMBOL) -> builder.SymbolInputs:
    base = 70000 if symbol == SYMBOL else 150000
    ids = 0 if symbol == SYMBOL else 1
    return builder.SymbolInputs(
        security_type="STOCK",
        bars=bars(base),
        screener=screener(
            1 + ids, symbol, "900000000000.00" if ids == 0 else "500000000000.00"
        ),
        valuation=valuation(11 + ids, "1.2500" if ids == 0 else "2.1000"),
        flows=flows(1000 + 100 * ids),
        news=(news(21 + ids),),
        disclosures=(disclosure(31 + ids),),
    )


def account_row(
    row_id: int = 41,
    account: str = KIS,
    cash: str | None = "5000000",
    **changes,
) -> builder.AccountRow:
    row = builder.AccountRow(
        row_id=row_id,
        account=account,
        day=DAY,
        observed_at=at(DAY, 7, 40),
        created_at=at(DAY, 7, 41),
        holdings=[
            {
                "symbol": SYMBOL,
                "quantity": "10",
                "currency": "KRW",
                "mark": "70000",
                "mark_source": "daily_candles:kis",
                "mark_asof": "2026-11-26T00:00:00+00:00",
            },
            {
                "symbol": "459580",
                "quantity": "3",
                "currency": "KRW",
                "mark": "1000000",
                "mark_source": "daily_candles:kis",
                "mark_asof": "2026-11-26T00:00:00+00:00",
            },
            {"symbol": "AAPL", "quantity": "1", "currency": "USD", "mark": "1"},
        ],
        cash={"KRW": cash, "USD": "10"},
        reasons=[],
    )
    return replace(row, **changes)


def accounts() -> list[builder.AccountRow]:
    return [
        account_row(),
        account_row(42, TOSS, None, holdings=[], reasons=["cash_unknown"]),
    ]


def sources(
    account_rows: list[builder.AccountRow] | None = None,
) -> builder.CandidateSources:
    return builder.CandidateSources(
        screener_partition=[
            screener(1, SYMBOL, "900000000000.00"),
            screener(2, OTHER, "500000000000.00"),
        ],
        screener_latest_date=PREVIOUS,
        picks=[
            builder.PickRow(
                row_id=51,
                call_id="00000000-0000-0000-0000-000000000001",
                source="screen_stocks",
                symbol=OTHER,
                selection_seq=1,
                recorded_at=at(PREVIOUS, 10, 0),
            )
        ],
        accounts=accounts() if account_rows is None else account_rows,
    )


def build(
    per_symbol: dict[str, builder.SymbolInputs] | None = None,
    *,
    account_rows: list[builder.AccountRow] | None = None,
    candidate_sources: builder.CandidateSources | None = None,
    closed_at: datetime = CLOSED_AT,
) -> dict:
    rows = candidate_sources or sources(account_rows)
    selection = builder.select_candidates(
        rows, facts=facts(), closed_at=closed_at, bindings=BINDINGS
    )
    data = {SYMBOL: inputs(SYMBOL), OTHER: inputs(OTHER)}
    data.update(per_symbol or {})
    return builder.build_bundle(
        facts=facts(),
        closed_at=closed_at,
        cutoff_at=CUTOFF_AT,
        selection=selection,
        inputs=data,
        accounts=rows.accounts,
        bindings=BINDINGS,
    )


def evidence(body: dict, symbol: str = SYMBOL) -> dict:
    return next(row for row in body["rows"] if row["symbol"] == symbol)
