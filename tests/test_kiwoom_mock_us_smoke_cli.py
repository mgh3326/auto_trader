from __future__ import annotations

import json
from argparse import Namespace
from decimal import Decimal
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


@pytest.mark.asyncio
async def test_preflight_exception_does_not_expose_provider_text(monkeypatch) -> None:
    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_open_orders(self) -> dict[str, Any]:
            raise TimeoutError("provider-secret-must-not-leak")

        async def get_positions(self) -> dict[str, Any]:
            return {"return_code": 0}

        async def get_today_orders(self) -> dict[str, Any]:
            return {"return_code": 0}

        async def get_foreign_deposit(self) -> dict[str, Any]:
            return {"return_code": 0}

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            return {"return_code": 0}

    monkeypatch.setattr(smoke, "validate_kiwoom_mock_us_config", lambda: [])
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)

    result = await smoke.run_preflight()

    assert result["ok"] is False
    assert result["error"] == "TimeoutError"
    assert "provider-secret-must-not-leak" not in str(result)


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

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            if "cancel" in calls:
                return _page([])
            return _page(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            if "cancel" not in calls:
                return _page([])
            return _page(
                [
                    {
                        "ord_no": "000000283",
                        "orig_ord_no": "000000282",
                        "ord_cntr_tp": "12",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
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
    cancel_attempted = False
    place_calls = 0

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
            nonlocal place_calls
            del kwargs
            place_calls += 1
            return {"return_code": 0, "ord_no": "000000282"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal cancel_attempted
            del kwargs
            cancel_attempted = True
            return {"return_code": 20, "return_msg": "cancel rejected"}

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            if cancel_attempted:
                return _page([])
            return _page(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            if not cancel_attempted:
                return _page([])
            return _page(
                [
                    {
                        "ord_no": "000000283",
                        "orig_ord_no": "000000282",
                        "ord_cntr_tp": "12",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
    args = Namespace(
        confirm_probes=True,
        confirm_existing_position=False,
        probe_order_types="26,27",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    assert await smoke.run_probe(args) == 2
    assert place_calls == 1


@pytest.mark.asyncio
async def test_probe_read_exception_returns_structured_cleanup_required(
    monkeypatch, capsys
) -> None:
    cancel_attempted = False

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
            nonlocal cancel_attempted
            del kwargs
            cancel_attempted = True
            return {"return_code": 0, "ord_no": "000000283"}

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise TimeoutError("provider-secret-must-not-leak")

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
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
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert cancel_attempted is True
    assert any(event["step"] == "cleanup_required" for event in events)
    assert any(event["step"] == "probe_final_reconciliation" for event in events)
    assert "provider-secret-must-not-leak" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [("exchange", "RuntimeError"), ("client", "ConnectionError")],
)
async def test_probe_setup_exception_is_redacted_and_stops_before_mutation(
    monkeypatch, capsys, failure: str, expected_error: str
) -> None:
    mutation_clients = 0

    async def fake_lookup(symbol: str) -> str:
        del symbol
        if failure == "exchange":
            raise RuntimeError("postgresql://user:secret@provider/db")
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            if failure == "client":
                raise ConnectionError("credential=provider-secret-must-not-leak")
            return cls()

    class GuardMutationClient:
        def __init__(self, client: Any) -> None:
            nonlocal mutation_clients
            del client
            mutation_clients += 1

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", GuardMutationClient)
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
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "step": "probe_setup_failed",
        "error": expected_error,
    }
    assert "secret" not in output
    assert "credential" not in output
    assert mutation_clients == 0


@pytest.mark.asyncio
async def test_probe_baseline_failure_stops_before_all_mutations(
    monkeypatch, capsys
) -> None:
    place_calls = 0
    position_calls = 0

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
            nonlocal place_calls
            del kwargs
            place_calls += 1
            return {"return_code": 20, "return_msg": "rejected"}

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal position_calls
            del kwargs
            position_calls += 1
            if position_calls == 1:
                return {
                    "return_code": 20,
                    "result_list": [],
                    "continuation": {"cont_yn": "N", "next_key": ""},
                }
            return _page([])

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
    args = Namespace(
        confirm_probes=True,
        confirm_existing_position=False,
        probe_order_types="26,27",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )

    assert await smoke.run_probe(args) == 2
    assert place_calls == 0
    assert position_calls == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["step"] == "probe_order_type" for event in events)


@pytest.mark.asyncio
async def test_probe_uncertain_acceptance_returns_nonzero(monkeypatch) -> None:
    place_calls = 0

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
            nonlocal place_calls
            del kwargs
            place_calls += 1
            return {"return_code": False}

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
    args = Namespace(
        confirm_probes=True,
        confirm_existing_position=False,
        probe_order_types="26,27",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )

    assert await smoke.run_probe(args) == 2
    assert place_calls == 1


@pytest.mark.asyncio
async def test_probe_place_exception_returns_nonzero(monkeypatch, capsys) -> None:
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
            raise TimeoutError("provider-secret-must-not-leak")

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
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
    output = capsys.readouterr().out
    assert "TimeoutError" in output
    assert "provider-secret-must-not-leak" not in output


def test_extract_order_id_accepts_bounded_digits_only() -> None:
    assert smoke.extract_order_id({"ord_no": "000000282"}) == "000000282"
    assert smoke.extract_order_id({"ord_no": "282"}) == "282"
    assert smoke.extract_order_id({"ord_no": "../000000282"}) is None
    assert smoke.extract_order_id({"ord_no": "١٢٣٤٥٦٧٨٩"}) is None
    assert smoke.extract_order_id({"ord_no": 282}) is None
    assert smoke.extract_order_id({"ord_no": "1" * 19}) is None
    assert smoke.extract_order_id({"ord_no": ""}) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"return_code": 0, "ord_no": "000000284"}, "000000284"),
        (
            {
                "order_id": "000000282",
                "broker_response": {"return_code": 0, "ord_no": "000000284"},
            },
            "000000284",
        ),
        ({"return_code": 0}, None),
        ({"return_code": 0, "ord_no": "not-an-id"}, None),
        (
            {
                "return_code": 0,
                "ord_no": "000000284",
                "order_no": "000000285",
            },
            None,
        ),
    ],
)
def test_modify_order_id_must_be_single_unambiguous_broker_evidence(
    payload: dict[str, Any], expected: str | None
) -> None:
    assert smoke._extract_unique_mutation_order_id(payload) == expected


def test_reconcile_compare_preserves_leading_zeroes() -> None:
    payload = {"result_list": [{"ord_no": "000000000282"}]}
    assert not smoke._payload_contains_order_id(payload, "000000282")
    assert smoke._payload_contains_order_id(payload, "000000000282")
    assert not smoke._payload_contains_order_id(payload, "000000283")
    assert not smoke._payload_contains_order_id({"ord_no": "28a2"}, "282")


def test_reconcile_ignores_unrelated_numeric_values() -> None:
    payload = {
        "result_list": [
            {"ord_qty": "000000282", "ord_uv": "282", "ord_no": "000000999"}
        ]
    }
    assert not smoke._payload_contains_order_id(payload, "000000282")


def test_target_classifier_ignores_nested_unrelated_order_id() -> None:
    target = "000000282"
    unrelated_cancel = {
        "ord_no": "000000999",
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_cntr_tp": "12",
        "metadata": {"ord_no": target},
    }

    assert smoke._classify_target([], [unrelated_cancel], target) == "unknown"


def test_accepted_untracked_wrapper_is_not_cli_success() -> None:
    assert not smoke._response_succeeded({"success": True, "ord_no": "000000282"})
    assert not smoke._response_succeeded(
        {
            "success": False,
            "status": "accepted_untracked",
            "reconcile_required": True,
            "broker_response": {"return_code": 0},
        }
    )


def test_target_classifier_covers_documented_lifecycle_states() -> None:
    target = "000000282"

    def row(**values: str) -> dict[str, str]:
        return {
            "ord_no": target,
            "ord_qty": "2",
            "cntr_qty": "0",
            "ord_remnq": "2",
            **values,
        }

    assert smoke._classify_target([row()], [], target) == "open"
    assert (
        smoke._classify_target([row(cntr_qty="1", ord_remnq="1")], [], target)
        == "partial"
    )
    assert (
        smoke._classify_target([], [row(cntr_qty="2", ord_remnq="0")], target)
        == "filled"
    )
    assert (
        smoke._classify_target([], [row(ord_cntr_tp="12", ord_remnq="1")], target)
        == "cancel_pending"
    )
    assert (
        smoke._classify_target([], [row(ord_cntr_tp="12", ord_remnq="0")], target)
        == "cancelled"
    )
    assert (
        smoke._classify_target([], [row(ord_stat="rejected", ord_remnq="0")], target)
        == "rejected"
    )
    assert smoke._classify_target([], [], target) == "unknown"


def test_target_classifier_fails_closed_on_open_and_terminal_conflict() -> None:
    target = "000000282"
    open_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "2",
    }
    cancelled_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_cntr_tp": "12",
    }

    assert smoke._classify_target([open_row], [cancelled_row], target) == "unknown"


def test_target_classifier_fails_closed_on_malformed_terminal_evidence() -> None:
    target = "000000282"
    malformed_row = {
        "ord_no": target,
        "ord_remnq": "0",
        "ord_cntr_tp": "12",
    }
    cancelled_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_cntr_tp": "12",
    }

    assert (
        smoke._classify_target([], [malformed_row, cancelled_row], target) == "unknown"
    )


