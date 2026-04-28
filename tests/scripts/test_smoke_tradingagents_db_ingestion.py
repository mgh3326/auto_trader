from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionProposal,
    TradingDecisionSession,
    UserResponse,
)

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.tasks",
]

_SECRET_MARKERS = (
    "OPENAI_API_KEY",
    "KIS_",
    "UPBIT_",
    "GOOGLE_API_KEY",
    "DATABASE_URL",
    "TELEGRAM_TOKEN",
    "OPENDART_API_KEY",
)


class FakeSessionContext:
    def __init__(self, db: SimpleNamespace) -> None:
        self.db = db

    async def __aenter__(self) -> SimpleNamespace:
        return self.db

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _base_argv() -> list[str]:
    return [
        "--symbol",
        "005930.KS",
        "--as-of",
        "2025-01-15",
        "--instrument-type",
        "equity_kr",
        "--user-id",
        "42",
        "--keep-on-success",
    ]


def _session(*, execution_allowed: bool = False) -> TradingDecisionSession:
    return TradingDecisionSession(
        id=1001,
        session_uuid=uuid.uuid4(),
        user_id=42,
        source_profile="tradingagents",
        market_scope="kr",
        market_brief={
            "advisory_only": True,
            "execution_allowed": execution_allowed,
        },
        status="open",
        generated_at=datetime(2025, 1, 15, 9, 0, tzinfo=UTC),
    )


def _proposal(session_id: int = 1001) -> TradingDecisionProposal:
    return TradingDecisionProposal(
        id=2002,
        session_id=session_id,
        symbol="005930.KS",
        instrument_type=InstrumentType.equity_kr,
        proposal_kind=ProposalKind.other,
        side="none",
        original_payload={
            "advisory_only": True,
            "execution_allowed": False,
        },
        user_response=UserResponse.pending,
    )


def _fake_db() -> SimpleNamespace:
    return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())


def _clear_forbidden_modules() -> None:
    for name in list(sys.modules):
        if name == "scripts.smoke_tradingagents_db_ingestion" or any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _FORBIDDEN_PREFIXES
        ):
            sys.modules.pop(name, None)


def _import_smoke():
    _clear_forbidden_modules()
    from scripts import smoke_tradingagents_db_ingestion as smoke

    return smoke


def test_argv_rejects_dry_run_false() -> None:
    smoke = _import_smoke()

    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=[*_base_argv(), "--dry-run=False"])

    assert exc.value.code == 64


def test_argv_rejects_place_order_flag() -> None:
    smoke = _import_smoke()

    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=[*_base_argv(), "--place-order"])

    assert exc.value.code == 64


def test_argv_rejects_register_watch_flag() -> None:
    smoke = _import_smoke()

    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=[*_base_argv(), "--register-watch"])

    assert exc.value.code == 64


def test_module_import_does_not_load_forbidden_prefixes() -> None:
    project_root = str(pathlib.Path(__file__).parent.parent.parent)
    prefixes_json = json.dumps(_FORBIDDEN_PREFIXES)
    script = f"""
import json
import sys

sys.path.insert(0, {project_root!r})
import scripts.smoke_tradingagents_db_ingestion

prefixes = json.loads({prefixes_json!r})
loaded = sorted(sys.modules)
violations = [
    name
    for prefix in prefixes
    for name in loaded
    if name == prefix or name.startswith(prefix + ".")
]
print(json.dumps({{"violations": violations, "loaded": loaded}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Subprocess import of smoke harness failed:\n" + result.stderr
    )

    payload = json.loads(result.stdout)
    assert payload["violations"] == []


def test_settings_missing_tradingagents_python_exits_78(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _import_smoke()

    monkeypatch.delenv("TRADINGAGENTS_PYTHON", raising=False)
    monkeypatch.setattr(
        smoke,
        "Settings",
        lambda: SimpleNamespace(
            tradingagents_python=None,
            tradingagents_repo_path="/tmp/tradingagents",
            tradingagents_runner_path="/tmp/tradingagents/scripts/run.py",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=_base_argv())

    assert exc.value.code == 78


def test_invariant_violation_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _import_smoke()
    from app.core import db as db_module

    db = _fake_db()
    monkeypatch.setattr(db_module, "AsyncSessionLocal", lambda: FakeSessionContext(db))
    monkeypatch.setattr(
        smoke,
        "Settings",
        lambda: SimpleNamespace(
            tradingagents_python=sys.executable,
            tradingagents_repo_path=str(pathlib.Path.cwd()),
            tradingagents_runner_path=__file__,
        ),
    )
    monkeypatch.setattr(
        smoke,
        "ingest_tradingagents_research",
        AsyncMock(return_value=(_session(execution_allowed=True), _proposal())),
    )
    monkeypatch.setattr(smoke, "_count_proposals", AsyncMock(return_value=1))
    monkeypatch.setattr(
        smoke,
        "_count_side_effects",
        AsyncMock(return_value={"actions": 0, "counterfactuals": 0, "outcomes": 0}),
    )

    _clear_forbidden_modules()
    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=_base_argv())

    assert exc.value.code == 1
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_success_path_prints_redacted_json_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _import_smoke()
    from app.core import db as db_module

    db = _fake_db()
    session_obj = _session()
    proposal = _proposal(session_obj.id)
    monkeypatch.setattr(db_module, "AsyncSessionLocal", lambda: FakeSessionContext(db))
    monkeypatch.setattr(
        smoke,
        "Settings",
        lambda: SimpleNamespace(
            tradingagents_python=sys.executable,
            tradingagents_repo_path=str(pathlib.Path.cwd()),
            tradingagents_runner_path=__file__,
        ),
    )
    monkeypatch.setattr(
        smoke,
        "ingest_tradingagents_research",
        AsyncMock(return_value=(session_obj, proposal)),
    )
    monkeypatch.setattr(smoke, "_count_proposals", AsyncMock(return_value=1))
    monkeypatch.setattr(
        smoke,
        "_count_side_effects",
        AsyncMock(return_value={"actions": 0, "counterfactuals": 0, "outcomes": 0}),
    )
    monkeypatch.setattr(
        smoke,
        "_reload_session_and_proposal",
        AsyncMock(return_value=(session_obj, proposal)),
        raising=False,
    )

    _clear_forbidden_modules()
    with pytest.raises(SystemExit) as exc:
        smoke.main(argv=_base_argv())

    assert exc.value.code == 0
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()

    report_text = capsys.readouterr().out
    report = json.loads(report_text)
    assert report["ok"] is True
    assert report["session"]["id"] == session_obj.id
    assert report["proposal"]["id"] == proposal.id
    assert report["side_effect_counts"] == {
        "actions": 0,
        "counterfactuals": 0,
        "outcomes": 0,
    }
    for marker in _SECRET_MARKERS:
        assert marker not in report_text
