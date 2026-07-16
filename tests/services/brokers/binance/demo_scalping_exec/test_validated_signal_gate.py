"""ROB-905 — fail-closed validated-signal gate for Demo scalping confirm=true.

The gate reads a JSON artifact whose path is given by
``BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH``. ``confirm=true`` may only take
effect when the artifact exists, parses, carries the
``validated_signal_gate.v1`` schema, a ``validated`` verdict, and (if present)
a future ``valid_until``. Every other case is fail-closed (``allowed=False``)
with a distinct, machine-readable ``reason``; no exception ever escapes.
"""

from __future__ import annotations

import datetime as dt
import json

from app.schemas.validated_run_card import GATE_SCHEMA
from app.services.brokers.binance.demo_scalping_exec.validated_signal_gate import (
    _GATE_PATH_ENV,
    _GATE_SCHEMA,
    GateDecision,
    evaluate_validated_signal_gate,
)

_ENV = _GATE_PATH_ENV


def _write(tmp_path, payload, *, name="gate.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_local_schema_literal_matches_schema_module() -> None:
    # The stdlib-only module duplicates the literal; it must not drift.
    assert _GATE_SCHEMA == GATE_SCHEMA


def test_gate_path_unset(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    decision = evaluate_validated_signal_gate()
    assert isinstance(decision, GateDecision)
    assert decision.allowed is False
    assert decision.reason == "gate_path_unset"


def test_gate_path_blank(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "   ")
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_path_unset"


def test_gate_file_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path / "does_not_exist.json"))
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_file_missing"


def test_gate_file_unreadable(monkeypatch, tmp_path) -> None:
    # A directory path is not a readable file → OSError, fail closed.
    d = tmp_path / "a_dir"
    d.mkdir()
    monkeypatch.setenv(_ENV, str(d))
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason in {"gate_file_unreadable", "gate_file_missing"}
    # A directory specifically must not be reported as a valid pass.
    assert decision.reason == "gate_file_unreadable"


def test_gate_invalid_json(monkeypatch, tmp_path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv(_ENV, str(p))
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_invalid_json"


def test_gate_json_not_object(monkeypatch, tmp_path) -> None:
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv(_ENV, str(p))
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_invalid_json"


def test_gate_schema_mismatch(monkeypatch, tmp_path) -> None:
    path = _write(tmp_path, {"schema": "something_else.v1", "verdict": "validated"})
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_schema_mismatch"


def test_gate_verdict_not_validated(monkeypatch, tmp_path) -> None:
    path = _write(tmp_path, {"schema": GATE_SCHEMA, "verdict": "not_validated"})
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_verdict_not_validated"


def test_gate_verdict_missing(monkeypatch, tmp_path) -> None:
    path = _write(tmp_path, {"schema": GATE_SCHEMA})
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_verdict_not_validated"


def test_gate_expired(monkeypatch, tmp_path) -> None:
    past = "2020-01-01T00:00:00+00:00"
    path = _write(
        tmp_path,
        {"schema": GATE_SCHEMA, "verdict": "validated", "valid_until": past},
    )
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_expired"


def test_gate_expired_unparseable_valid_until(monkeypatch, tmp_path) -> None:
    # Unparseable expiry is fail-closed, not silently allowed.
    path = _write(
        tmp_path,
        {"schema": GATE_SCHEMA, "verdict": "validated", "valid_until": "not-a-date"},
    )
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is False
    assert decision.reason == "gate_expired"


def test_gate_valid_no_expiry(monkeypatch, tmp_path) -> None:
    path = _write(tmp_path, {"schema": GATE_SCHEMA, "verdict": "validated"})
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is True
    assert decision.reason == "validated"


def test_gate_valid_future_expiry(monkeypatch, tmp_path) -> None:
    future = "2999-01-01T00:00:00+00:00"
    path = _write(
        tmp_path,
        {"schema": GATE_SCHEMA, "verdict": "validated", "valid_until": future},
    )
    monkeypatch.setenv(_ENV, path)
    decision = evaluate_validated_signal_gate()
    assert decision.allowed is True
    assert decision.reason == "validated"


def test_gate_expiry_uses_injected_now(monkeypatch, tmp_path) -> None:
    valid_until = "2026-01-01T00:00:00+00:00"
    path = _write(
        tmp_path,
        {"schema": GATE_SCHEMA, "verdict": "validated", "valid_until": valid_until},
    )
    monkeypatch.setenv(_ENV, path)

    before = dt.datetime(2025, 12, 31, tzinfo=dt.UTC)
    after = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    assert evaluate_validated_signal_gate(now=before).allowed is True
    assert evaluate_validated_signal_gate(now=after).allowed is False
    assert evaluate_validated_signal_gate(now=after).reason == "gate_expired"
