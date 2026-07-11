import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_order_proposals_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.ORDER_PROPOSALS_ENABLED is False


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
