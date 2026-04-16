#!/usr/bin/env python3
"""Paperclip CLI-first probe wrapper for Boss Action Queue."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ACTIVE_ISSUE_STATUSES = {"in_progress", "blocked"}
OPTIONAL_PENDING_STATUSES = {"backlog", "todo"}
NON_FATAL_ERROR_CODES = {"auth_bootstrap_required"}
REVIEW_GRACE_PERIOD = timedelta(minutes=30)
DEFAULT_HEARTBEAT_RUN_LIMIT = 50
DEFAULT_HEARTBEAT_THRESHOLD_MULTIPLIER = 1.5
DEFAULT_HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class CliLauncher:
    argv: list[str]
    display: str


@dataclass(frozen=True)
class RuntimeConfig:
    api_base: str | None
    company_id: str | None
    api_key: str | None
    paperclip_home: Path
    context_path: Path
    auth_store_path: Path
    config_path: Path | None = None


class CliProbeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        remediation: list[str] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation or []
        self.debug = debug or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paperclip CLI probe wrapper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    boss = subparsers.add_parser(
        "boss-queue",
        description="Collect Boss Action Queue probe snapshot",
    )
    boss.add_argument("--json", action="store_true", help="Output JSON envelope")
    boss.add_argument("--api-base", help="Override Paperclip API base URL")
    boss.add_argument("--company-id", help="Override Paperclip company id")
    boss.add_argument(
        "--default-company-id",
        help="Fallback company id if env/context is unset",
    )
    boss.add_argument(
        "--include-backlog-unassigned",
        action="store_true",
        help="Treat backlog/todo issues without assignee as queue items",
    )
    boss.add_argument(
        "--heartbeat-threshold-multiplier",
        type=float,
        default=DEFAULT_HEARTBEAT_THRESHOLD_MULTIPLIER,
        help="Age multiplier applied to heartbeat interval before flagging missed heartbeat",
    )
    boss.add_argument(
        "--heartbeat-run-limit",
        type=int,
        default=DEFAULT_HEARTBEAT_RUN_LIMIT,
        help="Max heartbeat runs fetched per agent when HTTP fallback is used",
    )
    return parser


def now_local() -> datetime:
    return datetime.now(UTC).astimezone()


def resolve_paperclip_home() -> Path:
    env_home = os.environ.get("PAPERCLIP_HOME", "").strip()
    if env_home:
        return Path(os.path.expanduser(env_home)).resolve()
    return (Path.home() / ".paperclip").resolve()


def normalize_api_base(api_base: str) -> str:
    return api_base.strip().rstrip("/")


def resolve_cli_launcher() -> CliLauncher:
    override = os.environ.get("PAPERCLIP_CLI_COMMAND", "").strip()
    if override:
        return CliLauncher(argv=shlex.split(override), display=override)

    direct = shutil.which("paperclipai")
    if direct:
        return CliLauncher(argv=[direct], display=direct)

    pnpm = shutil.which("pnpm")
    if pnpm:
        return CliLauncher(argv=[pnpm, "paperclipai"], display="pnpm paperclipai")

    npx = shutil.which("npx")
    if npx:
        return CliLauncher(
            argv=[npx, "-y", "paperclipai"], display="npx -y paperclipai"
        )

    raise CliProbeError(
        code="cli_not_found",
        message="No runnable Paperclip CLI command found",
        remediation=[
            "Install paperclipai or configure PAPERCLIP_CLI_COMMAND",
            "Verify pnpm/npm/corepack availability",
        ],
    )


def read_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _context_profile(context_data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context_data, dict):
        return {}
    profiles = context_data.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    profile_name = context_data.get("currentProfile") or "default"
    profile = profiles.get(profile_name)
    return profile if isinstance(profile, dict) else {}


def infer_api_base_from_config(config_path: Path) -> str | None:
    raw = read_json_file(config_path)
    if not isinstance(raw, dict):
        return None
    server = raw.get("server")
    if not isinstance(server, dict):
        return None
    port = server.get("port", 3100)
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        port_int = 3100
    host = str(server.get("host") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", "", "localhost"}:
        host = "127.0.0.1"
    return f"http://{host}:{port_int}"


def read_auth_token(auth_store_path: Path, api_base: str | None) -> str | None:
    if not api_base:
        return None
    raw = read_json_file(auth_store_path)
    if not isinstance(raw, dict):
        return None
    credentials = raw.get("credentials")
    if not isinstance(credentials, dict):
        return None
    entry = credentials.get(normalize_api_base(api_base))
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def resolve_runtime_config(
    *,
    api_base_override: str | None = None,
    company_id_override: str | None = None,
    default_company_id: str | None = None,
) -> RuntimeConfig:
    paperclip_home = resolve_paperclip_home()
    context_path = paperclip_home / "context.json"
    auth_store_path = paperclip_home / "auth.json"
    config_path = paperclip_home / "instances" / "default" / "config.json"

    context_data = read_json_file(context_path)
    profile = _context_profile(context_data)

    api_base = (
        (api_base_override or "").strip()
        or os.environ.get("PAPERCLIP_API_URL", "").strip()
        or str(profile.get("apiBase") or "").strip()
        or (infer_api_base_from_config(config_path) or "")
    )
    api_base = normalize_api_base(api_base) if api_base else None

    company_id = (
        (company_id_override or "").strip()
        or os.environ.get("PAPERCLIP_COMPANY_ID", "").strip()
        or str(profile.get("companyId") or "").strip()
        or str(default_company_id or "").strip()
        or None
    )

    api_key = os.environ.get("PAPERCLIP_API_KEY", "").strip() or None
    if not api_key:
        env_var_name = str(profile.get("apiKeyEnvVarName") or "").strip()
        if env_var_name:
            api_key = os.environ.get(env_var_name, "").strip() or None
    if not api_key:
        api_key = read_auth_token(auth_store_path, api_base)

    return RuntimeConfig(
        api_base=api_base,
        company_id=company_id,
        api_key=api_key,
        paperclip_home=paperclip_home,
        context_path=context_path,
        auth_store_path=auth_store_path,
        config_path=config_path,
    )


def run_cli_json_probe(
    launcher: CliLauncher,
    runtime: RuntimeConfig,
    args: list[str],
) -> Any:
    if not runtime.api_base:
        raise CliProbeError(
            "api_base_missing", "Paperclip API base URL is not configured"
        )
    if not runtime.company_id:
        raise CliProbeError(
            "company_id_missing", "Paperclip company id is not configured"
        )

    command = [*launcher.argv, *args]
    if "--company-id" not in command and "-C" not in command:
        command.extend(["--company-id", runtime.company_id])
    if "--api-base" not in command:
        command.extend(["--api-base", runtime.api_base])
    if "--json" not in command:
        command.append("--json")

    env = os.environ.copy()
    env["PAPERCLIP_HOME"] = str(runtime.paperclip_home)
    env["PAPERCLIP_CONTEXT"] = str(runtime.context_path)
    env["PAPERCLIP_AUTH_STORE"] = str(runtime.auth_store_path)
    env["PAPERCLIP_API_URL"] = runtime.api_base
    env["PAPERCLIP_COMPANY_ID"] = runtime.company_id
    if runtime.api_key:
        env["PAPERCLIP_API_KEY"] = runtime.api_key

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        raise CliProbeError(
            code="cli_command_failed",
            message=f"Paperclip CLI command failed: {' '.join(command)}",
            debug={
                "stderr": (result.stderr or "").strip(),
                "stdout": (result.stdout or "").strip(),
            },
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CliProbeError(
            code="cli_invalid_json",
            message=f"Paperclip CLI returned invalid JSON for {' '.join(args)}",
            debug={"stdout": result.stdout, "stderr": result.stderr},
        ) from exc


def fetch_heartbeat_runs(
    *,
    api_base: str,
    company_id: str,
    agent_id: str,
    api_key: str,
    limit: int = DEFAULT_HEARTBEAT_RUN_LIMIT,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> list[dict[str, Any]]:
    query = urlencode({"agentId": agent_id, "limit": str(limit)})
    url = f"{normalize_api_base(api_base)}/api/companies/{company_id}/heartbeat-runs?{query}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    with httpx.Client() as client:
        response = client.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list):
        raise CliProbeError(
            code="api_invalid_json",
            message="Heartbeat runs endpoint returned non-list JSON",
            debug={"url": url, "body": data},
        )
    return [row for row in data if isinstance(row, dict)]


def collect_raw_snapshot(
    launcher: CliLauncher,
    runtime: RuntimeConfig,
    *,
    heartbeat_run_limit: int = DEFAULT_HEARTBEAT_RUN_LIMIT,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    issues = run_cli_json_probe(launcher, runtime, ["issue", "list"])
    approvals = run_cli_json_probe(launcher, runtime, ["approval", "list"])
    agents = run_cli_json_probe(launcher, runtime, ["agent", "list"])

    heartbeat_runs: dict[str, list[dict[str, Any]]] = {}
    if runtime.api_key:
        for agent in [row for row in agents if isinstance(row, dict)]:
            heartbeat = (agent.get("runtimeConfig") or {}).get("heartbeat")
            if not isinstance(heartbeat, dict) or heartbeat.get("enabled") is not True:
                continue
            agent_id = agent.get("id")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            try:
                heartbeat_runs[agent_id] = fetch_heartbeat_runs(
                    api_base=runtime.api_base or "",
                    company_id=runtime.company_id or "",
                    agent_id=agent_id,
                    api_key=runtime.api_key,
                    limit=heartbeat_run_limit,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"heartbeat API fallback unavailable for agent {agent_id}: {exc}"
                )

    return (
        {
            "issues": issues if isinstance(issues, list) else [],
            "approvals": approvals if isinstance(approvals, list) else [],
            "agents": agents if isinstance(agents, list) else [],
            "heartbeat_runs": heartbeat_runs,
        },
        warnings,
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def fingerprint_for_item(kind: str, *parts: str | None) -> str:
    safe_parts = [p for p in parts if p]
    return ":".join([kind, *safe_parts])


def is_active_issue_status(status: str, *, include_backlog: bool = False) -> bool:
    normalized = (status or "").strip().lower()
    if normalized in ACTIVE_ISSUE_STATUSES:
        return True
    return include_backlog and normalized in OPTIONAL_PENDING_STATUSES


def _agent_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: row
        for row in snapshot.get("agents", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _child_issues_by_parent(
    snapshot: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.get("issues", []):
        if not isinstance(row, dict):
            continue
        parent_id = row.get("parentId")
        if not isinstance(parent_id, str) or not parent_id:
            continue
        grouped.setdefault(parent_id, []).append(row)
    return grouped


def _latest_timer_run_started_at(rows: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("invocationSource") or "") != "timer":
            continue
        started_at = parse_timestamp(row.get("startedAt"))
        if started_at is None:
            continue
        if latest is None or started_at > latest:
            latest = started_at
    return latest


def derive_boss_queue_items(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    include_backlog_unassigned: bool = False,
    heartbeat_threshold_multiplier: float = DEFAULT_HEARTBEAT_THRESHOLD_MULTIPLIER,
) -> tuple[list[dict[str, Any]], list[str]]:
    reference_now = now or now_local()
    warnings: list[str] = []
    items: list[dict[str, Any]] = []

    agents = _agent_by_id(snapshot)
    child_issues = _child_issues_by_parent(snapshot)
    active_issues_by_assignee: dict[str, list[dict[str, Any]]] = {}
    for issue in snapshot.get("issues", []):
        if not isinstance(issue, dict):
            continue
        assignee_agent_id = issue.get("assigneeAgentId")
        if not isinstance(assignee_agent_id, str) or not assignee_agent_id:
            continue
        if not is_active_issue_status(
            str(issue.get("status") or ""), include_backlog=False
        ):
            continue
        active_issues_by_assignee.setdefault(assignee_agent_id, []).append(issue)

    for issue in snapshot.get("issues", []):
        if not isinstance(issue, dict):
            continue
        issue_id = issue.get("id")
        status = str(issue.get("status") or "")
        identifier = str(issue.get("identifier") or "") or None
        title = str(issue.get("title") or "Untitled")
        assignee_agent_id = issue.get("assigneeAgentId")

        # manager_followup_needed
        if (
            isinstance(issue_id, str)
            and is_active_issue_status(status, include_backlog=False)
            and isinstance(assignee_agent_id, str)
            and assignee_agent_id in agents
            and child_issues.get(issue_id)
        ):
            agent = agents[assignee_agent_id]
            last_heartbeat_at = parse_timestamp(agent.get("lastHeartbeatAt"))
            latest_child_update = max(
                (
                    parse_timestamp(child.get("updatedAt"))
                    for child in child_issues[issue_id]
                    if parse_timestamp(child.get("updatedAt")) is not None
                ),
                default=None,
            )
            if latest_child_update and (
                last_heartbeat_at is None or latest_child_update > last_heartbeat_at
            ):
                owner = str(agent.get("name") or assignee_agent_id)
                items.append(
                    {
                        "fingerprint": fingerprint_for_item(
                            "manager_followup_needed",
                            identifier or issue_id,
                            latest_child_update.isoformat(),
                        ),
                        "kind": "manager_followup_needed",
                        "severity": "critical",
                        "owner": owner,
                        "issue_identifier": identifier,
                        "title": title,
                        "summary": "자식 이슈 업데이트가 assignee 마지막 heartbeat보다 최신",
                        "evidence": {
                            "latest_child_update": latest_child_update.isoformat(),
                            "last_heartbeat_at": last_heartbeat_at.isoformat()
                            if last_heartbeat_at
                            else None,
                        },
                        "recommended_action": f"{owner} 재실행 또는 parent issue 확인",
                    }
                )

        # active_issue_unassigned
        if (
            is_active_issue_status(
                status,
                include_backlog=include_backlog_unassigned,
            )
            and not assignee_agent_id
        ):
            items.append(
                {
                    "fingerprint": fingerprint_for_item(
                        "active_issue_unassigned",
                        identifier or str(issue_id),
                        str(issue.get("updatedAt") or ""),
                    ),
                    "kind": "active_issue_unassigned",
                    "severity": "high",
                    "owner": None,
                    "issue_identifier": identifier,
                    "title": title,
                    "summary": "활성 이슈에 assignee가 없다",
                    "evidence": {
                        "status": status,
                        "updated_at": issue.get("updatedAt"),
                    },
                    "recommended_action": "owner 지정 또는 backlog 복귀 여부 확인",
                }
            )

    for approval in snapshot.get("approvals", []):
        if not isinstance(approval, dict):
            continue
        if str(approval.get("status") or "") != "revision_requested":
            continue
        payload = (
            approval.get("payload") if isinstance(approval.get("payload"), dict) else {}
        )
        approval_title = str(
            payload.get("title")
            or payload.get("name")
            or approval.get("type")
            or "Approval"
        )
        items.append(
            {
                "fingerprint": fingerprint_for_item(
                    "approval_revision_requested",
                    str(approval.get("id") or ""),
                    str(approval.get("updatedAt") or ""),
                ),
                "kind": "approval_revision_requested",
                "severity": "high",
                "owner": "board",
                "issue_identifier": None,
                "title": approval_title,
                "summary": "approval이 revision_requested 상태다",
                "evidence": {
                    "approval_id": approval.get("id"),
                    "type": approval.get("type"),
                    "updated_at": approval.get("updatedAt"),
                },
                "recommended_action": "approval 재검토 또는 수정 요청 확인",
            }
        )

    # issue_review_needed / formal_approval_pending
    pending_approvals_by_issue: dict[str, list[dict[str, Any]]] = {}
    for approval in snapshot.get("approvals", []):
        if not isinstance(approval, dict):
            continue
        if str(approval.get("status") or "") != "pending":
            continue
        for issue_id in approval.get("issueIds") or []:
            if isinstance(issue_id, str) and issue_id:
                pending_approvals_by_issue.setdefault(issue_id, []).append(approval)

    for issue in snapshot.get("issues", []):
        if not isinstance(issue, dict):
            continue
        if str(issue.get("status") or "").strip().lower() != "in_review":
            continue
        issue_id = issue.get("id")
        identifier = str(issue.get("identifier") or "") or None
        title = str(issue.get("title") or "Untitled")
        updated_at = parse_timestamp(issue.get("updatedAt"))
        if updated_at and (reference_now - updated_at) < REVIEW_GRACE_PERIOD:
            continue

        assignee_user_id = issue.get("assigneeUserId")
        has_user = isinstance(assignee_user_id, str) and bool(assignee_user_id)
        assignee_agent_id = issue.get("assigneeAgentId")
        has_agent = isinstance(assignee_agent_id, str) and bool(assignee_agent_id)

        if has_user:
            linked_approvals = pending_approvals_by_issue.get(str(issue_id), [])
            if linked_approvals:
                for appr in linked_approvals:
                    approval_id = str(appr.get("id") or "")
                    items.append(
                        {
                            "fingerprint": fingerprint_for_item(
                                "formal_approval_pending",
                                identifier or str(issue_id),
                                approval_id,
                            ),
                            "kind": "formal_approval_pending",
                            "severity": "high",
                            "owner": "board",
                            "issue_identifier": identifier,
                            "title": title,
                            "summary": "in_review 이슈에 pending approval이 있다",
                            "evidence": {
                                "approval_id": approval_id,
                                "assignee_user_id": assignee_user_id,
                                "updated_at": issue.get("updatedAt"),
                            },
                            "recommended_action": "approval 검토 및 승인/반려 결정",
                        }
                    )
            else:
                items.append(
                    {
                        "fingerprint": fingerprint_for_item(
                            "issue_review_needed",
                            identifier or str(issue_id),
                            assignee_user_id,
                        ),
                        "kind": "issue_review_needed",
                        "severity": "high",
                        "owner": "board",
                        "issue_identifier": identifier,
                        "title": title,
                        "summary": "in_review 이슈가 사람 검토 대기 중이다",
                        "evidence": {
                            "assignee_user_id": assignee_user_id,
                            "updated_at": issue.get("updatedAt"),
                        },
                        "recommended_action": "이슈 검토 후 승인 또는 피드백 전달",
                    }
                )
        elif has_agent:
            agent_name = (
                str(agents[assignee_agent_id].get("name") or assignee_agent_id)
                if assignee_agent_id in agents
                else assignee_agent_id
            )
            items.append(
                {
                    "fingerprint": fingerprint_for_item(
                        "misrouted_review",
                        identifier or str(issue_id),
                        assignee_agent_id,
                    ),
                    "kind": "misrouted_review",
                    "severity": "medium",
                    "owner": agent_name,
                    "issue_identifier": identifier,
                    "title": title,
                    "summary": "in_review인데 agent만 할당되어 있다 (사람 reviewer 없음)",
                    "evidence": {
                        "assignee_agent_id": assignee_agent_id,
                        "agent_name": agent_name,
                        "updated_at": issue.get("updatedAt"),
                    },
                    "recommended_action": "사람 reviewer 할당 또는 상태 재조정",
                }
            )

    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        heartbeat = (agent.get("runtimeConfig") or {}).get("heartbeat")
        if not isinstance(heartbeat, dict) or heartbeat.get("enabled") is not True:
            continue
        interval_sec = heartbeat.get("intervalSec")
        try:
            interval_seconds = float(interval_sec)
        except (TypeError, ValueError):
            continue
        if interval_seconds <= 0:
            continue
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue

        latest_reference: datetime | None = None
        heartbeat_rows = snapshot.get("heartbeat_runs", {}).get(agent_id, [])
        if isinstance(heartbeat_rows, list) and heartbeat_rows:
            latest_reference = _latest_timer_run_started_at(heartbeat_rows)
        if latest_reference is None:
            latest_reference = parse_timestamp(agent.get("lastHeartbeatAt"))
            used_fallback = latest_reference is not None
        else:
            used_fallback = False
        if latest_reference is None:
            continue
        active_issues = active_issues_by_assignee.get(agent_id)
        if not active_issues:
            continue
        context_issue = max(
            active_issues,
            key=lambda row: (
                parse_timestamp(row.get("updatedAt"))
                or parse_timestamp(row.get("createdAt"))
                or datetime.min.replace(tzinfo=UTC)
            ),
        )
        age = reference_now - latest_reference
        if age <= timedelta(seconds=interval_seconds * heartbeat_threshold_multiplier):
            continue
        if used_fallback:
            warnings.append(
                f"heartbeat_missed used agent.lastHeartbeatAt fallback for agent {agent_id}"
            )

        owner = str(agent.get("name") or agent_id)
        context_issue_identifier = str(context_issue.get("identifier") or "") or None
        context_issue_title = str(
            context_issue.get("title") or f"{owner} heartbeat missed"
        )
        items.append(
            {
                "fingerprint": fingerprint_for_item(
                    "heartbeat_missed",
                    owner,
                    latest_reference.isoformat(),
                ),
                "kind": "heartbeat_missed",
                "severity": "high" if owner in {"CEO", "CTO"} else "medium",
                "owner": owner,
                "issue_identifier": context_issue_identifier,
                "title": context_issue_title,
                "summary": "expected timer heartbeat보다 마지막 실행이 오래됐다",
                "evidence": {
                    "last_heartbeat_at": latest_reference.isoformat(),
                    "age_seconds": round(age.total_seconds(), 1),
                    "interval_sec": interval_seconds,
                },
                "recommended_action": f"{owner} 상태 확인 또는 수동 heartbeat 검토",
            }
        )

    return items, warnings


def build_failure_payload(
    *,
    error: CliProbeError,
    runtime: RuntimeConfig | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "error_code": error.code,
        "message": error.message,
        "remediation": error.remediation,
    }
    if runtime is not None:
        payload["debug"] = {
            "api_base": runtime.api_base,
            "context_path": str(runtime.context_path),
            "auth_store_path": str(runtime.auth_store_path),
        }
    if error.debug:
        payload.setdefault("debug", {}).update(error.debug)
    return payload


def build_success_payload(
    *,
    launcher: CliLauncher,
    runtime: RuntimeConfig,
    snapshot: dict[str, Any],
    items: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    heartbeat_runs = snapshot.get("heartbeat_runs", {})
    heartbeat_count = sum(
        len(rows) for rows in heartbeat_runs.values() if isinstance(rows, list)
    )
    return {
        "success": True,
        "generated_at": now_local().isoformat(),
        "source": {
            "mode": "paperclip_cli_probe",
            "cli_command": launcher.display,
            "api_base": runtime.api_base,
            "company_id": runtime.company_id,
        },
        "probes": {
            "issues": {"count": len(snapshot.get("issues", []))},
            "approvals": {"count": len(snapshot.get("approvals", []))},
            "agents": {"count": len(snapshot.get("agents", []))},
            "heartbeat_runs": {
                "enabled": bool(heartbeat_runs),
                "count": heartbeat_count,
                "via": "api" if heartbeat_runs else "agent_lastHeartbeatAt",
            },
        },
        "items": items,
        "warnings": warnings,
    }


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    runtime = resolve_runtime_config(
        api_base_override=getattr(args, "api_base", None),
        company_id_override=getattr(args, "company_id", None),
        default_company_id=getattr(args, "default_company_id", None),
    )

    try:
        launcher = resolve_cli_launcher()
    except CliProbeError as exc:
        emit(
            build_failure_payload(error=exc, runtime=runtime),
            as_json=getattr(args, "json", False),
        )
        return 1

    if not runtime.api_key:
        failure = build_failure_payload(
            error=CliProbeError(
                code="auth_bootstrap_required",
                message="Paperclip CLI auth is not bootstrapped for non-interactive execution",
                remediation=[
                    "Run one interactive CLI command locally to seed ~/.paperclip/auth.json",
                    "or provide PAPERCLIP_API_KEY to n8n via environment or credential bridge",
                ],
            ),
            runtime=runtime,
        )
        emit(failure, as_json=getattr(args, "json", False))
        return 0

    try:
        snapshot, snapshot_warnings = collect_raw_snapshot(
            launcher,
            runtime,
            heartbeat_run_limit=max(1, int(args.heartbeat_run_limit)),
        )
        items, derivation_warnings = derive_boss_queue_items(
            snapshot,
            include_backlog_unassigned=bool(args.include_backlog_unassigned),
            heartbeat_threshold_multiplier=float(args.heartbeat_threshold_multiplier),
        )
        payload = build_success_payload(
            launcher=launcher,
            runtime=runtime,
            snapshot=snapshot,
            items=items,
            warnings=[*snapshot_warnings, *derivation_warnings],
        )
        emit(payload, as_json=bool(args.json))
        return 0
    except CliProbeError as exc:
        payload = build_failure_payload(error=exc, runtime=runtime)
        emit(payload, as_json=bool(args.json))
        return 0 if exc.code in NON_FATAL_ERROR_CODES else 1
    except Exception as exc:  # noqa: BLE001
        payload = build_failure_payload(
            error=CliProbeError(
                code="unexpected_error",
                message=str(exc),
            ),
            runtime=runtime,
        )
        emit(payload, as_json=bool(args.json))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
