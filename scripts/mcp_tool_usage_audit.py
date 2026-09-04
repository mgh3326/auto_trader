#!/usr/bin/env python3
"""Produce a fail-closed, Sentry-backed audit of the MCP tool surface.

The registry is discovered by executing every profile registrar against an
in-memory recorder.  No MCP server, database session, broker client, or
transport is started.  Usage is queried only from Sentry's spans dataset;
operator transcripts are intentionally not an input to this report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

SENTRY_ORG = "mgh3326-daum"
SENTRY_EVENTS_URL = f"https://sentry.io/api/0/organizations/{SENTRY_ORG}/events/"
SENTRY_QUERY = 'transaction:"tools/call *"'
REQUEST_TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_OUTPUT_DIR = Path("docs/mcp-tool-usage-audit-20260903-data")
DEFAULT_REPORT = Path("docs/mcp-tool-usage-audit-20260903.md")
DEFAULT_LANE_OUTPUT_DIR = Path("lane-allowlists.draft")
_TOOL_PREFIX = "tools/call "


class AuditError(RuntimeError):
    """A required audit input was unavailable or malformed."""


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    module: str
    registration_method: str


@dataclass(frozen=True)
class LaneSpec:
    """A declared consumer lane, its physical MCP profile, and source files."""

    name: str
    profiles: tuple[str, ...]
    source_globs: tuple[str, ...]
    server_names: tuple[str, ...] = ()


# The two server mappings below are evidenced by the KR-B1 runner's NCP
# deployment and the fable workbench's local readonly profile.  Sentry spans
# have no profile or lane tag, so all other lanes intentionally receive no
# guessed server attribution until an endpoint->profile inventory is supplied.
LANE_SPECS: tuple[LaneSpec, ...] = (
    LaneSpec("kr", ("default",), ("CLAUDE.md", "live/CLAUDE.md", "prompts/kr-*.md")),
    LaneSpec("us", ("default",), ("CLAUDE.md", "live/CLAUDE.md", "prompts/us-*.md")),
    LaneSpec(
        "crypto",
        ("crypto", "default"),
        ("CLAUDE.md", "live/CLAUDE.md", "prompts/crypto-*.md"),
    ),
    LaneSpec(
        "orch-live", ("default",), ("CLAUDE.md", "live/CLAUDE.md", "prompts/orch-*.md")
    ),
    LaneSpec(
        "orch-mock",
        ("hermes-paper-kis", "kiwoom", "us-paper"),
        ("CLAUDE.md", "mock/CLAUDE.md", "prompts/mock-*.md"),
    ),
    LaneSpec(
        "claude-mock",
        ("hermes-paper-kis", "kiwoom", "us-paper"),
        ("CLAUDE.md", "mock/CLAUDE.md", "prompts/mock-*.md"),
    ),
    LaneSpec(
        "krb1-cycle",
        ("kiwoom",),
        (
            "CLAUDE.md",
            "mock/CLAUDE.md",
            "prompts/krb1-cycle.md",
            "runners/krb1_headless.py",
            "runners/cycle_runner.py",
            "operator_contract.yaml",
        ),
        ("vm-naver-20260820095006",),
    ),
    LaneSpec("shadow-crypto", ("default",), ()),
    LaneSpec(
        "fill-handoff",
        ("crypto", "default"),
        ("CLAUDE.md", "live/CLAUDE.md", "prompts/crypto-fill-triage.md"),
    ),
    LaneSpec(
        "watch-alert-relay",
        ("default",),
        (
            "CLAUDE.md",
            "live/CLAUDE.md",
            "runners/watch_alert_relay.py",
            "docs/watch-alert-relay.md",
        ),
    ),
    LaneSpec("fable-workbench", ("analysis_readonly",), (), ("mbp-server",)),
)


class _RecordingMCP:
    """Minimal ``FastMCP`` registration target; it never invokes a tool."""

    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        explicit_name = kwargs.get("name")
        direct_function = args[0] if args and callable(args[0]) else None
        if explicit_name is None and args and isinstance(args[0], str):
            explicit_name = args[0]
        method = "mcp.tool direct" if direct_function else "mcp.tool decorator"

        def register(function: Any) -> Any:
            name = explicit_name or getattr(function, "__name__", None)
            if not isinstance(name, str) or not name:
                raise AuditError("registrar supplied a tool without a name")
            unwrapped = function
            while hasattr(unwrapped, "__wrapped__"):
                unwrapped = unwrapped.__wrapped__
            module = str(getattr(unwrapped, "__module__", "<unknown>"))
            tool = RegisteredTool(name=name, module=module, registration_method=method)
            existing = self.tools.get(name)
            if existing is not None and existing != tool:
                raise AuditError(f"duplicate registration for {name}")
            self.tools[name] = tool
            return function

        if direct_function is not None:
            return register(direct_function)
        return register


def _set_registry_only_environment() -> None:
    """Supply harmless validation placeholders only when local env is absent."""
    placeholders = {
        "KIS_APP_KEY": "registry-audit",
        "KIS_APP_SECRET": "registry-audit",
        "OPENDART_API_KEY": "registry-audit",
        "DATABASE_URL": "postgresql+asyncpg://registry:registry@localhost/registry",
        "UPBIT_ACCESS_KEY": "registry-audit",
        "UPBIT_SECRET_KEY": "registry-audit",
        "SECRET_KEY": "RegistryAuditKey0123456789Aa0123456789",
    }
    for key, value in placeholders.items():
        os.environ.setdefault(key, value)


def collect_registry() -> dict[str, dict[str, Any]]:
    """Mechanically execute every profile registrar with all feature gates on.

    Executing the registrars covers nested registrar calls and profile proxies,
    which a count of ``@mcp.tool`` lines does not.  Gates are changed only on
    the process-local settings singleton and always restored before returning.
    """
    _set_registry_only_environment()
    from app.core.config import settings
    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling.registry import register_all_tools

    original: dict[str, Any] = {}
    for name in settings.__class__.model_fields:
        if name.lower().endswith("enabled"):
            original[name] = getattr(settings, name)
            setattr(settings, name, True)

    by_name: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    try:
        for profile in McpProfile:
            recorder = _RecordingMCP()
            try:
                register_all_tools(recorder, profile=profile)
            except Exception as exc:  # Registry evidence must be all-or-nothing.
                failures.append(f"{profile.value}: {type(exc).__name__}")
                continue
            for name, registered in recorder.tools.items():
                entry = by_name.setdefault(
                    name,
                    {
                        "profiles": [],
                        "module": registered.module,
                        "registration_method": registered.registration_method,
                        "mutation": is_mutation_tool(name),
                        "mutation_kinds": mutation_kinds(name),
                    },
                )
                if entry["module"] != registered.module:
                    entry["module"] = "multiple"
                if entry["registration_method"] != registered.registration_method:
                    entry["registration_method"] = "multiple"
                entry["profiles"].append(profile.value)
    finally:
        for name, value in original.items():
            setattr(settings, name, value)
    if failures:
        raise AuditError("registry extraction failed: " + "; ".join(failures))
    for entry in by_name.values():
        entry["profiles"].sort()
    return dict(sorted(by_name.items()))


def mutation_kinds(tool_name: str) -> list[str]:
    """Conservative labels for review; they do not alter any MCP capability."""
    name = tool_name.lower()
    kinds: list[str] = []
    action_words = {
        "activate",
        "add",
        "advance",
        "append",
        "authorize",
        "cancel",
        "confirm",
        "create",
        "decide",
        "delete",
        "expire",
        "ingest",
        "kill",
        "modify",
        "place",
        "reconcile",
        "register",
        "reject",
        "reset",
        "save",
        "set",
        "submit",
        "update",
        "void",
    }
    is_write = bool(action_words.intersection(name.split("_")))
    if name.startswith("order_proposal_"):
        if is_write:
            kinds.append("proposal")
    elif "watch" in name and is_write:
        kinds.append("watch")
    elif (
        name.startswith("investment_report_") or name.startswith("investment_stage_")
    ) and is_write:
        kinds.append("report")
    if is_write and ("order" in name or name.startswith("paper_validation_")):
        kinds.append("order")
    if not kinds and is_write:
        kinds.append("persistence")
    return sorted(set(kinds))


def is_mutation_tool(tool_name: str) -> bool:
    return bool(mutation_kinds(tool_name))


def _read_sentry_token(token_file: Path) -> str:
    try:
        lines = token_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError(f"cannot read Sentry token file: {token_file}") from exc
    if len(lines) != 1 or "=" not in lines[0]:
        raise AuditError("Sentry token file must contain exactly one KEY=value line")
    _, token = lines[0].split("=", 1)
    if not token.strip():
        raise AuditError("Sentry token file has an empty token")
    return token.strip()


def _sentry_params(start: datetime, end: datetime) -> list[tuple[str, str]]:
    return [
        ("dataset", "spans"),
        ("project", "-1"),
        ("start", start.isoformat()),
        ("end", end.isoformat()),
        ("query", SENTRY_QUERY),
        ("per_page", "100"),
        ("field", "transaction"),
        ("field", "server_name"),
        ("field", "count()"),
        ("field", "p50(span.duration)"),
        ("field", "max(timestamp)"),
    ]


def fetch_sentry_rows(
    *,
    token: str,
    start: datetime,
    end: datetime,
    request_get: Callable[..., Any] = requests.get,
) -> list[dict[str, Any]]:
    """Fetch all aggregate pages, retrying only Sentry's explicit rate limit."""
    url = SENTRY_EVENTS_URL
    params: Sequence[tuple[str, str]] | None = _sentry_params(start, end)
    rows: list[dict[str, Any]] = []
    for _page in range(10_000):
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = request_get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise AuditError(
                    f"Sentry request failed: {type(exc).__name__}"
                ) from exc
            if response.status_code != 429:
                break
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise AuditError("Sentry rate limit remained after retries")
            try:
                delay = float(response.headers.get("Retry-After", "1"))
            except (TypeError, ValueError):
                delay = 1.0
            time.sleep(max(0.0, min(delay, 30.0)))
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AuditError(f"Sentry response unusable: {type(exc).__name__}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise AuditError("Sentry response has no usable aggregate data")
        rows.extend(data)
        # Sentry emits a ``rel=next`` cursor even at the final page, but marks
        # it ``results=\"false\"``.  ``requests.Response.links`` discards that
        # attribute, so inspecting only ``links['next']`` would loop forever.
        link_header = str(response.headers.get("Link", ""))
        next_url = None
        for candidate in link_header.split(","):
            if 'rel="next"' in candidate and 'results="true"' in candidate:
                matched = re.search(r"<([^>]+)>", candidate)
                if matched:
                    next_url = matched.group(1)
                break
        if not next_url:
            return rows
        url, params = str(next_url), None
    raise AuditError("Sentry pagination exceeded 10,000 pages")


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tool_from_transaction(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith(_TOOL_PREFIX):
        return None
    name = value.removeprefix(_TOOL_PREFIX).strip()
    return name or None


def _parse_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat()
        )
    except ValueError:
        return None


def summarize_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate Sentry group rows by tool, preserving server and p50 evidence."""
    totals: dict[str, int] = defaultdict(int)
    weighted_p50: dict[str, float] = defaultdict(float)
    p50_count: dict[str, int] = defaultdict(int)
    callers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    last_called: dict[str, str] = {}
    for row in rows:
        tool = _tool_from_transaction(row.get("transaction"))
        count = _number(row.get("count()"))
        if tool is None or count is None or count < 0:
            continue
        calls = int(count)
        totals[tool] += calls
        server = row.get("server_name")
        callers[tool][
            server if isinstance(server, str) and server else "<missing>"
        ] += calls
        p50 = _number(row.get("p50(span.duration)"))
        if p50 is not None:
            weighted_p50[tool] += p50 * calls
            p50_count[tool] += calls
        timestamp = _parse_timestamp(row.get("max(timestamp)"))
        if timestamp is not None and timestamp > last_called.get(tool, ""):
            last_called[tool] = timestamp
    return {
        tool: {
            "calls_total": totals[tool],
            "last_called_at": last_called.get(tool),
            "callers": [
                {"server_name": server, "calls": calls}
                for server, calls in sorted(callers[tool].items())
            ],
            "p50_ms": round(weighted_p50[tool] / p50_count[tool], 3)
            if p50_count[tool]
            else None,
        }
        for tool in sorted(totals)
    }


def collect_usage_90d(
    tool_names: Iterable[str],
    *,
    token: str,
    now: datetime,
    request_get: Callable[..., Any] = requests.get,
) -> dict[str, dict[str, Any]]:
    """Query total, last-30-day, and weekly Sentry aggregates fail-closed."""
    end = now.astimezone(UTC)
    start = end - timedelta(days=90)
    last_30_start = end - timedelta(days=30)
    try:
        ninety = summarize_rows(
            fetch_sentry_rows(
                token=token, start=start, end=end, request_get=request_get
            )
        )
        last_30 = summarize_rows(
            fetch_sentry_rows(
                token=token, start=last_30_start, end=end, request_get=request_get
            )
        )
        weeks: list[dict[str, Any]] = []
        week_start = start
        while week_start < end:
            week_end = min(week_start + timedelta(days=7), end)
            weekly = summarize_rows(
                fetch_sentry_rows(
                    token=token, start=week_start, end=week_end, request_get=request_get
                )
            )
            weeks.append(
                {
                    "start": week_start.isoformat(),
                    "end": week_end.isoformat(),
                    "tools": weekly,
                }
            )
            week_start = week_end
    except AuditError as exc:
        return {
            tool: {
                "measurement_status": "unknown",
                "measurement_error": str(exc),
                "calls_total": None,
                "calls_last_30d": None,
                "last_called_at": None,
                "callers": [],
                "p50_ms": None,
                "weeks": [],
            }
            for tool in sorted(tool_names)
        }
    result: dict[str, dict[str, Any]] = {}
    for tool in sorted(tool_names):
        total = ninety.get(tool, {})
        recent = last_30.get(tool, {})
        result[tool] = {
            "measurement_status": "ok",
            "calls_total": total.get("calls_total", 0),
            "calls_last_30d": recent.get("calls_total", 0),
            "last_called_at": total.get("last_called_at"),
            "callers": total.get("callers", []),
            "p50_ms": total.get("p50_ms"),
            "weeks": [
                {
                    "start": week["start"],
                    "end": week["end"],
                    "calls": week["tools"].get(tool, {}).get("calls_total", 0),
                    "callers": week["tools"].get(tool, {}).get("callers", []),
                }
                for week in weeks
            ],
        }
    return result


def _iter_text_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return (
        path
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_size <= 2_000_000
    )


def find_exact_references(
    tool_names: Iterable[str],
    *,
    operator_repo: Path,
    repo_root: Path,
) -> dict[str, dict[str, list[str]]]:
    """Find identifier-token references; catalog listings do not count as usage."""
    names = sorted(tool_names, key=len, reverse=True)
    patterns = {
        name: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        for name in names
    }
    groups = {
        "prompt_refs": [
            operator_repo / "prompts",
            operator_repo / "CLAUDE.md",
            operator_repo / ".claude" / "commands",
            operator_repo / "runners",
        ],
        "runbook_refs": [repo_root / "docs" / "runbooks"],
        "code_refs": [
            repo_root / "scripts",
            repo_root / "app" / "flows",
            repo_root / "app" / "tasks",
        ],
    }
    found = {name: {key: [] for key in groups} for name in names}
    for kind, roots in groups.items():
        for root in roots:
            for path in _iter_text_files(root):
                try:
                    lines = path.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, start=1):
                    for name, pattern in patterns.items():
                        if pattern.search(line):
                            found[name][kind].append(f"{path}:{line_number}")
    return found


def _lane_source_paths(operator_repo: Path, globs: Sequence[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in globs:
        paths.update(path for path in operator_repo.glob(pattern) if path.is_file())
    return sorted(paths)


def _tools_in_files(tool_names: Iterable[str], paths: Iterable[Path]) -> set[str]:
    patterns = {
        name: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        for name in tool_names
    }
    found: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.update(name for name, pattern in patterns.items() if pattern.search(text))
    return found


def build_lane_allowlists(
    registry: Mapping[str, Mapping[str, Any]],
    usage: Mapping[str, Mapping[str, Any]],
    *,
    operator_repo: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """Union exact prompt tokens and only explicitly mapped Sentry callers."""
    result: dict[str, dict[str, str]] = {}
    for spec in LANE_SPECS:
        eligible = {
            tool
            for tool, item in registry.items()
            if any(profile in item["profiles"] for profile in spec.profiles)
        }
        prompt = _tools_in_files(
            registry, _lane_source_paths(operator_repo, spec.source_globs)
        )
        sentry = {
            tool
            for tool, value in usage.items()
            if any(
                caller.get("server_name") in spec.server_names
                for caller in value["callers"]
            )
            and any(profile in registry[tool]["profiles"] for profile in spec.profiles)
        }
        prompt.intersection_update(eligible)
        if spec.name == "fable-workbench":
            # Explicit operator direction: the human workbench remains broad,
            # but its observed-zero dead tools are not retained by breadth alone.
            prompt.update(
                tool
                for tool, item in registry.items()
                if "analysis_readonly" in item["profiles"]
                and usage[tool].get("classification") != "D"
            )
        result[spec.name] = {
            tool: "both"
            if tool in prompt and tool in sentry
            else "prompt"
            if tool in prompt
            else "sentry"
            for tool in sorted(prompt | sentry)
        }
    profile_union: dict[str, set[str]] = defaultdict(set)
    for item in registry.values():
        for profile in item["profiles"]:
            profile_union[profile]
    for spec in LANE_SPECS:
        for profile in spec.profiles:
            profile_union[profile].update(result[spec.name])
    unlisted = {
        profile: sorted(
            tool
            for tool, item in registry.items()
            if profile in item["profiles"] and tool not in profile_union[profile]
        )
        for profile in sorted(profile_union)
    }
    return result, unlisted


def write_lane_allowlists(
    lanes: Mapping[str, Mapping[str, str]], output_dir: Path
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for lane, tools in lanes.items():
        content = "# tool\tbasis (prompt|sentry|both)\n" + "".join(
            f"{tool}\t{basis}\n" for tool, basis in sorted(tools.items())
        )
        (output_dir / f"{lane}.txt").write_text(content, encoding="utf-8")


def classify(usage: Mapping[str, Any], references: Mapping[str, Sequence[str]]) -> str:
    if usage.get("measurement_status") != "ok":
        return "U"
    calls_90d = usage.get("calls_total")
    calls_30d = usage.get("calls_last_30d")
    if not isinstance(calls_90d, int) or not isinstance(calls_30d, int):
        return "U"
    if calls_30d > 0:
        return "A"
    if calls_90d > 0:
        return "B"
    if any(
        references.get(key, []) for key in ("prompt_refs", "runbook_refs", "code_refs")
    ):
        return "C"
    return "D"


def render_markdown(
    registry: Mapping[str, Mapping[str, Any]],
    usage: Mapping[str, Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Sequence[str]]],
    lanes: Mapping[str, Mapping[str, str]],
    profile_unlisted: Mapping[str, Sequence[str]],
    *,
    generated_at: datetime,
) -> str:
    rows: list[tuple[str, str]] = []
    profile_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tool, registration in registry.items():
        category = classify(usage[tool], references[tool])
        rows.append((tool, category))
        for profile in registration["profiles"]:
            profile_counts[profile][category] += 1
    lines = [
        "# MCP tool usage audit — 2026-09-03",
        "",
        f"Generated at: `{generated_at.astimezone(UTC).isoformat()}`",
        "",
        '- Usage source of truth: Sentry `spans`, filter `transaction:"tools/call *"`.',
        "- A = live-used (30d); B = seasonal-or-rare (90d only); C = referenced-unused; D = dead; U = unknown.",
        "- Registry extraction executes each profile registrar against an in-memory recorder with feature gates enabled; it starts no server or client.",
        "- NCP `tools/list` byte measurements are intentionally left for orchestration: this audit does not access NCP secrets or hosts.",
        "",
        "## Profile summary",
        "",
        "| Profile | A | B | C | D | U | Total |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for profile, counts in sorted(profile_counts.items()):
        total = sum(counts.values())
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                profile, *(counts.get(k, 0) for k in "ABCDU"), total
            )
        )
    lines.extend(
        [
            "",
            "## Proposed follow-up (no change applied)",
            "",
            "| Profile | Remove candidate (D) | Deprecated-log candidate (B/C) | Prompt-contract review (C) |",
            "|---|---|---|---|",
        ]
    )
    for profile in sorted(profile_counts):
        profile_tools = [
            tool for tool, item in registry.items() if profile in item["profiles"]
        ]
        groups = {
            category: [
                tool
                for tool in profile_tools
                if classify(usage[tool], references[tool]) == category
            ]
            for category in "BCD"
        }
        lines.append(
            f"| {profile} | {', '.join(groups['D']) or '—'} | "
            f"{', '.join(groups['B'] + groups['C']) or '—'} | {', '.join(groups['C']) or '—'} |"
        )
    lines.extend(
        [
            "",
            "## Complete classification",
            "",
            "| Tool | Profiles | Module | Mutation | Class | 90d | 30d | Prompt refs | Runbook refs | Code refs |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for tool, category in rows:
        registered, measured, refs = registry[tool], usage[tool], references[tool]
        mutation = ", ".join(registered["mutation_kinds"]) or "no"
        calls_90 = (
            measured["calls_total"]
            if measured["calls_total"] is not None
            else "unknown"
        )
        calls_30 = (
            measured["calls_last_30d"]
            if measured["calls_last_30d"] is not None
            else "unknown"
        )
        lines.append(
            f"| {tool} | {', '.join(registered['profiles'])} | {registered['module']} | {mutation} | {category} | {calls_90} | {calls_30} | "
            f"{len(refs['prompt_refs'])} | {len(refs['runbook_refs'])} | {len(refs['code_refs'])} |"
        )
    unlisted_count = len(
        {tool for tools in profile_unlisted.values() for tool in tools}
    )
    lines.extend(
        [
            "",
            "## Lane allowlist draft and derivation design",
            "",
            "Each `lane-allowlists.draft/<lane>.txt` line is `tool<TAB>basis`; basis is exact prompt token, explicitly mapped Sentry server evidence, or both. `server_name` does not carry an MCP profile/lane tag, so only KR-B1 (NCP) and fable workbench (Mac readonly) have Sentry attribution in this draft; all other lanes deliberately avoid guessed attribution.",
            "",
            "| Lane | Profiles | Draft tools |",
            "|---|---|---:|",
        ]
    )
    lane_profiles = {spec.name: ", ".join(spec.profiles) for spec in LANE_SPECS}
    for lane, tools in sorted(lanes.items()):
        lines.append(f"| {lane} | {lane_profiles[lane]} | {len(tools)} |")
    lines.extend(
        [
            "",
            "For a future generator, commit reviewed `config/mcp_lane_allowlists/*.txt`; generate profile registration from the union of its assigned lane manifests; make CI red when a registered tool is in no assigned lane. Migration: (1) commit manifests and report-only diff, (2) two-week deprecation warning on B/C and unlisted calls, (3) remove MCP registration, (4) separately delete unreachable code only after scripts/flows have been checked. MCP deregistration is not service-function deletion.",
            "",
            "| Profile | D immediate-review | A/B unlisted (trace lane before removal) | C unlisted (contract review) |",
            "|---|---|---|---|",
        ]
    )
    for profile, tools in sorted(profile_unlisted.items()):
        groups = {
            category: [
                tool for tool in tools if usage[tool]["classification"] == category
            ]
            for category in "ABCD"
        }
        lines.append(
            f"| {profile} | {', '.join(groups['D']) or '—'} | "
            f"{', '.join(groups['A'] + groups['B']) or '—'} | "
            f"{', '.join(groups['C']) or '—'} |"
        )
    lines.append(f"UNLISTED={unlisted_count}")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--lane-output-dir", type=Path, default=DEFAULT_LANE_OUTPUT_DIR)
    parser.add_argument(
        "--operator-repo",
        type=Path,
        default=Path.home() / "services" / "auto_trader-operator",
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path.home() / ".config" / "sentry" / "token"
    )
    parser.add_argument(
        "--reuse-usage-json",
        type=Path,
        help="reuse a completed Sentry result without issuing another Sentry query",
    )
    parser.add_argument("--now", help="ISO-8601 end timestamp, for reproducible tests")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(UTC)
        )
        registry = collect_registry()
        if args.reuse_usage_json:
            usage = json.loads(args.reuse_usage_json.read_text(encoding="utf-8"))
            if not isinstance(usage, dict) or set(usage) != set(registry):
                raise AuditError(
                    "reused usage JSON does not exactly match the registry"
                )
        else:
            usage = collect_usage_90d(
                registry, token=_read_sentry_token(args.token_file), now=now
            )
        references = find_exact_references(
            registry, operator_repo=args.operator_repo, repo_root=Path.cwd()
        )
        for tool in registry:
            usage[tool]["classification"] = classify(usage[tool], references[tool])
        lanes, profile_unlisted = build_lane_allowlists(
            registry, usage, operator_repo=args.operator_repo
        )
        _write_json(args.output_dir / "registry.json", registry)
        _write_json(args.output_dir / "usage-90d.json", usage)
        _write_json(args.output_dir / "references.json", references)
        _write_json(args.output_dir / "profile-unlisted.json", profile_unlisted)
        write_lane_allowlists(lanes, args.lane_output_dir)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            render_markdown(
                registry,
                usage,
                references,
                lanes,
                profile_unlisted,
                generated_at=now,
            ),
            encoding="utf-8",
        )
    except (AuditError, OSError, ValueError) as exc:
        print(f"MCP tool usage audit failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
