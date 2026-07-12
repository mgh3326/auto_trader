from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleCreateRequest,
    AnalysisBundleCreateResponse,
    AnalysisFrozenDocument,
    AnalysisSection,
    AnalysisSectionName,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectorRegistry,
    SnapshotCollectResult,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

AnalysisFn = Callable[..., Awaitable[Any]]
DecisionHistoryFn = Callable[[str, str, str | None], Awaitable[Any]]
Clock = Callable[[], dt.datetime]

_SECTION_TTLS: dict[str, tuple[int, int]] = {
    "portfolio": (180, 300),
    "quotes_orderbooks": (60, 180),
    "indicators_support_resistance": (60, 180),
    "market_gate_inputs": (180, 300),
    "investor_flow": (900, 86400),
    "decision_history": (300, 900),
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).strip() or type(exc).__name__}"


def _unavailable_section(
    name: AnalysisSectionName,
    now: dt.datetime,
    source: dict[str, Any],
    error: str,
) -> AnalysisSection:
    soft_ttl, hard_ttl = _SECTION_TTLS[name]
    return AnalysisSection(
        status="unavailable",
        collected_at=now,
        as_of=now,
        source=source,
        soft_ttl_seconds=soft_ttl,
        hard_ttl_seconds=hard_ttl,
        error=error,
    )


def _available_section(
    name: AnalysisSectionName,
    now: dt.datetime,
    as_of: dt.datetime,
    source: dict[str, Any],
    data: Any,
    partial: bool = False,
    error: str | None = None,
) -> AnalysisSection:
    soft_ttl, hard_ttl = _SECTION_TTLS[name]
    return AnalysisSection(
        status="partial" if partial else "ok",
        collected_at=now,
        as_of=as_of,
        source=source,
        soft_ttl_seconds=soft_ttl,
        hard_ttl_seconds=hard_ttl,
        data=data,
        error=error,
    )


def _result_payload(result: SnapshotCollectResult) -> dict[str, Any]:
    return {
        "payload_json": result.payload_json,
        "errors_json": result.errors_json,
        "coverage_json": result.coverage_json,
        "source_timestamps_json": result.source_timestamps_json,
        "freshness_status": result.freshness_status,
        "symbol": result.symbol,
    }


def _first_diagnostic(results: list[SnapshotCollectResult], kind: str) -> str | None:
    for result in results:
        for value in result.errors_json.values():
            if isinstance(value, str):
                return value
            if value is not None:
                return json.dumps(value, sort_keys=True, separators=(",", ":"))
        quote = result.payload_json.get("quote")
        if isinstance(quote, dict) and quote.get("status") != "ok":
            error = quote.get("error")
            if isinstance(error, str) and error:
                return error
            return f"quote status: {quote.get('status')}"
    if any(result.freshness_status == "partial" for result in results):
        return f"{kind} collector returned partial data"
    return None


