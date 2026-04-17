from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import paperclip_cli_probe as probe


def test_resolve_cli_launcher_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPERCLIP_CLI_COMMAND", "/custom/bin/paperclipai")
    monkeypatch.setattr(probe.shutil, "which", lambda _name: None)

    launcher = probe.resolve_cli_launcher()

    assert launcher.argv == ["/custom/bin/paperclipai"]
    assert launcher.display == "/custom/bin/paperclipai"


def test_resolve_cli_launcher_uses_npx_when_direct_launcher_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "npx":
            return "/usr/bin/npx"
        return None

    monkeypatch.delenv("PAPERCLIP_CLI_COMMAND", raising=False)
    monkeypatch.setattr(probe.shutil, "which", fake_which)

    launcher = probe.resolve_cli_launcher()

    assert launcher.argv == ["/usr/bin/npx", "-y", "paperclipai"]
    assert launcher.display == "npx -y paperclipai"


def test_resolve_runtime_config_reads_instance_config_when_context_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paperclip_home = tmp_path / ".paperclip"
    instance_dir = paperclip_home / "instances" / "default"
    instance_dir.mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        '{"server":{"host":"0.0.0.0","port":3100}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("PAPERCLIP_HOME", str(paperclip_home))
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_COMPANY_ID", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)

    config = probe.resolve_runtime_config()

    assert config.api_base == "http://127.0.0.1:3100"
    assert config.paperclip_home == paperclip_home


def test_resolve_runtime_config_uses_auth_store_when_env_token_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paperclip_home = tmp_path / ".paperclip"
    paperclip_home.mkdir()
    (paperclip_home / "auth.json").write_text(
        '{"version":1,"credentials":{"http://127.0.0.1:3100":{"apiBase":"http://127.0.0.1:3100","token":"board-token","createdAt":"2026-04-15T00:00:00Z","updatedAt":"2026-04-15T00:00:00Z"}}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("PAPERCLIP_HOME", str(paperclip_home))
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://127.0.0.1:3100")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "company-1")
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)

    config = probe.resolve_runtime_config()

    assert config.api_key == "board-token"
    assert config.company_id == "company-1"


def test_run_cli_json_probe_returns_parsed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = probe.CliLauncher(argv=["paperclipai"], display="paperclipai")
    runtime = probe.RuntimeConfig(
        api_base="http://127.0.0.1:3100",
        company_id="company-1",
        api_key="token-1",
        paperclip_home=Path("/tmp/home"),
        context_path=Path("/tmp/home/context.json"),
        auth_store_path=Path("/tmp/home/auth.json"),
    )

    def fake_run(argv: list[str], **kwargs) -> CompletedProcess[str]:
        assert "issue" in argv
        assert "--json" in argv
        assert kwargs["env"]["PAPERCLIP_API_KEY"] == "token-1"
        return CompletedProcess(
            argv,
            0,
            '[{"id":"i1","identifier":"ROB-1","status":"in_progress"}]',
            "",
        )

    monkeypatch.setattr(probe.subprocess, "run", fake_run)

    rows = probe.run_cli_json_probe(launcher, runtime, ["issue", "list"])

    assert rows[0]["identifier"] == "ROB-1"


def test_run_cli_json_probe_raises_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = probe.CliLauncher(argv=["paperclipai"], display="paperclipai")
    runtime = probe.RuntimeConfig(
        api_base="http://127.0.0.1:3100",
        company_id="company-1",
        api_key="token-1",
        paperclip_home=Path("/tmp/home"),
        context_path=Path("/tmp/home/context.json"),
        auth_store_path=Path("/tmp/home/auth.json"),
    )

    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda argv, **kwargs: CompletedProcess(argv, 0, "not-json", ""),
    )

    with pytest.raises(probe.CliProbeError) as exc:
        probe.run_cli_json_probe(launcher, runtime, ["agent", "list"])

    assert exc.value.code == "cli_invalid_json"


