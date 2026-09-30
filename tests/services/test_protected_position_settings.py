"""#728 operator read-model behavior for failed broker or policy observations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import protected_position_settings as settings_read
from app.services.protected_quantity_service import (
    BrokerPositionUnobserved,
    ProtectedPositionSnapshot,
    ProtectionKey,
    ProtectionStateUnavailable,
)
from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    TossPortfolioSnapshot,
)


def _head(key: ProtectionKey) -> ProtectedPositionSnapshot:
    return ProtectedPositionSnapshot(
        id=1,
        key=key,
        protected_quantity=Decimal("4"),
        revision=3,
        last_confirmed_broker_held=Decimal("8"),
        last_confirmed_at=datetime.now(UTC),
        updated_by_user_id=728,
        updated_at=datetime.now(UTC),
    )


class _Service:
    def __init__(self, _db, head: ProtectedPositionSnapshot):
        self.head = head

    async def list(self):
        return [self.head]

    async def list_revisions(self, *, key):
        assert key == self.head.key
        return []


@pytest.mark.asyncio
async def test_us_kis_fresh_observation_uses_overseas_sellable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The US settings reader must accept the real overseas sellable field."""

    class _KIS:
        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "10",
                    "ovrs_ord_psbl_qty": "7",
                }
            ]

    monkeypatch.setattr(settings_read, "KISClient", _KIS)
    observation = await settings_read.fresh_broker_observation(
        key=ProtectionKey("kis_live", "us", "AAPL")
    )
    assert observation.held == Decimal("10")
    assert observation.sellable == Decimal("7")


@pytest.mark.asyncio
async def test_us_kis_fresh_observation_rejects_when_all_sellable_fields_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _KIS:
        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "10",
                }
            ]

    monkeypatch.setattr(settings_read, "KISClient", _KIS)
    with pytest.raises(settings_read.BrokerObservationUnavailable):
        await settings_read.fresh_broker_observation(
            key=ProtectionKey("kis_live", "us", "AAPL")
        )


@pytest.mark.asyncio
async def test_successful_missing_kis_head_is_orphan_shortfall_not_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {}, {}

    async def projection_must_not_replace_successful_absence(**_kwargs):
        raise AssertionError("orphan shortfall must retain direct zero evidence")

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    monkeypatch.setattr(
        settings_read,
        "headroom_for_observation",
        projection_must_not_replace_successful_absence,
    )
    row = (await settings_read.read_protected_position_settings(object()))[0]
    assert row["protected_quantity"] == "4"
    assert row["broker_held"] == "0"
    assert row["broker_sellable"] == "0"
    assert row["headroom"] == "0"
    assert row["state"] == "shortfall"
    assert row["read_error"] is None


@pytest.mark.asyncio
async def test_broker_read_failure_remains_unverified_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {}, {("kis_live", "kr"): "broker_read_failed"}

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    rows = await settings_read.read_protected_position_settings(object())
    assert rows == [
        {
            "account_scope": "kis_live",
            "market": "kr",
            "symbol": "005930",
            "name": "005930",
            "protected_quantity": "4",
            "broker_held": None,
            "broker_sellable": None,
            "headroom": None,
            "state": "unverified",
            "mode": "off",
            "broker_observed_at": None,
            "read_error": "broker_read_failed",
            "revision": 3,
            "latest_revision": None,
            "history_url": "/invest/api/settings/protected-positions/kis_live/kr/005930/history",
        }
    ]


@pytest.mark.asyncio
async def test_policy_read_failure_keeps_raw_broker_facts_but_marks_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {
            key: settings_read.LivePositionObservation(
                key=key,
                name="삼성전자",
                held=Decimal("10"),
                sellable=Decimal("8"),
                observed_at=datetime.now(UTC),
            )
        }, {}

    async def unavailable(**_kwargs):
        raise ProtectionStateUnavailable("test policy read failure")

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    monkeypatch.setattr(settings_read, "headroom_for_observation", unavailable)
    row = (await settings_read.read_protected_position_settings(object()))[0]
    assert row["state"] == "unverified"
    assert row["broker_held"] == "10"
    assert row["broker_sellable"] == "8"
    assert row["headroom"] is None
    assert row["protected_quantity"] == "4"


# --- #1061: one unreadable Toss position must not blank the whole account ---


def _toss_position(
    symbol: str, *, quantity: object, sellable: object, market: str = "kr"
) -> TossPortfolioPosition:
    return TossPortfolioPosition(
        account="toss",
        account_name="Toss",
        broker="toss",
        source="toss_api",
        instrument_type="equity_kr" if market == "kr" else "equity_us",
        market=market,
        symbol=symbol,
        name=f"name-{symbol}",
        quantity=quantity,  # type: ignore[arg-type]
        avg_buy_price=Decimal("1"),
        current_price=Decimal("1"),
        evaluation_amount=None,
        profit_loss=None,
        profit_rate=None,
        sellable_quantity=sellable,  # type: ignore[arg-type]
    )


