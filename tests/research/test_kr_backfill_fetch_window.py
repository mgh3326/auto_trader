from datetime import datetime

import pytest

from research.kr_backfill.sources import (
    KST,
    FetchWindowClosed,
    assert_fetch_window_open,
)


@pytest.mark.parametrize(
    ("hour", "minute", "approved", "blocked"),
    [
        pytest.param(11, 22, None, True, id="daytime-unset-blocked"),
        pytest.param(11, 22, "true", False, id="daytime-true-open"),
        pytest.param(20, 0, None, False, id="outside-unset-open"),
        pytest.param(20, 0, "true", False, id="outside-true-open"),
    ],
)
def test_fetch_window_daytime_override_matrix(
    monkeypatch: pytest.MonkeyPatch,
    hour: int,
    minute: int,
    approved: str | None,
    blocked: bool,
) -> None:
    if approved is None:
        monkeypatch.delenv("BACKFILL_DAYTIME_APPROVED", raising=False)
    else:
        monkeypatch.setenv("BACKFILL_DAYTIME_APPROVED", approved)

    now = datetime(2026, 8, 4, hour, minute, tzinfo=KST)
    if blocked:
        with pytest.raises(FetchWindowClosed):
            assert_fetch_window_open(override_now=now)
    else:
        assert_fetch_window_open(override_now=now)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("false", id="false"),
        pytest.param("TRUE", id="uppercase-TRUE"),
        pytest.param("1", id="one"),
        pytest.param("yes", id="yes"),
        pytest.param("", id="empty"),
        pytest.param(" ", id="whitespace"),
    ],
)
def test_fetch_window_rejects_every_non_true_override(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("BACKFILL_DAYTIME_APPROVED", value)

    with pytest.raises(FetchWindowClosed):
        assert_fetch_window_open(override_now=datetime(2026, 8, 4, 11, 22, tzinfo=KST))
