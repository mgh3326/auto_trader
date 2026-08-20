"""ROB-1297 static boundaries: notifier-only, no cron, no leftover mix-in."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
DIGEST_DIR = REPO / "app" / "services" / "market_close_digest"
FLOW_PATH = REPO / "app" / "flows" / "market_close_digest_flow.py"
CLI_PATH = REPO / "scripts" / "run_market_close_digest.py"
LEFTOVER_PATH = REPO / "app" / "services" / "manual_holdings_leftover.py"


def _iter_py(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.is_dir() else [root]


def test_digest_send_path_is_trade_notifier_only() -> None:
    send_calls: list[str] = []
    forbidden = (
        "send_telegram(",
        "send_telegram_message(",
        "Bot(",
        "telegram.Bot",
        "https://api.telegram.org",
        "httpx.AsyncClient",
    )
    for path in [*_iter_py(DIGEST_DIR), CLI_PATH, FLOW_PATH]:
        text = path.read_text()
        for snippet in forbidden:
            assert snippet not in text, f"{path} contains {snippet!r}"
        if "notify_agent_message" in text:
            send_calls.append(str(path.relative_to(REPO)))
    assert send_calls, "digest must send via TradeNotifier.notify_agent_message"


def test_digest_does_not_import_leftover_cleanup() -> None:
    for path in _iter_py(DIGEST_DIR):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "manual_holdings_leftover" not in node.module
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "manual_holdings_leftover" not in alias.name


def test_no_cron_or_prefect_deployment_in_digest_surface() -> None:
    paths = [
        *_iter_py(DIGEST_DIR),
        FLOW_PATH,
        CLI_PATH,
        LEFTOVER_PATH,
        REPO / "scripts" / "cleanup_toss_manual_holdings.py",
        REPO / "app" / "tasks",
    ]
    cron_pattern = re.compile(r"schedule\s*=")
    deployment_pattern = re.compile(r"Deployment\s*\(")
    for path in paths:
        if path.is_dir():
            files = _iter_py(path)
        else:
            files = [path]
        for file in files:
            if not file.exists() or file.suffix != ".py":
                continue
            text = file.read_text()
            if "market_close_digest" not in text and file != FLOW_PATH:
                continue
            assert not cron_pattern.search(text) or "INTENDED_CRON" in text, (
                f"cron schedule registration found in {file}"
            )
            assert deployment_pattern.search(text) is None, (
                f"Prefect Deployment() found in {file}"
            )


def test_flow_file_documents_cadence_but_defaults_send_false() -> None:
    text = FLOW_PATH.read_text()
    assert "@flow" in text
    assert "send: bool = False" in text
    assert "deployment registration is deferred" in text.lower()
    assert "05:05" in text
    assert "15:45" in text
    assert "09:05" in text


def test_no_taskiq_schedule_for_digest() -> None:
    tasks_dir = REPO / "app" / "tasks"
    for path in _iter_py(tasks_dir):
        text = path.read_text()
        assert "market_close_digest" not in text
        assert "run_market_close_digest" not in text


def test_digest_has_no_llm_imports() -> None:
    forbidden = ("openai", "google.genai", "google.generativeai", "anthropic")
    for path in _iter_py(DIGEST_DIR):
        text = path.read_text()
        for name in forbidden:
            assert name not in text
