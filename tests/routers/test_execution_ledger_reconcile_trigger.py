"""Reconnect reconcile trigger endpoint (fillwire P0).

Kernels are always mocked here: no broker call, no reconcile booking.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import execution_ledger_ingest as ingest_module
from app.routers.execution_ledger_ingest import router as ingest_router
from app.services.reconcile_trigger import ReconcileTriggerCoordinator

_PATH = "/trading/api/execution-ledger/reconcile/trigger"


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _kernel_result(actions: list[str]) -> dict[str, Any]:
    return {
        "success": True,
        "dry_run": False,
        "counts": {"filled": len(actions)},
        "reconciled": [{"action": action} for action in actions],
        "message": "Reconciled",
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list[tuple[str, bool]],
    clock: _Clock | None = None,
    result: dict[str, Any] | None = None,
) -> ReconcileTriggerCoordinator:
    def _make(market: str):
        async def _kernel(*, dry_run: bool) -> dict[str, Any]:
            calls.append((market, dry_run))
            return result if result is not None else _kernel_result([])

        return _kernel

    coordinator = ReconcileTriggerCoordinator(
        window_seconds=60.0,
        clock=clock or _Clock(),
        kernels={market: _make(market) for market in ("kr", "us", "crypto")},
    )
    monkeypatch.setattr(
        ingest_module, "get_reconcile_trigger_coordinator", lambda: coordinator
    )
    return coordinator


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    return app


async def _post(body: dict[str, Any]):
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="https://test"
    ) as client:
        return await client.post(_PATH, json=body)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
async def test_market_dispatch(monkeypatch: pytest.MonkeyPatch, market: str) -> None:
    calls: list[tuple[str, bool]] = []
    _install(monkeypatch, calls=calls)

    resp = await _post({"market": market, "reason": "reconnect"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == market
    assert body["status"] == "executed"
    assert body["deduped"] is False
    assert body["backfilled"] == 0
    assert body["dedupe_window_seconds"] == 60.0
    assert calls == [(market, True)], "dry_run must default to True"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_defaults_to_true_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []
    _install(monkeypatch, calls=calls)

    body = (await _post({"market": "kr"})).json()

    assert body["dry_run"] is True
    assert calls == [("kr", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_run_reports_booked_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        calls=[],
        result=_kernel_result(["booked", "noop_pending", "booked_filled"]),
    )

    body = (await _post({"market": "kr", "dry_run": False})).json()

    assert body["backfilled"] == 2
    assert body["kernel"]["reconciled_rows"] == 3
    assert "reconciled" not in body["kernel"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_never_reports_backfilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, calls=[], result=_kernel_result(["would_book", "would_book"]))

    body = (await _post({"market": "kr", "dry_run": True})).json()

    assert body["backfilled"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_reconnect_inside_60s_is_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []
    clock = _Clock()
    _install(monkeypatch, calls=calls, clock=clock)

    first = (await _post({"market": "kr", "reason": "reconnect"})).json()
    clock.advance(59.9)
    second = (await _post({"market": "kr", "reason": "reconnect"})).json()

    assert first["status"] == "executed"
    assert second["status"] == "deduped"
    assert second["deduped"] is True
    assert second["backfilled"] == 0
    assert calls == [("kr", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_after_the_window_the_kernel_runs_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []
    clock = _Clock()
    _install(monkeypatch, calls=calls, clock=clock)

    await _post({"market": "kr"})
    clock.advance(61.0)
    again = (await _post({"market": "kr"})).json()

    assert again["status"] == "executed"
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_market_is_rejected_by_the_schema() -> None:
    resp = await _post({"market": "jp"})
    assert resp.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["manual", "", "reconnect-storm", "RECONNECT"])
async def test_reason_is_an_exact_literal(reason: str) -> None:
    """``reason`` is not a free-text slot: only ``"reconnect"`` is accepted."""
    resp = await _post({"market": "kr", "reason": reason})
    assert resp.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extra_body_keys_are_rejected() -> None:
    resp = await _post({"market": "kr", "limit": 500})
    assert resp.status_code == 422
