import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.unit
def test_order_proposals_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.ORDER_PROPOSALS_ENABLED is False
    assert s.ORDER_PROPOSALS_SUBMIT_AGENT_ID == ""


@pytest.mark.unit
def test_telegram_flags_default_off_and_allowlist_parses():
    s = Settings(_env_file=None)
    assert s.ORDER_PROPOSALS_TELEGRAM_ENABLED is False
    assert s.ORDER_PROPOSALS_TELEGRAM_TOKEN == ""
    assert s.ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER == "X-Telegram-Bot-Api-Secret-Token"
    assert s.order_proposals_telegram_chat_allowlist == []
    s2 = Settings(
        _env_file=None, ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR="111, 222"
    )
    assert s2.order_proposals_telegram_chat_allowlist == ["111", "222"]


@pytest.mark.unit
def test_durable_callback_enqueue_timeout_is_finite_positive_and_capped() -> None:
    """R33: an ACK deadline must never become zero, infinite, or unbounded."""
    baseline = Settings(_env_file=None)
    assert baseline.ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS == 2.0

    for accepted in (10.0, 0.000_001):
        configured = Settings(
            _env_file=None,
            ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS=accepted,
        )
        assert (
            configured.ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS
            == accepted
        )

    for invalid in (
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -0.01,
        10.01,
    ):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS=invalid,
            )
