"""Tests for sell signal evaluation service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from app.schemas.n8n.sell_signal import N8nSellCondition, N8nSellSignalResponse
from app.services.sell_signal_service import (
    TRIGGER_THRESHOLD,
    _check_bollinger_reentry,
    _check_foreign_selling,
    _check_rsi_momentum,
    _check_stoch_rsi,
    _check_trailing_stop,
    _fetch_current_price,
    _fetch_stock_name,
    evaluate_sell_signal,
)


def _make_ohlcv_df(closes: list[float], n: int | None = None) -> pd.DataFrame:
    if n is None:
        n = len(closes)
    return pd.DataFrame(
        {
            "open": closes[:n],
            "high": [c * 1.01 for c in closes[:n]],
            "low": [c * 0.99 for c in closes[:n]],
            "close": closes[:n],
            "volume": [1000.0] * n,
        }
    )


def _make_large_ohlcv(
    n: int = 200, base: float = 100.0, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    changes = rng.normal(0, 1, n)
    closes = [base]
    for c in changes[1:]:
        closes.append(max(closes[-1] + c, 1.0))
    return _make_ohlcv_df(closes, n)


# ---------------------------------------------------------------------------
# _fetch_current_price
# ---------------------------------------------------------------------------


class TestFetchCurrentPrice:
    @pytest.mark.asyncio
    async def test_returns_price_on_success(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame({"close": [1_150_000.0]})
        price, err = await _fetch_current_price(kis, "000660")
        assert price == 1_150_000.0
        assert err is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_df(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame()
        price, err = await _fetch_current_price(kis, "000660")
        assert price is None
        assert err is None

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self):
        kis = AsyncMock()
        kis.inquire_price.side_effect = RuntimeError("API down")
        price, err = await _fetch_current_price(kis, "000660")
        assert price is None
        assert err == "API down"


# ---------------------------------------------------------------------------
# _fetch_stock_name
# ---------------------------------------------------------------------------


class TestFetchStockName:
    @pytest.mark.asyncio
    async def test_returns_name(self):
        kis = AsyncMock()
        kis.fetch_fundamental_info.return_value = {"종목명": "SK하이닉스"}
        name = await _fetch_stock_name(kis, "000660")
        assert name == "SK하이닉스"

    @pytest.mark.asyncio
    async def test_falls_back_to_symbol(self):
        kis = AsyncMock()
        kis.fetch_fundamental_info.side_effect = RuntimeError("fail")
        name = await _fetch_stock_name(kis, "000660")
        assert name == "000660"


# ---------------------------------------------------------------------------
# _check_trailing_stop
# ---------------------------------------------------------------------------


class TestCheckTrailingStop:
    @pytest.mark.asyncio
    async def test_met_when_price_below_threshold(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame({"close": [1_100_000.0]})
        cond, price, errors = await _check_trailing_stop(kis, "000660", 1_150_000)
        assert cond.name == "trailing_stop"
        assert cond.met is True
        assert price == 1_100_000.0
        assert not errors

    @pytest.mark.asyncio
    async def test_met_when_price_equals_threshold(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame({"close": [1_150_000.0]})
        cond, price, errors = await _check_trailing_stop(kis, "000660", 1_150_000)
        assert cond.met is True

    @pytest.mark.asyncio
    async def test_not_met_when_price_above_threshold(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame({"close": [1_200_000.0]})
        cond, price, errors = await _check_trailing_stop(kis, "000660", 1_150_000)
        assert cond.met is False
        assert price == 1_200_000.0

    @pytest.mark.asyncio
    async def test_not_met_when_price_unavailable(self):
        kis = AsyncMock()
        kis.inquire_price.return_value = pd.DataFrame()
        cond, price, errors = await _check_trailing_stop(kis, "000660", 1_150_000)
        assert cond.met is False
        assert price is None

    @pytest.mark.asyncio
    async def test_error_recorded_on_api_failure(self):
        kis = AsyncMock()
        kis.inquire_price.side_effect = RuntimeError("timeout")
        cond, price, errors = await _check_trailing_stop(kis, "000660", 1_150_000)
        assert cond.met is False
        assert len(errors) == 1
        assert errors[0]["condition"] == "trailing_stop"


# ---------------------------------------------------------------------------
# _check_stoch_rsi
# ---------------------------------------------------------------------------


class TestCheckStochRsi:
    @pytest.mark.asyncio
    async def test_met_when_k_below_threshold(self):
        df = _make_large_ohlcv(200, base=100)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": 25.0, "d": 30.0},
            ),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.name == "stoch_rsi"
            assert cond.met is True
            assert cond.value == pytest.approx(25.0)
            assert not errors

    @pytest.mark.asyncio
    async def test_not_met_when_k_above_threshold(self):
        df = _make_large_ohlcv(200, base=100)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": 85.0, "d": 82.0},
            ),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_empty_dataframe(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=pd.DataFrame(),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("network"),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False
            assert len(errors) == 1
            assert errors[0]["condition"] == "stoch_rsi"


# ---------------------------------------------------------------------------
# _check_foreign_selling
# ---------------------------------------------------------------------------


class TestCheckForeignSelling:
    @pytest.mark.asyncio
    async def test_met_with_consecutive_sell_days(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = [
            {"frgn_ntby_qty": "-5000"},
            {"frgn_ntby_qty": "-3000"},
        ]
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.name == "foreign_selling"
        assert cond.met is True
        assert "2일 연속 순매도" in cond.detail

    @pytest.mark.asyncio
    async def test_not_met_with_mixed_days(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = [
            {"frgn_ntby_qty": "-5000"},
            {"frgn_ntby_qty": "3000"},
        ]
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.met is False

    @pytest.mark.asyncio
    async def test_not_met_with_buy_days(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = [
            {"frgn_ntby_qty": "5000"},
            {"frgn_ntby_qty": "3000"},
        ]
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.met is False

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = [{"frgn_ntby_qty": "-5000"}]
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.met is False
        assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_empty_rows(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = []
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.met is False

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        kis = AsyncMock()
        kis.inquire_investor.side_effect = RuntimeError("API error")
        cond, errors = await _check_foreign_selling(kis, "000660", 2)
        assert cond.met is False
        assert len(errors) == 1
        assert errors[0]["condition"] == "foreign_selling"

    @pytest.mark.asyncio
    async def test_single_day_consecutive(self):
        kis = AsyncMock()
        kis.inquire_investor.return_value = [{"frgn_ntby_qty": "-1000"}]
        cond, errors = await _check_foreign_selling(kis, "000660", 1)
        assert cond.met is True


# ---------------------------------------------------------------------------
# _check_rsi_momentum
# ---------------------------------------------------------------------------


class TestCheckRsiMomentum:
    def _mock_redis(self, stored_state: dict | None = None):
        mock_r = AsyncMock()
        if stored_state:
            mock_r.get.return_value = json.dumps(stored_state)
        else:
            mock_r.get.return_value = None
        mock_r.set.return_value = True
        mock_r.aclose.return_value = None
        return mock_r

    @pytest.mark.asyncio
    async def test_met_when_rsi_drops_below_low_mark_after_high(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis({"was_above_high": True, "rsi": 72.0})

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 63.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is True
            assert "하락" in cond.detail
            # After trigger, was_above_high resets to False
            set_call = mock_r.set.call_args
            saved = json.loads(set_call[0][1])
            assert saved["was_above_high"] is False

    @pytest.mark.asyncio
    async def test_not_met_when_rsi_above_low_mark(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis({"was_above_high": True, "rsi": 72.0})

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 68.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "돌파 이력 있음" in cond.detail

    @pytest.mark.asyncio
    async def test_not_met_when_never_reached_high(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 50.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "미돌파" in cond.detail

    @pytest.mark.asyncio
    async def test_sets_was_above_high_when_rsi_reaches_high_mark(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 75.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            set_call = mock_r.set.call_args
            saved = json.loads(set_call[0][1])
            assert saved["was_above_high"] is True

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_rsi_none_returns_not_met(self):
        df = _make_large_ohlcv(200)
        self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": None},
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "계산 불가" in cond.detail

    @pytest.mark.asyncio
    async def test_redis_state_ttl_is_7_days(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 50.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            await _check_rsi_momentum("000660", 70, 65)
            set_call = mock_r.set.call_args
            assert set_call[1]["ex"] == 86400 * 7

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("redis down"),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert len(errors) == 1


# ---------------------------------------------------------------------------
# _check_bollinger_reentry
# ---------------------------------------------------------------------------


class TestCheckBollingerReentry:
    @pytest.mark.asyncio
    async def test_met_on_reentry_failure(self):
        # Build prices: above ref, then drop below ref (re-entry), current below bb_upper
        prices_above = [1_200_000.0] * 5
        prices_below = [1_100_000.0] * 5
        closes = [1_000_000.0] * 190 + prices_above + prices_below
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_100_000.0, 1_142_000.0
            )
            assert cond.name == "bollinger_reentry"
            assert cond.met is True
            assert "재진입" in cond.detail

    @pytest.mark.asyncio
    async def test_not_met_when_still_above_ref(self):
        closes = [1_200_000.0] * 200
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_200_000.0, 1_142_000.0
            )
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_not_met_when_never_above_ref(self):
        closes = [1_000_000.0] * 200
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_000_000.0, 1_142_000.0
            )
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_not_met_when_current_price_none(self):
        df = _make_large_ohlcv(200)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry("000660", None, 1_142_000.0)
            assert cond.met is False
            assert "계산 불가" in cond.detail

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_bb_upper_none(self):
        df = _make_large_ohlcv(200)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={"upper": None, "middle": None, "lower": None},
            ),
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("fail"),
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False
            assert len(errors) == 1
            assert errors[0]["condition"] == "bollinger_reentry"


# ---------------------------------------------------------------------------
# evaluate_sell_signal — Integration
# ---------------------------------------------------------------------------


class TestEvaluateSellSignal:
    def _patch_all(
        self,
        price: float | None = 1_100_000.0,
        stoch_k: float = 25.0,
        foreign_rows: list | None = None,
        rsi_val: float = 63.0,
        rsi_state: dict | None = None,
        bb_upper: float = 1_150_000.0,
        stock_name: str = "SK하이닉스",
    ):
        if foreign_rows is None:
            foreign_rows = [
                {"frgn_ntby_qty": "-5000"},
                {"frgn_ntby_qty": "-3000"},
            ]
        if rsi_state is None:
            rsi_state = {"was_above_high": True, "rsi": 72.0}

        kis_mock = AsyncMock()
        if price is not None:
            kis_mock.inquire_price.return_value = pd.DataFrame({"close": [price]})
        else:
            kis_mock.inquire_price.return_value = pd.DataFrame()
        kis_mock.fetch_fundamental_info.return_value = {"종목명": stock_name}
        kis_mock.inquire_investor.return_value = foreign_rows

        df = _make_large_ohlcv(200)

        mock_r = AsyncMock()
        mock_r.get.return_value = json.dumps(rsi_state)
        mock_r.set.return_value = True
        mock_r.aclose.return_value = None

        return (
            patch("app.services.sell_signal_service.KISClient", return_value=kis_mock),
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": stoch_k, "d": 30.0},
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": rsi_val},
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": bb_upper,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
            patch("app.services.sell_signal_service._get_redis", return_value=mock_r),
        )

    @pytest.mark.asyncio
    async def test_triggered_when_two_or_more_conditions_met(self):
        # trailing_stop met (price 1.1M <= threshold 1.152M)
        # stoch_rsi met (k=25 < 80)
        # foreign met (2 consecutive sell days)
        # rsi_momentum met (was_above_high + rsi 63 <= 65)
        patches = self._patch_all(price=1_100_000.0, stoch_k=25.0, rsi_val=63.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is True
            assert result["conditions_met"] >= TRIGGER_THRESHOLD
            assert "매도 검토" in result["message"]
            assert result["symbol"] == "000660"
            assert result["name"] == "SK하이닉스"

    @pytest.mark.asyncio
    async def test_not_triggered_when_one_condition_met(self):
        # Only trailing_stop met (price below threshold)
        # stoch_rsi not met (k=85 >= 80)
        # foreign not met (buy days)
        # rsi not met (never above high)
        patches = self._patch_all(
            price=1_100_000.0,
            stoch_k=85.0,
            foreign_rows=[
                {"frgn_ntby_qty": "5000"},
                {"frgn_ntby_qty": "3000"},
            ],
            rsi_val=50.0,
            rsi_state={},
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert result["conditions_met"] < TRIGGER_THRESHOLD
            assert "매도 대기" in result["message"]

    @pytest.mark.asyncio
    async def test_zero_conditions_met(self):
        patches = self._patch_all(
            price=1_200_000.0,  # above threshold
            stoch_k=85.0,  # above threshold
            foreign_rows=[
                {"frgn_ntby_qty": "5000"},
                {"frgn_ntby_qty": "3000"},
            ],
            rsi_val=50.0,
            rsi_state={},
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert result["conditions_met"] == 0

    @pytest.mark.asyncio
    async def test_returns_all_five_conditions(self):
        patches = self._patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert len(result["conditions"]) == 5
            names = {c.name for c in result["conditions"]}
            assert names == {
                "trailing_stop",
                "stoch_rsi",
                "foreign_selling",
                "rsi_momentum",
                "bollinger_reentry",
            }

    @pytest.mark.asyncio
    async def test_errors_collected_from_evaluators(self):
        kis_mock = AsyncMock()
        kis_mock.inquire_price.side_effect = RuntimeError("price fail")
        kis_mock.fetch_fundamental_info.return_value = {"종목명": "테스트"}
        kis_mock.inquire_investor.side_effect = RuntimeError("investor fail")

        with (
            patch("app.services.sell_signal_service.KISClient", return_value=kis_mock),
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                side_effect=RuntimeError("ohlcv fail"),
            ),
        ):
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert len(result["errors"]) > 0


# ---------------------------------------------------------------------------
# API Endpoint Tests
# ---------------------------------------------------------------------------


class TestSellSignalEndpoint:
    @pytest.fixture
    def client(self):
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.core.db import get_db
        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)

        async def override_get_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = override_get_db
        return TestClient(app)

    @pytest.mark.asyncio
    async def test_success_response_schema(self, client):
        mock_result = {
            "symbol": "000660",
            "name": "SK하이닉스",
            "triggered": False,
            "conditions_met": 0,
            "conditions": [],
            "message": "[매도 대기] SK하이닉스 0/5 조건 충족",
            "errors": [],
        }
        with (
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ),
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=None,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/000660")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["symbol"] == "000660"
            assert "as_of" in data
            assert "triggered" in data
            assert "conditions_met" in data
            assert "conditions" in data
            assert "message" in data
            assert "errors" in data

    @pytest.mark.asyncio
    async def test_custom_query_params_forwarded(self, client):
        mock_result = {
            "symbol": "005930",
            "name": "삼성전자",
            "triggered": False,
            "conditions_met": 0,
            "conditions": [],
            "message": "",
            "errors": [],
        }
        with (
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ) as mock_eval,
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=None,
            ),
        ):
            resp = client.get(
                "/api/n8n/sell-signal/005930",
                params={
                    "price_threshold": 80000,
                    "stoch_rsi_threshold": 70,
                    "foreign_days": 3,
                    "rsi_high": 75,
                    "rsi_low": 60,
                    "bb_upper_ref": 78000,
                },
            )
            assert resp.status_code == 200
            call_kwargs = mock_eval.call_args[1]
            assert call_kwargs["symbol"] == "005930"
            assert call_kwargs["price_threshold"] == 80000
            assert call_kwargs["stoch_rsi_threshold"] == 70
            assert call_kwargs["foreign_consecutive_days"] == 3
            assert call_kwargs["rsi_high_mark"] == 75
            assert call_kwargs["rsi_low_mark"] == 60
            assert call_kwargs["bb_upper_ref"] == 78000

    @pytest.mark.asyncio
    async def test_500_on_evaluate_exception(self, client):
        with (
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                side_effect=RuntimeError("catastrophic"),
            ),
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=None,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/000660")
            assert resp.status_code == 500
            data = resp.json()
            assert data["success"] is False
            assert data["triggered"] is False
            assert len(data["errors"]) > 0

    @pytest.mark.asyncio
    async def test_response_validates_as_model(self, client):
        mock_result = {
            "symbol": "000660",
            "name": "SK하이닉스",
            "triggered": True,
            "conditions_met": 3,
            "conditions": [
                N8nSellCondition(
                    name="trailing_stop",
                    met=True,
                    value=1_100_000,
                    threshold=1_152_000,
                    detail="현재가 ₩1,100,000",
                ),
                N8nSellCondition(
                    name="stoch_rsi",
                    met=True,
                    value=25.0,
                    threshold=80,
                    detail="StochRSI K=25.0",
                ),
                N8nSellCondition(
                    name="foreign_selling",
                    met=True,
                    value=None,
                    detail="2일 연속 순매도",
                ),
                N8nSellCondition(
                    name="rsi_momentum", met=False, value=68.0, detail="RSI 68.0"
                ),
                N8nSellCondition(
                    name="bollinger_reentry",
                    met=False,
                    value=1_150_000,
                    detail="밴드 상단 ₩1,150,000",
                ),
            ],
            "message": "[매도 검토] SK하이닉스 3/5 조건 충족 (trailing_stop, stoch_rsi, foreign_selling)",
            "errors": [],
        }
        with (
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ),
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=None,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/000660")
            assert resp.status_code == 200
            validated = N8nSellSignalResponse(**resp.json())
            assert validated.triggered is True
            assert validated.conditions_met == 3
            assert len(validated.conditions) == 5
