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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo

from app.schemas.investment_reports import MarketLiteral
from app.schemas.session_context import SessionContextAppendEntry
from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.lane_events import (
    LANE_EVENT_TEXT_LIMIT,
    LaneEventConfig,
    emit_lane_event,
)
from app.services.session_context import SessionContextService

from .kick_filter import (
    DEFAULT_KICK_DAILY_CAP,
    DEFAULT_MIN_POSITION_FRACTION,
    DEFAULT_PARKING_SYMBOLS,
    DEFAULT_SMALL_BUY_NOTIONAL,
    FillPositionFacts,
    KickDecision,
    KickVerdict,
    classify_fill_for_kick,
    classify_without_position,
)
from .state import HandoffState
from .watch_kick import (
    DbWatchKickSource,
    WatchKickCursor,
    classify_watch_for_kick,
    is_tradable_now,
)

KST = ZoneInfo("Asia/Seoul")
DEDUP_WINDOW = timedelta(hours=24)
# session_context open_questions exist per market — the schema Literal is the
# authority for which markets a queued fill can name.
QUEUEABLE_MARKETS = frozenset(get_args(MarketLiteral))
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
WATCH_KICK_BATCH_LIMIT = 500


def _exact_int(value: Any) -> int | None:
    """Exact-integer parse; ``None`` for fractional/bool/other garbage.

    ``int()`` silently truncates floats (``int(0.5) == 0``), which would
    launder a corrupt cursor or event id into a valid-looking replay point.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _watch_row_order_key(row: Mapping[str, Any]) -> tuple[str, int]:
    event_id = _exact_int(row.get("event_id"))
    return str(row.get("delivered_at") or ""), -1 if event_id is None else event_id


def _watch_kick_cursor_from_state(
    state: Mapping[str, Any],
) -> WatchKickCursor | None:
    """Parse the persisted cursor; ``None`` marks corrupt state.

    A corrupt cursor (negative or fractional watermark, unparseable or
    naive ``delivered_at``) must never be fed to ``list_after`` —
    ``id > -1`` or ``id > 0`` after truncation would silently replay the
    delivered backlog as kick candidates.
    """
    watermark = _exact_int(state.get("watch_kick_watermark"))
    if watermark is None:
        return None
    delivered_raw = state.get("watch_kick_delivered_at")
    try:
        cursor = WatchKickCursor(
            None
            if delivered_raw is None
            else datetime.fromisoformat(str(delivered_raw)),
            watermark,
        )
    except (TypeError, ValueError):
        return None
    if cursor.delivered_at is not None and (
        cursor.delivered_at.tzinfo is None
        or cursor.delivered_at.tzinfo.utcoffset(cursor.delivered_at) is None
    ):
        return None
    return cursor


def _advance_watch_kick_cursor(
    current: WatchKickCursor,
    rows: Sequence[Mapping[str, Any]],
    resolved: set[int],
) -> WatchKickCursor:
    """Advance past resolved rows in delivery order; stall before a gap."""
    cursor = current
    for row in rows:
        row_id = int(row["event_id"])
        if row_id not in resolved:
            break
        delivered_at = row.get("delivered_at")
        if delivered_at is None:
            break
        candidate = WatchKickCursor(datetime.fromisoformat(str(delivered_at)), row_id)
        if cursor.delivered_at is None or (
            candidate.delivered_at,
            candidate.event_id,
        ) > (cursor.delivered_at, cursor.event_id):
            cursor = candidate
    return cursor


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
    lane_events: Mapping[str, str] | None = None
    lane_event: LaneEventConfig | None = None
    kick_daily_cap: int = DEFAULT_KICK_DAILY_CAP
    kick_parking_symbols: frozenset[str] | None = None
    kick_small_buy_notional: Mapping[str, Decimal] | None = None
    kick_min_position_fraction: Decimal = DEFAULT_MIN_POSITION_FRACTION

    def __post_init__(self) -> None:
        if self.kick_daily_cap < 0:
            raise ValueError("kick_daily_cap must be non-negative")
        if self.kick_min_position_fraction <= 0:
            raise ValueError("kick_min_position_fraction must be positive")
        parking = (
            DEFAULT_PARKING_SYMBOLS
            if self.kick_parking_symbols is None
            else frozenset(
                str(item).strip().upper()
                for item in self.kick_parking_symbols
                if str(item).strip()
            )
        )
        object.__setattr__(self, "kick_parking_symbols", parking)
        notionals = (
            dict(DEFAULT_SMALL_BUY_NOTIONAL)
            if self.kick_small_buy_notional is None
            else {
                str(k).upper(): Decimal(str(v))
                for k, v in self.kick_small_buy_notional.items()
            }
        )
        if any(not value.is_finite() or value < 0 for value in notionals.values()):
            raise ValueError("kick_small_buy_notional values must be non-negative")
        object.__setattr__(self, "kick_small_buy_notional", notionals)


def dedupe_key(fill: Mapping[str, Any]) -> str:
    return "|".join(
        str(fill[key])
        for key in ("broker", "broker_order_id", "side", "filled_qty", "filled_price")
    )


def _money(value: str) -> str:
    # Display text only — degenerate ledger data must never crash the run
    # before the durable append (e.g. a quantity like 1e9999999 overflows
    # Decimal context arithmetic during normalize()).
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (ArithmeticError, InvalidOperation, ValueError):
        return str(value)[:32]


def handoff_text(fill: Mapping[str, Any]) -> tuple[str, str]:
    direction = "매수" if fill["side"] == "buy" else "매도"
    flow = "투입" if fill["side"] == "buy" else "해제"
    title = f"{fill['symbol']} {direction} 체결 {_money(fill['filled_qty'])}@{_money(fill['filled_price'])} — {fill['currency']} {_money(fill['filled_notional'])} {flow}, 재배치·잔여주문 판단 미결"
    try:
        filled_at = (
            datetime.fromisoformat(str(fill["filled_at"])).astimezone(KST).isoformat()
        )
    except (TypeError, ValueError):
        filled_at = str(fill["filled_at"])
    body = (
        f"계좌모드: {fill['account_mode']}; venue: {fill['venue']}; KST 체결시각: {filled_at}; "
        f"brokerOrderId: {fill['broker_order_id']}. 이 항목을 읽은 rep는 판단 결과(재배치/보류/사유)를 "
        "같은 refs로 decision 엔트리로 닫는다."
    )
    return title, body


def handoff_lane_event_text(fill: Mapping[str, Any]) -> str:
    """Render one shared prompt for lane delivery and the herdr fallback."""
    prompt = (
        f"체결 인계: {fill['symbol']} {fill['side']} {fill['filled_qty']}@{fill['filled_price']} "
        f"({fill['currency']} {fill['filled_notional']}). briefing.session_context의 "
        "fill_handoff=v1 open_question을 같은 refs의 decision으로 닫아라."
    )
    if len(prompt.encode("utf-8")) <= LANE_EVENT_TEXT_LIMIT:
        return prompt
    return (
        f"체결 인계: {fill['symbol']} {fill['side']} {fill['filled_qty']}@"
        f"{fill['filled_price']} (ledger_id={fill['ledger_id']})"
    )


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
        self,
        fill: Mapping[str, Any],
        *,
        pushed: int,
        kicked: bool,
        delivery: Literal["lane_event", "lane_event_duplicate", "herdr", "none"],
    ) -> None:
        if not self.config.discord_webhook:
            return
        content = (
            f"{fill['symbol']} {fill['side']} {fill['currency']} {fill['filled_notional']} "
            f"— session_context 적재 / 푸시 {pushed}건 / kick {'yes' if kicked else 'no'} "
            f"/ delivery {delivery}"
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

    async def _classify_fill(self, fill: Mapping[str, Any], repo: Any) -> KickVerdict:
        """Filter verdict for one fill; missing position facts fail closed.

        Position-free classes (parking, small DCA, malformed) skip the ledger
        read entirely.  Everything else reads the net position strictly before
        the fill and falls back to ``position_read_failed``/``position_
        unproven`` — never a guess.
        """
        early = classify_without_position(
            fill,
            parking_symbols=self.config.kick_parking_symbols or frozenset(),
            small_buy_notional=self.config.kick_small_buy_notional or {},
        )
        if early is not None:
            return early
        try:
            filled_at = datetime.fromisoformat(str(fill["filled_at"]))
        except (KeyError, TypeError, ValueError):
            return KickVerdict(False, "fill_malformed")
        if filled_at.tzinfo is None or filled_at.utcoffset() is None:
            # A naive timestamp cannot be ordered strictly against the
            # ledger — fail closed rather than guessing a timezone.
            return KickVerdict(False, "fill_malformed")
        facts: FillPositionFacts | None
        try:
            qty_before, rows_before = await repo.position_before_fill(
                broker=str(fill["broker"]),
                account_mode=str(fill["account_mode"]),
                venue=str(fill["venue"]),
                instrument_type=str(fill.get("instrument_type")),
                symbol=str(fill["symbol"]),
                currency=str(fill["currency"]),
                filled_at=filled_at,
                ledger_id=int(fill["ledger_id"]),
            )
            facts = FillPositionFacts(
                qty_before=Decimal(str(qty_before)), rows_before=int(rows_before)
            )
        except Exception:  # noqa: BLE001 - a failed read must never guess
            facts = None
        try:
            return classify_fill_for_kick(
                fill,
                facts,
                parking_symbols=self.config.kick_parking_symbols or frozenset(),
                small_buy_notional=self.config.kick_small_buy_notional or {},
                min_position_fraction=self.config.kick_min_position_fraction,
            )
        except Exception:  # noqa: BLE001 - classification must never skip queueing
            return KickVerdict(False, "classification_failed")

    async def _kick(
        self,
        fill: Mapping[str, Any],
        locked: HandoffState,
        verdict: KickVerdict | None,
    ) -> KickDecision:
        """Prefect kick gated by the priority filter, daily cap, and cooldown."""
        return await self._gated_kick(
            market=str(fill["market"]),
            tag=f"fill{fill['ledger_id']}",
            locked=locked,
            verdict=verdict,
        )

    async def _gated_kick(
        self,
        *,
        market: str,
        tag: str,
        locked: HandoffState,
        verdict: KickVerdict | None,
    ) -> KickDecision:
        """Shared kick gate for fills and watch events.

        Every kick — fill (#825) or watch (#865) — funnels through the same
        ``kick_days`` daily-cap counter and ``cooldowns`` map, so both kinds
        together can never exceed ``kick_daily_cap`` per market per KST day.
        """
        if (
            not self.config.kick_enabled
            or not self.config.prefect_api_url
            or not self.config.kick_deployments
            or not self.http_post
        ):
            return KickDecision("queue_only", "kick_not_configured")
        if verdict is None or not verdict.eligible:
            return KickDecision(
                "queue_only", verdict.reason if verdict is not None else "unclassified"
            )
        now = self.now()
        if in_regular_rep_window(market, now):
            return KickDecision("queue_only", "rep_window")
        state = locked.data
        kick_days = state.setdefault("kick_days", {})
        today = f"{now.astimezone(KST):%Y%m%d}"
        day = kick_days.get(market)
        if not isinstance(day, dict) or day.get("date") != today:
            day = {"date": today, "count": 0}
            kick_days[market] = day
        if int(day.get("count") or 0) >= self.config.kick_daily_cap:
            return KickDecision("capped", "daily_cap")
        previous = float(state["cooldowns"].get(market, 0))
        if now.timestamp() - previous < self.config.kick_cooldown_seconds:
            return KickDecision("queue_only", "cooldown")
        name = self.config.kick_deployments.get(market)
        if not name:
            return KickDecision("queue_only", "deployment_unmapped")
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
            return KickDecision("queue_only", "prefect_lookup_failed")
        # Reserve the slot and persist BEFORE the create_flow_run call: a
        # crash after Prefect receives the kick but before the next save
        # must still count it, or the daily cap could be exceeded across
        # process failure.  An ambiguous failure (timeout/exception) keeps
        # the reservation — the kick may exist.  Only a definitive response
        # without a run id releases it.
        state["cooldowns"][market] = now.timestamp()
        day["count"] = int(day.get("count") or 0) + 1
        locked.save()
        result = await self.http_post(
            f"{self.config.prefect_api_url.rstrip('/')}/api/deployments/{items[0]['id']}/create_flow_run",
            {
                "parameters": {
                    "rep": next_rep(market, now),
                    "date_tag": f"{now.astimezone(KST):%Y%m%d}-{tag}",
                }
            },
        )
        flow_run_id = result.get("id")
        if isinstance(flow_run_id, str) and flow_run_id:
            return KickDecision("kick", verdict.reason, flow_run_id)
        day["count"] = int(day.get("count") or 0) - 1
        state["cooldowns"].pop(market, None)
        locked.save()
        return KickDecision("queue_only", "prefect_no_run_id")

    @staticmethod
    def _decision_record(
        fill: Mapping[str, Any],
        verdict: KickVerdict,
        decision: KickDecision | None,
    ) -> dict[str, Any]:
        """One per-fill decision line for the run outcome log.

        ``decision=None`` is the dry-run preview shape: the recorded class is
        the would-be filter verdict because no kick/cap state is touched.
        """
        record: dict[str, Any] = {
            "ledger_id": int(fill["ledger_id"]),
            "market": str(fill["market"]),
            "filter": verdict.reason,
            "flow_run_id": None,
        }
        if decision is None:
            record["class"] = "kick" if verdict.eligible else "queue_only"
            record["reason"] = verdict.reason
            record["dry_run"] = True
        else:
            record["class"] = decision.klass
            record["reason"] = decision.reason
            record["flow_run_id"] = decision.flow_run_id
        return record

    async def _fallback_to_herdr_then_kick(
        self,
        fill: Mapping[str, Any],
        *,
        prompt: str,
        locked: HandoffState,
        service: SessionContextService,
        context_row: Any,
        outcome: dict[str, Any],
        db: Any,
        verdict: KickVerdict | None,
    ) -> tuple[int, KickDecision | None]:
        """Keep the original discovery → prompt → kickoff fallback ordering."""
        all_panes: list[tuple[str, str]] = []
        discovery_complete = True
        for target in self.config.herdr_targets:
            try:
                listed = self.command(self._target_command(target, ("agent", "list")))
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
        pushed = 0
        for target, pane in all_panes:
            if self._submit(target, pane, prompt):
                pushed += 1
        decision: KickDecision | None = None
        if not all_panes and discovery_complete:
            try:
                decision = await self._kick(fill, locked, verdict)
            except Exception:  # noqa: BLE001 - durable context remains canonical
                decision = KickDecision("queue_only", "kick_error")
            if decision.flow_run_id:
                outcome["kicked"] += 1
                if context_row is not None:
                    await service.append_fill_handoff_kick_result(
                        entry_id=context_row.id,
                        flow_run_id=decision.flow_run_id,
                    )
                    await db.commit()
            elif decision.klass == "capped" and context_row is not None:
                await service.append_fill_handoff_kick_capped(entry_id=context_row.id)
                await db.commit()
        elif pushed == 0:
            decision = KickDecision(
                "queue_only",
                "pane_submit_failed" if all_panes else "pane_discovery_incomplete",
            )
        return pushed, decision

    def _classify_watch(self, event: Mapping[str, Any], now: datetime) -> KickVerdict:
        """Filter verdict for one delivered watch event; fails closed.

        The tradable-hours flag is evaluated from the injected clock, and the
        source alert's ``max_action`` arrives pre-joined on the row — an
        unreadable side signal (``None``) can only ever produce queue-only.
        A delivered row older than the dedupe window is stale: it rides the
        next regular rep and can never kick — which also bounds the
        ``watchkick:<id>`` replay mark so its TTL expiry can never reopen a
        crash-replay double-kick.
        """
        tradable = is_tradable_now(str(event.get("market") or ""), now)
        verdict = classify_watch_for_kick(
            event,
            event.get("alert_max_action"),
            tradable=tradable,
        )
        if not verdict.eligible:
            return verdict
        try:
            delivered_at = datetime.fromisoformat(str(event.get("delivered_at")))
            stale = (
                delivered_at.tzinfo is None
                or delivered_at.tzinfo.utcoffset(delivered_at) is None
                or now - delivered_at >= DEDUP_WINDOW
            )
        except (TypeError, ValueError):
            stale = True
        if stale:
            return KickVerdict(False, "stale_event")
        return verdict

    async def _seed_watch_kick_cursor(
        self, db: Any, state: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        """Install-boundary seed: never replay the delivered-event backlog."""
        try:
            high = await DbWatchKickSource(db).high_watermark()
        except Exception:  # noqa: BLE001 - seeding must not break the fill pass
            outcome["watch_errors"].append("watch_high_watermark_failed")
            return
        state["watch_kick_watermark"] = int(high.event_id)
        state["watch_kick_delivered_at"] = (
            None if high.delivered_at is None else high.delivered_at.isoformat()
        )

    async def _notify_watch(
        self, event: Mapping[str, Any], *, decision: KickDecision
    ) -> None:
        if not self.config.discord_webhook:
            return
        content = (
            f"{event.get('symbol')} {event.get('metric')} {event.get('operator')} "
            f"{event.get('threshold')} watch 발화 — kick "
            f"{'yes' if decision.flow_run_id else 'no'} "
            f"/ {decision.klass}:{decision.reason}"
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

    @staticmethod
    def _watch_decision_record(
        event: Mapping[str, Any],
        verdict: KickVerdict,
        decision: KickDecision | None,
    ) -> dict[str, Any]:
        """One per-event decision line; ``decision=None`` is the dry-run shape."""
        record: dict[str, Any] = {
            "event_id": int(event["event_id"]),
            "market": str(event.get("market") or ""),
            "symbol": str(event.get("symbol") or ""),
            "action_mode": str(event.get("action_mode") or ""),
            "filter": verdict.reason,
            "flow_run_id": None,
        }
        if decision is None:
            record["class"] = "kick" if verdict.eligible else "queue_only"
            record["reason"] = verdict.reason
            record["dry_run"] = True
        else:
            record["class"] = decision.klass
            record["reason"] = decision.reason
            record["flow_run_id"] = decision.flow_run_id
        return record

    async def _run_watch_kicks(
        self, db: Any, locked: HandoffState, outcome: dict[str, Any]
    ) -> None:
        """Classify newly delivered watch events under the shared kick cap.

        Same-symbol ladder fires within one poll form one kick candidate (the
        first eligible row in delivery order) — the bundle dedupe key is the
        per-alert idempotency key, so the candidate grouping key must be the
        (market, symbol) pair, which this grouping supplies.  A read failure
        leaves the cursor untouched so the next pass retries the same window.
        """
        state = locked.data
        if "watch_kick_watermark" not in state:
            # A fresh install or an upgrade onto a pre-kick state file seeds
            # the cursor and processes nothing — history never kicks.
            await self._seed_watch_kick_cursor(db, state, outcome)
            if not self.config.dry_run:
                locked.save()
            return
        cursor = _watch_kick_cursor_from_state(state)
        if cursor is None:
            # Corrupt persisted cursor: reseed to the delivered high-water
            # mark like a fresh install — backlog is history, never a queue
            # of pending kicks.
            outcome["watch_errors"].append("watch_cursor_corrupt")
            await self._seed_watch_kick_cursor(db, state, outcome)
            if not self.config.dry_run:
                locked.save()
            return
        try:
            rows = await DbWatchKickSource(db).list_after(
                cursor, limit=WATCH_KICK_BATCH_LIMIT
            )
            # Re-sort defensively; an unparseable id must not poison the
            # whole batch ordering — it sorts first and is dropped below.
            rows = sorted(rows, key=_watch_row_order_key)
        except Exception:  # noqa: BLE001 - the fill handoff must not be wedged
            outcome["watch_errors"].append("watch_read_failed")
            return
        now = self.now()
        resolved: set[int] = set()
        clean: list[Mapping[str, Any]] = []
        for event in rows:
            try:
                if _exact_int(event.get("event_id")) is None:
                    raise ValueError("malformed event_id")
                delivered = event.get("delivered_at")
                if delivered is None:
                    raise ValueError("missing delivered_at")
                parsed = datetime.fromisoformat(str(delivered))
                if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                    raise ValueError("naive delivered_at")
            except (KeyError, TypeError, ValueError):
                # A row that cannot be identified or cursor-tracked is dropped
                # with an error record rather than wedging the pass.
                outcome["watch_errors"].append("event_malformed")
                continue
            clean.append(event)
        # Rows are processed in delivery order, not per symbol group, so the
        # first ELIGIBLE row globally — not the first eligible row of the
        # first-seen symbol — consumes the cap slot.  ``candidates`` records
        # which (market, symbol) groups already spent their one kick attempt.
        candidates: set[tuple[str, str]] = set()
        for event in clean:
            event_id = int(event["event_id"])
            group_key = (
                str(event.get("market") or "").strip().lower(),
                str(event.get("symbol") or "").strip().upper(),
            )
            try:
                verdict = self._classify_watch(event, now)
            except Exception:  # noqa: BLE001 - a classifier bug resolves too
                verdict = KickVerdict(False, "classification_failed")
            decision: KickDecision | None
            if not verdict.eligible:
                decision = KickDecision("queue_only", verdict.reason)
            elif group_key in candidates:
                decision = KickDecision("queue_only", "ladder_grouped")
            elif self.config.dry_run:
                candidates.add(group_key)
                decision = None
            else:
                candidates.add(group_key)
                # Crash-replay dedupe, mirroring the fill path's `seen`
                # map: the mark is written before the gate so the gate's
                # own reservation save persists it atomically.  A crash
                # between a successful kick and the cursor advance would
                # otherwise re-gate this event next poll.
                kick_seen = f"watchkick:{event_id}"
                seen_at = float(state["seen"].get(kick_seen, 0))
                if now.timestamp() - seen_at < DEDUP_WINDOW.total_seconds():
                    decision = KickDecision("queue_only", "already_kicked")
                else:
                    state["seen"][kick_seen] = now.timestamp()
                    try:
                        decision = await self._gated_kick(
                            market=group_key[0],
                            tag=f"watch{event_id}",
                            locked=locked,
                            verdict=verdict,
                        )
                    except Exception:  # noqa: BLE001 - record is canonical
                        decision = KickDecision("queue_only", "kick_error")
            outcome["watch_decisions"].append(
                self._watch_decision_record(event, verdict, decision)
            )
            if decision is not None:
                if decision.flow_run_id:
                    outcome["watch_kicked"] += 1
                if decision.klass in {"kick", "capped"}:
                    await self._notify_watch(event, decision=decision)
            resolved.add(event_id)
        if self.config.dry_run:
            return
        advanced = _advance_watch_kick_cursor(cursor, clean, resolved)
        state["watch_kick_watermark"] = advanced.event_id
        state["watch_kick_delivered_at"] = (
            None if advanced.delivered_at is None else advanced.delivered_at.isoformat()
        )

    async def run(self, db: Any) -> dict[str, Any]:
        repo = ExecutionLedgerRepository(db)
        with HandoffState(self.config.state_dir) as locked:
            state = locked.data
            outcome: dict[str, Any] = {
                "durable": 0,
                "pushed": 0,
                "kicked": 0,
                "duplicate": 0,
                "fallback": [],
            }
            if self.config.kick_enabled:
                # Per-fill and per-watch kick-gate decisions for the 2-week
                # measurement; only present when the kick path is even
                # possible so a disabled deployment produces byte-identical
                # output to before.
                outcome["decisions"] = []
                outcome["watch_decisions"] = []
                outcome["watch_kicked"] = 0
                outcome["watch_errors"] = []
            if locked.is_new:
                if self.config.since_ledger_id is None:
                    # An empty state directory is an installation, not an
                    # instruction to replay the historical ledger.  Seed to
                    # its high-water mark and let the next fill be the first
                    # operator handoff.
                    state["watermark"] = await repo.max_ledger_id()
                    if self.config.kick_enabled:
                        # The delivered watch backlog is history, not a kick
                        # queue — seed the cursor alongside the ledger mark.
                        await self._seed_watch_kick_cursor(db, state, outcome)
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
                try:
                    fill = sanitize_fill(row)
                except Exception:  # noqa: BLE001 - one poison row must not wedge the batch
                    skipped_id = getattr(row, "id", None)
                    outcome.setdefault("skipped", []).append(
                        {"ledger_id": skipped_id, "reason": "sanitize_failed"}
                    )
                    if self.config.kick_enabled:
                        outcome["decisions"].append(
                            {
                                "ledger_id": skipped_id,
                                "market": None,
                                "filter": "sanitize_failed",
                                "class": "queue_only",
                                "reason": "sanitize_failed",
                                "flow_run_id": None,
                            }
                        )
                    try:
                        skipped_ledger = int(skipped_id)
                    except (TypeError, ValueError):
                        skipped_ledger = None
                    # dry_run never mutates state — the trailing save would
                    # otherwise persist the in-memory watermark advance.
                    if skipped_ledger is not None and not self.config.dry_run:
                        state["watermark"] = max(
                            int(state["watermark"]), skipped_ledger
                        )
                        locked.save()
                    continue
                if str(fill.get("market") or "") not in QUEUEABLE_MARKETS:
                    # e.g. forex — the fill can never be an open_question and
                    # must never kick, but it must be recorded and skipped so
                    # the poller cannot wedge on it.
                    outcome.setdefault("skipped", []).append(
                        {
                            "ledger_id": fill["ledger_id"],
                            "market": fill["market"],
                            "reason": "unsupported_market",
                        }
                    )
                    if self.config.kick_enabled:
                        verdict = KickVerdict(False, "unsupported_market")
                        outcome["decisions"].append(
                            self._decision_record(
                                fill,
                                verdict,
                                KickDecision("queue_only", "unsupported_market"),
                            )
                        )
                    if not self.config.dry_run:
                        state["watermark"] = max(
                            int(state["watermark"]), int(fill["ledger_id"])
                        )
                        locked.save()
                    continue
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
                verdict = (
                    await self._classify_fill(fill, repo)
                    if self.config.kick_enabled
                    else None
                )
                if context_row is None:
                    title, body = handoff_text(fill)
                    refs: dict[str, Any] = {
                        "event_key": fill["event_key"],
                        "ledger_id": fill["ledger_id"],
                        "correlation_id": fill["correlation_id"],
                        "symbols": [fill["symbol"]],
                        "broker_order_id": fill["broker_order_id"],
                        "side": fill["side"],
                        "filled_notional": fill["filled_notional"],
                        "currency": fill["currency"],
                        "fill_handoff": "v1",
                    }
                    if verdict is not None:
                        refs["kick_filter_class"] = (
                            "kick" if verdict.eligible else "queue_only"
                        )
                        refs["kick_filter_reason"] = verdict.reason
                        if verdict.position_before is not None:
                            refs["position_before"] = str(verdict.position_before)
                        if verdict.position_after is not None:
                            refs["position_after"] = str(verdict.position_after)
                    entry = SessionContextAppendEntry(
                        market=fill["market"],
                        entry_type="open_question",
                        title=title,
                        body=body,
                        refs=refs,
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
                    if verdict is not None:
                        outcome["decisions"].append(
                            self._decision_record(fill, verdict, None)
                        )
                    continue
                state["seen"][key] = now.timestamp()
                prompt = handoff_lane_event_text(fill)
                delivery: Literal[
                    "lane_event", "lane_event_duplicate", "herdr", "none"
                ] = "none"
                pushed = 0
                decision: KickDecision | None = None
                market = str(fill["market"])
                lane = (self.config.lane_events or {}).get(market)
                if lane:
                    lane_result = emit_lane_event(
                        lane,
                        event_id=str(fill["event_key"]),
                        text=prompt,
                        config=self.config.lane_event or LaneEventConfig(),
                    )
                    if lane_result.outcome == "emitted":
                        pushed = 1
                        delivery = "lane_event"
                        decision = KickDecision("queue_only", "lane_event_delivered")
                    elif lane_result.outcome == "duplicate":
                        outcome["duplicate"] += 1
                        delivery = "lane_event_duplicate"
                        decision = KickDecision("queue_only", "lane_event_duplicate")
                    else:
                        outcome["fallback"].append(lane_result.reason or "os_error")
                        pushed, decision = await self._fallback_to_herdr_then_kick(
                            fill,
                            prompt=prompt,
                            locked=locked,
                            service=service,
                            context_row=context_row,
                            outcome=outcome,
                            db=db,
                            verdict=verdict,
                        )
                        if pushed:
                            delivery = "herdr"
                else:
                    pushed, decision = await self._fallback_to_herdr_then_kick(
                        fill,
                        prompt=prompt,
                        locked=locked,
                        service=service,
                        context_row=context_row,
                        outcome=outcome,
                        db=db,
                        verdict=verdict,
                    )
                    if pushed:
                        delivery = "herdr"
                if decision is None:
                    decision = KickDecision("queue_only", "delivered_pane")
                if verdict is not None:
                    outcome["decisions"].append(
                        self._decision_record(fill, verdict, decision)
                    )
                outcome["pushed"] += pushed
                await self._notify(
                    fill,
                    pushed=pushed,
                    kicked=decision.flow_run_id is not None,
                    delivery=delivery,
                )
                state["watermark"] = max(
                    int(state["watermark"]), int(fill["ledger_id"])
                )
                locked.save()
            if self.config.kick_enabled:
                await self._run_watch_kicks(db, locked, outcome)
            if not self.config.dry_run:
                locked.save()
            return outcome
