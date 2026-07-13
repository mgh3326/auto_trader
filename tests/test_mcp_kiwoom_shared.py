from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success,
    finalize_broker_response,
)


def test_success_requires_explicit_zero_return_code() -> None:
    assert derive_broker_success({"return_code": 0}) is True
    assert derive_broker_success({"return_code": "0"}) is True
    assert derive_broker_success({}) is False
    assert derive_broker_success({"return_code": None}) is False
    assert derive_broker_success({"return_code": 20}) is False


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
