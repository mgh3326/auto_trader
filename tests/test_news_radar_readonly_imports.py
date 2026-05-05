# tests/test_news_radar_readonly_imports.py
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RADAR_FILES = (
    REPO_ROOT / "app/routers/news_radar.py",
    REPO_ROOT / "app/services/news_radar_service.py",
    REPO_ROOT / "app/services/news_radar_classifier.py",
    REPO_ROOT / "app/schemas/news_radar.py",
)
FORBIDDEN = (
    "place_order",
    "cancel_order",
    "modify_order",
    "manage_watch_alerts",
    "paper_order_handler",
    "kis_trading_service",
    "fill_notification",
    "alpaca_paper_ledger_service",
    "watch_order_intent_ledger_service",
)


@pytest.mark.unit
def test_news_radar_files_have_no_mutation_imports() -> None:
    violations = []
    for path in RADAR_FILES:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if re.search(rf"\b{re.escape(token)}\b", text):
                violations.append(f"{path.name}: {token}")
    assert violations == []