def _mixed_toss_account(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Declared-None 005930, declared-good 000660 and US AAPL, undeclared-None
    035720, and an unreadable held on 051910.  Quantities are distinctive so a
    leak into a log line is detectable."""

    calls: list[int] = []

    async def snapshot(**kwargs):
        assert kwargs == {
            "need_sellable": True,
            "need_cash": False,
            "use_shared_snapshot": False,
        }
        calls.append(1)
        return TossPortfolioSnapshot(
            positions=[
                _toss_position("005930", quantity=Decimal("4321"), sellable=None),
                _toss_position(
                    "000660", quantity=Decimal("8765"), sellable=Decimal("8764")
                ),
                _toss_position("035720", quantity=Decimal("5555"), sellable=None),
                _toss_position(
                    "051910", quantity=Decimal("NaN"), sellable=Decimal("3")
                ),
                _toss_position(
                    "AAPL",
                    quantity=Decimal("2.5"),
                    sellable=Decimal("2.5"),
                    market="us",
                ),
            ],
            errors=[
                {
                    "source": "toss_api",
                    "stage": "sellable_quantity",
                    "symbol": "005930",
                    "error": "fake failure",
                }
            ],
        )

    monkeypatch.setattr(settings_read, "fetch_toss_portfolio_snapshot", snapshot)
    return calls


@pytest.mark.asyncio
async def test_toss_read_marks_only_the_unreadable_symbol_unobserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mixed_toss_account(monkeypatch)

    observations = {o.key.symbol: o for o in await settings_read._read_toss_positions()}

    assert set(observations) == {"005930", "000660", "035720", "051910", "AAPL"}
    good = observations["000660"]
    assert (good.held, good.sellable, good.error) == (
        Decimal("8765"),
        Decimal("8764"),
        None,
    )
    assert good.observed_at is not None
    assert observations["AAPL"].key == ProtectionKey("toss_live", "us", "AAPL")
    assert observations["AAPL"].held == Decimal("2.5")
    for symbol, field in (
        ("005930", "sellable_quantity"),
        ("035720", "sellable_quantity"),
        ("051910", "quantity"),
    ):
        bad = observations[symbol]
        # Unobserved is listed but carries no quantity at all: never held 0.
        assert bad.unobserved_field == field
        assert bad.error == f"{field}_unavailable"
        assert (bad.held, bad.sellable, bad.observed_at) == (None, None, None)


@pytest.mark.asyncio
async def test_declared_good_toss_key_is_observed_despite_a_none_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mixed_toss_account(monkeypatch)

    observation = await settings_read.fresh_broker_observation(
        key=ProtectionKey("toss_live", "kr", "000660")
    )

    assert (observation.held, observation.sellable) == (
        Decimal("8765"),
        Decimal("8764"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "field"), [("005930", "sellable_quantity"), ("051910", "quantity")]
)
async def test_declared_unreadable_toss_key_raises_unobserved_never_zero(
    monkeypatch: pytest.MonkeyPatch, symbol: str, field: str
) -> None:
    _mixed_toss_account(monkeypatch)
    key = ProtectionKey("toss_live", "kr", symbol)

    with pytest.raises(settings_read.PositionObservationUnavailable) as caught:
        await settings_read.fresh_broker_observation(key=key)

    # Existing callers (router, CLI, save) keep catching the old class.
    assert isinstance(caught.value, settings_read.BrokerObservationUnavailable)
    assert isinstance(caught.value, BrokerPositionUnobserved)
    assert (caught.value.key, caught.value.field) == (key, field)
    message = str(caught.value)
    assert symbol in message and field in message
    for leaked in ("4321", "8765", "8764", "5555", "000660", "AAPL"):
        assert leaked not in message


@pytest.mark.asyncio
async def test_successful_toss_absence_is_still_zero_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an unreadable listed row is unobserved; a real absence stays 0."""

    _mixed_toss_account(monkeypatch)

    observation = await settings_read.fresh_broker_observation(
        key=ProtectionKey("toss_live", "kr", "000270")
    )

    assert (observation.held, observation.sellable) == (Decimal("0"), Decimal("0"))


@pytest.mark.asyncio
async def test_toss_unobserved_log_names_symbol_and_field_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _mixed_toss_account(monkeypatch)

    with caplog.at_level("WARNING", logger=settings_read.__name__):
        await settings_read._read_toss_positions()

    lines = [r.getMessage() for r in caplog.records if r.name == settings_read.__name__]
    assert lines == [
        "protected position unobserved: toss_live kr 005930 sellable_quantity is unavailable",
        "protected position unobserved: toss_live kr 035720 sellable_quantity is unavailable",
        "protected position unobserved: toss_live kr 051910 quantity is unavailable",
    ]
    text = "\n".join(lines)
    for leaked in ("4321", "8765", "8764", "5555", "000660", "AAPL", "fake failure"):
        assert leaked not in text


@pytest.mark.asyncio
async def test_settings_inventory_keeps_other_toss_rows_when_one_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mixed_toss_account(monkeypatch)

    async def kis_ok(*, market: str):
        return []

    async def upbit_ok():
        return []

    monkeypatch.setattr(settings_read, "_read_kis_market", kis_ok)
    monkeypatch.setattr(settings_read, "_read_upbit_positions", upbit_ok)

    observations, failures = await settings_read.read_live_position_inventory()

    assert failures == {}
    good = observations[ProtectionKey("toss_live", "kr", "000660")]
    bad = observations[ProtectionKey("toss_live", "kr", "005930")]
    assert good.held == Decimal("8765") and good.error is None
    assert bad.held is None and bad.error == "sellable_quantity_unavailable"
    assert settings_read._state_from_observation(snapshot=None, observation=bad) == (
        "unverified",
        None,
    )


@pytest.mark.asyncio
async def test_kis_missing_sellable_still_fails_the_whole_market_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1061 is Toss only: the KIS reader keeps its whole-market failure."""

    class _KIS:
        async def fetch_my_stocks(self):
            return [
                {"pdno": "005930", "prdt_name": "A", "hldg_qty": "3"},
                {
                    "pdno": "000660",
                    "prdt_name": "B",
                    "hldg_qty": "2",
                    "ord_psbl_qty": "2",
                },
            ]

    monkeypatch.setattr(settings_read, "KISClient", _KIS)
    with pytest.raises(settings_read.BrokerObservationUnavailable) as caught:
        await settings_read.fresh_broker_observation(
            key=ProtectionKey("kis_live", "kr", "000660")
        )
    assert not isinstance(caught.value, BrokerPositionUnobserved)
