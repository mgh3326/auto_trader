"""hk 4754 (3797 T-P8) — kiwoom_mock_us proposals: session, adapter, capability.

Unit tests only: a stub US order client and the in-memory recorders from
``tests/test_mcp_kiwoom_order_variants.py`` stand in for the broker and the
database. Every refused path is proven to never reach the broker by a POST
count of zero, and the accepted path runs the same service-layer place sequence
the ``kiwoom_mock_us_place_order`` MCP tool runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.kiwoom_mock_place import (
    UsPlacePreflight,
    place_kiwoom_mock_us_order,
)
from app.services.order_proposals import approval_window as policy
from app.services.order_proposals.auto_approve import _VETO_CAPABLE_ACCOUNT_MARKETS
from app.services.order_proposals.auto_approve_audit import _ENUM_INPUT_VALUES
from app.services.order_proposals.revalidation import _default_place_order_fn
from app.services.order_proposals.service import (
    _ACTION_CAPABILITIES,
    _SUBMITTABLE_ACCOUNT_MODE_MARKETS,
)
from tests.test_mcp_kiwoom_order_variants import (
    FakeKiwoomPlaceRecorder,
    FakeKiwoomSignalRecorder,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

#: 2026-11-16 14:30 UTC = 09:30 ET (EST): XNYS regular session is open.
FROZEN_NOW = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
ACK = {"return_code": 0, "return_msg": "모의투자 매수주문완료", "ord_no": "000000063"}


def _group(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "symbol": "NVDA",
        "market": "equity_us",
        "account_mode": "kiwoom_mock_us",
        "action": "place",
        "order_type": "limit",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --- session policy ---------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 11, 16, 14, 30, tzinfo=UTC), True),  # 09:30 ET open
        (datetime(2026, 11, 16, 20, 59, 59, tzinfo=UTC), True),  # 15:59:59 ET
        (datetime(2026, 11, 16, 21, 0, tzinfo=UTC), False),  # 16:00 ET close
        (datetime(2026, 11, 16, 13, 0, tzinfo=UTC), False),  # 08:00 ET pre-market
        (datetime(2026, 11, 16, 23, 0, tzinfo=UTC), False),  # 18:00 ET after-hours
        (datetime(2026, 11, 14, 16, 0, tzinfo=UTC), False),  # Saturday
    ],
)
async def test_kiwoom_mock_us_session_is_xnys_regular_only(at, expected):
    evidence = await policy.resolve_submission_session(_group(), now=at)

    assert evidence.known is True
    assert evidence.allowed_now is expected
    assert evidence.allowed_sessions == ("regular",)
    assert evidence.source == "exchange_calendars:XNYS"


async def test_kiwoom_mock_us_session_ignores_the_toss_us_extended_key():
    # The Toss pre/post policy must never reach the mock lane: 18:00 ET is
    # post-market for Toss, and still refused here.
    after_hours = datetime(2026, 11, 16, 23, 0, tzinfo=UTC)
    evidence = await policy.resolve_submission_session(_group(), now=after_hours)
    assert evidence.allowed_now is False
    assert evidence.current_session == "closed"


# --- capability and approval sets ------------------------------------------


def test_kiwoom_mock_us_is_submittable_place_only() -> None:
    assert ("kiwoom_mock_us", "equity_us") in _SUBMITTABLE_ACCOUNT_MODE_MARKETS
    assert _ACTION_CAPABILITIES["place"] is _SUBMITTABLE_ACCOUNT_MODE_MARKETS
    assert (
        "kiwoom_mock_us",
        "equity_us",
    ) not in _ACTION_CAPABILITIES["replace"]
    assert (
        "kiwoom_mock_us",
        "equity_us",
    ) not in _ACTION_CAPABILITIES["cancel"]


def test_auto_approve_and_veto_sets_do_not_know_kiwoom_mock_us() -> None:
    assert ("kiwoom_mock_us", "equity_us") not in _VETO_CAPABLE_ACCOUNT_MARKETS
    assert ("kiwoom_mock", "equity_kr") not in _VETO_CAPABLE_ACCOUNT_MARKETS
    flat = " ".join(str(value) for value in _ENUM_INPUT_VALUES)
    assert "kiwoom_mock_us" not in flat


# --- adapter ----------------------------------------------------------------


class _StubOrders:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.posts: list[dict[str, Any]] = []

    def factory(self):
        stub = self

        class _OrderClient:
            def __init__(self, _client):
                del _client

            async def place_buy_order(self, **kwargs):
                stub.posts.append(kwargs)
                return stub.raw

            async def place_sell_order(self, **kwargs):
                stub.posts.append(kwargs)
                return stub.raw

        return _OrderClient


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch):
    """Route ``default_us_deps`` to in-memory recorders + a stub order client."""

    from app.services import kiwoom_mock_place as place_module

    place = FakeKiwoomPlaceRecorder()
    signal = FakeKiwoomSignalRecorder()
    stubs: dict[str, _StubOrders] = {}

    def _install(raw: Any) -> _StubOrders:
        stub = _StubOrders(raw)
        stubs["current"] = stub

        async def _preflight(**_kwargs: Any) -> UsPlacePreflight:
            return UsPlacePreflight(ok=True, stex_tp="ND")

        monkeypatch.setattr(
            place_module,
            "default_us_deps",
            lambda: place_module.KiwoomMockUsPlaceDeps(
                new_client=lambda: object(),
                order_client_cls=stub.factory(),
                preflight=_preflight,
                place_recorder=lambda: place,
                signal_recorder=lambda: signal,
                utc_now=lambda: FROZEN_NOW,
                finalize=place_module.default_us_finalize,
            ),
        )
        return stub

    monkeypatch.setattr(place_module, "validate_kiwoom_mock_us_config", lambda: [])
    return SimpleNamespace(install=_install, place=place, signal=signal, stubs=stubs)


async def _submit(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "account_mode": "kiwoom_mock_us",
        "market": "equity_us",
        "order_type": "limit",
        "symbol": "NVDA",
        "side": "buy",
        "quantity": 1,
        "price": Decimal("213.04"),
        "strategy": "unit-test",
        "dry_run": False,
    }
    kwargs.update(overrides)
    return await _default_place_order_fn(**kwargs)


async def test_preview_sends_and_writes_nothing(seams) -> None:
    stub = seams.install(ACK)

    result = await _submit(dry_run=True)

    assert result["success"] is True
    assert result["preview"] is True
    assert result["stex_tp"] == "ND"
    assert stub.posts == []
    assert seams.place.reserved == {}
    assert seams.signal.rows == []


async def test_submit_runs_the_shared_sequence_then_maps_resting(seams) -> None:
    stub = seams.install(ACK)

    result = await _submit()

    assert result["success"] is True
    assert result["status"] == "resting"
    assert result["broker_order_id"] == "000000063"
    assert result["idempotency_key"] == result["client_order_id"]
    assert seams.place.events == ["reserve", "record:accepted"]
    assert seams.signal.rows[0]["account_mode"] == "kiwoom_mock_us"
    assert len(stub.posts) == 1


async def test_missing_strategy_is_attribution_required_and_sends_nothing(
    seams,
) -> None:
    stub = seams.install(ACK)

    result = await _submit(strategy=None)

    assert result["success"] is False
    assert result["error_code"] == "attribution_required"
    assert stub.posts == []
    assert seams.place.reserved == {}


async def test_config_gate_refuses_before_any_broker_call(
    seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import kiwoom_mock_place as place_module

    stub = seams.install(ACK)
    monkeypatch.setattr(
        place_module,
        "validate_kiwoom_mock_us_config",
        lambda: ["KIWOOM_MOCK_US_APP_KEY"],
    )

    result = await _submit()

    assert result["success"] is False
    assert "disabled" in result["error"]
    assert stub.posts == []


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"market": "equity_kr"}, "kiwoom_mock_us_market_unsupported"),
        ({"order_type": "market"}, "kiwoom_mock_us_order_type_unsupported"),
        ({"quantity": 0}, "kiwoom_mock_us_quantity_price_invalid"),
        ({"quantity": 1.5}, "kiwoom_mock_us_quantity_price_invalid"),
        ({"price": None}, "kiwoom_mock_us_quantity_price_invalid"),
        ({"price": Decimal("0")}, "kiwoom_mock_us_quantity_price_invalid"),
        ({"price": Decimal("-1.5")}, "kiwoom_mock_us_quantity_price_invalid"),
    ],
)
async def test_shape_refusals_happen_before_any_send(
    seams, overrides: dict[str, Any], error_code: str
) -> None:
    stub = seams.install(ACK)

    result = await _submit(**overrides)

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert result["mutation_sent"] is False
    assert stub.posts == []


@pytest.mark.parametrize(
    "raw",
    [
        {"return_code": 0},
        {"return_code": 0, "ord_no": "not-digits"},
    ],
)
async def test_untrackable_acceptance_is_not_terminal(seams, raw) -> None:
    seams.install(raw)

    result = await _submit()

    assert "success" not in result  # ambiguous: neither accepted nor rejected
    assert result["status"] == "accepted_untracked"
    assert result["ledger_status"] == "uncertain"


async def test_broker_rejection_is_an_explicit_rejection(seams) -> None:
    stub = seams.install({"return_code": 20, "return_msg": "주문가능금액 부족"})

    result = await _submit()

    assert result["success"] is False
    assert result["ledger_status"] == "rejected"
    assert len(stub.posts) == 1


async def test_duplicate_dispatch_reserves_once_and_sends_once(seams) -> None:
    stub = seams.install(ACK)

    first = await _submit()
    second = await _submit()

    assert first["status"] == "resting"
    # The earlier dispatch under this key may exist, so the second call is not
    # allowed to claim "no order": the classifier sees an ambiguous answer and
    # records ``unverified``. Only one POST happened.
    assert "success" not in second
    assert second["status"] == "not_submitted"
    assert second["reconcile_required"] is True
    assert second["client_order_id"] == first["client_order_id"]
    assert len(stub.posts) == 1


async def test_pre_send_hook_block_releases_the_reservation_and_propagates(
    seams,
) -> None:
    from app.services.order_proposals.revalidation import PreSendFreshnessError

    stub = seams.install(ACK)

    async def _blocked() -> None:
        raise PreSendFreshnessError(("boundary",))

    with pytest.raises(PreSendFreshnessError):
        await _submit(pre_send_hook=_blocked)

    assert stub.posts == []
    assert seams.place.events == ["reserve", "release"]


async def test_the_adapter_and_the_mcp_tool_share_one_function() -> None:
    """The proposal branch must not grow a second place sequence."""
    import inspect

    from app.services import order_proposals

    source = inspect.getsource(order_proposals.revalidation)
    assert "place_kiwoom_mock_us_order(" in source
    assert place_kiwoom_mock_us_order.__module__ == "app.services.kiwoom_mock_place"