def test_target_classifier_rejects_rejected_status_with_remaining_quantity() -> None:
    target = "000000282"
    contradictory_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "1",
        "ord_stat": "rejected",
    }

    assert smoke._classify_target([], [contradictory_row], target) == "unknown"


def test_target_classifier_fails_closed_on_conflicting_terminal_states() -> None:
    target = "000000282"
    cancelled_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_cntr_tp": "12",
    }
    rejected_row = {
        "ord_no": target,
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_stat": "rejected",
    }

    assert (
        smoke._classify_target([], [cancelled_row, rejected_row], target) == "unknown"
    )


def _page(
    rows: list[dict[str, Any]], *, cont_yn: str = "N", next_key: str = ""
) -> dict[str, Any]:
    return {
        "return_code": 0,
        "result_list": rows,
        "continuation": {"cont_yn": cont_yn, "next_key": next_key},
    }


@pytest.mark.asyncio
async def test_target_order_on_continuation_page_two_is_found() -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def reader(*, cont_yn=None, next_key=None):
        calls.append((cont_yn, next_key))
        if next_key is None:
            return _page([], cont_yn="Y", next_key="page-2")
        return _page([{"ord_no": "000000282"}])

    rows = await smoke._collect_pages(reader, page_cap=3)
    assert smoke._payload_contains_order_id({"result_list": rows}, "000000282")
    assert calls == [(None, None), ("Y", "page-2")]