class AnalysisInputFrozenCollector:
    snapshot_kind = "llm_input_frozen"

    def __init__(
        self,
        collectors: SnapshotCollectorRegistry,
        *,
        analysis_fn: AnalysisFn,
        decision_history_fn: DecisionHistoryFn,
        clock: Clock | None = None,
        requested_by: str = "claude_code",
    ) -> None:
        self._collectors = collectors
        self._analysis_fn = analysis_fn
        self._decision_history_fn = decision_history_fn
        self._clock = clock or _utcnow
        self._requested_by = requested_by

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        captured_at = self._clock()
        sections = await asyncio.gather(
            self._collector_section("portfolio", "portfolio", request, captured_at),
            self._collector_section(
                "quotes_orderbooks", "symbol", request, captured_at
            ),
            self._analysis_section(request, captured_at),
            self._collector_section(
                "market_gate_inputs", "market", request, captured_at
            ),
            self._collector_section(
                "investor_flow", "investor_flow", request, captured_at
            ),
            self._decision_history_section(request, captured_at),
        )
        section_map = dict(zip(ANALYSIS_SECTION_NAMES, sections, strict=True))
        document = AnalysisFrozenDocument(
            captured_at=captured_at,
            request=AnalysisBundleCreateRequest(
                market=request.market,
                account_scope=request.account_scope,
                symbols=list(request.symbols or []),
                user_id=request.user_id,
                market_session=request.market_session,
                requested_by=self._requested_by,
            ),
            sections=section_map,
        )
        complete = [
            name for name, section in section_map.items() if section.status == "ok"
        ]
        partial = [
            name for name, section in section_map.items() if section.status == "partial"
        ]
        unavailable = [
            name
            for name, section in section_map.items()
            if section.status == "unavailable"
        ]
        return [
            SnapshotCollectResult(
                snapshot_kind="llm_input_frozen",
                market=request.market,
                account_scope=request.account_scope,
                source_kind="combined",
                payload_json=document.model_dump(mode="json"),
                source_timestamps_json={
                    name: section.as_of.isoformat()
                    for name, section in document.sections.items()
                },
                coverage_json={
                    "complete_sections": complete,
                    "partial_sections": partial,
                    "unavailable_sections": unavailable,
                },
                errors_json={
                    name: section.error
                    for name, section in document.sections.items()
                    if section.error is not None
                },
                as_of=captured_at,
                freshness_status="partial" if unavailable or partial else "fresh",
            )
        ]

    async def _collector_section(
        self,
        name: AnalysisSectionName,
        kind: str,
        request: CollectorRequest,
        now: dt.datetime,
    ) -> AnalysisSection:
        source = {"snapshot_kind": kind}
        collector = self._collectors.get(kind)
        if collector is None:
            return _unavailable_section(
                name, now, source, f"{kind} collector unavailable"
            )
        try:
            results = list(await collector.collect(request))
        except Exception as exc:  # noqa: BLE001 - each section fails independently
            return _unavailable_section(name, now, source, _error_text(exc))
        if not results:
            return _unavailable_section(
                name, now, source, f"{kind} collector returned no results"
            )
        unavailable = any(
            result.freshness_status == "unavailable" for result in results
        )
        diagnostic = _first_diagnostic(results, kind)
        if unavailable:
            return _unavailable_section(
                name,
                now,
                source,
                diagnostic or f"{kind} collector returned unavailable data",
            )
        partial = any(result.freshness_status == "partial" for result in results)
        partial = partial or any(result.errors_json for result in results)
        partial = partial or diagnostic is not None
        return _available_section(
            name,
            now,
            min(result.as_of for result in results),
            source,
            [_result_payload(result) for result in results],
            partial=partial,
            error=diagnostic,
        )

    async def _analysis_section(
        self, request: CollectorRequest, now: dt.datetime
    ) -> AnalysisSection:
        source = {"service": "full_analysis"}
        try:
            data = await self._analysis_fn(
                list(request.symbols or []),
                market=request.market,
                include_peers=False,
                quick=False,
                include_position=False,
                refresh=False,
            )
        except Exception as exc:  # noqa: BLE001 - section-local diagnostic
            return _unavailable_section(
                "indicators_support_resistance", now, source, _error_text(exc)
            )
        return _available_section(
            "indicators_support_resistance", now, now, source, data
        )

    async def _decision_history_section(
        self, request: CollectorRequest, now: dt.datetime
    ) -> AnalysisSection:
        source = {"service": "build_decision_context"}
        symbols = list(request.symbols or [])
        outcomes = await asyncio.gather(
            *(
                self._decision_history_fn(symbol, request.market, request.account_scope)
                for symbol in symbols
            ),
            return_exceptions=True,
        )
        data: dict[str, Any] = {}
        errors: list[str] = []
        for symbol, outcome in zip(symbols, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                error = _error_text(outcome)
                data[symbol] = {"status": "unavailable", "error": error}
                errors.append(error)
            else:
                data[symbol] = outcome
        if errors and len(errors) == len(symbols):
            unavailable = _unavailable_section(
                "decision_history", now, source, errors[0]
            )
            return unavailable.model_copy(update={"data": data})
        return _available_section(
            "decision_history",
            now,
            now,
            source,
            data,
            partial=bool(errors),
            error=errors[0] if errors else None,
        )


class AnalysisBundleCaptureService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        collectors: SnapshotCollectorRegistry,
        analysis_fn: AnalysisFn,
        decision_history_fn: DecisionHistoryFn,
        clock: Clock | None = None,
    ) -> None:
        self._session = session
        self._repo = InvestmentSnapshotsRepository(session)
        self._collectors = collectors
        self._analysis_fn = analysis_fn
        self._decision_history_fn = decision_history_fn
        self._clock = clock or _utcnow
        self._last_capture_at: dt.datetime | None = None

    def _capture_time(self) -> dt.datetime:
        captured_at = self._clock()
        if self._last_capture_at is not None and captured_at <= self._last_capture_at:
            captured_at = self._last_capture_at + dt.timedelta(microseconds=1)
        self._last_capture_at = captured_at
        return captured_at

    async def capture(
        self, request: AnalysisBundleCreateRequest
    ) -> AnalysisBundleCreateResponse:
        captured_at = self._capture_time()
        frozen_collector = AnalysisInputFrozenCollector(
            self._collectors,
            analysis_fn=self._analysis_fn,
            decision_history_fn=self._decision_history_fn,
            clock=lambda: captured_at,
            requested_by=request.requested_by,
        )
        frozen_registry = SnapshotCollectorRegistry()
        frozen_registry.register(frozen_collector)
        ensure_service = SnapshotBundleEnsureService(
            self._session,
            repository=self._repo,
            collectors=frozen_registry,
            clock=lambda: captured_at,
        )
        ensured = await ensure_service.ensure(
            EnsureBundleRequest(
                purpose="analysis_recheck",
                market=request.market,
                account_scope=request.account_scope,
                policy_version="analysis_snapshot_bundle_v1",
                mode="create_new",
                symbols=request.symbols,
                market_session=request.market_session,
                requested_by=request.requested_by,
                user_id=request.user_id,
            )
        )
        if ensured.bundle_uuid is None:
            raise RuntimeError("analysis bundle ensure returned no bundle UUID")
        bundle = await self._repo.get_bundle_by_uuid(ensured.bundle_uuid)
        if bundle is None or bundle.purpose != "analysis_recheck":
            raise RuntimeError(
                "persisted analysis bundle is missing or has wrong purpose"
            )
        pairs = await self._repo.list_bundle_items_with_snapshots(bundle.id)
        if len(pairs) != 1 or pairs[0][1].snapshot_kind != "llm_input_frozen":
            raise RuntimeError("analysis bundle must contain one frozen input snapshot")
        snapshot = pairs[0][1]
        document = AnalysisFrozenDocument.model_validate(snapshot.payload_json)
        unavailable = [
            name
            for name in ANALYSIS_SECTION_NAMES
            if document.sections[name].status == "unavailable"
        ]
        partial = [
            name
            for name in ANALYSIS_SECTION_NAMES
            if document.sections[name].status == "partial"
        ]
        return AnalysisBundleCreateResponse(
            bundle_id=bundle.bundle_uuid,
            content_hash=snapshot.canonical_payload_hash,
            status="partial" if unavailable or partial else "complete",
            captured_at=document.captured_at,
            unavailable_sections=unavailable,
            partial_sections=partial,
        )
