"""ROB-364 — cleanup gate/reason hardening for the KIS mock **overseas/US**
holdings-delta smoke (the US counterpart to ROB-358's domestic smoke).

These tests prove the ``--confirm`` round trip for overseas/US KIS mock:

* fail-fast BEFORE any BUY when the smoke is disabled, the account cannot
  satisfy the SELL/CANCEL cleanup path, or the US market is closed — the BUY
  path (client construction) is never reached;
* fail-fast BEFORE any BUY on a missing/stale quote (no marketable limit) or an
  unresolved exchange;
* the cleanup SELL flattens a filled residual back to baseline (final delta 0);
* an unfilled resting BUY is CANCELLED (not left to fill later);
* every off-baseline / rejected / id-less cleanup outcome is an explicit anomaly
  (exit 3), never a silent success or a leaked exception (exit 1).

Unlike the domestic smoke there is NO scalping-exit validator and NO overseas
``KisMockBroker`` — cleanup goes through the overseas order client directly, so
the fakes here implement that client surface (``fetch_my_us_stocks`` /
``sell_overseas_stock`` / ``cancel_overseas_order`` / ``inquire_overseas_minute_chart``).

stdlib + pandas + fakes only; no broker / network / secrets.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import scripts.kis_mock_overseas_holdings_delta_smoke as smoke


class _FakeSettings:
    """Minimal stand-in for the Settings object the gate returns."""

    def __init__(self, *, account_no: str = "12345678-01") -> None:
        self.kis_mock_app_key = "k"
        self.kis_mock_app_secret = "s"
        self.kis_mock_account_no = account_no


def _holdings(symbol: str | None, qty: int) -> list[dict]:
    """A KIS overseas holdings row list (pre-filtered to nonzero positions)."""
    if symbol is None or qty == 0:
        return []
    return [{"ovrs_pdno": symbol, "ovrs_cblc_qty": str(qty)}]


class _Page:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame


def _fresh_minute_frame(close: float) -> pd.DataFrame:
    # A candle stamped at "now" in US/Eastern (the tz the smoke localizes to)
    # so the freshness gate passes deterministically.
    now_et = dt.datetime.now(ZoneInfo("America/New_York")).replace(
        tzinfo=None, microsecond=0
    )
    return pd.DataFrame([{"datetime": now_et, "close": close}])


class _FakeOverseasClient:
    """Fake KIS overseas client covering only the surfaces the smoke calls."""

    def __init__(
        self,
        *,
        holdings_seq: list[list[dict]] | None = None,
        close: float | None = 100.0,
        stale: bool = False,
        sell: dict | None = None,
        sell_exc: Exception | None = None,
        cancel: dict | None = None,
        cancel_exc: Exception | None = None,
        buy: dict | None = None,
        buy_exc: Exception | None = None,
        fetch_exc: bool = False,
    ) -> None:
        self._holdings_seq = list(holdings_seq or [[]])
        self._close = close
        self._stale = stale
        self._sell = sell
        self._sell_exc = sell_exc
        self._cancel = cancel
        self._cancel_exc = cancel_exc
        self._buy = buy
        self._buy_exc = buy_exc
        self._fetch_exc = fetch_exc
        self.sell_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.buy_calls: list[dict] = []

    async def fetch_my_us_stocks(self, is_mock: bool = False, exchange: str = "NASD"):
        if self._fetch_exc:
            raise RuntimeError("overseas balance read failed")
        if len(self._holdings_seq) > 1:
            return self._holdings_seq.pop(0)
        return self._holdings_seq[0] if self._holdings_seq else []

    async def inquire_overseas_minute_chart(
        self, symbol, exchange_code="NASD", n=1, keyb=""
    ):
        if self._close is None:
            return _Page(pd.DataFrame(columns=["datetime", "close"]))
        if self._stale:
            return _Page(
                pd.DataFrame(
                    [{"datetime": dt.datetime(2020, 1, 1, 9, 31), "close": self._close}]
                )
            )
        return _Page(_fresh_minute_frame(self._close))

    async def inquire_overseas_margin(self, is_mock: bool = False):
        raise RuntimeError("OPSQ0002 no such service code")

    async def buy_overseas_stock(
        self, symbol, exchange_code, quantity, price=0.0, is_mock=False
    ):
        self.buy_calls.append(
            {"symbol": symbol, "exchange_code": exchange_code, "quantity": quantity}
        )
        if self._buy_exc is not None:
            raise self._buy_exc
        return self._buy or {"odno": "BUY0000001"}

    async def sell_overseas_stock(
        self, symbol, exchange_code, quantity, price=0.0, is_mock=False
    ):
        self.sell_calls.append(
            {"symbol": symbol, "exchange_code": exchange_code, "quantity": quantity}
        )
        if self._sell_exc is not None:
            raise self._sell_exc
        return self._sell if self._sell is not None else {"odno": "SELL0000001"}

    async def cancel_overseas_order(
        self, order_number, symbol, exchange_code, quantity, is_mock=False
    ):
        self.cancel_calls.append(
            {
                "order_number": order_number,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "quantity": quantity,
            }
        )
        if self._cancel_exc is not None:
            raise self._cancel_exc
        return self._cancel if self._cancel is not None else {"odno": "CANCEL00001"}


def _confirm_args(extra: list[str] | None = None):
    argv = [
        "--confirm",
        "--symbol",
        "AAPL",
        "--exchange",
        "NASD",
        "--max-poll",
        "2",
        "--poll-interval",
        "0",
    ]
    return smoke._parse_args(argv + (extra or []))


# --- fail-fast preflight (before any BUY / client construction) ------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fails_fast_when_smoke_disabled(monkeypatch):
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: None)
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 4
    created.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fails_fast_when_account_cannot_clean_up(monkeypatch):
    # account_no too short to form CANO/ACNT_PRDT_CD -> SELL/CANCEL would fail,
    # so we must never acquire a position. Stop before any BUY.
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings(account_no="123"))
    monkeypatch.setattr(smoke, "_us_market_open", lambda: True)
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 4
    created.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fails_fast_when_us_market_closed(monkeypatch):
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: False)
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 4
    created.assert_not_called()


# --- pre-BUY quote / exchange gates (client built, BUY never sent) ---------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_no_quote_blocks_before_buy(monkeypatch):
    client = _FakeOverseasClient(close=None)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: True)
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 2
    assert client.buy_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_stale_quote_blocks_before_buy(monkeypatch):
    client = _FakeOverseasClient(close=100.0, stale=True)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: True)
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 2
    assert client.buy_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_unresolved_exchange_blocks_before_buy(monkeypatch):
    client = _FakeOverseasClient(close=100.0)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: True)
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )

    async def _boom(*_a, **_k):
        raise smoke._ExchangeResolutionError("AAPL not registered")

    monkeypatch.setattr(smoke, "_resolve_exchange", _boom)
    rc = await smoke.run_confirm(_confirm_args(["--exchange", ""]))
    assert rc == 2
    assert client.buy_calls == []


# --- happy path: buy fills, cleanup flattens to baseline (final delta 0) ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_happy_path_buys_fills_and_flattens(monkeypatch):
    # baseline 0 -> buy fills to 1 -> cleanup SELL -> back to 0.
    client = _FakeOverseasClient(
        holdings_seq=[
            _holdings(None, 0),  # baseline
            _holdings("AAPL", 1),  # entry fill poll
            _holdings("AAPL", 1),  # cleanup current
            _holdings(None, 0),  # post-sell
        ],
        close=100.0,
    )
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: True)
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )
    rc = await smoke.run_confirm(_confirm_args(["--notional-usd", "100"]))
    assert rc == 0
    assert client.buy_calls and client.buy_calls[0]["exchange_code"] == "NASD"
    assert client.sell_calls and client.sell_calls[0]["quantity"] == 1


# --- cleanup matrix (call _cleanup_and_verify directly) --------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_happy_path_flattens_to_baseline():
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 1), _holdings(None, 0)], close=100.0
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 0
    assert evidence["cleanup"] == "flattened"
    assert evidence["cleanup_sell_order_id"] == "SELL0000001"
    assert evidence["final_position_delta_vs_baseline"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_residual_position_is_explicit_anomaly():
    client = _FakeOverseasClient(holdings_seq=[_holdings("AAPL", 1)], close=100.0)
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup"] == "UNCONFIRMED_residual_position"
    assert evidence["final_position_delta_vs_baseline"] == "1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_sell_submit_rejection_is_anomaly_not_unexpected():
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 1)],
        close=100.0,
        sell_exc=RuntimeError("APBK0918 rejected"),
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup_sell_order_id"] is None
    assert "cleanup_error" in evidence
    assert evidence["cleanup"] == "SELL_submit_rejected"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_sell_missing_order_id_is_anomaly():
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 1)], close=100.0, sell={"odno": None}
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup"] == "SELL_no_order_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_below_baseline_at_start_is_anomaly():
    # current holdings (0) below baseline (1): final delta -1, never a clean exit.
    client = _FakeOverseasClient(holdings_seq=[_holdings(None, 0)], close=100.0)
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("1"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["final_position_delta_vs_baseline"] == "-1"
    assert evidence["cleanup"] != "flattened"
    assert "cleanup_error" in evidence


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_over_flatten_below_baseline_is_anomaly():
    # baseline 1 -> current 2 (delta 1) -> SELL -> post 0 (below baseline): delta -1.
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 2), _holdings(None, 0)], close=100.0
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("1"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup"] == "over_flattened_anomaly"
    assert evidence["final_position_delta_vs_baseline"] == "-1"
    assert "cleanup_error" in evidence


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_holdings_read_failure_is_anomaly():
    client = _FakeOverseasClient(fetch_exc=True, close=100.0)
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert "cleanup_error" in evidence


# --- unfilled resting BUY -> CANCEL (not left to fill later) ----------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_unfilled_resting_buy_is_cancelled():
    # delta 0 at cleanup but a BUY odno is live -> cancel it; stays flat -> exit 2.
    client = _FakeOverseasClient(
        holdings_seq=[_holdings(None, 0), _holdings(None, 0)], close=100.0
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=None,  # never filled
    )
    assert rc == 2  # fill-unconfirmed but flat
    assert (
        client.cancel_calls and client.cancel_calls[0]["order_number"] == "BUY0000001"
    )
    assert client.cancel_calls[0]["exchange_code"] == "NASD"
    assert client.sell_calls == []  # nothing to flatten
    assert evidence["buy_cancel_attempted"] is True
    assert evidence["buy_cancel_order_id"] == "CANCEL00001"
    assert evidence["open_order_check_status"] == "resting_buy_cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_cancel_submit_rejection_is_anomaly():
    client = _FakeOverseasClient(
        holdings_seq=[_holdings(None, 0)],
        close=100.0,
        cancel_exc=RuntimeError("cancel rejected"),
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=None,
    )
    assert rc == 3
    assert evidence["cleanup"] == "CANCEL_submit_rejected"
    assert evidence["buy_cancel_order_id"] is None
    assert evidence["open_order_check_status"] == "resting_buy_cancel_failed"
    assert (
        client.sell_calls == []
    )  # fail-closed: no SELL after an un-cancellable resting BUY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_cancel_missing_order_id_is_anomaly():
    client = _FakeOverseasClient(
        holdings_seq=[_holdings(None, 0)], close=100.0, cancel={"odno": None}
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=None,
    )
    assert rc == 3
    assert evidence["cleanup"] == "CANCEL_no_order_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_nothing_to_flatten_no_buy_odno_is_fill_unconfirmed():
    # delta 0 and no BUY odno (submit never acked): nothing to cancel/sell -> exit 2.
    client = _FakeOverseasClient(holdings_seq=[_holdings(None, 0)], close=100.0)
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        None,  # no buy odno
        Decimal("1"),
        evidence,
        entry_fill=None,
    )
    assert rc == 2
    assert evidence["cleanup"] == "nothing_to_flatten"
    assert client.cancel_calls == []
    assert client.sell_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_late_fill_during_cancel_falls_through_to_sell():
    # delta 0 at cleanup start -> cancel the resting BUY -> a late fill lands
    # (delta 1 on the post-cancel re-read) -> fall through to SELL -> flatten.
    # The entry was NEVER a confirmed full fill, so even flat this is exit 2,
    # NOT exit 0 (blocker 2).
    client = _FakeOverseasClient(
        holdings_seq=[_holdings(None, 0), _holdings("AAPL", 1), _holdings(None, 0)],
        close=100.0,
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("1"),
        evidence,
        entry_fill=None,  # poll missed it; it filled during the cancel window
    )
    assert rc == 2
    assert client.cancel_calls and client.sell_calls
    assert client.sell_calls[0]["quantity"] == 1
    assert evidence["cleanup"] == "flattened_entry_unconfirmed"
    assert evidence["final_position_delta_vs_baseline"] == "0"


# --- pre-BUY baseline / size gates (run_confirm, BUY never sent) -----------


def _confirm_env(monkeypatch, client, *, market_open=True):
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(smoke, "_us_market_open", lambda: market_open)
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_baseline_read_failed_blocks_before_buy(monkeypatch):
    # quote passes (inquire ok) but the baseline holdings read raises -> no BUY.
    client = _FakeOverseasClient(close=100.0, fetch_exc=True)
    _confirm_env(monkeypatch, client)
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 2
    assert client.buy_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_size_zero_blocks_before_buy(monkeypatch):
    # notional 20 / close 100000 floors to 0 shares -> size_zero, no BUY.
    client = _FakeOverseasClient(close=100000.0)
    _confirm_env(monkeypatch, client)
    rc = await smoke.run_confirm(_confirm_args())
    assert rc == 2
    assert client.buy_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_entry_unconfirmed_but_flat_is_exit_2(monkeypatch):
    # BUY acked but the entry never confirms a fill; holdings stay flat -> the
    # resting BUY is cancelled and the run reports fill-unconfirmed (exit 2).
    client = _FakeOverseasClient(
        holdings_seq=[_holdings(None, 0), _holdings(None, 0)], close=100.0
    )
    _confirm_env(monkeypatch, client)

    async def _no_fill(*_a, **_k):
        return smoke._EntryOutcome(
            fill=None, verdict="none", observed_delta=Decimal("0")
        )

    monkeypatch.setattr(smoke, "_await_entry_fill", _no_fill)
    rc = await smoke.run_confirm(_confirm_args(["--notional-usd", "100"]))
    assert rc == 2
    assert client.buy_calls  # the BUY was placed
    assert client.cancel_calls  # the resting BUY was cancelled


# --- final evidence packet structure (AC5) ---------------------------------


def _last_json_with(caplog, key: str) -> dict | None:
    found = None
    for rec in caplog.records:
        try:
            obj = json.loads(rec.getMessage())
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and key in obj:
            found = obj
    return found


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_happy_path_evidence_has_required_fields(monkeypatch, caplog):
    client = _FakeOverseasClient(
        holdings_seq=[
            _holdings(None, 0),  # baseline
            _holdings("AAPL", 1),  # entry fill poll
            _holdings("AAPL", 1),  # cleanup current
            _holdings(None, 0),  # post-sell
        ],
        close=100.0,
    )
    _confirm_env(monkeypatch, client)
    with caplog.at_level(logging.INFO, logger=smoke.__name__):
        rc = await smoke.run_confirm(_confirm_args(["--notional-usd", "100"]))
    assert rc == 0
    ev = _last_json_with(caplog, "exit_code")
    assert ev is not None
    for key in (
        "mode",
        "symbol",
        "exchange",
        "buy_limit_price",
        "baseline_holdings_qty",
        "quantity",
        "buy_order_id",
        "entry_order_id",
        "confirmation_signal",
        "entry_fill_verdict",
        "entry_fill_confirmed",
        "entry_filled",
        "fill_price_source",
        "buy_cancel_attempted",
        "open_order_check_status",
        "cleanup",
        "cleanup_sell_order_id",
        "final_position_delta_vs_baseline",
        "final_exit_reason",
        "exit_code",
    ):
        assert key in ev, f"missing evidence field: {key}"
    assert ev["exit_code"] == 0
    assert ev["final_position_delta_vs_baseline"] == "0"
    assert ev["fill_price_source"] == "limit_fallback"  # cash OPSQ0002 in mock
    assert ev["entry_fill_verdict"] == "filled"
    assert ev["entry_fill_confirmed"] is True
    assert ev["open_order_check_status"] == "full_fill_no_resting_order"
    assert (
        ev["buy_cancel_attempted"] is False
    )  # full fill -> no resting order to cancel


# --- run_preflight (read-only mode) ----------------------------------------


def _preflight_args(extra: list[str] | None = None):
    argv = ["--preflight", "--symbol", "AAPL", "--exchange", "NASD"]
    return smoke._parse_args(argv + (extra or []))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: None)
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )
    rc = await smoke.run_preflight(_preflight_args())
    assert rc == 4
    created.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_exchange_unresolved_is_exit_2(monkeypatch):
    client = _FakeOverseasClient(close=100.0)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )

    async def _boom(*_a, **_k):
        raise smoke._ExchangeResolutionError("AAPL not registered")

    monkeypatch.setattr(smoke, "_resolve_exchange", _boom)
    rc = await smoke.run_preflight(_preflight_args(["--exchange", ""]))
    assert rc == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_holdings_read_failure_is_exit_2(monkeypatch):
    client = _FakeOverseasClient(fetch_exc=True, close=100.0)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )
    rc = await smoke.run_preflight(_preflight_args())
    assert rc == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_success_reports_holdings_and_opsq2_cash(monkeypatch, caplog):
    # margin raises OPSQ0002 (the fake default) -> cash null + opsq2 source.
    client = _FakeOverseasClient(holdings_seq=[_holdings("AAPL", 4)], close=100.0)
    monkeypatch.setattr(smoke, "_gate_or_exit", lambda: _FakeSettings())
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client",
        lambda *a, **k: client,
    )
    with caplog.at_level(logging.INFO, logger=smoke.__name__):
        rc = await smoke.run_preflight(_preflight_args())
    assert rc == 0
    ev = _last_json_with(caplog, "holdings_qty")
    assert ev is not None
    assert ev["mode"] == "preflight"
    assert ev["exchange"] == "NASD"
    assert ev["holdings_qty"] == "4"
    assert ev["cash_usd"] is None
    assert ev["cash_source"] == "unavailable_opsq0002"


# --- pure smoke-script helpers ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_env_truthy_accepts(value):
    assert smoke._env_truthy(value) is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "false", "", "no", "maybe", None])
def test_env_truthy_rejects(value):
    assert smoke._env_truthy(value) is False


@pytest.mark.unit
def test_localize_quote_ts_naive_is_eastern_to_utc():
    naive = dt.datetime(2026, 5, 29, 9, 31)  # 09:31 US/Eastern
    out = smoke._localize_quote_ts(naive)
    assert out.utcoffset() == dt.timedelta(0)
    assert out == naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(dt.UTC)


@pytest.mark.unit
def test_localize_quote_ts_aware_utc_passthrough():
    aware = dt.datetime(2026, 5, 29, 13, 31, tzinfo=dt.UTC)
    assert smoke._localize_quote_ts(aware) == aware


@pytest.mark.unit
def test_localize_quote_ts_aware_other_tz_to_utc():
    seoul = dt.datetime(2026, 5, 29, 22, 31, tzinfo=ZoneInfo("Asia/Seoul"))
    out = smoke._localize_quote_ts(seoul)
    assert out.utcoffset() == dt.timedelta(0)
    assert out == seoul.astimezone(dt.UTC)


@pytest.mark.unit
def test_parse_usd_cash_non_list_is_unavailable():
    assert smoke._parse_usd_cash(None) == (None, "unavailable")


@pytest.mark.unit
def test_parse_usd_cash_reads_usd_row():
    rows = [{"crcy_cd": "USD", "frcr_dncl_amt1": "123.45"}]
    cash, source = smoke._parse_usd_cash(rows)
    assert cash == Decimal("123.45")
    assert source == "frcr_dncl_amt1"


@pytest.mark.unit
def test_parse_usd_cash_no_usd_row_is_unavailable():
    rows = [{"crcy_cd": "KRW", "frcr_dncl_amt1": "1000"}]
    assert smoke._parse_usd_cash(rows) == (None, "unavailable")


@pytest.mark.unit
def test_parse_usd_cash_invalid_decimal_falls_back_to_next_key():
    rows = [{"crcy_cd": "USD", "frcr_dncl_amt1": "abc", "frcr_dncl_amt_2": "50"}]
    cash, source = smoke._parse_usd_cash(rows)
    assert cash == Decimal("50")
    assert source == "frcr_dncl_amt_2"


# --- partial / unconfirmed fill must cancel the resting BUY + never exit 0 ---
# (safety-review blockers 1 & 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_partial_fill_cancels_resting_buy_and_is_not_exit_0():
    # entry never confirmed FULL (entry_fill=None) but holdings moved (+1) -> the
    # original BUY may have an unfilled resting remainder. Cancel it first, SELL
    # the filled delta, and even though flat, this is exit 2 (NOT 0).
    client = _FakeOverseasClient(
        holdings_seq=[
            _holdings("AAPL", 1),  # cleanup current (partial fill landed)
            _holdings("AAPL", 1),  # post-cancel re-read (still held)
            _holdings(None, 0),  # post-sell flat
        ],
        close=100.0,
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("2"),  # ordered 2, only 1 filled -> resting remainder
        evidence,
        entry_fill=None,
    )
    assert rc == 2  # flat but entry never confirmed full -> NOT a clean exit 0
    assert (
        client.cancel_calls and client.cancel_calls[0]["order_number"] == "BUY0000001"
    )
    assert client.sell_calls and client.sell_calls[0]["quantity"] == 1
    assert evidence["buy_cancel_attempted"] is True
    assert evidence["open_order_check_status"] == "resting_buy_cancelled"
    assert evidence["cleanup"] == "flattened_entry_unconfirmed"
    assert evidence["final_position_delta_vs_baseline"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_partial_fill_cancel_rejected_is_fail_closed():
    # entry unconfirmed + positive delta, but the resting BUY cancel is rejected:
    # we cannot authoritatively remove the resting-order risk -> fail closed (3),
    # and we do NOT proceed to SELL.
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 1)],
        close=100.0,
        cancel_exc=RuntimeError("cancel rejected"),
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        "BUY0000001",
        Decimal("2"),
        evidence,
        entry_fill=None,
    )
    assert rc == 3
    assert evidence["cleanup"] == "CANCEL_submit_rejected"
    assert evidence["open_order_check_status"] == "resting_buy_cancel_failed"
    assert client.sell_calls == []  # never SELL when the resting BUY is un-cancellable


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_unconfirmed_positive_delta_no_odno_is_not_exit_0():
    # positive delta with an unconfirmed entry and NO acked BUY odno (nothing to
    # cancel): flatten the delta but still never exit 0.
    client = _FakeOverseasClient(
        holdings_seq=[_holdings("AAPL", 1), _holdings(None, 0)], close=100.0
    )
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        client,
        _confirm_args(),
        "NASD",
        Decimal("0"),
        None,  # no acked BUY order id
        Decimal("1"),
        evidence,
        entry_fill=None,
    )
    assert rc == 2
    assert client.cancel_calls == []
    assert client.sell_calls and client.sell_calls[0]["quantity"] == 1
    assert evidence["open_order_check_status"] == "no_order_acked"
    assert evidence["cleanup"] == "flattened_entry_unconfirmed"


# --- BUY submit exception: explicit evidence + consistent non-zero exit ------
# (safety-review blocker 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_buy_submit_exception_is_consistent_nonzero(monkeypatch, caplog):
    client = _FakeOverseasClient(
        close=100.0, buy_exc=RuntimeError("APBK0918 buy rejected")
    )
    _confirm_env(monkeypatch, client)
    with caplog.at_level(logging.INFO, logger=smoke.__name__):
        rc = await smoke.run_confirm(_confirm_args(["--notional-usd", "100"]))
    assert rc != 0  # a BUY submit failure is never a clean success
    ev = _last_json_with(caplog, "exit_code")
    assert ev is not None
    assert ev["entry"] == "BUY_submit_rejected"
    assert "buy_submit_exception" in ev
    assert ev["entry_order_id"] is None
    assert ev["entry_fill_confirmed"] is False
    # evidence exit_code is consistent with the actual process exit code
    assert ev["exit_code"] == rc