@pytest.mark.asyncio
async def test_repeated_continuation_token_fails_closed() -> None:
    async def reader(*, cont_yn=None, next_key=None):
        del cont_yn, next_key
        return _page([], cont_yn="Y", next_key="repeat")

    with pytest.raises(smoke.SmokeRejected, match="repeated continuation"):
        await smoke._collect_pages(reader, page_cap=3)


@pytest.mark.asyncio
async def test_malformed_continuation_fails_closed() -> None:
    async def reader(*, cont_yn=None, next_key=None):
        del cont_yn, next_key
        return {"return_code": 0, "result_list": [], "continuation": "Y"}

    with pytest.raises(smoke.SmokeRejected, match="malformed continuation"):
        await smoke._collect_pages(reader)


@pytest.mark.asyncio
async def test_continuation_page_cap_fails_closed() -> None:
    calls = 0

    async def reader(*, cont_yn=None, next_key=None):
        nonlocal calls
        del cont_yn, next_key
        calls += 1
        return _page([], cont_yn="Y", next_key=f"page-{calls + 1}")

    with pytest.raises(smoke.SmokeRejected, match="page cap"):
        await smoke._collect_pages(reader, page_cap=2)


@pytest.mark.asyncio
async def test_partial_fill_and_position_delta_fail_cleanup() -> None:
    async def history(*, scope, cont_yn=None, next_key=None):
        del cont_yn, next_key
        if scope == "open":
            return _page(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "2",
                        "cntr_qty": "1",
                        "ord_remnq": "1",
                    }
                ]
            )
        return _page(
            [
                {
                    "ord_no": "000000282",
                    "ord_qty": "2",
                    "cntr_qty": "1",
                    "ord_remnq": "1",
                }
            ]
        )

    async def positions(*, cont_yn=None, next_key=None):
        del cont_yn, next_key
        return _page([{"stk_cd": "NVDA", "poss_qty": "1"}])

    proof = await smoke._prove_cleanup(
        history,
        positions,
        symbol="NVDA",
        order_id="000000282",
        baseline={"NVDA": Decimal("0")},
    )
    assert proof.ok is False
    assert proof.state == "partial"
    assert proof.position_delta


