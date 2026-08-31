"""CLI mode selection stays explicit and defaults to the non-mutating path."""

from __future__ import annotations

import datetime as dt
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts import run_b0x_kr_kiwoom_cycle as cli
from scripts.b0x.kr import kiwoom_bounded_send, kiwoom_coordination, kiwoom_cycle
from scripts.run_b0x_kr_kiwoom_cycle import _parse_args, _run

pytestmark = pytest.mark.unit


def test_cli_defaults_to_preview_and_preserves_acceptance_form() -> None:
    preview = _parse_args([])
    acceptance = _parse_args(["--confirm"])
    ordering = _parse_args(["--ordering", "--confirm"])

    assert preview.confirm is False
    assert preview.ordering is False
    assert preview.bounded_send is False
    assert preview.seal is None
    assert preview.durable_ports_factory is None
    assert acceptance.confirm is True
    assert acceptance.ordering is False
    assert ordering.confirm is True
    assert ordering.ordering is True
    assert set(vars(ordering)) == {
        "table_dir",
        "out_dir",
        "readiness",
        "confirm",
        "ordering",
        "bounded_send",
        "seal",
        "durable_ports_factory",
        "now",
        "json",
    }


def _outcome() -> SimpleNamespace:
    return SimpleNamespace(
        lane="kr.kiwoom.mock",
        at=dt.datetime(2026, 8, 31, 0, 0, tzinfo=dt.UTC),
        zero_order_reason=None,
        record={},
        table_hash=None,
        derivation=None,
        artifact_path=None,
        exit_code=0,
    )


@pytest.mark.asyncio
async def test_cli_bounded_send_reaches_registered_seal_factory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """The explicit flag injects the bounded factory without real consumption."""

    expires_at = "2026-08-31T06:30:00Z"
    seal = {
        "lane_id": "kr.kiwoom.mock",
        "physical_account_id": "test-account",
        "expires_at": expires_at,
        "seal_digest": kiwoom_bounded_send.compute_bounded_send_seal_digest(
            lane_id="kr.kiwoom.mock",
            physical_account_id="test-account",
            expires_at=expires_at,
        ),
    }
    seal_path = tmp_path / "seal.json"
    seal_path.write_text(json.dumps(seal), encoding="utf-8")

    ports_module = ModuleType("rob1337_test_durable_ports")

    def ports_factory(_entry):  # noqa: ANN001, ANN202 - injected test sentinel
        return object()

    ports_module.build_ports = ports_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, ports_module.__name__, ports_module)

    selected_factory_calls = 0
    bounded_builds: list[tuple[object, object]] = []
    production_builds = 0
    cycle_factories: list[object] = []

    def selected_factory() -> object:
        nonlocal selected_factory_calls
        selected_factory_calls += 1
        return object()

    def build_bounded(*, seal, ports_factory):  # noqa: ANN001, ANN202
        bounded_builds.append((seal, ports_factory))
        return selected_factory

    def build_production():  # noqa: ANN202
        nonlocal production_builds
        production_builds += 1
        return lambda: object()

    async def run_cycle(**kwargs):  # noqa: ANN003, ANN202
        factory = kwargs["coordination_factory"]
        cycle_factories.append(factory)
        factory()
        return _outcome()

    monkeypatch.setattr(
        kiwoom_coordination,
        "build_bounded_send_kiwoom_coordination_factory",
        build_bounded,
    )
    monkeypatch.setattr(
        kiwoom_coordination,
        "production_kiwoom_coordination_factory",
        build_production,
    )
    monkeypatch.setattr(kiwoom_cycle, "run_kiwoom_cycle", run_cycle)

    result = await _run(
        _parse_args(
            [
                "--bounded-send",
                "--seal",
                str(seal_path),
                "--durable-ports-factory",
                f"{ports_module.__name__}:build_ports",
                "--confirm",
            ]
        )
    )

    assert result == 0
    assert bounded_builds == [(seal, ports_factory)]
    assert production_builds == 0
    assert cycle_factories == [selected_factory]
    assert selected_factory_calls == 1


@pytest.mark.asyncio
async def test_cli_default_still_injects_production_grant_only_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bounded flag means the existing production factory remains selected."""

    bounded_builds: list[tuple[object, object]] = []
    cycle_factories: list[object] = []
    owners: list[object] = []

    def build_bounded(*, seal, ports_factory):  # noqa: ANN001, ANN202
        bounded_builds.append((seal, ports_factory))
        return lambda: SimpleNamespace(grant_only=False)

    async def run_cycle(**kwargs):  # noqa: ANN003, ANN202
        factory = kwargs["coordination_factory"]
        cycle_factories.append(factory)
        owners.append(factory())
        return _outcome()

    monkeypatch.setattr(
        kiwoom_coordination,
        "build_bounded_send_kiwoom_coordination_factory",
        build_bounded,
    )
    monkeypatch.setattr(kiwoom_cycle, "run_kiwoom_cycle", run_cycle)
    # These are only reached by mutant (b); return sentinels so the mutant
    # reaches an assertion RED instead of failing in configuration plumbing.
    monkeypatch.setattr(cli, "_load_bounded_send_seal", lambda _path: {})
    monkeypatch.setattr(cli, "_load_durable_ports_factory", lambda _ref: object())
    monkeypatch.setattr(
        kiwoom_bounded_send,
        "snapshot_bounded_send_seal",
        lambda _seal: SimpleNamespace(canonical=lambda: {}),
    )

    result = await _run(_parse_args([]))

    assert result == 0
    assert bounded_builds == []
    assert len(cycle_factories) == 1
    assert len(owners) == 1
    assert getattr(owners[0], "grant_only", None) is True


@pytest.mark.asyncio
async def test_cli_rejects_ordering_without_per_call_confirmation() -> None:
    """This returns before any cycle/account work, so it cannot reach a broker."""

    assert await _run(_parse_args(["--ordering"])) == 2


@pytest.mark.asyncio
async def test_cli_rejects_bounded_send_without_per_call_confirmation() -> None:
    assert (
        await _run(
            _parse_args(
                [
                    "--bounded-send",
                    "--seal",
                    "unused.json",
                    "--durable-ports-factory",
                    "unused:build_ports",
                ]
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_cli_rejects_ordering_replay_clock_before_any_cycle_work() -> None:
    assert (
        await _run(
            _parse_args(
                [
                    "--ordering",
                    "--confirm",
                    "--now",
                    "2026-08-10T02:00:00+00:00",
                ]
            )
        )
        == 2
    )
