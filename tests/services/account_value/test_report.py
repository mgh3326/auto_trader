from decimal import Decimal

import pytest

from app.services.account_value.report import ReportRow, render


def test_weekly_table():
    row = ReportRow(
        "upbit",
        "crypto",
        None,
        Decimal("100"),
        Decimal("160"),
        Decimal("50"),
        Decimal("0.08"),
        Decimal("0"),
    )
    missing = ReportRow(
        "kis",
        "us",
        "alpha",
        None,
        None,
        None,
        None,
        None,
        ("cash_unknown", "benchmark_total_return_unverified"),
        "sleeve_attributed_pnl_over_budget",
    )
    assert render([row, missing]) == "\n".join(
        [
            "account | market | sleeve | start | end | flows | return | benchmark | excess | basis | fx | unmeasured",
            "kis | us | alpha | unknown | unknown | unknown | unknown | unknown | unknown | sleeve_attributed_pnl_over_budget | - | cash_unknown, benchmark_total_return_unverified",
            "upbit | crypto | - | 100 | 160 | 50 | 8.00% | 0.00% | 8.00% | modified_dietz | - | -",
        ]
    )


def test_incomplete_observation_has_no_value():
    from app.services.account_value.report import snapshot_value

    assert snapshot_value([], {"KRW": None}, [], "KRW") is None
    assert snapshot_value([], {"KRW": "0"}, [], "KRW") == 0
    assert snapshot_value([], {"KRW": "0", "USD": "1"}, [], "KRW") is None
    assert snapshot_value([], {"KRW": "0"}, ["allocation_unknown"], "KRW") is None


