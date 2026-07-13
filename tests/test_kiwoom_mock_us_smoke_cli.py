from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from scripts import kiwoom_mock_us_smoke as smoke


def test_parser_defaults_are_non_mutating() -> None:
    args = smoke.build_parser().parse_args(["--mode", "preflight"])
    assert args.confirm is False
    assert args.probe_order_types is None
    assert args.confirm_probes is False


@pytest.mark.asyncio
async def test_preflight_reports_only_missing_key_names(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_us_config",
        lambda: ["KIWOOM_MOCK_US_APP_KEY"],
    )
    result = await smoke.run_preflight()
    assert result == {
        "step": "preflight",
        "ok": False,
        "missing_env_keys": ["KIWOOM_MOCK_US_APP_KEY"],
    }


@pytest.mark.asyncio
async def test_complete_preflight_calls_all_read_trs(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_open_orders(self) -> dict[str, Any]:
            calls.append("ust21050")
            return {"return_code": 0}

        async def get_positions(self) -> dict[str, Any]:
            calls.append("ust21070")
            return {"return_code": 0}

        async def get_today_orders(self) -> dict[str, Any]:
            calls.append("ust21510")
            return {"return_code": 0}

        async def get_foreign_deposit(self) -> dict[str, Any]:
            calls.append("ust21110")
            return {"return_code": 0}

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            calls.append("ust21160")
            return {"return_code": 0}

    monkeypatch.setattr(smoke, "validate_kiwoom_mock_us_config", lambda: [])
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
    result = await smoke.run_preflight()
    assert result["ok"] is True
    assert calls == ["ust21050", "ust21070", "ust21510", "ust21110", "ust21160"]


def test_parse_probe_codes_is_ordered_and_deduplicated() -> None:
    assert smoke.parse_probe_codes("26,27,26,30") == ("26", "27", "30")
    assert smoke.parse_probe_codes(None) == ()


@pytest.mark.asyncio
async def test_probe_requires_second_confirmation_before_client(monkeypatch) -> None:
    calls = {"client": 0}

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    args = Namespace(
        confirm_probes=False,
        confirm_existing_position=False,
        probe_order_types="26",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    with pytest.raises(smoke.SmokeRejected, match="confirm-probes"):
        await smoke.run_probe(args)
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_probe_cancels_every_accepted_order(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            del client

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            calls.append("place")
            return {"return_code": 0, "ord_no": "000000282"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            calls.append("cancel")
            return {"return_code": 0, "ord_no": "000000283"}

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    args = Namespace(
        confirm_probes=True,
        confirm_existing_position=False,
        probe_order_types="26",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    assert await smoke.run_probe(args) == 0
    assert calls == ["place", "cancel"]


@pytest.mark.asyncio
async def test_probe_cleanup_failure_returns_nonzero(monkeypatch) -> None:
    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            del client

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {"return_code": 0, "ord_no": "000000282"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {"return_code": 20, "return_msg": "cancel rejected"}

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    args = Namespace(
        confirm_probes=True,
        confirm_existing_position=False,
        probe_order_types="26",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    assert await smoke.run_probe(args) == 2


def test_extract_order_id_accepts_bounded_digits_only() -> None:
    assert smoke.extract_order_id({"ord_no": "000000282"}) == "000000282"
    assert smoke.extract_order_id({"ord_no": "282"}) == "282"
    assert smoke.extract_order_id({"ord_no": "../000000282"}) is None
    assert smoke.extract_order_id({"ord_no": "1" * 19}) is None
    assert smoke.extract_order_id({"ord_no": ""}) is None


def test_reconcile_compare_is_zero_padding_insensitive() -> None:
    payload = {"result_list": [{"ord_no": "000000000282"}]}
    assert smoke._payload_contains_order_id(payload, "000000282")
    assert not smoke._payload_contains_order_id(payload, "000000283")
    assert not smoke._payload_contains_order_id({"ord_no": "28a2"}, "282")


@pytest.mark.asyncio
async def test_full_is_limit_only_before_tool_calls(monkeypatch) -> None:
    calls = {"tools": 0}

    def tools() -> dict[str, Any]:
        calls["tools"] += 1
        return {}

    monkeypatch.setattr(smoke, "_tools", tools)
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--trde-tp",
            "03",
        ]
    )
    with pytest.raises(smoke.SmokeRejected, match="limit-only"):
        await smoke.run_full(args)
    assert calls["tools"] == 0


@pytest.mark.asyncio
async def test_preview_failure_returns_nonzero(monkeypatch) -> None:
    async def rejected_preview(args: Namespace) -> dict[str, Any]:
        del args
        return {"success": False, "error": "preview rejected"}

    monkeypatch.setattr(smoke, "run_preview", rejected_preview)
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "preview",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--price",
            "1.00",
        ]
    )

    assert await smoke._amain(args) == 2


@pytest.mark.asyncio
async def test_full_stops_after_dry_run_without_confirm(monkeypatch) -> None:
    calls: list[str] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("preview")
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        calls.append("place-live" if not kwargs["dry_run"] else "place-dry")
        return {"success": True}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
        },
    )
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--price",
            "1.00",
        ]
    )

    assert await smoke.run_full(args) == 0
    assert calls == ["preview", "place-dry"]


