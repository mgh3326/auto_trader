from __future__ import annotations

import json
from typing import Any

from app.services.kr_market_close_summary.types import CloseSummary


def format_summary(summary: CloseSummary, *, commit: bool, stored: bool | None) -> str:
    payload: dict[str, Any] = summary.to_dict()
    payload["mode"] = "commit" if commit else "preview"
    if commit:
        payload["stored"] = stored
        payload["result"] = "inserted" if stored else "already_exists_noop"
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
