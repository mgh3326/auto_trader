import pytest
from app.services import kis_websocket as mod
from app.services.kis_websocket import KISSubscriptionAckError, KISExecutionWebSocket

def test_protocol_constants():
    """Pin the existence and values of protocol constants"""
    assert mod.DOMESTIC_EXECUTION_TR_REAL == "H0STCNI0"
    assert mod.OVERSEAS_EXECUTION_TR_REAL == "H0GSCNI0"
    assert mod.DOMESTIC_EXECUTION_TR_MOCK == "H0STCNI9"
    assert mod.OVERSEAS_EXECUTION_TR_MOCK == "H0GSCNI9"
    
    assert "H0STCNI0" in mod.DOMESTIC_EXECUTION_TR_CODES
    assert "H0GSCNI0" in mod.OVERSEAS_EXECUTION_TR_CODES
    
    assert mod.RECOVERABLE_APPROVAL_MSG_CODES == {"OPSP0011", "OPSP8996"}

def test_kis_subscription_ack_error_properties():
    """Pin KISSubscriptionAckError properties"""
    error = KISSubscriptionAckError(tr_id="TR1", rt_cd="1", msg_cd="MSG1", msg1="Failure")
    assert error.tr_id == "TR1"
    assert error.rt_cd == "1"
    assert error.msg_cd == "MSG1"
    assert error.msg1 == "Failure"
    assert "TR1" in str(error)
    assert "MSG1" in str(error)

def test_validate_subscription_ack_success_stores_keys():
    """Pin _validate_subscription_ack stores encryption keys on success"""
    client = KISExecutionWebSocket(on_execution=lambda x: x)
    parsed = {
        "header": {"tr_id": "H0STCNI0"},
        "body": {
            "rt_cd": "0",
            "msg_cd": "OPSP0000",
            "msg1": "OK",
            "output": {"key": "test-key", "iv": "test-iv"}
        }
    }
    
    client._validate_subscription_ack(parsed, expected_tr_id="H0STCNI0")
    
    assert client._encryption_keys_by_tr["H0STCNI0"] == ("test-key", "test-iv")

def test_validate_subscription_ack_failure_raises_custom_error():
    """Pin _validate_subscription_ack raises KISSubscriptionAckError on failure"""
    client = KISExecutionWebSocket(on_execution=lambda x: x)
    parsed = {
        "header": {"tr_id": "H0STCNI0"},
        "body": {
            "rt_cd": "1",
            "msg_cd": "OPSP0011",
            "msg1": "Invalid key"
        }
    }
    
    with pytest.raises(KISSubscriptionAckError) as excinfo:
        client._validate_subscription_ack(parsed, expected_tr_id="H0STCNI0")
    
    assert excinfo.value.rt_cd == "1"
    assert excinfo.value.msg_cd == "OPSP0011"
    assert excinfo.value.tr_id == "H0STCNI0"