def test_main_returns_success_false_payload_when_auth_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    paperclip_home = tmp_path / ".paperclip"
    paperclip_home.mkdir()

    monkeypatch.setenv("PAPERCLIP_HOME", str(paperclip_home))
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "company-1")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://127.0.0.1:3100")
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    monkeypatch.setattr(
        probe,
        "resolve_cli_launcher",
        lambda: probe.CliLauncher(["paperclipai"], "paperclipai"),
    )

    exit_code = probe.main(["boss-queue", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["success"] is False
    assert payload["error_code"] == "auth_bootstrap_required"


def test_derive_manager_followup_needed_when_child_newer_than_assignee_heartbeat() -> (
    None
):
    snapshot = {
        "issues": [
            {
                "id": "p1",
                "identifier": "ROB-33",
                "title": "Parent",
                "status": "in_progress",
                "parentId": None,
                "assigneeAgentId": "a1",
                "updatedAt": "2026-04-15T10:57:47+09:00",
            },
            {
                "id": "c1",
                "identifier": "ROB-52",
                "title": "Child",
                "status": "done",
                "parentId": "p1",
                "assigneeAgentId": None,
                "updatedAt": "2026-04-15T10:58:24+09:00",
            },
        ],
        "approvals": [],
        "agents": [
            {
                "id": "a1",
                "name": "CEO",
                "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 3600}},
                "lastHeartbeatAt": "2026-04-15T10:54:16+09:00",
            }
        ],
        "heartbeat_runs": {},
    }

    items, warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert warnings == []
    assert any(item["kind"] == "manager_followup_needed" for item in items)


def _manager_followup_snapshot(
    *,
    parent_status: str,
    child_status: str,
    parent_updated_at: str = "2026-04-15T10:57:47+09:00",
    child_updated_at: str = "2026-04-15T10:58:24+09:00",
    last_heartbeat_at: str = "2026-04-15T10:54:16+09:00",
) -> dict:
    return {
        "issues": [
            {
                "id": "p1",
                "identifier": "ROB-111",
                "title": "Parent",
                "status": parent_status,
                "parentId": None,
                "assigneeAgentId": "a1",
                "updatedAt": parent_updated_at,
            },
            {
                "id": "c1",
                "identifier": "ROB-181c",
                "title": "Child",
                "status": child_status,
                "parentId": "p1",
                "assigneeAgentId": None,
                "updatedAt": child_updated_at,
            },
        ],
        "approvals": [],
        "agents": [
            {
                "id": "a1",
                "name": "CEO",
                "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 3600}},
                "lastHeartbeatAt": last_heartbeat_at,
            }
        ],
        "heartbeat_runs": {},
    }


def test_manager_followup_skipped_when_parent_blocked_and_child_in_review() -> None:
    # ROB-166 regression: blocked parent waiting on external blocker +
    # child moved to in_review (lane handoff) must NOT emit manager_followup.
    snapshot = _manager_followup_snapshot(
        parent_status="blocked",
        child_status="in_review",
    )

    items, _warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert all(item["kind"] != "manager_followup_needed" for item in items)


def test_manager_followup_skipped_when_child_in_progress_only() -> None:
    # In-lane child work (in_progress) is not a parent-owner signal.
    snapshot = _manager_followup_snapshot(
        parent_status="in_progress",
        child_status="in_progress",
    )

    items, _warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert all(item["kind"] != "manager_followup_needed" for item in items)


def test_manager_followup_skipped_when_child_in_review() -> None:
    # Code Reviewer lane handoff must not trigger parent rerun signal.
    snapshot = _manager_followup_snapshot(
        parent_status="in_progress",
        child_status="in_review",
    )

    items, _warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert all(item["kind"] != "manager_followup_needed" for item in items)


def test_manager_followup_emitted_when_child_blocked() -> None:
    # Child blocked on something the parent owner may need to resolve.
    snapshot = _manager_followup_snapshot(
        parent_status="in_progress",
        child_status="blocked",
    )

    items, _warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert any(item["kind"] == "manager_followup_needed" for item in items)


def test_derive_approval_revision_requested_from_approval_status() -> None:
    snapshot = {
        "issues": [],
        "approvals": [
            {
                "id": "ap-1",
                "type": "hire_agent",
                "status": "revision_requested",
                "payload": {"name": "KR Scout", "title": "KR Stock Strategy Scout"},
                "requestedByAgentId": "agent-1",
                "requestedByUserId": None,
                "updatedAt": "2026-04-15T11:00:00+09:00",
            }
        ],
        "agents": [],
        "heartbeat_runs": {},
    }

    items, _warnings = probe.derive_boss_queue_items(snapshot)

    assert any(item["kind"] == "approval_revision_requested" for item in items)
    assert any(item["title"] == "KR Stock Strategy Scout" for item in items)


def test_derive_active_issue_unassigned_ignores_backlog_by_default() -> None:
    snapshot = {
        "issues": [
            {
                "id": "i1",
                "identifier": "ROB-100",
                "title": "Queued",
                "status": "backlog",
                "parentId": None,
                "assigneeAgentId": None,
                "updatedAt": "2026-04-15T09:00:00+09:00",
            },
            {
                "id": "i2",
                "identifier": "ROB-101",
                "title": "Active",
                "status": "in_progress",
                "parentId": None,
                "assigneeAgentId": None,
                "updatedAt": "2026-04-15T10:00:00+09:00",
            },
        ],
        "approvals": [],
        "agents": [],
        "heartbeat_runs": {},
    }

    items, _warnings = probe.derive_boss_queue_items(snapshot)

    assert any(item["issue_identifier"] == "ROB-101" for item in items)
    assert all(item.get("issue_identifier") != "ROB-100" for item in items)


def test_fetch_heartbeat_runs_uses_same_auth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, str]]:
            return [
                {"invocationSource": "timer", "startedAt": "2026-04-15T09:14:18+09:00"}
            ]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def get(self, url: str, headers=None, timeout=None):
            called["url"] = url
            called["headers"] = headers
            called["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(probe.httpx, "Client", lambda: FakeClient())

    rows = probe.fetch_heartbeat_runs(
        api_base="http://127.0.0.1:3100",
        company_id="company-1",
        agent_id="agent-1",
        api_key="token-1",
    )

    assert rows[0]["invocationSource"] == "timer"
    assert called["headers"]["Authorization"] == "Bearer token-1"


def test_derive_heartbeat_missed_skips_agents_without_active_issue_context() -> None:
    snapshot = {
        "issues": [],
        "approvals": [],
        "agents": [
            {
                "id": "a1",
                "name": "CTO",
                "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 3600}},
                "lastHeartbeatAt": "2026-04-15T11:50:00+09:00",
            }
        ],
        "heartbeat_runs": {
            "a1": [
                {
                    "invocationSource": "on_demand",
                    "startedAt": "2026-04-15T11:50:00+09:00",
                },
                {"invocationSource": "timer", "startedAt": "2026-04-15T09:00:00+09:00"},
            ]
        },
    }

    items, warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    assert warnings == []
    assert all(item["kind"] != "heartbeat_missed" for item in items)


def test_derive_heartbeat_missed_links_to_latest_active_issue_context() -> None:
    snapshot = {
        "issues": [
            {
                "id": "i1",
                "identifier": "ROB-33",
                "title": "Parent planning",
                "status": "in_progress",
                "parentId": None,
                "assigneeAgentId": "a1",
                "updatedAt": "2026-04-15T10:10:00+09:00",
            },
            {
                "id": "i2",
                "identifier": "ROB-49",
                "title": "Blocked rollout",
                "status": "blocked",
                "parentId": None,
                "assigneeAgentId": "a1",
                "updatedAt": "2026-04-15T10:40:00+09:00",
            },
        ],
        "approvals": [],
        "agents": [
            {
                "id": "a1",
                "name": "CTO",
                "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 3600}},
                "lastHeartbeatAt": "2026-04-15T11:50:00+09:00",
            }
        ],
        "heartbeat_runs": {
            "a1": [
                {"invocationSource": "timer", "startedAt": "2026-04-15T09:00:00+09:00"},
            ]
        },
    }

    items, warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T11:00:00+09:00"),
    )

    heartbeat_item = next(item for item in items if item["kind"] == "heartbeat_missed")
    assert warnings == []
    assert heartbeat_item["issue_identifier"] == "ROB-49"
    assert heartbeat_item["title"] == "Blocked rollout"


