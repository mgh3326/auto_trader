import json
from pathlib import Path

from app.services.brokers.token_keys import (
    kis_live_token_keys,
    kis_mock_token_keys,
    kis_websocket_approval_keys,
    toss_oauth_token_keys,
)


def test_broker_token_key_names_match_golden_fixture() -> None:
    fixture_path = Path(__file__).parents[2] / "fixtures" / "broker_token_keys.json"
    fixture = json.loads(fixture_path.read_text())
    inputs = fixture["inputs"]

    kis_live_access_token, kis_live_lock = kis_live_token_keys()
    kis_mock_access_token, kis_mock_lock = kis_mock_token_keys(
        netloc=inputs["kis_mock_netloc"],
        app_key=inputs["kis_mock_app_key"],
    )
    toss_access_token, toss_lock = toss_oauth_token_keys(
        client_id=inputs["toss_client_id"]
    )
    kis_websocket_live_key, kis_websocket_live_lock = kis_websocket_approval_keys(
        account_mode="kis_live"
    )
    kis_websocket_mock_key, kis_websocket_mock_lock = kis_websocket_approval_keys(
        account_mode="kis_mock"
    )

    assert {
        "kis_live_access_token": kis_live_access_token,
        "kis_live_lock": kis_live_lock,
        "kis_mock_access_token": kis_mock_access_token,
        "kis_mock_lock": kis_mock_lock,
        "toss_access_token": toss_access_token,
        "toss_lock": toss_lock,
        "kis_websocket_live_approval_key": kis_websocket_live_key,
        "kis_websocket_live_lock": kis_websocket_live_lock,
        "kis_websocket_mock_approval_key": kis_websocket_mock_key,
        "kis_websocket_mock_lock": kis_websocket_mock_lock,
    } == fixture["expected"]
