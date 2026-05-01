"""Safety tests for scripts/smoke/alpaca_paper_dev_smoke.py (ROB-73)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.mcp_server.tooling.alpaca_paper import (
    reset_alpaca_paper_service_factory,
    set_alpaca_paper_service_factory,
)
from app.mcp_server.tooling.alpaca_paper_orders import (
    reset_alpaca_paper_orders_service_factory,
    set_alpaca_paper_orders_service_factory,
)

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke"
    / "alpaca_paper_dev_smoke.py"
)

FORBIDDEN_SECRET_STRINGS = (
    "APCA-API-KEY-ID",
    "APCA-API-SECRET-KEY",
    "Authorization",
    "get_secret_value",
    "api_secret",
)


@pytest.mark.unit
def test_dev_smoke_script_exists() -> None:
    assert SCRIPT_PATH.exists()


@pytest.mark.unit
def test_dev_smoke_script_has_no_secret_or_header_strings() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    hits = [s for s in FORBIDDEN_SECRET_STRINGS if s in text]
    assert not hits, f"dev smoke script references secret strings: {hits}"


@pytest.mark.unit
def test_dev_smoke_script_no_raw_payload_print() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    raw_names = {
        "payload",
        "result",
        "orders",
        "positions",
        "account",
        "fills",
        "assets",
        "order",
        "submit",
        "cancel",
        "cash",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in raw_names:
                        pytest.fail(
                            f"smoke script calls print({arg.id}) "
                            "which would dump a raw broker payload"
                        )


@pytest.mark.unit
def test_dev_smoke_script_does_not_route_through_legacy_order_tools() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    forbidden = (
        "place_order",
        "modify_order",
        "replace_order",
        "cancel_all",
        "cancel_by_symbol",
    )
    hits = [s for s in forbidden if s in text]
    assert not hits, f"dev smoke script references forbidden order routes: {hits}"


def _load_module():
    spec = importlib.util.spec_from_file_location("_alpaca_dev_smoke", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.mark.unit
def test_dev_smoke_parser_accepts_crypto_operator_metadata() -> None:
    module = _load_module()
    args = module.build_parser().parse_args(
        [
            "--asset-class",
            "crypto",
            "--symbol",
            "BTC/USD",
            "--notional",
            "10",
            "--limit-price",
            "50000",
            "--candidate-report",
            str(SCRIPT_PATH),
        ]
    )
    assert args.asset_class == "crypto"
    assert args.symbol == "BTC/USD"
    assert args.notional == module.Decimal("10")
    assert args.limit_price == module.Decimal("50000")
    assert module._order_payload(args)["time_in_force"] == "gtc"


@pytest.mark.unit
def test_dev_smoke_parser_accepts_crypto_ioc_time_in_force() -> None:
    module = _load_module()
    args = module.build_parser().parse_args(
        [
            "--asset-class",
            "crypto",
            "--symbol",
            "BTC/USD",
            "--notional",
            "10",
            "--limit-price",
            "50000",
            "--time-in-force",
            "ioc",
        ]
    )

    assert module._order_payload(args)["time_in_force"] == "ioc"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tif", ["day", "fok"])
async def test_dev_smoke_crypto_blocks_invalid_time_in_force_before_broker_calls(
    monkeypatch: pytest.MonkeyPatch,
    bad_tif: str,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    ro = FakeAlpacaPaperService()
    orders = FakeOrdersService()
    set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
    try:
        module = _load_module()
        args = module.build_parser().parse_args(
            [
                "--asset-class",
                "crypto",
                "--symbol",
                "BTC/USD",
                "--notional",
                "10",
                "--limit-price",
                "50000",
                "--time-in-force",
                bad_tif,
            ]
        )
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_service_factory()
        reset_alpaca_paper_orders_service_factory()

    assert rc == 2
    assert ro.calls == []
    assert orders.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_crypto_preview_uses_confirm_false_and_redacted_report(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    ro = FakeAlpacaPaperService()
    orders = FakeOrdersService()
    set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
    try:
        module = _load_module()
        args = module.build_parser().parse_args(
            [
                "--asset-class",
                "crypto",
                "--symbol",
                "BTC/USD",
                "--notional",
                "10",
                "--limit-price",
                "50000",
                "--candidate-report",
                str(SCRIPT_PATH),
            ]
        )
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_service_factory()
        reset_alpaca_paper_orders_service_factory()

    out = capsys.readouterr().out
    assert rc == 0
    assert "mode=preview_only" in out
    assert "asset_class=crypto" in out
    assert "candidate_report_attached=True" in out
    assert "blocked_reason=confirmation_required" in out
    assert "cancel_order(confirm=False)" in out
    assert [c for c in orders.calls if c[0] in ("submit_order", "cancel_order")] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_candidate_report_must_exist_before_broker_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    ro = FakeAlpacaPaperService()
    orders = FakeOrdersService()
    set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
    try:
        module = _load_module()
        args = module.build_parser().parse_args(
            [
                "--asset-class",
                "crypto",
                "--candidate-report",
                str(tmp_path / "rob74-missing-candidate-report.md"),
            ]
        )
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_service_factory()
        reset_alpaca_paper_orders_service_factory()

    assert rc == 2
    assert ro.calls == []
    assert orders.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_crypto_side_effect_requires_explicit_limit_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService

    orders = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.setenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", "1")
    try:
        module = _load_module()
        args = module.build_parser().parse_args(
            ["--asset-class", "crypto", "--confirm-paper-side-effect"]
        )
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_orders_service_factory()
        monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)

    assert rc == 2
    assert [c for c in orders.calls if c[0] in ("submit_order", "cancel_order")] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_default_mode_no_broker_calls(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    ro = FakeAlpacaPaperService()
    orders = FakeOrdersService()
    set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
    try:
        module = _load_module()
        args = module.build_parser().parse_args([])
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_service_factory()
        reset_alpaca_paper_orders_service_factory()

    captured = capsys.readouterr()
    assert rc == 0
    assert "mode=preview_only" in captured.out
    submit_calls = [c for c in orders.calls if c[0] == "submit_order"]
    cancel_calls = [c for c in orders.calls if c[0] == "cancel_order"]
    assert submit_calls == []
    assert cancel_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_flag_without_env_is_blocked(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService

    orders = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
    try:
        module = _load_module()
        args = module.build_parser().parse_args(["--confirm-paper-side-effect"])
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_orders_service_factory()

    assert rc == 2
    assert [c for c in orders.calls if c[0] in ("submit_order", "cancel_order")] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_env_without_flag_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService

    orders = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.setenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", "1")
    try:
        module = _load_module()
        args = module.build_parser().parse_args([])
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_orders_service_factory()
        monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)

    assert rc == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_smoke_both_gates_runs_submit_then_cancel(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_alpaca_paper_orders_tools import FakeOrdersService
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    ro = FakeAlpacaPaperService()
    orders = FakeOrdersService()
    set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
    set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
    monkeypatch.setenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", "1")
    try:
        module = _load_module()
        args = module.build_parser().parse_args(["--confirm-paper-side-effect"])
        rc = await module._async_main(args)
    finally:
        reset_alpaca_paper_service_factory()
        reset_alpaca_paper_orders_service_factory()
        monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)

    out = capsys.readouterr().out
    assert rc == 0
    assert "mode=side_effects" in out
    submit_calls = [c for c in orders.calls if c[0] == "submit_order"]
    cancel_calls = [c for c in orders.calls if c[0] == "cancel_order"]
    assert len(submit_calls) == 1
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1]["order_id"] == "paper-order-123"
