"""Weekend crypto Alpaca Paper cycle runner MVP (ROB-94).

Safe-by-default orchestration for Upbit crypto signal candidates mapped to Alpaca
Paper crypto execution symbols:
plan -> buy preview -> preflight/packet validation -> optional buy execute ->
fill reconcile -> sell preview -> optional sell execute -> final reconcile -> report.

Default behavior is dry_run=True. Broker mutation is only possible when the
caller explicitly sets dry_run=False, confirm=True, supplies an operator token,
and supplies per-candidate approval tokens.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

__all__ = [
    "ALLOWED_EXECUTION_SYMBOLS",
    "ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL",
    "MAX_CANDIDATES",
    "MAX_NOTIONAL_USD",
    "CryptoCycleCandidate",
    "CycleGateError",
    "CycleRunnerError",
    "StageTrace",
    "WeekendCryptoCandidateTrace",
    "WeekendCryptoPaperCycleReport",
    "WeekendCryptoPaperCycleRunner",
]

MAX_CANDIDATES: int = 3
MAX_NOTIONAL_USD: Decimal = Decimal("10")
ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL: dict[str, str] = {
    "KRW-BTC": "BTC/USD",
    "KRW-ETH": "ETH/USD",
    "KRW-SOL": "SOL/USD",
}
ALLOWED_EXECUTION_SYMBOLS: frozenset[str] = frozenset(
    ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL.values()
)
ALLOWED_SIGNAL_VENUES: frozenset[str] = frozenset({"upbit"})
ALLOWED_EXECUTION_VENUES: frozenset[str] = frozenset({"alpaca_paper"})
ALLOWED_ASSET_CLASSES: frozenset[str] = frozenset({"crypto"})
ALLOWED_ORDER_TYPES: frozenset[str] = frozenset({"limit"})


class CycleRunnerError(RuntimeError):
    """Base error for cycle runner failures."""


class CycleGateError(CycleRunnerError):
    """Raised when execute mode is refused by an explicit safety gate."""


@dataclass
class StageTrace:
    stage: str
    status: Literal["ok", "skipped", "blocked", "error"]
    detail: str = ""
    payload_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
            "payload_summary": _redact_sensitive(self.payload_summary),
        }


@dataclass
class WeekendCryptoCandidateTrace:
    candidate_uuid: str
    signal_symbol: str
    signal_venue: str
    execution_symbol: str
    execution_venue: str
    lifecycle_correlation_id: str
    dry_run: bool
    final_state: Literal[
        "planned",
        "previewed",
        "validated",
        "submitted",
        "filled",
        "position_reconciled",
        "sell_validated",
        "closed",
        "final_reconciled",
        "anomaly",
        "gate_blocked",
        "cap_blocked",
    ]
    stages: list[StageTrace] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    ledger_client_order_ids: list[str] = field(default_factory=list)
    roundtrip_report: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_uuid": self.candidate_uuid,
            "signal_symbol": self.signal_symbol,
            "signal_venue": self.signal_venue,
            "execution_symbol": self.execution_symbol,
            "execution_venue": self.execution_venue,
            "lifecycle_correlation_id": self.lifecycle_correlation_id,
            "dry_run": self.dry_run,
            "final_state": self.final_state,
            "stages": [stage.to_dict() for stage in self.stages],
            "anomalies": _redact_sensitive(self.anomalies),
            "ledger_client_order_ids": self.ledger_client_order_ids,
            "roundtrip_report": _redact_sensitive(self.roundtrip_report),
        }


@dataclass
class WeekendCryptoPaperCycleReport:
    status: Literal["ok", "partial", "blocked", "failed", "dry_run_ok"]
    dry_run: bool
    confirm: bool
    checked_at: datetime
    candidates_seen: int
    candidates_selected: int
    candidates_completed: int
    candidates_blocked: int
    traces: list[WeekendCryptoCandidateTrace] = field(default_factory=list)
    cycle_anomalies: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "confirm": self.confirm,
            "checked_at": self.checked_at.isoformat(),
            "candidates_seen": self.candidates_seen,
            "candidates_selected": self.candidates_selected,
            "candidates_completed": self.candidates_completed,
            "candidates_blocked": self.candidates_blocked,
            "traces": [trace.to_dict() for trace in self.traces],
            "cycle_anomalies": _redact_sensitive(self.cycle_anomalies),
        }


@dataclass
class CryptoCycleCandidate:
    candidate_uuid: str
    signal_symbol: str
    signal_venue: str
    execution_symbol: str
    execution_venue: str
    execution_asset_class: str
    order_type: str
    notional: Decimal
    limit_price: Decimal
    time_in_force: str
    lifecycle_correlation_id: str


_SubmitCallable = Callable[..., Coroutine[Any, Any, dict[str, Any]]]
_PreviewCallable = Callable[..., Coroutine[Any, Any, dict[str, Any]]]
_FillReadCallable = Callable[[str], Coroutine[Any, Any, dict[str, Any] | None]]


class WeekendCryptoPaperCycleRunner:
    """Bounded, dependency-injected weekend crypto Alpaca Paper cycle runner."""

    def __init__(
        self,
        *,
        candidate_loader: Callable[..., Coroutine[Any, Any, list[CryptoCycleCandidate]]]
        | None = None,
        preview_fn: _PreviewCallable | None = None,
        submit_fn: _SubmitCallable | None = None,
        fill_read_fn: _FillReadCallable | None = None,
        preflight_fn: Callable[..., Any] | None = None,
        ledger_service: Any | None = None,
        packet_freshness_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        packet_idempotency_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        sell_source_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        report_service: Any | None = None,
    ) -> None:
        self._candidate_loader = candidate_loader
        self._preview_fn = preview_fn
        self._submit_fn = submit_fn
        self._fill_read_fn = fill_read_fn
        self._preflight_fn = preflight_fn
        self._ledger = ledger_service
        self._packet_freshness_fn = packet_freshness_fn
        self._packet_idempotency_fn = packet_idempotency_fn
        self._sell_source_fn = sell_source_fn
        self._report_service = report_service

    async def run_cycle(
        self,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        max_candidates: int = MAX_CANDIDATES,
        symbols: list[str] | None = None,
        approval_tokens: dict[str, str] | None = None,
        operator_token: str | None = None,
        now: datetime | None = None,
    ) -> WeekendCryptoPaperCycleReport:
        """Run one bounded cycle. dry_run=True never calls submit_fn."""
        now = now or datetime.now(UTC)
        if not dry_run:
            self._assert_execute_gates(
                confirm=confirm,
                operator_token=operator_token,
                approval_tokens=approval_tokens,
            )

        effective_max = min(max(1, max_candidates), MAX_CANDIDATES)
        raw_candidates = await self._load_candidates(
            symbols=symbols, max_candidates=effective_max
        )
        selected: list[CryptoCycleCandidate] = []
        traces: list[WeekendCryptoCandidateTrace] = []

        for candidate in raw_candidates[:effective_max]:
            rejection = self._validate_candidate(candidate)
            if rejection:
                traces.append(
                    self._blocked_trace(candidate, dry_run=dry_run, reason=rejection)
                )
            else:
                selected.append(candidate)

        completed = 0
        for candidate in selected:
            trace = await self._run_candidate_lifecycle(
                candidate=candidate,
                dry_run=dry_run,
                confirm=confirm,
                approval_tokens=approval_tokens or {},
                operator_token=operator_token,
                now=now,
            )
            traces.append(trace)
            if trace.final_state in {"validated", "final_reconciled"}:
                completed += 1

        blocked = sum(
            1
            for trace in traces
            if trace.final_state in {"anomaly", "gate_blocked", "cap_blocked"}
        )
        return WeekendCryptoPaperCycleReport(
            status=self._compute_status(
                dry_run=dry_run,
                selected=len(selected),
                completed=completed,
                blocked=blocked,
            ),
            dry_run=dry_run,
            confirm=confirm,
            checked_at=now,
            candidates_seen=len(raw_candidates),
            candidates_selected=len(selected),
            candidates_completed=completed,
            candidates_blocked=blocked,
            traces=traces,
            cycle_anomalies=[],
        )

    def _assert_execute_gates(
        self,
        *,
        confirm: bool,
        operator_token: str | None,
        approval_tokens: dict[str, str] | None,
    ) -> None:
        if not confirm:
            raise CycleGateError("execute requires confirm=True")
        if not operator_token:
            raise CycleGateError(
                "execute requires operator_token; set WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN"
            )
        if approval_tokens is None:
            raise CycleGateError("execute requires per-candidate approval_tokens dict")

    def _validate_candidate(self, c: CryptoCycleCandidate) -> str:
        if c.execution_symbol not in ALLOWED_EXECUTION_SYMBOLS:
            return f"execution_symbol {c.execution_symbol!r} not in allowlist {sorted(ALLOWED_EXECUTION_SYMBOLS)}"
        expected_execution_symbol = ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL.get(
            c.signal_symbol
        )
        if expected_execution_symbol is None:
            return f"signal_symbol {c.signal_symbol!r} not in allowlist {sorted(ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL)}"
        if c.execution_symbol != expected_execution_symbol:
            return f"execution_symbol {c.execution_symbol!r} does not match expected {expected_execution_symbol!r} for signal_symbol {c.signal_symbol!r}"
        if c.signal_venue not in ALLOWED_SIGNAL_VENUES:
            return "signal_venue must be 'upbit'"
        if c.execution_venue not in ALLOWED_EXECUTION_VENUES:
            return "execution_venue must be 'alpaca_paper'"
        if c.execution_asset_class not in ALLOWED_ASSET_CLASSES:
            return "execution_asset_class must be 'crypto'"
        if c.order_type not in ALLOWED_ORDER_TYPES:
            return "order_type must be 'limit'"
        if c.notional > MAX_NOTIONAL_USD:
            return f"notional ${c.notional} exceeds runner cap ${MAX_NOTIONAL_USD}"
        return ""

    def _blocked_trace(
        self,
        candidate: CryptoCycleCandidate,
        *,
        dry_run: bool,
        reason: str,
    ) -> WeekendCryptoCandidateTrace:
        return WeekendCryptoCandidateTrace(
            candidate_uuid=candidate.candidate_uuid,
            signal_symbol=candidate.signal_symbol,
            signal_venue=candidate.signal_venue,
            execution_symbol=candidate.execution_symbol,
            execution_venue=candidate.execution_venue,
            lifecycle_correlation_id=candidate.lifecycle_correlation_id,
            dry_run=dry_run,
            final_state="cap_blocked",
            stages=[StageTrace("validate_candidate", "blocked", reason)],
            anomalies=[{"check_id": "cap_or_allowlist", "summary": reason}],
        )

    async def _run_candidate_lifecycle(
        self,
        *,
        candidate: CryptoCycleCandidate,
        dry_run: bool,
        confirm: bool,
        approval_tokens: dict[str, str],
        operator_token: str | None,
        now: datetime,
    ) -> WeekendCryptoCandidateTrace:
        stages: list[StageTrace] = []
        anomalies: list[dict[str, Any]] = []
        ledger_ids: list[str] = []
        trace = WeekendCryptoCandidateTrace(
            candidate_uuid=candidate.candidate_uuid,
            signal_symbol=candidate.signal_symbol,
            signal_venue=candidate.signal_venue,
            execution_symbol=candidate.execution_symbol,
            execution_venue=candidate.execution_venue,
            lifecycle_correlation_id=candidate.lifecycle_correlation_id,
            dry_run=dry_run,
            final_state="planned",
            stages=stages,
            anomalies=anomalies,
            ledger_client_order_ids=ledger_ids,
        )

        buy_client_order_id = self._make_client_order_id("buy", candidate)
        ledger_ids.append(buy_client_order_id)
        if self._ledger is not None:
            try:
                await self._ledger.record_plan(
                    client_order_id=buy_client_order_id,
                    candidate_uuid=candidate.candidate_uuid,
                    lifecycle_correlation_id=candidate.lifecycle_correlation_id,
                    signal_symbol=candidate.signal_symbol,
                    signal_venue=candidate.signal_venue,
                    execution_symbol=candidate.execution_symbol,
                    execution_venue=candidate.execution_venue,
                    execution_asset_class=candidate.execution_asset_class,
                    side="buy",
                    order_type=candidate.order_type,
                    requested_notional=candidate.notional,
                    requested_price=candidate.limit_price,
                    dry_run=dry_run,
                )
            except Exception as exc:
                return self._fail(
                    trace, stages, anomalies, "plan", "ledger_plan_error", exc
                )
        stages.append(StageTrace("plan", "ok", buy_client_order_id))
        trace.final_state = "planned"

        if self._preview_fn is not None:
            try:
                preview_result = await self._preview_fn(
                    symbol=candidate.execution_symbol,
                    side="buy",
                    type=candidate.order_type,
                    notional=float(candidate.notional),
                    limit_price=float(candidate.limit_price),
                    time_in_force=candidate.time_in_force,
                    asset_class=candidate.execution_asset_class,
                    client_order_id=buy_client_order_id,
                )
                if self._ledger is not None:
                    await self._ledger.record_preview(
                        client_order_id=buy_client_order_id,
                        preview_payload=preview_result,
                    )
                stages.append(
                    StageTrace(
                        "buy_preview",
                        "ok",
                        payload_summary={
                            "symbol": candidate.execution_symbol,
                            "side": "buy",
                        },
                    )
                )
            except Exception as exc:
                return self._fail(
                    trace, stages, anomalies, "buy_preview", "preview_error", exc
                )
        else:
            stages.append(
                StageTrace("buy_preview", "skipped", "no preview_fn injected")
            )
        trace.final_state = "previewed"

        if self._preflight_fn is not None:
            try:
                preflight_report = self._preflight_fn(
                    expected_signal_symbol=candidate.signal_symbol,
                    expected_execution_symbol=candidate.execution_symbol,
                    now=now,
                )
                if getattr(preflight_report, "should_block", False):
                    for anomaly in getattr(preflight_report, "anomalies", []):
                        anomalies.append(_safe_anomaly_dict(anomaly))
                    stages.append(
                        StageTrace(
                            "preflight", "blocked", "preflight should_block=True"
                        )
                    )
                    trace.final_state = "anomaly"
                    return trace
                stages.append(StageTrace("preflight", "ok"))
            except Exception as exc:
                return self._fail(
                    trace, stages, anomalies, "preflight", "preflight_error", exc
                )
        else:
            stages.append(
                StageTrace("preflight", "skipped", "no preflight_fn injected")
            )

        buy_packet = self._build_packet(
            candidate, buy_client_order_id, side="buy", now=now
        )
        if await self._validate_packet_gate(
            trace,
            stages,
            anomalies,
            packet=buy_packet,
            now=now,
        ):
            return trace
        if self._ledger is not None:
            try:
                await self._ledger.record_validation_attempt(
                    client_order_id=buy_client_order_id,
                    validation_outcome="passed",
                )
            except Exception as exc:
                stages.append(StageTrace("record_validation", "error", str(exc)))
        stages.append(StageTrace("packet_validate", "ok"))
        trace.final_state = "validated"

        if not dry_run:
            if not approval_tokens.get(candidate.candidate_uuid):
                stages.append(
                    StageTrace(
                        "candidate_approval_token",
                        "blocked",
                        f"no approval token for candidate {candidate.candidate_uuid}",
                    )
                )
                anomalies.append(
                    {
                        "check_id": "missing_candidate_approval_token",
                        "summary": "missing approval_token for candidate",
                    }
                )
                trace.final_state = "gate_blocked"
                return trace
            stages.append(StageTrace("candidate_approval_token", "ok"))

        if dry_run:
            stages.append(
                StageTrace(
                    "execute_gate",
                    "skipped",
                    "dry_run=True; stopping before buy submit",
                    payload_summary={
                        "would_submit": {
                            "symbol": candidate.execution_symbol,
                            "side": "buy",
                            "type": candidate.order_type,
                            "notional": str(candidate.notional),
                            "limit_price": str(candidate.limit_price),
                        }
                    },
                )
            )
            return trace

        if self._submit_fn is None:
            stages.append(StageTrace("buy_submit", "blocked", "no submit_fn injected"))
            trace.final_state = "gate_blocked"
            return trace
        try:
            buy_submit_result = await self._submit_fn(
                symbol=candidate.execution_symbol,
                side="buy",
                type=candidate.order_type,
                notional=float(candidate.notional),
                limit_price=float(candidate.limit_price),
                time_in_force=candidate.time_in_force,
                asset_class=candidate.execution_asset_class,
                client_order_id=buy_client_order_id,
                confirm=confirm,
                operator_token=operator_token,
            )
            if self._ledger is not None:
                await self._ledger.record_submit(
                    client_order_id=buy_client_order_id,
                    broker_order_id=buy_submit_result.get("id"),
                    submit_payload=_redact_sensitive(buy_submit_result),
                )
            stages.append(StageTrace("buy_submit", "ok"))
            trace.final_state = "submitted"
        except Exception as exc:
            return self._fail(
                trace, stages, anomalies, "buy_submit", "submit_error", exc
            )

        if self._fill_read_fn is None:
            stages.append(
                StageTrace("fill_reconcile", "blocked", "no fill_read_fn injected")
            )
            trace.final_state = "anomaly"
            return trace
        fill = await self._fill_read_fn(buy_client_order_id)
        if not fill:
            stages.append(StageTrace("fill_reconcile", "blocked", "buy fill not found"))
            anomalies.append(
                {"check_id": "fill_missing", "summary": "buy fill not found"}
            )
            trace.final_state = "anomaly"
            return trace
        if self._ledger is not None:
            await self._ledger.record_status(
                client_order_id=buy_client_order_id,
                lifecycle_state="filled",
                broker_payload=_redact_sensitive(fill),
            )
            await self._ledger.record_position_snapshot(
                lifecycle_correlation_id=candidate.lifecycle_correlation_id,
                symbol=candidate.execution_symbol,
                snapshot_payload=_redact_sensitive(fill.get("position", {})),
            )
        stages.append(StageTrace("fill_reconcile", "ok"))
        trace.final_state = "position_reconciled"

        sell_client_order_id = self._make_client_order_id("sell", candidate)
        ledger_ids.append(sell_client_order_id)
        if self._preview_fn is not None:
            await self._preview_fn(
                symbol=candidate.execution_symbol,
                side="sell",
                type=candidate.order_type,
                qty=fill.get("filled_qty"),
                limit_price=fill.get("filled_avg_price")
                or float(candidate.limit_price),
                time_in_force=candidate.time_in_force,
                asset_class=candidate.execution_asset_class,
                client_order_id=sell_client_order_id,
            )
        stages.append(
            StageTrace("sell_preview", "ok" if self._preview_fn else "skipped")
        )

        sell_packet = self._build_packet(
            candidate, sell_client_order_id, side="sell", now=now
        )
        if await self._validate_packet_gate(
            trace,
            stages,
            anomalies,
            packet=sell_packet,
            now=now,
        ):
            return trace
        if self._sell_source_fn is not None:
            try:
                await self._sell_source_fn(sell_packet, ledger=self._ledger)
                stages.append(StageTrace("sell_source", "ok"))
            except Exception as exc:
                stages.append(StageTrace("sell_source", "blocked", str(exc)))
                anomalies.append(
                    {"check_id": "sell_source_mismatch", "summary": str(exc)}
                )
                trace.final_state = "anomaly"
                return trace
        if self._ledger is not None:
            await self._ledger.record_sell_validation(
                client_order_id=sell_client_order_id,
                validation_outcome="passed",
            )
        stages.append(StageTrace("sell_validate", "ok"))
        trace.final_state = "sell_validated"

        if not approval_tokens.get(f"{candidate.candidate_uuid}:sell"):
            stages.append(
                StageTrace(
                    "sell_approval_token", "blocked", "missing sell approval token"
                )
            )
            anomalies.append(
                {
                    "check_id": "missing_sell_approval_token",
                    "summary": "missing sell approval token",
                }
            )
            trace.final_state = "gate_blocked"
            return trace

        try:
            sell_result = await self._submit_fn(
                symbol=candidate.execution_symbol,
                side="sell",
                type=candidate.order_type,
                qty=fill.get("filled_qty"),
                limit_price=fill.get("filled_avg_price")
                or float(candidate.limit_price),
                time_in_force=candidate.time_in_force,
                asset_class=candidate.execution_asset_class,
                client_order_id=sell_client_order_id,
                confirm=confirm,
                operator_token=operator_token,
            )
            if self._ledger is not None:
                await self._ledger.record_close(
                    client_order_id=sell_client_order_id,
                    broker_order_id=sell_result.get("id"),
                    close_payload=_redact_sensitive(sell_result),
                )
                await self._ledger.record_final_reconcile(
                    lifecycle_correlation_id=candidate.lifecycle_correlation_id,
                    outcome="closed",
                )
            stages.append(StageTrace("sell_submit", "ok"))
            trace.final_state = "closed"
        except Exception as exc:
            return self._fail(
                trace,
                stages,
                anomalies,
                "sell_submit",
                "sell_submit_or_reconcile_error",
                exc,
            )

        if self._report_service is not None:
            report = await self._report_service.build_report(
                lifecycle_correlation_id=candidate.lifecycle_correlation_id
            )
            trace.roundtrip_report = _safe_report_dict(report)
        stages.append(StageTrace("final_reconcile", "ok"))
        trace.final_state = "final_reconciled"
        return trace

    async def _validate_packet_gate(
        self,
        trace: WeekendCryptoCandidateTrace,
        stages: list[StageTrace],
        anomalies: list[dict[str, Any]],
        *,
        packet: Any,
        now: datetime,
    ) -> bool:
        if self._packet_freshness_fn is not None:
            try:
                await self._packet_freshness_fn(packet, now=now)
                stages.append(StageTrace("packet_freshness", "ok"))
            except Exception as exc:
                stages.append(StageTrace("packet_freshness", "blocked", str(exc)))
                anomalies.append({"check_id": "stale_packet", "summary": str(exc)})
                trace.final_state = "anomaly"
                return True
        if self._packet_idempotency_fn is not None and self._ledger is not None:
            try:
                await self._packet_idempotency_fn(packet, ledger=self._ledger)
                stages.append(StageTrace("packet_idempotency", "ok"))
            except Exception as exc:
                stages.append(StageTrace("packet_idempotency", "blocked", str(exc)))
                anomalies.append(
                    {"check_id": "duplicate_client_order_id", "summary": str(exc)}
                )
                trace.final_state = "anomaly"
                return True
        return False

    async def _load_candidates(
        self, *, symbols: list[str] | None, max_candidates: int
    ) -> list[CryptoCycleCandidate]:
        if self._candidate_loader is None:
            return []
        return (
            await self._candidate_loader(symbols=symbols, max_candidates=max_candidates)
        )[:max_candidates]

    def _make_client_order_id(self, side: str, candidate: CryptoCycleCandidate) -> str:
        safe_symbol = candidate.execution_symbol.replace("/", "")
        return f"rob94-{side}-{safe_symbol}-{candidate.candidate_uuid[:8]}-{secrets.token_hex(4)}"

    def _build_packet(
        self,
        candidate: CryptoCycleCandidate,
        client_order_id: str,
        *,
        side: Literal["buy", "sell"],
        now: datetime,
    ) -> Any:
        from app.services.paper_approval_packet import PaperApprovalPacket

        return PaperApprovalPacket(
            signal_source="weekend_crypto_cycle_runner",
            artifact_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"{candidate.lifecycle_correlation_id}:{side}"
            ),
            signal_symbol=candidate.signal_symbol,
            signal_venue="upbit",
            execution_symbol=candidate.execution_symbol,
            execution_venue="alpaca_paper",
            execution_asset_class="crypto",
            side=side,
            max_notional=candidate.notional,
            qty_source="notional_estimate" if side == "buy" else "filled_position",
            expected_lifecycle_step="validated",
            lifecycle_correlation_id=candidate.lifecycle_correlation_id,
            client_order_id=client_order_id,
            expires_at=now + timedelta(minutes=30),
        )

    def _fail(
        self,
        trace: WeekendCryptoCandidateTrace,
        stages: list[StageTrace],
        anomalies: list[dict[str, Any]],
        stage: str,
        check_id: str,
        exc: Exception,
    ) -> WeekendCryptoCandidateTrace:
        stages.append(StageTrace(stage, "error", str(exc)))
        anomalies.append({"check_id": check_id, "summary": str(exc)})
        trace.final_state = "anomaly"
        return trace

    def _compute_status(
        self, *, dry_run: bool, selected: int, completed: int, blocked: int
    ) -> Literal["ok", "partial", "blocked", "failed", "dry_run_ok"]:
        if dry_run:
            return "dry_run_ok"
        if selected == 0:
            return "blocked"
        if blocked == 0:
            return "ok"
        if completed > 0:
            return "partial"
        return "failed"


_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "approval_token",
        "approval_tokens",
        "operator_token",
        "secret",
        "password",
        "api_key",
        "account_no",
        "authorization",
        "auth_header",
        "connection_string",
        "dsn",
        "email",
    }
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEYS)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _is_sensitive_key(str(key))
            else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value


def _safe_anomaly_dict(anomaly: Any) -> dict[str, Any]:
    to_dict = getattr(anomaly, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
            return (
                _redact_sensitive(raw)
                if isinstance(raw, dict)
                else {"summary": str(raw)}
            )
        except Exception as exc:
            return {"check_id": "anomaly_serialization_error", "summary": str(exc)}
    if isinstance(anomaly, dict):
        return _redact_sensitive(anomaly)
    try:
        return _redact_sensitive(dict(anomaly))
    except Exception:
        return {"summary": str(anomaly)}


def _safe_report_dict(report: Any) -> dict[str, Any]:
    """Convert a roundtrip report to a safe dict with sensitive values redacted."""
    if hasattr(report, "model_dump"):
        raw = report.model_dump()
    elif hasattr(report, "__dict__"):
        raw = dict(report.__dict__)
    elif isinstance(report, dict):
        raw = report
    else:
        return {}
    return _redact_sensitive(raw)