@pytest.mark.asyncio
async def test_cancel_pending_timeout_uses_injected_sleep() -> None:
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    async def history(*, scope, cont_yn=None, next_key=None):
        del cont_yn, next_key
        if scope == "open":
            return _page(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )
        return _page(
            [
                {
                    "ord_no": "000000283",
                    "orig_ord_no": "000000282",
                    "ord_cntr_tp": "12",
                    "ord_qty": "1",
                    "cntr_qty": "0",
                    "ord_remnq": "1",
                }
            ]
        )

    async def positions(*, cont_yn=None, next_key=None):
        del cont_yn, next_key
        return _page([])

    proof = await smoke._prove_cleanup(
        history,
        positions,
        symbol="NVDA",
        order_id="000000282",
        baseline={},
        clock=clock,
        sleep=sleep,
        timeout=2.0,
        poll_interval=1.0,
    )
    assert proof.ok is False
    assert proof.state == "cancel_pending"
    assert proof.reason == "cleanup reconciliation timed out"
    assert sleeps == [1.0, 1.0]


@pytest.mark.asyncio
async def test_cleanup_proof_requires_all_related_order_ids_terminal() -> None:
    original_order_id = "000000111"
    replacement_order_id = "000000222"

    async def history_reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs["scope"] == "open":
            return _page([])
        return _page(
            [
                {
                    "ord_no": "000000333",
                    "orig_ord_no": original_order_id,
                    "ord_cntr_tp": "12",
                    "cntr_qty": "0",
                    "ord_remnq": "0",
                },
                {
                    "ord_no": "000000444",
                    "orig_ord_no": replacement_order_id,
                    "ord_cntr_tp": "12",
                    "cntr_qty": "0",
                    "ord_remnq": "0",
                },
            ]
        )

    async def positions_reader(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _page([])

    proof = await smoke._prove_cleanup(
        history_reader,
        positions_reader,
        symbol="NVDA",
        order_id=original_order_id,
        related_order_ids=(replacement_order_id,),
        baseline={},
        timeout=0,
        poll_interval=0,
    )

    assert proof.ok is True
    assert proof.order_states == {
        original_order_id: "cancelled",
        replacement_order_id: "cancelled",
    }


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
async def test_full_empty_post_place_history_fails_closed(monkeypatch) -> None:
    calls: list[str] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        calls.append("place-live" if not kwargs["dry_run"] else "place-dry")
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "return_code": 0, "ord_no": "000000282"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("history")
        return {
            "success": True,
            "return_code": 0,
            "broker_response": _page([]),
        }

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("cancel")
        return {"success": True, "return_code": 0, "ord_no": "000000283"}

    async def modify(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("modify")
        return {"success": True, "return_code": 0, "ord_no": "000000284"}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_cancel_order": cancel,
            "kiwoom_mock_us_modify_order": modify,
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
            "--new-price",
            "0.90",
        ]
    )
    assert await smoke.run_full(args) == 2
    assert calls.index("cancel") > calls.index("place-live")
    assert "modify" not in calls


