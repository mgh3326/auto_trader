import pytest

from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success,
    finalize_broker_response,
    finalize_place_broker_response,
)


def test_success_requires_explicit_zero_return_code() -> None:
    assert derive_broker_success({"return_code": 0}) is True
    assert derive_broker_success({"return_code": "0"}) is True
    assert derive_broker_success({}) is False
    assert derive_broker_success({"return_code": None}) is False
    assert derive_broker_success({"return_code": 20}) is False


@pytest.mark.parametrize(
    "return_code",
    [False, 0.0, " 0 ", "", "zero", [], {}],
)
def test_success_rejects_non_contract_return_codes(return_code: object) -> None:
    assert derive_broker_success({"return_code": return_code}) is False


def test_rc9000_is_classified_without_losing_raw_evidence() -> None:
    raw = {
        "return_code": 20,
        "return_msg": "[2000](RC9000:모의투자에서는 해당업무가 제공되지 않습니다.)",
    }
    result = finalize_broker_response({"source": "kiwoom"}, raw)
    assert result["success"] is False
    assert result["error_code"] == "capability_unsupported"
    assert result["broker_response"] == raw
    assert result["return_msg"] == raw["return_msg"]


def test_caller_supplied_success_cannot_override_broker_derivation() -> None:
    from app.mcp_server.tooling.orders_kiwoom_shared import finalize_broker_response

    response = finalize_broker_response(
        {"success": True, "source": "kiwoom"},
        {"return_code": 20, "return_msg": "rejected"},
    )
    assert response["success"] is False


@pytest.mark.parametrize(
    "broker_response",
    [
        {},
        {"return_code": False},
        {"return_code": 0.0},
        {"return_code": " 0 "},
        {"return_code": "zero"},
        {"return_code": "9" * 5000},
    ],
)
def test_place_malformed_broker_result_is_uncertain_and_not_retryable(
    broker_response: dict[str, object],
) -> None:
    response = finalize_place_broker_response({"source": "kiwoom"}, broker_response)

    assert response["success"] is False
    assert response["status"] == "acceptance_uncertain"
    assert response["reconcile_required"] is True
    assert response["retry_allowed"] is False


@pytest.mark.parametrize("return_code", [20, -1, "20", "-1"])
def test_place_explicit_nonzero_broker_code_is_rejected(
    return_code: int | str,
) -> None:
    response = finalize_place_broker_response(
        {"source": "kiwoom"},
        {"return_code": return_code, "return_msg": "rejected"},
    )

    assert response["success"] is False
    assert response["status"] == "rejected"
    assert response["reconcile_required"] is False
