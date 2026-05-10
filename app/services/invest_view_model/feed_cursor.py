"""Shared cursor encode/decode for /invest/api/feed/* endpoints (ROB-179)."""

from __future__ import annotations

import base64
import json
from datetime import datetime


def encode_feed_cursor(published_at: datetime | None, row_id: int) -> str:
    payload = {
        "p": published_at.isoformat() if published_at is not None else None,
        "i": row_id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_feed_cursor(cursor: str) -> dict:
    """Decode a feed cursor string. Raises ValueError on any invalid input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc

    if not isinstance(payload, dict) or "p" not in payload or "i" not in payload:
        raise ValueError("Cursor missing required keys 'p' and 'i'")

    return payload
