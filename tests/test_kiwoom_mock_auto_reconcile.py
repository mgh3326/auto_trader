"""hk 4722 (3797 T-P3) — Kiwoom mock reconcile CLI modes and exit codes.

The kernel is replaced by a stub, so these tests cover the CLI contract only:
the gate refuses before any work, dry run is the default, and exit 3 means work
remains (unresolved rows / deferred fill booking / truncated scan).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import scripts.kiwoom_mock_auto_reconcile as cli
from app.services.kiwoom_mock_reconcile import FULL_GATE_ENVS, TERMINAL_GATE_ENV

pytestmark = pytest.mark.unit


class _Stub:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.result


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "dry_run": True,
        "scope": "dry_run",
        "reconciled": [],
        "candidate_scan": {"truncated": False},
        "unresolved_count": 0,
    }
    result.update(overrides)
    return result


def _install(monkeypatch: pytest.MonkeyPatch, stub: _Stub) -> None:
    monkeypatch.setattr(cli, "kiwoom_mock_reconcile_orders_impl", stub)


def test_terminal_only_without_gate_refuses_before_any_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(TERMINAL_GATE_ENV, raising=False)
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    assert cli.main(["--apply-terminal-only"]) == 2

    assert stub.calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "paused"
    assert TERMINAL_GATE_ENV in payload["message"]


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "", " true"])
def test_gate_is_exact_true_only(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, value)
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    assert cli.main(["--apply-terminal-only"]) == 2
    assert stub.calls == []


def test_terminal_only_with_gate_runs_apply_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    stub = _Stub(_result(dry_run=False, scope="terminal_only"))
    _install(monkeypatch, stub)

    assert cli.main(["--apply-terminal-only"]) == 0

    assert stub.calls[0]["dry_run"] is False
    assert stub.calls[0]["terminal_only"] is True
    assert stub.calls[0]["limit"] == 100
    assert stub.calls[0]["min_request_interval_seconds"] == 1.0
    capsys.readouterr()


def test_dry_run_is_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    assert cli.main([]) == 0

    assert stub.calls[0]["dry_run"] is True
    assert stub.calls[0]["terminal_only"] is False
    capsys.readouterr()


def test_pending_work_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _Stub(
        _result(
            reconciled=[
                {"action": "would_mark_cancelled"},
                {"action": "deferred_fill_booking"},
            ]
        )
    )
    _install(monkeypatch, stub)

    assert cli.main([]) == 3
    capsys.readouterr()


@pytest.mark.parametrize(
    "result",
    [
        _result(unresolved_count=1),
        _result(candidate_scan={"truncated": True}),
        _result(reconciled=[{"action": "noop_no_evidence"}]),
    ],
)
def test_unresolved_or_truncated_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], result
) -> None:
    _install(monkeypatch, _Stub(result))

    assert cli.main([]) == 3
    capsys.readouterr()


def test_kernel_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Stub(_result(success=False, error="boom")))

    assert cli.main([]) == 1
    capsys.readouterr()


def test_limit_and_spacing_are_configurable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    assert cli.main(["--limit", "5", "--min-request-interval-seconds", "2.5"]) == 0
    assert stub.calls[0]["limit"] == 5
    assert stub.calls[0]["min_request_interval_seconds"] == 2.5
    capsys.readouterr()


# ---- hk 4734 (3797 T-P4): full mode ---------------------------------------


def _clear_full_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "enabled",
    [
        {},
        {"KIWOOM_MOCK_AUTO_RECONCILE_ENABLED": "true"},
        {"KIWOOM_MOCK_AUTO_RECONCILE_SAFETY_REVIEW_PASSED": "true"},
        {
            "KIWOOM_MOCK_AUTO_RECONCILE_ENABLED": "true",
            "KIWOOM_MOCK_AUTO_RECONCILE_SAFETY_REVIEW_PASSED": "TRUE",
        },
    ],
)
def test_full_apply_without_both_gates_refuses_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enabled: dict[str, str],
) -> None:
    _clear_full_gates(monkeypatch)
    for name, value in enabled.items():
        monkeypatch.setenv(name, value)
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    assert cli.main(["--apply"]) == 2

    assert stub.calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "paused"
    assert payload["required_env"] == list(FULL_GATE_ENVS)
    for name in FULL_GATE_ENVS:
        assert name in payload["message"]


def test_full_apply_with_both_gates_runs_full_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    stub = _Stub(_result(dry_run=False, scope="full"))
    _install(monkeypatch, stub)

    assert cli.main(["--apply"]) == 0

    assert stub.calls == [
        {
            "market": "kr",
            "dry_run": False,
            "terminal_only": False,
            "limit": 100,
            "min_request_interval_seconds": (cli.DEFAULT_MIN_REQUEST_INTERVAL_SECONDS),
        }
    ]


def test_full_apply_is_refused_for_the_us_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full-mode fill booking is KR only (T-P4); the US scan stays terminal-only."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    stub = _Stub(_result(dry_run=False, scope="full"))
    _install(monkeypatch, stub)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--apply", "--market", "us"])

    assert excinfo.value.code == 2
    assert stub.calls == []


def test_apply_and_apply_terminal_only_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    stub = _Stub(_result())
    _install(monkeypatch, stub)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--apply", "--apply-terminal-only"])

    assert excinfo.value.code == 2
    assert stub.calls == []
