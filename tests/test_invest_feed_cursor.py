"""Cursor helper tests for /invest/api/feed/research (ROB-179)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_encode_decode_round_trip():
    from app.services.invest_view_model.feed_cursor import (
        decode_feed_cursor,
        encode_feed_cursor,
    )

    dt = datetime(2026, 5, 10, 8, 0, 0, tzinfo=UTC)
    row_id = 12345
    encoded = encode_feed_cursor(dt, row_id)
    assert isinstance(encoded, str)
    assert len(encoded) > 0

    decoded = decode_feed_cursor(encoded)
    assert decoded["i"] == row_id
    assert decoded["p"] == dt.isoformat()


def test_decode_rejects_garbage():
    from app.services.invest_view_model.feed_cursor import decode_feed_cursor

    with pytest.raises(ValueError):
        decode_feed_cursor("not-base64!!!")

    with pytest.raises(ValueError):
        decode_feed_cursor("aGVsbG8=")  # valid b64 but not JSON {"p": ..., "i": ...}

    with pytest.raises(ValueError):
        decode_feed_cursor("e30=")  # valid b64, valid JSON {} but missing keys


def test_decode_handles_null_published_at():
    from app.services.invest_view_model.feed_cursor import (
        decode_feed_cursor,
        encode_feed_cursor,
    )

    encoded = encode_feed_cursor(None, 42)
    decoded = decode_feed_cursor(encoded)
    assert decoded["p"] is None
    assert decoded["i"] == 42
