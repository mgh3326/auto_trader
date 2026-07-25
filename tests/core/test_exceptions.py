import httpx

from app.core.exceptions import describe_exception


def test_empty_message_falls_back_to_class_name():
    assert describe_exception(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe_exception(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_whitespace_only_message_falls_back_to_class_name():
    assert describe_exception(ValueError("   ")) == "ValueError"


def test_nonempty_message_is_preserved():
    assert describe_exception(RuntimeError("EGW00201 초당 거래건수 초과")) == (
        "EGW00201 초당 거래건수 초과"
    )
    assert describe_exception(httpx.ReadTimeout("Read timed out")) == "Read timed out"