def test_derive_heartbeat_missed_falls_back_to_agent_last_heartbeat_with_warning() -> (
    None
):
    snapshot = {
        "issues": [
            {
                "id": "i1",
                "identifier": "ROB-77",
                "title": "Scaling follow-up",
                "status": "in_progress",
                "parentId": None,
                "assigneeAgentId": "a1",
                "updatedAt": "2026-04-15T09:30:00+09:00",
            }
        ],
        "approvals": [],
        "agents": [
            {
                "id": "a1",
                "name": "Trader",
                "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 3600}},
                "lastHeartbeatAt": "2026-04-15T08:00:00+09:00",
            }
        ],
        "heartbeat_runs": {},
    }

    items, warnings = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:01+09:00"),
    )

    assert any("fallback" in warning for warning in warnings)
    assert any(item["kind"] == "heartbeat_missed" for item in items)


def _in_review_snapshot(
    *,
    assignee_user_id: str | None = None,
    assignee_agent_id: str | None = None,
    updated_at: str = "2026-04-15T09:00:00+09:00",
    approvals: list | None = None,
) -> dict:
    issue: dict = {
        "id": "i1",
        "identifier": "ROB-200",
        "title": "Review test issue",
        "status": "in_review",
        "parentId": None,
        "assigneeAgentId": assignee_agent_id,
        "assigneeUserId": assignee_user_id,
        "updatedAt": updated_at,
    }
    return {
        "issues": [issue],
        "approvals": approvals or [],
        "agents": [
            {
                "id": "agent-cto",
                "name": "CTO",
                "runtimeConfig": {},
                "lastHeartbeatAt": None,
            }
        ],
        "heartbeat_runs": {},
    }


