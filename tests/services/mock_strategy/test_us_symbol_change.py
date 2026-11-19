"""Operator-declared ticker change (BKH→HZN, hk 5518) for due US paper exits."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.mock_strategy import MockStrategyOrder
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.mock_strategy import us_broker
from app.services.mock_strategy.us import UsPaperStrategy
from app.services.mock_strategy.us_broker import PreDispatchRefusal, UsPaperBroker
from app.services.mock_strategy.us_signals import Candidate, UsSelection
from app.services.mock_strategy.us_symbol_change import (
    open_quantity,
    parse_symbol_changes,
)
from tests.services.mock_strategy.test_us import MemoryRecorder, PaperBroker, asset
from tests.services.mock_strategy.test_us_broker import FixedDatetime, Transport

DAY = date(2026, 11, 2)
DUE = date(2026, 11, 9)
CHANGES = {"BKH": "HZN"}


class ChangeRecorder(MemoryRecorder):
    async def record_symbol_change(self, client_order_id, event):
        row = self.rows[client_order_id]
        assert row.status == "planned" and row.side in {"sell", "cover"}
        assert event["from"] == row.symbol
        existing = row.meta.get("symbol_change")
        if existing is not None:
            assert (existing["from"], existing["to"]) == (event["from"], event["to"])
            return
        row.meta = {**row.meta, "symbol_change": event}


class ChangeBroker(PaperBroker):
    """BKH no longer exists at the broker; HZN holds the position."""

    def __init__(self):
        super().__init__()
        self.asset_map = {s: asset(s) for s in ("BKH", "NVDA")}
        self.verify_calls = []
        self.verify_refusal = None

    async def prepare(self, request, *, side, asof, confirm):
        assert confirm is True
        if request.symbol == "BKH" and side in {"sell", "cover"}:
            raise PreDispatchRefusal("asset not tradable US equity")

    async def verify_symbol_change(self, old, new, side, required):
        self.verify_calls.append((old, new, side, required))
        if self.verify_refusal:
            raise PreDispatchRefusal(self.verify_refusal)
        return {"old_asset": "not_found", "required_qty": str(required)}


def selection(horizon):
    return UsSelection(
        DAY,
        date(2026, 10, 30),
        horizon,
        (Candidate("BKH", Decimal("123")),),
        (Candidate("NVDA", Decimal("123")),),
        "fixture-prefix",
    )


async def entries(recorder, broker, horizons=(3, 5)):
    strategy = UsPaperStrategy(recorder, broker)
    for horizon in horizons:
        await strategy.run(
            asof=DAY,
            selection=lambda h=horizon: selection(h),
            horizon=horizon,
            top_n=1,
            confirm=True,
        )
    return strategy


def bkh_exits(recorder):
    return [r for r in recorder.rows.values() if r.side == "sell" and r.symbol == "BKH"]


def sells(broker):
    return [req for side, req in broker.posts if side == "sell"]


async def stranded():
    """The 2026-11-20 state: exit legs created but left planned (BKH refused)."""
    recorder, broker = ChangeRecorder(), ChangeBroker()
    strategy = await entries(recorder, broker)
    first = await strategy.close_due(asof=DUE, confirm=True)
    assert not sells(broker)
    assert len(bkh_exits(recorder)) == 2
    assert all(r.status == "planned" for r in bkh_exits(recorder))
    assert "symbol_changes" not in first
    return recorder, broker, strategy


@pytest.mark.asyncio
async def test_declared_change_sends_each_leg_once_as_new_symbol():
    recorder, broker, strategy = await stranded()
    result = await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    legs = bkh_exits(recorder)
    assert len(legs) == 2
    assert [(r.symbol, str(r.qty)) for r in sells(broker)] == [("HZN", "100")] * 2
    assert broker.verify_calls == [("BKH", "HZN", "sell", Decimal("200"))]
    for leg in legs:
        assert leg.symbol == "BKH"  # immutable journal column
        assert leg.meta["symbol_change"]["from"] == "BKH"
        assert leg.meta["symbol_change"]["to"] == "HZN"
        assert leg.status == "closed"
    assert result["symbol_changes"] == [{"from": "BKH", "to": "HZN", "applied": True}]
    bkh_entries = [
        r for r in recorder.rows.values() if r.side == "buy" and r.symbol == "BKH"
    ]
    assert [r.status for r in bkh_entries] == ["closed", "closed"]
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert len(sells(broker)) == 2  # rerun never resends


@pytest.mark.asyncio
async def test_flagless_close_due_is_unchanged():
    recorder, broker, strategy = await stranded()
    result = await strategy.close_due(asof=DUE, confirm=True)
    assert not broker.verify_calls and not sells(broker)
    assert "symbol_changes" not in result
    assert all("symbol_change" not in o for o in result["orders"])
    assert all("symbol_change" not in r.meta for r in recorder.rows.values())


@pytest.mark.parametrize(
    "refusal",
    [
        "old symbol is still an active tradable asset",
        "old symbol still has an account position",
        "new symbol is not an active tradable US equity",
        "new symbol position direction differs",
        "new symbol position below open episode quantity",
        "symbol change pre-dispatch read failed",
    ],
)
@pytest.mark.asyncio
async def test_unmet_broker_condition_sends_nothing_and_keeps_legs_planned(refusal):
    recorder, broker, strategy = await stranded()
    broker.verify_refusal = refusal
    result = await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert not sells(broker)
    assert all(r.status == "planned" for r in bkh_exits(recorder))
    assert all("symbol_change" not in r.meta for r in bkh_exits(recorder))
    assert result["symbol_changes"] == [
        {"from": "BKH", "to": "HZN", "applied": False, "reason": refusal}
    ]
    bkh = [o for o in result["orders"] if o["symbol"] == "BKH"]
    assert bkh and all(o["symbol_change"]["reason"] == refusal for o in bkh)


@pytest.mark.asyncio
async def test_mapping_without_a_due_episode_is_not_verified_or_sent():
    recorder, broker, strategy = await stranded()
    result = await strategy.close_due(
        asof=DUE, confirm=True, symbol_changes={"ZZZ": "YYY"}
    )
    assert not broker.verify_calls and not sells(broker)
    assert result["symbol_changes"][0]["reason"] == "no_due_episode"


@pytest.mark.parametrize("state", ["sending", "uncertain"])
@pytest.mark.asyncio
async def test_unresolved_leg_is_never_resent_even_with_mapping(state):
    recorder, broker, strategy = await stranded()
    leg = bkh_exits(recorder)[0]
    leg.status = state
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert not sells(broker)
    assert leg.status == state
    assert not broker.verify_calls  # quantity unknowable → refused before broker


@pytest.mark.asyncio
async def test_lost_response_after_mapped_send_stays_uncertain_without_resend():
    recorder, broker, strategy = await stranded()
    broker.fail_submit = True
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    count = len(broker.posts)
    statuses = {r.status for r in bkh_exits(recorder)}
    assert "uncertain" in statuses
    broker.fail_submit = False
    broker.orders.clear()
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert len(broker.posts) == count


@pytest.mark.asyncio
async def test_echo_must_name_the_new_symbol():
    recorder, broker, strategy = await stranded()
    broker.mismatch = True  # echoes WRONG
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert {r.status for r in bkh_exits(recorder)} == {"uncertain"}

    class OldEcho(ChangeBroker):
        async def submit(self, request, *, side, asof, confirm):
            order = await super().submit(request, side=side, asof=asof, confirm=confirm)
            order = order.model_copy(update={"symbol": "BKH"})
            self.orders[request.client_order_id] = order
            return order

    recorder, broker = ChangeRecorder(), OldEcho()
    strategy = await entries(recorder, broker)
    await strategy.close_due(asof=DUE, confirm=True)
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert {r.status for r in bkh_exits(recorder)} == {"uncertain"}


@pytest.mark.asyncio
async def test_journaled_mapping_is_not_sent_without_this_run_verifying_it():
    recorder, broker, strategy = await stranded()
    legs = bkh_exits(recorder)
    for leg in legs:
        leg.meta = {**leg.meta, "symbol_change": {"from": "BKH", "to": "HZN"}}
    result = await strategy.close_due(asof=DUE, confirm=True)
    assert not sells(broker) and {leg.status for leg in legs} == {"planned"}
    assert any(
        o.get("reason") == "symbol_change_not_verified" for o in result["orders"]
    )
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes={"BKH": "QQQ"})
    assert not sells(broker)
    assert {leg.meta["symbol_change"]["to"] for leg in legs} == {"HZN"}


@pytest.mark.asyncio
async def test_new_leg_without_prior_stranded_leg_also_uses_mapping():
    recorder, broker = ChangeRecorder(), ChangeBroker()
    strategy = await entries(recorder, broker)
    await strategy.close_due(asof=DUE, confirm=True, symbol_changes=CHANGES)
    assert [r.symbol for r in sells(broker)] == ["HZN", "HZN"]


def row(**kwargs) -> MockStrategyOrder:
    base = {
        "id": 1,
        "strategy_id": "s",
        "venue": "alpaca_paper",
        "account_ref": "a",
        "episode_id": "e1",
        "symbol": "BKH",
        "side": "buy",
        "qty": None,
        "notional": Decimal(1),
        "client_order_id": "c1",
        "exit_due_at": None,
        "status": "filled",
        "meta": {"filled_qty": "4.5", "broker_terminal": True},
    }
    return MockStrategyOrder(**(base | kwargs))


def test_open_quantity_sums_unclosed_entries_net_of_proven_exit_fills():
    rows = [
        row(),
        row(episode_id="e2", client_order_id="c2", meta={"filled_qty": "5"}),
        row(
            id=3,
            episode_id="e1",
            client_order_id="x1",
            side="sell",
            status="partially_filled",
            meta={"filled_qty": "1.5"},
        ),
        row(episode_id="e3", client_order_id="c3", status="closed"),
        row(episode_id="e4", client_order_id="c4", symbol="OTHER"),
    ]
    assert open_quantity(rows, "BKH") == ("sell", Decimal("8.0"))


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([row(status="uncertain")], "entry_fill_unresolved"),
        ([row(status="accepted", meta={})], "entry_fill_unresolved"),
        (
            [row(status="partially_filled", meta={"filled_qty": "1"})],
            "entry_fill_unresolved",
        ),
        (
            [row(), row(side="sell", client_order_id="x", status="sending", meta={})],
            "exit_leg_unresolved",
        ),
        (
            [row(), row(episode_id="s", client_order_id="s1", side="short")],
            "mixed_direction_episodes",
        ),
        ([row(status="closed")], "no_open_quantity"),
    ],
)
def test_open_quantity_refuses_when_unknowable_or_mixed(rows, reason):
    assert open_quantity(rows, "BKH") == reason


def test_open_quantity_short_closes_with_cover():
    assert open_quantity([row(side="short")], "BKH") == ("cover", Decimal("4.5"))


@pytest.mark.parametrize(
    "values",
    [
        ["BKH"],
        ["BKH=HZN=X"],
        ["BKH="],
        ["=HZN"],
        ["BKH=BKH"],
        ["BKH=HZN", "BKH=ABC"],
        ["BKH=HZN", "ABC=HZN"],
        ["BKH=HZN", "HZN=ABC"],
        ["1BAD=HZN"],
        ["BKH=H Z"],
    ],
)
def test_malformed_declarations_are_rejected(values):
    with pytest.raises(ValueError, match="symbol-change"):
        parse_symbol_changes(values)


def test_declarations_normalize_to_db_symbols():
    assert parse_symbol_changes(["bkh=hzn", " brk-b = brk/c "]) == {
        "BKH": "HZN",
        "BRK.B": "BRK.C",
    }
    assert parse_symbol_changes(None) == {} == parse_symbol_changes([])


# ---- real broker proof, against the HTTP boundary -------------------------


class SwapTransport(Transport):
    def __init__(self):
        super().__init__()
        self.assets = {
            "HZN": {"symbol": "HZN", "status": "active", "tradable": True},
        }
        self.positions = {"HZN": {"qty": "14", "qty_available": "14", "side": "long"}}

    async def request(self, method, path, **kwargs):
        import httpx

        if path.startswith("/v2/assets/"):
            self.calls.append((method, path, kwargs))
            symbol = path.rsplit("/", 1)[1]
            if symbol not in self.assets:
                return httpx.Response(404, json={"message": "asset not found"})
            data = {
                "id": f"id-{symbol}",
                "class": "us_equity",
                "shortable": True,
                "easy_to_borrow": True,
                "fractionable": True,
                **self.assets[symbol],
            }
            return httpx.Response(200, json=data)
        if path.startswith("/v2/positions/"):
            self.calls.append((method, path, kwargs))
            symbol = path.rsplit("/", 1)[1]
            if symbol not in self.positions:
                return httpx.Response(404, json={})
            return httpx.Response(
                200,
                json={
                    "asset_id": f"id-{symbol}",
                    "symbol": symbol,
                    "avg_entry_price": "85.8",
                    **self.positions[symbol],
                },
            )
        return await super().request(method, path, **kwargs)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "mock_strategy_us_enabled", True)
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", True)
    monkeypatch.setattr(us_broker, "datetime", FixedDatetime)


def make(transport):
    return UsPaperBroker(
        settings=AlpacaPaperSettings(
            api_key="fixture",
            api_secret="fixture",
            expected_account_id_suffix="paper-id",
            expected_account_number_suffix="paper-number",
        ),
        transport=transport,
    )


async def verify(transport, side="sell", required="13.981351141"):
    return await make(transport).verify_symbol_change(
        "BKH", "HZN", side, Decimal(required)
    )


@pytest.mark.asyncio
async def test_broker_proof_passes_for_missing_old_asset_and_covering_new_position(
    enabled,
):
    transport = SwapTransport()
    evidence = await verify(transport)
    assert evidence == {
        "old_asset": "not_found",
        "old_position_qty": "0",
        "new_position_qty": "14",
        "required_qty": "13.981351141",
    }
    assert {m for m, _, _ in transport.calls} == {"GET"}  # read only
    await verify(transport, required="14")  # exactly covering is enough


@pytest.mark.parametrize(
    "asset",
    [
        {"symbol": "BKH", "status": "inactive", "tradable": True},
        {"symbol": "BKH", "status": "active", "tradable": False},
    ],
)
@pytest.mark.asyncio
async def test_inactive_or_untradable_old_asset_also_counts(enabled, asset):
    transport = SwapTransport()
    transport.assets["BKH"] = asset
    assert (await verify(transport))["old_asset"] in {"inactive", "not_tradable"}


@pytest.mark.parametrize(
    "mutate,message",
    [
        (
            lambda t: t.assets.update(
                BKH={"symbol": "BKH", "status": "active", "tradable": True}
            ),
            "still an active tradable",
        ),
        (
            lambda t: t.positions.update(BKH={"qty": "1", "side": "long"}),
            "old symbol still has",
        ),
        (lambda t: t.assets.pop("HZN"), "pre-dispatch read failed"),
        (
            lambda t: t.assets.update(
                HZN={"symbol": "HZN", "status": "inactive", "tradable": True}
            ),
            "not an active tradable",
        ),
        (
            lambda t: t.assets.update(
                HZN={"symbol": "HZN", "status": "active", "tradable": False}
            ),
            "not an active tradable",
        ),
        (
            lambda t: t.assets.update(
                HZN={"symbol": "OTHER", "status": "active", "tradable": True}
            ),
            "not an active tradable",
        ),
        (lambda t: t.positions.pop("HZN"), "direction differs"),
        (
            lambda t: t.positions.update(HZN={"qty": "-14", "side": "short"}),
            "direction differs",
        ),
        (
            lambda t: t.positions.update(HZN={"qty": "13.98", "side": "long"}),
            "below open episode quantity",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_missing_broker_condition_refuses(enabled, mutate, message):
    transport = SwapTransport()
    mutate(transport)
    with pytest.raises(PreDispatchRefusal, match=message):
        await verify(transport)
    assert {m for m, _, _ in transport.calls} == {"GET"}


@pytest.mark.asyncio
async def test_cover_needs_a_short_position(enabled):
    transport = SwapTransport()
    with pytest.raises(PreDispatchRefusal, match="direction differs"):
        await verify(transport, side="cover")
    transport.positions["HZN"] = {"qty": "-14", "side": "short"}
    assert (await verify(transport, side="cover"))["new_position_qty"] == "-14"


@pytest.mark.asyncio
async def test_proof_requires_the_enabling_switches(enabled, monkeypatch):
    broker = make(SwapTransport())
    monkeypatch.setattr(settings, "mock_strategy_us_enabled", False)
    with pytest.raises(PreDispatchRefusal, match="ENABLED"):
        await broker.verify_symbol_change("BKH", "HZN", "sell", Decimal(1))


# ---- CLI -------------------------------------------------------------------


def cli(*extra, command="close-due"):
    from scripts.mock_strategy import parser

    return parser().parse_args(
        [command, "--venue", "alpaca_paper", "--asof", "2026-11-09", *extra]
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--symbol-change", "BKH"],
        ["--symbol-change", "BKH=BKH"],
        ["--symbol-change", "BKH=HZN", "--symbol-change", "BKH=ABC"],
    ],
)
@pytest.mark.asyncio
async def test_malformed_flag_is_rejected_before_any_broker_or_database(
    extra, monkeypatch
):
    from scripts import mock_strategy

    def boom(*a, **k):
        raise AssertionError("must not reach recorder or broker")

    monkeypatch.setattr(
        "app.services.mock_strategy.recorder.MockStrategyRecorder", boom
    )
    monkeypatch.setattr("app.services.mock_strategy.us_broker.UsPaperBroker", boom)
    with pytest.raises(ValueError, match="symbol-change"):
        await mock_strategy.execute(cli("--confirm", *extra))


@pytest.mark.parametrize("flag", ["BKH", "BKH=BKH"])
def test_entrypoint_reports_a_bad_flag_as_a_failed_run(flag, capsys):
    from scripts import mock_strategy

    code = mock_strategy.main(
        [
            "close-due",
            "--venue",
            "alpaca_paper",
            "--asof",
            "2026-11-09",
            "--confirm",
            "--symbol-change",
            flag,
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "mock_strategy_failed",
        "error_class": "ValueError",
    }


@pytest.mark.asyncio
async def test_run_refuses_the_flag():
    from scripts import mock_strategy

    with pytest.raises(ValueError, match="close-due and status only"):
        await mock_strategy.execute(
            cli("--confirm", "--symbol-change", "BKH=HZN", command="run")
        )


@pytest.mark.asyncio
async def test_status_shows_declared_and_journaled_mapping(monkeypatch):
    from scripts import mock_strategy

    journaled = row(
        side="sell",
        client_order_id="leg",
        status="planned",
        meta={
            "asof": "2026-11-02",
            "horizon": 3,
            "symbol_change": {"from": "BKH", "to": "HZN"},
        },
    )
    plain = row(client_order_id="plain", meta={"asof": "2026-11-02"})

    class Recorder:
        async def list_orders(self, **kwargs):
            return [journaled, plain]

    monkeypatch.setattr(
        "app.services.mock_strategy.recorder.MockStrategyRecorder", Recorder
    )
    result = await mock_strategy.execute(
        cli("--symbol-change", "BKH=HZN", command="status")
    )
    by_id = {o["client_order_id"]: o for o in result["orders"]}
    assert by_id["leg"]["symbol_change"] == {"from": "BKH", "to": "HZN"}
    assert "symbol_change" not in by_id["plain"]
    assert result["symbol_changes"] == [{"from": "BKH", "to": "HZN", "verified": False}]
    flagless = await mock_strategy.execute(cli(command="status"))
    assert "symbol_changes" not in flagless
    json.dumps(result)
