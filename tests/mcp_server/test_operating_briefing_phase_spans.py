"""ROB-1487: ``get_operating_briefing`` phase spans, concurrency and query cuts.

The tool was p95 45 s over 101 calls/30 d with no attribution inside its
transaction. These tests pin the three changes:

* every phase emits a child span under ``op="operating_briefing.phase"``;
* running the independent sections concurrently produces byte-identical output
  to running them sequentially;
* the latest-report summary reads items only (it used to pay for all ten
  ``get_bundle`` queries and discard nine).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp_server.tooling import operating_briefing as ob


class _SpanRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start_span(self, *, op: str, name: str) -> _RecordingSpan:
        self.calls.append((op, name))
        return _RecordingSpan()

    @property
    def ops(self) -> set[str]:
        return {op for op, _ in self.calls}

    @property
    def names(self) -> set[str]:
        return {name for _, name in self.calls}


class _RecordingSpan:
    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def set_data(self, key: str, value: Any) -> None:
        return None


class _FakeSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


_AS_OF = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trade_journal import aggregates as aggregate_service

    async def fake_holdings(**_kwargs: Any) -> dict[str, Any]:
        return {
            "filters": {"market": "kr"},
            "total_accounts": 1,
            "total_positions": 1,
            "summary": {"total_value": 10},
            "top_movers": [],
            "accounts": [
                {
                    "account": "kis",
                    "account_name": "KIS",
                    "account_mode": "kis_live",
                    "order_routable": True,
                    "positions": [{"symbol": "005930"}],
                }
            ],
            "errors": [],
        }

    async def fake_pending(db: Any, *, market: str, account_scope: str) -> Any:
        return SimpleNamespace(
            orders=[],
            as_of=_AS_OF.isoformat(),
            freshness_status="fresh",
            unavailable_reason=None,
        )

    async def fake_latest_report(db: Any, *, market: str, account_scope: str) -> Any:
        return None

    async def fake_session_context(
        db: Any, *, market: str, account_scope: str, limit: int
    ) -> dict[str, Any]:
        return {"count": 0, "entries": [], "constraints": {"count": 0, "entries": []}}

    async def fake_analysis_artifacts(
        db: Any, *, market: str, limit: int = 10
    ) -> dict[str, Any]:
        return {"count": 0, "artifacts": []}

    async def fake_negative_class(db: Any, *, market: str, now: Any) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda: {"status": "ok", "market": market})

    async def fake_scoreboard(db: Any, *, market: str, cohort: str) -> dict[str, Any]:
        return {"cohort": cohort, "market": market}

    async def fake_active_watches(**kwargs: Any) -> dict[str, Any]:
        return {
            "count": 0,
            "as_of": _AS_OF.isoformat(),
            "active_watches": [],
        }

    async def fake_account_costs() -> None:
        return None

    monkeypatch.setattr(ob, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(ob, "now_kst", lambda: _AS_OF)
    monkeypatch.setattr(ob, "_get_portfolio_summary_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "_latest_report_summary", fake_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", fake_session_context)
    monkeypatch.setattr(ob, "_recent_analysis_artifacts", fake_analysis_artifacts)
    monkeypatch.setattr(ob, "load_negative_class_health", fake_negative_class)
    monkeypatch.setattr(aggregate_service, "build_trading_scoreboard", fake_scoreboard)
    monkeypatch.setattr(ob, "list_active_watches_impl", fake_active_watches)
    monkeypatch.setattr(ob, "get_account_costs_setting", fake_account_costs)
    monkeypatch.setattr(ob, "policy_version_stamp", lambda: {"version": "t"})


@pytest.mark.asyncio
async def test_every_phase_emits_a_child_span(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)
    recorder = _SpanRecorder()
    monkeypatch.setattr(
        ob, "sentry_sdk", SimpleNamespace(start_span=recorder.start_span)
    )

    result = await ob.get_operating_briefing_impl(market="kr", account_scope="kis_live")

    assert result["success"] is True
    assert recorder.ops == {"operating_briefing.phase"}
    assert recorder.names == {
        "operating_briefing.holdings",
        "operating_briefing.pending_orders",
        "operating_briefing.latest_report",
        "operating_briefing.session_context",
        "operating_briefing.analysis_artifacts",
        "operating_briefing.negative_class_recording",
        "operating_briefing.trading_scoreboards",
        "operating_briefing.active_watches",
        "operating_briefing.account_costs",
        "operating_briefing.policy_version",
    }


@pytest.mark.asyncio
async def test_span_instrumentation_does_not_change_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch)
    without_spans = await ob.get_operating_briefing_impl(
        market="kr", account_scope="kis_live"
    )

    monkeypatch.setattr(
        ob, "sentry_sdk", SimpleNamespace(start_span=_SpanRecorder().start_span)
    )
    with_spans = await ob.get_operating_briefing_impl(
        market="kr", account_scope="kis_live"
    )

    assert _render(with_spans) == _render(without_spans)


@pytest.mark.asyncio
async def test_concurrent_sections_match_a_sequential_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gather must not change the document a sequential run produced."""

    _install_fakes(monkeypatch)
    concurrent = await ob.get_operating_briefing_impl(
        market="kr", account_scope="kis_live"
    )

    async def sequential(*coros: Any) -> list[Any]:
        return [await coro for coro in coros]

    monkeypatch.setattr(ob, "_gather_sections", sequential)
    serial = await ob.get_operating_briefing_impl(market="kr", account_scope="kis_live")

    assert _render(concurrent) == _render(serial)


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


@pytest.mark.asyncio
async def test_latest_report_summary_reads_items_without_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_uuid = uuid.uuid4()
    report_uuid = uuid.uuid4()

    class _Service:
        def __init__(self, _db: Any) -> None:
            pass

        async def latest_report(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                id=7,
                report_uuid=report_uuid,
                title="t",
                status="draft",
                created_by_profile="p",
                created_at=None,
            )

        async def list_items_for_report(self, report_id: int) -> list[Any]:
            assert report_id == 7
            return [
                SimpleNamespace(
                    item_uuid=item_uuid,
                    symbol="AAPL",
                    item_kind="watch",
                    intent="buy_review",
                    status="draft",
                    rationale="r",
                )
            ]

        async def get_bundle(self, _report_uuid: Any) -> Any:  # pragma: no cover
            raise AssertionError("briefing summary must not call get_bundle")

    monkeypatch.setattr(ob, "InvestmentReportQueryService", _Service)

    summary = await ob._latest_report_summary(
        object(), market="kr", account_scope="kis_live"
    )

    assert summary is not None
    assert summary["report_uuid"] == str(report_uuid)
    assert summary["items"] == {
        "total": 1,
        "by_status": {"draft": 1},
        "top": [
            {
                "item_uuid": str(item_uuid),
                "symbol": "AAPL",
                "item_kind": "watch",
                "intent": "buy_review",
                "status": "draft",
                "rationale": "r",
            }
        ],
    }