@pytest.mark.asyncio
async def test_full_always_cancels_accepted_order(monkeypatch) -> None:
    calls: list[str] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        calls.append("place-live" if not kwargs["dry_run"] else "place-dry")
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "ord_no": "000000282"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("history")
        return {"success": True, "result_list": []}

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("cancel")
        return {"success": True, "ord_no": "000000283"}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_cancel_order": cancel,
            "kiwoom_mock_us_get_positions": history,
        },
    )
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--price",
            "1.00",
            "--confirm",
        ]
    )
    assert await smoke.run_full(args) == 0
    assert calls.index("cancel") > calls.index("place-live")


def test_emit_does_not_add_sensitive_values(capsys) -> None:
    smoke._emit(
        {
            "step": "preflight",
            "missing_env_keys": ["KIWOOM_MOCK_US_APP_KEY"],
            "broker_response": {
                "acnt_no": "ACCOUNT-FIXTURE",
                "accountNumber": "ACCOUNT-CAMEL-FIXTURE",
                "nested": ({"token": "TOKEN-FIXTURE"},),
            },
        }
    )
    rendered = capsys.readouterr().out
    assert "KIWOOM_MOCK_US_APP_KEY" in rendered
    assert "TOKEN-FIXTURE" not in rendered
    assert "ACCOUNT-FIXTURE" not in rendered
    assert "ACCOUNT-CAMEL-FIXTURE" not in rendered
    assert rendered.count("[REDACTED]") == 3


@pytest.mark.asyncio
async def test_preflight_mode_rejects_probe_flags(monkeypatch) -> None:
    calls = {"preflight": 0}

    async def fake_preflight() -> dict[str, Any]:
        calls["preflight"] += 1
        return {"ok": True}

    monkeypatch.setattr(smoke, "run_preflight", fake_preflight)
    args = smoke.build_parser().parse_args(
        ["--mode", "preflight", "--probe-order-types", "26"]
    )
    with pytest.raises(smoke.SmokeRejected, match="--mode probe"):
        await smoke._amain(args)
    assert calls["preflight"] == 0


@pytest.mark.asyncio
async def test_probe_mode_requires_symbol_quantity_and_codes(monkeypatch) -> None:
    async def fake_preflight() -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(smoke, "run_preflight", fake_preflight)

    args = smoke.build_parser().parse_args(["--mode", "probe"])
    with pytest.raises(smoke.SmokeRejected, match="symbol"):
        await smoke._amain(args)

    args = smoke.build_parser().parse_args(
        ["--mode", "probe", "--symbol", "NVDA", "--quantity", "1"]
    )
    with pytest.raises(smoke.SmokeRejected, match="probe-order-types"):
        await smoke._amain(args)


@pytest.mark.asyncio
async def test_probe_mode_runs_preflight_before_probes(monkeypatch) -> None:
    order: list[str] = []

    async def fake_preflight() -> dict[str, Any]:
        order.append("preflight")
        return {"ok": False}

    async def fake_probe(args) -> int:
        del args
        order.append("probe")
        return 0

    monkeypatch.setattr(smoke, "run_preflight", fake_preflight)
    monkeypatch.setattr(smoke, "run_probe", fake_probe)
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "probe",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--probe-order-types",
            "26",
            "--confirm-probes",
        ]
    )
    assert await smoke._amain(args) == 2
    assert order == ["preflight"]
