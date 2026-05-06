from datetime import datetime

import pytest

from app.schemas.execution_contracts import OrderLifecycleEvent
from app.services.kis_websocket_internal.events import build_lifecycle_event


@pytest.mark.unit
class TestBuildLifecycleEvent:
    def test_domestic_full_fill_maps_to_fill_state(self):
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 70000.0,
            "filled_qty": 10.0,
            "filled_amount": 700000.0,
            "filled_at": "2026-05-04T10:00:00",
            "fill_yn": "2",
            "received_at": "2026-05-04T10:00:01",
            "correlation_id": "corr-1",
            "broker": "kis",
            "account_mode": "kis_mock",
            "execution_source": "websocket",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert isinstance(event, OrderLifecycleEvent)
        assert event.account_mode == "kis_mock"
        assert event.execution_source == "websocket"
        assert event.state == "fill"
        assert event.broker_order_id == "0000123456"
        assert event.correlation_id == "corr-1"
        assert isinstance(event.occurred_at, datetime)
        # raw KIS fields belong in detail
        assert event.detail["tr_code"] == "H0STCNI9"
        assert event.detail["market"] == "kr"
        assert event.detail["symbol"] == "005930"
        assert event.detail["side"] == "bid"
        assert event.detail["filled_qty"] == pytest.approx(10.0)
        assert event.detail["filled_price"] == pytest.approx(70000.0)
        assert event.detail["fill_yn"] == "2"

    def test_domestic_acknowledgement_maps_to_pending(self):
        parsed = {
            "tr_code": "H0STCNI0",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 0.0,
            "filled_qty": 0.0,
            "filled_amount": 0.0,
            "filled_at": "2026-05-04T10:00:00",
            "fill_yn": "1",
            "correlation_id": "corr-2",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_live")

        assert event.state == "pending"
        assert event.account_mode == "kis_live"

    def test_overseas_filled_status(self):
        parsed = {
            "tr_code": "H0GSCNI0",
            "market": "us",
            "symbol": "AAPL",
            "side": "bid",
            "order_id": "ORD-1",
            "filled_price": 200.0,
            "filled_qty": 5.0,
            "filled_amount": 1000.0,
            "filled_at": "2026-05-04T14:30:00",
            "execution_status": "filled",
            "cntg_yn": "Y",
            "rfus_yn": "N",
            "acpt_yn": "Y",
            "rctf_cls": "0",
            "currency": "USD",
            "correlation_id": "corr-3",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_live")

        assert event.state == "fill"
        assert event.detail["execution_status"] == "filled"
        assert event.detail["currency"] == "USD"

    def test_unknown_state_falls_back_to_anomaly_with_warning(self):
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "unknown",
            "order_id": None,
            "fill_yn": "Z",  # not in known mapping
            "correlation_id": "corr-4",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert event.state == "anomaly"
        assert any("unknown" in w.lower() for w in event.warnings)

    def test_account_mode_arg_overrides_dict_value(self):
        # Caller's account_mode argument is authoritative; dict value is ignored.
        # Rationale: the client/runtime knows its own mode; trusting an
        # in-band field would let a malformed message lie about it.
        parsed = {
            "tr_code": "H0STCNI9",
            "market": "kr",
            "symbol": "005930",
            "side": "bid",
            "order_id": "0000123456",
            "filled_price": 70000.0,
            "filled_qty": 10.0,
            "fill_yn": "2",
            "account_mode": "kis_live",  # WRONG — caller passes kis_mock
            "correlation_id": "corr-5",
        }

        event = build_lifecycle_event(parsed, account_mode="kis_mock")

        assert event.account_mode == "kis_mock"