def test_in_review_with_user_no_approval_emits_issue_review_needed() -> None:
    snapshot = _in_review_snapshot(assignee_user_id="user-1")
    items, _ = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:00+09:00"),
    )
    assert len(items) == 1
    assert items[0]["kind"] == "issue_review_needed"
    assert items[0]["severity"] == "high"
    assert items[0]["evidence"]["assignee_user_id"] == "user-1"


def test_in_review_with_user_and_pending_approval_emits_formal_approval_pending() -> (
    None
):
    snapshot = _in_review_snapshot(
        assignee_user_id="user-1",
        approvals=[
            {
                "id": "ap-1",
                "type": "request_board_approval",
                "status": "pending",
                "issueIds": ["i1"],
                "payload": {"title": "Test approval"},
                "updatedAt": "2026-04-15T09:00:00+09:00",
            }
        ],
    )
    items, _ = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:00+09:00"),
    )
    assert len(items) == 1
    assert items[0]["kind"] == "formal_approval_pending"
    assert items[0]["severity"] == "high"
    assert items[0]["evidence"]["approval_id"] == "ap-1"


def test_in_review_within_grace_period_emits_no_signal() -> None:
    snapshot = _in_review_snapshot(
        assignee_user_id="user-1",
        updated_at="2026-04-15T09:50:00+09:00",
    )
    items, _ = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:00+09:00"),
    )
    review_items = [
        i
        for i in items
        if i["kind"]
        in ("issue_review_needed", "formal_approval_pending", "misrouted_review")
    ]
    assert review_items == []


def test_in_review_with_agent_only_emits_misrouted_review() -> None:
    snapshot = _in_review_snapshot(assignee_agent_id="agent-cto")
    items, _ = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:00+09:00"),
    )
    assert len(items) == 1
    assert items[0]["kind"] == "misrouted_review"
    assert items[0]["severity"] == "medium"
    assert items[0]["evidence"]["assignee_agent_id"] == "agent-cto"
    assert items[0]["evidence"]["agent_name"] == "CTO"


def test_in_review_with_no_assignee_falls_to_active_issue_unassigned() -> None:
    snapshot = _in_review_snapshot()
    items, _ = probe.derive_boss_queue_items(
        snapshot,
        now=probe.parse_timestamp("2026-04-15T10:00:00+09:00"),
    )
    review_items = [
        i
        for i in items
        if i["kind"]
        in ("issue_review_needed", "formal_approval_pending", "misrouted_review")
    ]
    assert review_items == []