def test_fixture_snapshots_build_flow_adjusted_row():
    from datetime import UTC, date, datetime, timedelta
    from types import SimpleNamespace

    from app.services.account_value.report import account_row

    start = date(2026, 1, 1)
    snapshots = [
        SimpleNamespace(
            day=start + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100" if i < 3 else "150"},
            reasons=[],
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for i in range(8)
    ]
    flow = SimpleNamespace(
        day=start + timedelta(days=3), currency="KRW", amount=Decimal(50)
    )
    row = account_row(
        "upbit",
        "account",
        start,
        start + timedelta(days=7),
        snapshots,
        [flow],
        (None, None),
        flows_complete=True,
    )
    assert row.return_rate == 0
    assert row.flows == 50
    assert row.benchmark is None
    assert row.reasons == ("account_benchmark_unavailable",)
    assert (
        account_row(
            "upbit",
            "account",
            start,
            start + timedelta(days=7),
            snapshots,
            [flow],
            (None, None),
        ).return_rate
        is None
    )
    snapshots.pop(3)
    missing = account_row(
        "upbit",
        "account",
        start,
        start + timedelta(days=7),
        snapshots,
        [flow],
        (None, None),
        flows_complete=True,
    )
    assert missing.return_rate is None
    assert "missing_snapshot_day" in missing.reasons


def test_sleeve_uses_existing_book_and_period_budget():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.services.account_value.load_report import sleeve_row
    from app.services.account_value.returns import PeriodReturn
    from app.services.strategy_sleeves.book import SleeveFill

    first = datetime(2026, 1, 1, tzinfo=UTC)
    last = datetime(2026, 1, 8, tzinfo=UTC)
    sleeve = SimpleNamespace(account_mode="upbit", market="crypto", sleeve_key="alpha")
    version = SimpleNamespace(set_at=first, budget_cap=Decimal(100))
    fill = SleeveFill(
        1, 1, "KRW-BTC", "buy", Decimal(1), Decimal(10), Decimal(0), first
    )
    inputs = SimpleNamespace(
        orders=(), fills=(fill,), unattributed_rungs=(), diagnostics={}
    )

    def snap(at, mark):
        return SimpleNamespace(
            observed_at=at,
            reasons=[],
            holdings=[
                {
                    "symbol": "KRW-BTC",
                    "mark": str(mark),
                    "mark_source": "daily_candles",
                    "mark_asof": at.isoformat(),
                }
            ],
        )

    result = sleeve_row(
        sleeve,
        [version],
        inputs,
        snap(first, 10),
        snap(last, 15),
        PeriodReturn(Decimal(0), None),
    )
    assert result.return_rate == Decimal("0.05")
    assert result.basis == "sleeve_attributed_pnl_over_budget"
    assert (
        sleeve_row(
            sleeve,
            [version],
            inputs,
            snap(first, 10),
            snap(last, 15),
            PeriodReturn(Decimal(0), None),
            complete_days=False,
        ).return_rate
        is None
    )


@pytest.mark.asyncio
async def test_distinct_account_fingerprints_are_not_joined(monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from app.services.account_value import load_report

    start, end = date(2026, 1, 1), date(2026, 1, 2)
    rows = [
        SimpleNamespace(
            account="toss:toss_live:old",
            market="us",
            day=start,
            holdings=[],
            cash={"USD": "100"},
            reasons=[],
        ),
        SimpleNamespace(
            account="toss:toss_live:new",
            market="us",
            day=end,
            holdings=[],
            cash={"USD": "110"},
            reasons=[],
        ),
    ]

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([rows, [], [(row.account, row.market) for row in rows]])

        async def execute(self, query):
            return Result(next(self.results))

    async def benchmark(*args):
        return None, None

    async def no_sleeves(*args):
        return []

    monkeypatch.setattr(load_report, "_benchmark", benchmark)
    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    result = await load_report.load_weekly_report(
        Session(), start, end, flows_complete=True
    )
    assert len(result) == 2
    assert all(
        row.return_rate is None and "series_break" in row.reasons for row in result
    )


def test_flow_on_snapshot_boundary_is_unmeasured():
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.services.account_value.report import account_row

    start = date(2026, 1, 1)
    end = start + timedelta(days=1)
    snapshots = [
        SimpleNamespace(day=day, holdings=[], cash={"KRW": "100"}, reasons=[])
        for day in (start, end)
    ]
    flow = SimpleNamespace(day=start, currency="KRW", amount=Decimal(10))
    row = account_row(
        "upbit",
        "account",
        start,
        end,
        snapshots,
        [flow],
        (None, None),
        flows_complete=True,
    )
    assert row.flows is None
    assert row.return_rate is None
    assert "boundary_flow_time_unknown" in row.reasons


def test_account_value_uses_recorded_fx_and_preserves_unknown():
    from types import SimpleNamespace

    from app.services.account_value.report import account_total

    observation = SimpleNamespace(
        reasons=[],
        cash={"KRW": "100", "USD": "2"},
        holdings=[
            {
                "symbol": "VOO",
                "quantity": "1",
                "currency": "USD",
                "mark": "3",
                "mark_source": "daily",
                "mark_asof": "2026-01-01T00:00:00+00:00",
            }
        ],
        fx={
            "USD/KRW": {
                "rate": "10",
                "source": "toss",
                "asof": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    assert account_total(observation) == Decimal(150)
    observation.fx = {}
    assert account_total(observation) is None


def test_account_report_converts_cash_and_shows_fx_provenance():
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.services.account_value.report import account_row

    start = date(2026, 1, 1)
    snapshots = [
        SimpleNamespace(
            day=start + timedelta(days=i),
            holdings=[],
            cash={"USD": "100"},
            reasons=[],
            fx={
                "USD/KRW": {
                    "rate": "10" if i == 0 else "11",
                    "source": "toss",
                    "asof": "2026-01-01T00:00:00+00:00",
                }
            },
        )
        for i in range(2)
    ]
    row = account_row(
        "kis",
        "account",
        start,
        start + timedelta(days=1),
        snapshots,
        [],
        (None, None),
        flows_complete=True,
    )
    assert row.start_value == 1000
    assert row.end_value == 1100
    assert row.return_rate == Decimal("0.1")
    assert "@toss:" in row.fx_evidence


@pytest.mark.asyncio
async def test_known_account_without_any_period_snapshot_is_unmeasured(monkeypatch):
    from datetime import date

    from app.services.account_value import load_report

    account = "upbit:upbit:" + "a" * 64

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([[], [], [(account, "account")]])

        async def execute(self, query):
            return Result(next(self.results))

    async def benchmark(*args):
        return None, None

    async def no_sleeves(*args):
        return []

    monkeypatch.setattr(load_report, "_benchmark", benchmark)
    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    [row] = await load_report.load_weekly_report(
        Session(), date(2026, 1, 1), date(2026, 1, 8), flows_complete=True
    )
    assert row.return_rate is None
    assert "missing_snapshot_day" in row.reasons


@pytest.mark.asyncio
async def test_account_series_break_only_when_window_contains_multiple_identities(
    monkeypatch,
):
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.services.account_value import load_report

    start = date(2026, 1, 1)
    old, new = "upbit:upbit:" + "a" * 64, "upbit:upbit:" + "b" * 64
    snapshots = [
        SimpleNamespace(
            account=new,
            market="account",
            day=start + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
        )
        for i in range(2)
    ]

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Session:
        def __init__(self):
            self.values = iter([snapshots, [], [(old, "account"), (new, "account")]])

        async def execute(self, query):
            return Result(next(self.values))

    async def no_sleeves(*args):
        return []

    async def benchmark(*args):
        return None, None

    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", benchmark)
    rows = await load_report.load_weekly_report(
        Session(), start, start + timedelta(days=1), flows_complete=True
    )
    current = next(row for row in rows if row.account == new)
    assert current.return_rate == 0
    assert "series_break" not in current.reasons
    previous = next(row for row in rows if row.account == old)
    assert previous.return_rate is None
    assert "missing_snapshot_day" in previous.reasons


@pytest.mark.asyncio
async def test_account_series_break_blocks_returns_even_if_old_series_has_endpoints(
    monkeypatch,
):
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.services.account_value import load_report

    start = date(2026, 1, 1)
    old, new = "upbit:upbit:" + "a" * 64, "upbit:upbit:" + "b" * 64
    snapshots = [
        SimpleNamespace(
            account=account,
            market="account",
            day=day,
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
        )
        for account, day in [
            (old, start),
            (old, start + timedelta(days=1)),
            (new, start + timedelta(days=1)),
        ]
    ]

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Session:
        def __init__(self):
            self.values = iter([snapshots, [], [(old, "account"), (new, "account")]])

        async def execute(self, query):
            return Result(next(self.values))

    async def no_sleeves(*args):
        return []

    async def benchmark(*args):
        return None, None

    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", benchmark)
    rows = await load_report.load_weekly_report(
        Session(), start, start + timedelta(days=1), flows_complete=True
    )
    prior = next(row for row in rows if row.account == old)
    assert "series_break" in prior.reasons
    assert prior.return_rate is None
    assert prior.flows is None
