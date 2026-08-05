from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.kis_mock_runner import session


def test_krx_regular_session_uses_confirmed_calendar_bounds(monkeypatch) -> None:
    open_at = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)  # 09:00 KST
    close_at = datetime(2026, 8, 5, 6, 30, tzinfo=UTC)  # 15:30 KST
    monkeypatch.setattr(
        session,
        "regular_session_bounds",
        lambda market, day: (open_at, close_at) if market == "kr" else None,
    )
    assert session.is_krx_regular_session(open_at) is True
    assert session.is_krx_regular_session(close_at - timedelta(microseconds=1)) is True
    assert session.is_krx_regular_session(close_at) is False


def test_krx_regular_session_fails_closed_when_calendar_cannot_confirm(
    monkeypatch,
) -> None:
    monkeypatch.setattr(session, "regular_session_bounds", lambda market, day: None)
    assert (
        session.is_krx_regular_session(datetime(2026, 8, 5, 3, 0, tzinfo=UTC)) is False
    )
