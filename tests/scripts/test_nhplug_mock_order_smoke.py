"""Offline tests for the NHPLUG Stage 2 order smoke CLI (#711)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services.nhplug_mock import operations
from app.services.nhplug_mock.ledger_service import NHPlugMockLedgerService
from scripts import nhplug_mock_order_smoke as smoke
from tests.services.nhplug_mock._fake_broker import MOCK_ACCOUNT, FakeNHMockBroker

SENTINEL_KEY = "NHPLUG_KEY_MUST_NOT_LEAK_123"
SENTINEL_SECRET = "NHPLUG_SECRET_MUST_NOT_LEAK_456"


def _env(tmp_path: Path) -> Path:
    path = tmp_path / ".env.nhplug-mock.native"
    path.write_text(
        f"NHPLUG_APP_KEY={SENTINEL_KEY}\nNHPLUG_APP_SECRET={SENTINEL_SECRET}\n"
        f"NHPLUG_MOCK_ACCOUNT_NO={MOCK_ACCOUNT}\n",
        encoding="utf-8",
    )
    return path


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    for secret in (SENTINEL_KEY, SENTINEL_SECRET, MOCK_ACCOUNT, "CUSTOMER_NAME"):
        assert secret not in out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class _NoNetwork:
    def __init__(self, **_: Any) -> None:
        raise AssertionError("constructed a network client")


@pytest.mark.unit
def test_preflight_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    monkeypatch.setattr(smoke, "NHPlugAuthClient", _NoNetwork)
    assert smoke.main(["--env-file", str(_env(tmp_path)), "--mode", "preflight"]) == 0
    [line] = _lines(capsys)
    assert line["network_calls"] == 0
    assert line["mutation_path_count"] == 4
    assert line["order_type"] == "limit_only"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "reason"),
    (
        (["--mode", "place", "--price", "50000"], "--confirm-mock-order"),
        (
            ["--mode", "roundtrip", "--price", "1", "--modify-price", "1"],
            "--confirm-mock-order",
        ),
        (["--mode", "positions"], "--confirm-read"),
        (["--mode", "place", "--confirm-mock-order"], "--price"),
        (
            ["--mode", "roundtrip", "--confirm-mock-order", "--price", "1"],
            "--modify-price",
        ),
        (["--mode", "cancel", "--confirm-mock-order"], "--order-id"),
    ),
)
def test_network_modes_need_explicit_flags_before_any_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    reason: str,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    monkeypatch.setattr(smoke, "NHPlugAuthClient", _NoNetwork)
    assert smoke.main(["--env-file", str(_env(tmp_path)), *argv]) == 2
    [line] = _lines(capsys)
    assert line["status"] == "failed" and reason in line["reason"]


@pytest.mark.unit
def test_gate_must_be_in_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)
    monkeypatch.setattr(smoke, "NHPlugAuthClient", _NoNetwork)
    assert smoke.main(["--env-file", str(_env(tmp_path)), "--mode", "preflight"]) == 2
    assert "NHPLUG_MOCK_ENABLED" in _lines(capsys)[0]["reason"]


@pytest.mark.unit
def test_prod_env_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    prod = tmp_path / ".env.prod.nhplug"
    prod.write_text(_env(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")
    assert smoke.main(["--env-file", str(prod), "--mode", "preflight"]) == 2
    assert "prod" in _lines(capsys)[0]["reason"]


def _wire(
    monkeypatch: pytest.MonkeyPatch, broker: FakeNHMockBroker, db_session: Any
) -> None:
    class _Auth:
        def __init__(self, **_: Any) -> None:
            pass

        async def get_access_token(self) -> str:
            return "t"

    real_open = operations.open_verified_client

    async def open_with_fake(
        credentials: Any, *, token_provider: Any, transport: Any = None
    ):
        return await real_open(
            credentials, token_provider=token_provider, transport=broker.transport
        )

    async def ledger_scope(body: Any) -> int:
        return await body(NHPlugMockLedgerService(db_session))

    monkeypatch.setattr(smoke, "NHPlugAuthClient", _Auth)
    monkeypatch.setattr(smoke.operations, "open_verified_client", open_with_fake)
    monkeypatch.setattr(smoke, "_ledger_scope", ledger_scope)


def _roundtrip_argv(tmp_path: Path, date: str) -> list[str]:
    return [
        "--env-file",
        str(_env(tmp_path)),
        "--mode",
        "roundtrip",
        "--confirm-mock-order",
        "--price",
        "67400",
        "--modify-price",
        "67000",
        "--order-date",
        date,
        "--settle-seconds",
        "0",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_offline_roundtrip_passes_every_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: Any,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    broker = FakeNHMockBroker()
    _wire(monkeypatch, broker, db_session)
    code = await _run(_roundtrip_argv(tmp_path, _order_date()))
    lines = _lines(capsys)
    steps = [line.get("step") for line in lines]
    assert code == 0, lines[-1]
    assert steps[-1] == "summary" and lines[-1]["status"] == "ok"
    assert lines[-1]["empty_open_listing_reported_as_unknown"] is True
    reconcile = next(line for line in lines if line.get("step") == "9_reconcile_apply")
    assert reconcile["unresolved"] == 0
    assert broker.order_paths() == [
        "/krstock/order/v1/cashBuy",
        "/krstock/order/v1/modify",
        "/krstock/order/v1/cancel",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_roundtrip_aborts_and_cancels_when_listing_hides_the_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: Any,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    broker = FakeNHMockBroker()
    _wire(monkeypatch, broker, db_session)
    real_open_orders = operations.get_open_orders
    calls = {"n": 0}

    async def hide_after_place(client: Any, **kwargs: Any) -> dict[str, Any]:
        result = await real_open_orders(client, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:  # the check right after the place step
            return {**result, "open_orders_state": "unknown", "open_orders": []}
        return result

    monkeypatch.setattr(smoke.operations, "get_open_orders", hide_after_place)
    code = await _run(_roundtrip_argv(tmp_path, _order_date()))
    lines = _lines(capsys)
    abort = lines[-1]
    assert code == 2
    assert abort["step"] == "abort"
    assert abort["cleanup_cancel"]["attempted"] is True
    assert abort["cleanup_cancel"]["status"] == "accepted"
    assert broker.order_paths() == [
        "/krstock/order/v1/cashBuy",
        "/krstock/order/v1/cancel",
    ]
    assert broker.orders[0].open_qty == 0


async def _run(argv: list[str]) -> int:
    return await smoke.run(smoke.build_parser().parse_args(argv))


def _order_date() -> str:
    return f"3{uuid.uuid4().int % 10_000_000:07d}"


# --- tester round 1 (finding 7): no false "ok" ------------------------------


def _patch_nth_open_orders(
    monkeypatch: pytest.MonkeyPatch, nth: int, override: dict[str, Any]
) -> None:
    real = operations.get_open_orders
    calls = {"n": 0}

    async def wrapped(client: Any, **kwargs: Any) -> dict[str, Any]:
        result = await real(client, **kwargs)
        calls["n"] += 1
        return {**result, **override} if calls["n"] == nth else result

    monkeypatch.setattr(smoke.operations, "get_open_orders", wrapped)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nth", "override"),
    (
        (
            4,
            {
                "open_orders_state": "unknown",
                "open_orders": [],
                "reasons": ["open_scope_incomplete:gateway_error_envelope"],
            },
        ),  # after cancel: a broken source, not the clean empty unknown
        (
            5,
            {
                "open_orders_state": "unknown",
                "open_orders": [],
                "reasons": ["all_scope_incomplete:pagination_truncated"],
            },
        ),  # final check
        (5, {"open_orders_state": "present", "open_orders": []}),  # final, not none
        (
            1,
            {
                "open_orders_state": "unknown",
                "reasons": ["open_scope_incomplete:gateway_error_envelope"],
            },
        ),  # error-shaped baseline
    ),
    ids=("after_cancel_unknown", "final_unknown", "final_present", "baseline_error"),
)
async def test_roundtrip_never_reports_ok_on_unknown_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: Any,
    nth: int,
    override: dict[str, Any],
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    broker = FakeNHMockBroker()
    _wire(monkeypatch, broker, db_session)
    _patch_nth_open_orders(monkeypatch, nth, override)
    code = await _run(_roundtrip_argv(tmp_path, _order_date()))
    lines = _lines(capsys)
    assert code == 2
    assert lines[-1]["step"] == "abort"
    assert not any(line.get("step") == "summary" for line in lines)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_roundtrip_aborts_when_reconcile_leaves_rows_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: Any,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    broker = FakeNHMockBroker()
    _wire(monkeypatch, broker, db_session)

    async def unresolved(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"success": False, "unresolved": 1, "results": []}

    monkeypatch.setattr(smoke.operations, "reconcile_orders", unresolved)
    code = await _run(_roundtrip_argv(tmp_path, _order_date()))
    lines = _lines(capsys)
    assert code == 2 and lines[-1]["step"] == "abort"
    assert lines[-1]["reason"] == "reconcile did not verify every ledger row"
