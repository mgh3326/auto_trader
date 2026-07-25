"""ROB-BAC: gateway per-second throttle rejections on order POSTs are retried.

Live evidence (2026-07-22 us-live session): two KIS overseas sells (BAC, IVV)
were answered ``EGW00201 초당 거래건수를 초과하였습니다`` and terminalized as
``rejected`` while five sibling sells on the same account succeeded. The
rejection is issued at the gateway, before the order engine — no order exists —
so a bounded re-POST is safe and is the difference between a filled trim and a
session-long dead proposal.

These tests pin both halves of that contract: the throttle family re-POSTs
within a hard cap, and every *ambiguous* outcome still fails closed exactly as
ROB-645 requires.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis.order_throttle import (
    MAX_THROTTLE_RESUBMITS,
    is_provider_throttle_reject,
    throttle_backoff_seconds,
)
from app.services.brokers.kis.send_outcome import OrderSendOutcomeTracker

_THROTTLE_BODY = {
    "rt_cd": "1",
    "msg_cd": "EGW00201",
    "msg1": "초당 거래건수를 초과하였습니다.",
}
_ACCEPTED_BODY = {
    "rt_cd": "0",
    "output": {"ODNO": "0030808418", "ORD_TMD": "233959"},
    "msg1": "정상처리 되었습니다.",
}


def _make_parent(responses):
    """Parent whose transport yields ``responses`` in order."""
    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    parent._request_with_rate_limit = AsyncMock(side_effect=list(responses))
    return parent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the backoff contract but not its wall-clock cost."""
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "app.services.brokers.kis.overseas_orders.asyncio.sleep", _fake_sleep
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.asyncio.sleep", _fake_sleep
    )
    return slept


@pytest.fixture(autouse=True)
def _no_nxt(monkeypatch):
    from app.services.brokers.kis import domestic_orders

    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )


def _overseas(responses):
    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    parent = _make_parent(responses)
    return OverseasOrderClient(parent), parent


def _domestic(responses):
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = _make_parent(responses)
    return DomesticOrderClient(parent), parent


@pytest.mark.unit
class TestThrottleClassifier:
    def test_documented_codes_classify(self):
        assert is_provider_throttle_reject(
            "EGW00201", "초당 거래건수를 초과하였습니다."
        )
        assert is_provider_throttle_reject("EGW00215", "초당 거래건수 초과")

    def test_undocumented_code_falls_back_to_message(self):
        assert is_provider_throttle_reject(
            "EGW99999", "초당 거래건수를 초과하였습니다."
        )

    def test_business_rejections_are_not_throttles(self):
        # The one that must never be re-POSTed: a real order-engine rejection.
        assert not is_provider_throttle_reject(
            "APBK0656", "주문가능금액을 초과하였습니다."
        )
        assert not is_provider_throttle_reject(
            "APBK0918", "주문가능수량을 초과하였습니다"
        )
        assert not is_provider_throttle_reject(None, None)

    def test_backoff_grows_and_is_bounded(self):
        delays = [throttle_backoff_seconds(d) for d in range(MAX_THROTTLE_RESUBMITS)]
        assert delays == sorted(delays)
        assert all(0 < d <= 2.0 for d in delays)


@pytest.mark.unit
class TestOverseasThrottleRepost:
    """The BAC/IVV path."""

    @pytest.mark.asyncio
    async def test_throttled_sell_reposts_and_succeeds(self, _no_sleep):
        instance, parent = _overseas([_THROTTLE_BODY, _ACCEPTED_BODY])

        result = await instance.order_overseas_stock(
            "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
        )

        assert result["odno"] == "0030808418"
        assert parent._request_with_rate_limit.await_count == 2
        assert _no_sleep == [throttle_backoff_seconds(0)]

    @pytest.mark.asyncio
    async def test_repost_is_capped_then_fails_closed(self, _no_sleep):
        instance, parent = _overseas([_THROTTLE_BODY] * (MAX_THROTTLE_RESUBMITS + 1))

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == MAX_THROTTLE_RESUBMITS + 1

    @pytest.mark.asyncio
    async def test_repost_preserves_order_parameters(self):
        instance, parent = _overseas([_THROTTLE_BODY, _ACCEPTED_BODY])

        await instance.order_overseas_stock(
            "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
        )

        first, second = parent._request_with_rate_limit.await_args_list
        assert first.kwargs["json_body"] == second.kwargs["json_body"]
        assert first.kwargs["tr_id"] == second.kwargs["tr_id"]

    @pytest.mark.asyncio
    async def test_business_rejection_is_not_reposted(self):
        instance, parent = _overseas(
            [
                {
                    "rt_cd": "1",
                    "msg_cd": "APBK0656",
                    "msg1": "주문가능금액을 초과하였습니다.",
                }
            ]
        )

        with pytest.raises(RuntimeError, match="APBK0656"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_timeout_still_sends_exactly_once(self):
        instance, parent = _overseas([])
        parent._request_with_rate_limit = AsyncMock(side_effect=httpx.ReadTimeout(""))

        with pytest.raises(httpx.ReadTimeout):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_throttle_body_carried_by_5xx_fails_closed(self):
        """A 5xx never proves the order was not created — no re-POST."""
        instance, parent = _overseas([])

        async def _respond(*args, **kwargs):
            tracker = kwargs["send_outcome"]
            tracker.mark_dispatched()
            tracker.mark_http_response(500)
            return _THROTTLE_BODY

        parent._request_with_rate_limit = AsyncMock(side_effect=_respond)

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_caller_tracker_is_threaded_through_the_repost(self):
        """The re-POST re-uses the caller's tracker, not a detached copy.

        Each dispatch re-marks it (``mark_dispatched`` clears the previous
        attempt), so the caller ends up observing the *final* attempt rather
        than the intermediate throttle rejection.
        """
        instance, parent = _overseas([_THROTTLE_BODY, _ACCEPTED_BODY])
        tracker = OrderSendOutcomeTracker()

        await instance.order_overseas_stock(
            "BAC", "NYSE", "sell", 1, 62.15, is_mock=False, send_outcome=tracker
        )

        seen = [
            call.kwargs["send_outcome"]
            for call in parent._request_with_rate_limit.await_args_list
        ]
        assert seen == [tracker, tracker]


@pytest.mark.unit
class TestDomesticThrottleRepost:
    @pytest.mark.asyncio
    async def test_throttled_order_reposts_and_succeeds(self):
        instance, parent = _domestic([_THROTTLE_BODY, _ACCEPTED_BODY])

        result = await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert result["odno"] == "0030808418"
        assert parent._request_with_rate_limit.await_count == 2

    @pytest.mark.asyncio
    async def test_repost_is_capped_then_fails_closed(self):
        instance, parent = _domestic([_THROTTLE_BODY] * (MAX_THROTTLE_RESUBMITS + 1))

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert parent._request_with_rate_limit.await_count == MAX_THROTTLE_RESUBMITS + 1

    @pytest.mark.asyncio
    async def test_business_rejection_is_not_reposted(self):
        instance, parent = _domestic(
            [
                {
                    "rt_cd": "1",
                    "msg_cd": "APBK0918",
                    "msg1": "주문가능수량을 초과하였습니다",
                }
            ]
        )

        with pytest.raises(RuntimeError, match="APBK0918"):
            await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert parent._request_with_rate_limit.await_count == 1
