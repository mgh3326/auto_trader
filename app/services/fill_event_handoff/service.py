"""Pure orchestration for fill evidence handoff; no trading or model calls."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.session_context import SessionContextAppendEntry
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.session_context import SessionContextService

from .state import HandoffState

KST = ZoneInfo("Asia/Seoul")
DEDUP_WINDOW = timedelta(hours=24)
PANE_LABEL = re.compile(r"^opa-(crypto|kr|us|nxt)(?:-|$)")
REP_SCHEDULE: dict[str, tuple[tuple[int, int, str], ...]] = {
    "kr": (
        (7, 15, "nxt-prep"),
        (7, 55, "nxt-open"),
        (9, 5, "0905"),
        (11, 30, "1130"),
        (14, 30, "1430"),
        (15, 50, "nxt-eve"),
    ),
    "us": ((22, 35, "us-2235"),),
    "crypto": (
        (2, 20, "crypto-0220"),
        (8, 20, "crypto-0820"),
        (14, 20, "crypto-1420"),
        (20, 20, "crypto-2020"),
    ),
}
REP_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class HandoffConfig:
    state_dir: Path
    herdr_targets: tuple[str, ...] = ()
    kick_enabled: bool = False
    kick_cooldown_seconds: int = 3600
    kick_deployments: Mapping[str, str] | None = None
    prefect_api_url: str | None = None
    discord_webhook: str | None = None
    since_ledger_id: int | None = None
    dry_run: bool = False


def dedupe_key(fill: Mapping[str, Any]) -> str:
    return "|".join(
        str(fill[key])
        for key in ("broker", "broker_order_id", "side", "filled_qty", "filled_price")
    )


def _money(value: str) -> str:
    return format(Decimal(value).normalize(), "f")


def handoff_text(fill: Mapping[str, Any]) -> tuple[str, str]:
    direction = "매수" if fill["side"] == "buy" else "매도"
    flow = "투입" if fill["side"] == "buy" else "해제"
    title = f"{fill['symbol']} {direction} 체결 {_money(fill['filled_qty'])}@{_money(fill['filled_price'])} — {fill['currency']} {_money(fill['filled_notional'])} {flow}, 재배치·잔여주문 판단 미결"
    filled_at = (
        datetime.fromisoformat(str(fill["filled_at"])).astimezone(KST).isoformat()
    )
    body = (
        f"계좌모드: {fill['account_mode']}; venue: {fill['venue']}; KST 체결시각: {filled_at}; "
        f"brokerOrderId: {fill['broker_order_id']}. 이 항목을 읽은 rep는 판단 결과(재배치/보류/사유)를 "
        "같은 refs로 decision 엔트리로 닫는다."
    )
    return title, body


def _agents(payload: str) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        result = data.get("result", data)
        if isinstance(result, dict) and isinstance(result.get("agents"), list):
            return [item for item in result["agents"] if isinstance(item, dict)]
    return []


def live_panes(market: str, payload: str) -> list[str]:
    panes: list[str] = []
    for agent in _agents(payload):
        label = str(
            (agent.get("agent_session") or {}).get("label") or agent.get("name") or ""
        )
        matched = PANE_LABEL.match(label)
        mapped = (
            "kr"
            if matched and matched.group(1) == "nxt"
            else (matched.group(1) if matched else None)
        )
        status = str(agent.get("agent_status") or agent.get("status") or "")
        pane = agent.get("pane_id")
        if mapped == market and status in {"idle", "working"} and isinstance(pane, str):
            panes.append(pane)
    return panes


def _composer_is_empty(text: str) -> bool:
    for line in reversed(text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("❯"):
            return not stripped.removeprefix("❯").strip()
    return False


def _submission_state(text: str, prompt: str) -> str:
    lowered = text.lower()
    if "[pasted text" in lowered or "prompt_unsent" in lowered:
        return "unsent"
    if "prompt_submitted" in lowered or "esc to interrupt" in lowered:
        return "submitted"
    if prompt in text:
        return "submitted" if _composer_is_empty(text) else "unsent"
    return "unknown"


def in_regular_rep_window(market: str, now: datetime) -> bool:
    local = now.astimezone(KST)
    for hour, minute, _ in REP_SCHEDULE[market]:
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= local < start + REP_WINDOW:
            return True
    return False


def next_rep(market: str, now: datetime) -> str:
    local = now.astimezone(KST)
    for hour, minute, rep in REP_SCHEDULE[market]:
        if local < local.replace(hour=hour, minute=minute, second=0, microsecond=0):
            return rep
    return REP_SCHEDULE[market][0][2]


class FillHandoffRunner:
    def __init__(
        self,
        config: HandoffConfig,
        *,
        command: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
        | None = None,
        now: Callable[[], datetime] | None = None,
        http_post: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self.config, self.command = config, command or self._command
        self.now, self.http_post = now or (lambda: datetime.now(UTC)), http_post

    @staticmethod
    def _command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv), text=True, capture_output=True, check=False, timeout=30
        )

    def _target_command(self, target: str, args: Sequence[str]) -> list[str]:
        kind, _, rest = target.partition(":")
        if kind == "local" and rest:
            return ["herdr", *args]
        if kind == "ssh" and ":" in rest:
            host, _, _workspace = rest.partition(":")
            return ["ssh", host, "herdr " + " ".join(args)]
        raise ValueError("invalid FILL_HANDOFF_HERDR_TARGETS target")

    def _submit(self, target: str, pane: str, prompt: str) -> bool:
        def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
            return self.command(self._target_command(target, args))

        try:
            sent = run(("agent", "prompt", pane, prompt))
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        if sent.returncode:
            return False
        first = run(("agent", "read", pane, "--lines", "10"))
        state = (
            _submission_state(first.stdout, prompt)
            if first.returncode == 0
            else "unknown"
        )
        if state == "submitted":
            return True
        if state != "unsent":
            return False
        if run(("agent", "send-keys", pane, "return")).returncode:
            return False
        second = run(("agent", "read", pane, "--lines", "10"))
        return (
            second.returncode == 0
            and _submission_state(second.stdout, prompt) == "submitted"
        )

    async def _notify(
        self, fill: Mapping[str, Any], *, pushed: int, kicked: bool
    ) -> None:
        if not self.config.discord_webhook:
            return
        content = (
            f"{fill['symbol']} {fill['side']} {fill['currency']} {fill['filled_notional']} "
            f"— session_context 적재 / 푸시 {pushed}건 / kick {'yes' if kicked else 'no'}"
        )
        request = urllib.request.Request(
            self.config.discord_webhook,
            data=json.dumps({"content": content}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            await asyncio.to_thread(urllib.request.urlopen, request, timeout=10)
        except Exception:  # noqa: BLE001 - notification is strictly best effort
            return

    async def _kick(self, fill: Mapping[str, Any], state: dict[str, Any]) -> str | None:
        if (
            not self.config.kick_enabled
            or not self.config.prefect_api_url
            or not self.config.kick_deployments
            or not self.http_post
        ):
            return None
        market, now = str(fill["market"]), self.now()
        if in_regular_rep_window(market, now):
            return None
        previous = float(state["cooldowns"].get(market, 0))
        if now.timestamp() - previous < self.config.kick_cooldown_seconds:
            return None
        name = self.config.kick_deployments.get(market)
        if not name:
            return None
        deployments = await self.http_post(
            f"{self.config.prefect_api_url.rstrip('/')}/api/deployments/filter",
            {"deployments": {"name": {"any_": [name]}}},
        )
        items = deployments.get("items", [])
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or not isinstance(items[0].get("id"), str)
        ):
            return None
        result = await self.http_post(
            f"{self.config.prefect_api_url.rstrip('/')}/api/deployments/{items[0]['id']}/create_flow_run",
            {
                "parameters": {
                    "rep": next_rep(market, now),
                    "date_tag": f"{now.astimezone(KST):%Y%m%d}-fill{fill['ledger_id']}",
                }
            },
        )
        flow_run_id = result.get("id")
        if isinstance(flow_run_id, str):
            state["cooldowns"][market] = now.timestamp()
            return flow_run_id
        return None

    async def run(self, db: Any) -> dict[str, int]:
        repo = ExecutionLedgerRepository(db)
        with HandoffState(self.config.state_dir) as locked:
            state = locked.data
            outcome = {"durable": 0, "pushed": 0, "kicked": 0}
            if locked.is_new:
                if self.config.since_ledger_id is None:
                    # An empty state directory is an installation, not an
                    # instruction to replay the historical ledger.  Seed to
                    # its high-water mark and let the next fill be the first
                    # operator handoff.
                    state["watermark"] = await repo.max_ledger_id()
                    if not self.config.dry_run:
                        locked.save()
                    return outcome
                state["watermark"] = self.config.since_ledger_id
            now_epoch = self.now().timestamp()
            state["seen"] = {
                key: value
                for key, value in state["seen"].items()
                if isinstance(value, (int, float))
                and now_epoch - value < DEDUP_WINDOW.total_seconds()
            }
            rows = await repo.list_recent_fills_for_triage(
                after_id=int(state["watermark"]), source=None, limit=500
            )
            for row in rows:
                fill = sanitize_fill(row)
                key, now = dedupe_key(fill), self.now()
                seen_at = float(state["seen"].get(key, 0))
                if now.timestamp() - seen_at < DEDUP_WINDOW.total_seconds():
                    state["watermark"] = max(
                        int(state["watermark"]), int(fill["ledger_id"])
                    )
                    continue
                service = SessionContextService(db)
                context_row = await service.get_open_question_for_event_key(
                    str(fill["event_key"])
                )
                if context_row is None:
                    title, body = handoff_text(fill)
                    entry = SessionContextAppendEntry(
                        market=fill["market"],
                        entry_type="open_question",
                        title=title,
                        body=body,
                        refs={
                            "event_key": fill["event_key"],
                            "ledger_id": fill["ledger_id"],
                            "correlation_id": fill["correlation_id"],
                            "symbols": [fill["symbol"]],
                            "broker_order_id": fill["broker_order_id"],
                            "side": fill["side"],
                            "filled_notional": fill["filled_notional"],
                            "currency": fill["currency"],
                            "fill_handoff": "v1",
                        },
                        created_by="fill-event-handoff",
                        session_label="fill-handoff",
                    )
                    if self.config.dry_run:
                        context_row = None
                    else:
                        context_row = (await service.append_entries([entry]))[0]
                        await db.commit()
                        outcome["durable"] += 1
                if self.config.dry_run:
                    continue
                state["seen"][key] = now.timestamp()
                pushed = 0
                prompt = f"체결 인계: {fill['symbol']} {fill['side']} {fill['filled_qty']}@{fill['filled_price']} ({fill['currency']} {fill['filled_notional']}). briefing.session_context의 fill_handoff=v1 open_question을 같은 refs의 decision으로 닫아라."
                all_panes: list[tuple[str, str]] = []
                discovery_complete = True
                for target in self.config.herdr_targets:
                    try:
                        listed = self.command(
                            self._target_command(target, ("agent", "list"))
                        )
                    except (OSError, subprocess.SubprocessError, ValueError):
                        discovery_complete = False
                        continue
                    if listed.returncode == 0:
                        all_panes.extend(
                            (target, pane)
                            for pane in live_panes(str(fill["market"]), listed.stdout)
                        )
                    else:
                        discovery_complete = False
                for target, pane in all_panes:
                    if self._submit(target, pane, prompt):
                        pushed += 1
                outcome["pushed"] += pushed
                flow_run_id = None
                if not all_panes and discovery_complete:
                    try:
                        flow_run_id = await self._kick(fill, state)
                    except Exception:  # noqa: BLE001 - durable context remains canonical
                        flow_run_id = None
                    if flow_run_id:
                        outcome["kicked"] += 1
                        if context_row is not None:
                            await service.append_fill_handoff_kick_result(
                                entry_id=context_row.id, flow_run_id=flow_run_id
                            )
                            await db.commit()
                await self._notify(fill, pushed=pushed, kicked=flow_run_id is not None)
                state["watermark"] = max(
                    int(state["watermark"]), int(fill["ledger_id"])
                )
                locked.save()
            locked.save()
            return outcome