@pytest.mark.asyncio
async def test_full_cancel_pending_state_skips_modify(monkeypatch) -> None:
    cancelled = False
    calls: list[str] = []

    def wrapped(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "success": True,
            "return_code": 0,
            "broker_response": _page(rows),
        }

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "return_code": 0, "ord_no": "000000282"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        if kwargs["scope"] == "open":
            return wrapped([])
        if cancelled:
            return wrapped(
                [
                    {
                        "ord_no": "000000283",
                        "orig_ord_no": "000000282",
                        "ord_cntr_tp": "12",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )
        return wrapped(
            [
                {
                    "ord_no": "000000283",
                    "orig_ord_no": "000000282",
                    "ord_cntr_tp": "12",
                    "cntr_qty": "0",
                    "ord_remnq": "1",
                }
            ]
        )

    async def positions(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return wrapped([])

    async def modify(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append("modify")
        return {"success": True, "return_code": 0, "ord_no": "000000284"}

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        nonlocal cancelled
        del kwargs
        cancelled = True
        calls.append("cancel")
        return {"success": True, "return_code": 0, "ord_no": "000000283"}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_get_positions": positions,
            "kiwoom_mock_us_modify_order": modify,
            "kiwoom_mock_us_cancel_order": cancel,
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
            "--new-price",
            "0.90",
            "--confirm",
        ]
    )

    assert await smoke.run_full(args) == 0
    assert calls == ["cancel"]


@pytest.mark.asyncio
async def test_full_modify_proves_original_and_replacement_terminality(
    monkeypatch, capsys
) -> None:
    original_order_id = "000000111"
    replacement_order_id = "000000222"
    cancelled_ids: list[str] = []

    def wrapped(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "success": True,
            "return_code": 0,
            "broker_response": _page(rows),
        }

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "return_code": 0, "ord_no": original_order_id}

    async def modify(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["order_id"] == original_order_id
        return {
            "success": True,
            "return_code": 0,
            "ord_no": replacement_order_id,
        }

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        cancelled_ids.append(kwargs["order_id"])
        return {"success": True, "return_code": 0, "ord_no": "000000333"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        if kwargs["scope"] == "open":
            return wrapped(
                [
                    {
                        "ord_no": original_order_id,
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )
        if replacement_order_id in cancelled_ids:
            return wrapped(
                [
                    {
                        "ord_no": "000000333",
                        "orig_ord_no": replacement_order_id,
                        "ord_cntr_tp": "12",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )
        return wrapped([])

    async def positions(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return wrapped([])

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_get_positions": positions,
            "kiwoom_mock_us_modify_order": modify,
            "kiwoom_mock_us_cancel_order": cancel,
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
            "--new-price",
            "0.90",
            "--confirm",
        ]
    )

    assert await smoke.run_full(args, cleanup_timeout=0, poll_interval=0) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    reconciliation = next(
        event for event in events if event["step"] == "final_reconciliation"
    )
    assert cancelled_ids == [replacement_order_id]
    assert reconciliation["order_states"] == {
        original_order_id: "open",
        replacement_order_id: "cancelled",
    }
    assert any(
        event["step"] == "cleanup_required"
        and original_order_id in event.get("unresolved_order_ids", [])
        for event in events
    )


@pytest.mark.asyncio
async def test_full_uncertain_modify_requires_unknown_replacement_reconciliation(
    monkeypatch, capsys
) -> None:
    original_order_id = "000000111"
    cancelled = False

    def wrapped(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "success": True,
            "return_code": 0,
            "broker_response": _page(rows),
        }

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "return_code": 0, "ord_no": original_order_id}

    async def modify(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["order_id"] == original_order_id
        return {
            "success": False,
            "status": "acceptance_uncertain",
            "reconcile_required": True,
            "retry_allowed": False,
            "error": "TimeoutError",
        }

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        nonlocal cancelled
        assert kwargs["order_id"] == original_order_id
        cancelled = True
        return {"success": True, "return_code": 0, "ord_no": "000000333"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        if kwargs["scope"] == "open":
            if cancelled:
                return wrapped([])
            return wrapped(
                [
                    {
                        "ord_no": original_order_id,
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )
        if cancelled:
            return wrapped(
                [
                    {
                        "ord_no": "000000333",
                        "orig_ord_no": original_order_id,
                        "ord_cntr_tp": "12",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )
        return wrapped([])

    async def positions(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return wrapped([])

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_get_positions": positions,
            "kiwoom_mock_us_modify_order": modify,
            "kiwoom_mock_us_cancel_order": cancel,
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
            "--new-price",
            "0.90",
            "--confirm",
        ]
    )

    assert await smoke.run_full(args, cleanup_timeout=0, poll_interval=0) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    reconciliation = next(
        event for event in events if event["step"] == "final_reconciliation"
    )
    assert reconciliation["ok"] is False
    assert reconciliation["lineage_complete"] is False
    assert any(
        event["step"] == "cleanup_required"
        and "unknown replacement" in event.get("reason", "")
        for event in events
    )
    assert cancelled is True


@pytest.mark.asyncio
async def test_full_returns_zero_only_after_terminal_cleanup_and_no_position_delta(
    monkeypatch,
) -> None:
    cancelled = False

    def wrapped(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "success": True,
            "return_code": 0,
            "broker_response": _page(rows),
        }

    async def preview(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "return_code": 0, "ord_no": "000000282"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        scope = kwargs["scope"]
        if scope == "open" and not cancelled:
            return wrapped(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )
        if scope == "today" and cancelled:
            return wrapped(
                [
                    {
                        "ord_no": "000000283",
                        "orig_ord_no": "000000282",
                        "ord_cntr_tp": "12",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ]
            )
        return wrapped([])

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        nonlocal cancelled
        del kwargs
        cancelled = True
        return {"success": True, "return_code": 0, "ord_no": "000000283"}

    async def positions(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return wrapped([])

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_cancel_order": cancel,
            "kiwoom_mock_us_get_positions": positions,
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
    assert cancelled is True


@pytest.mark.asyncio
async def test_probe_cancel_success_but_final_order_open_fails(monkeypatch) -> None:
    now = 0.0

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

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
            return {"return_code": 0, "ord_no": "000000283"}

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_positions(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page(
                [
                    {
                        "ord_no": "000000282",
                        "ord_qty": "1",
                        "cntr_qty": "0",
                        "ord_remnq": "1",
                    }
                ]
            )

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return _page([])

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
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
    assert (
        await smoke.run_probe(
            args,
            clock=clock,
            sleep=sleep,
            cleanup_timeout=2.0,
            poll_interval=1.0,
        )
        == 2
    )


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


def test_main_normalizes_smoke_rejection_to_structured_exit_two(
    monkeypatch, capsys
) -> None:
    async def reject(args: Any) -> int:
        del args
        raise smoke.SmokeRejected("full mode is limit-only")

    monkeypatch.setattr(smoke, "_amain", reject)
    monkeypatch.setattr("sys.argv", ["kiwoom_mock_us_smoke", "--mode", "preflight"])

    with pytest.raises(SystemExit) as exc_info:
        smoke.main()

    assert exc_info.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "step": "rejected",
        "error_code": "smoke_rejected",
        "reason": "full mode is limit-only",
    }


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
