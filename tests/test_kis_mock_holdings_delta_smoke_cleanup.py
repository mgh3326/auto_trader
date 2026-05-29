"""ROB-358 — cleanup gate/reason hardening for the holdings-delta smoke.

These tests prove the ``--confirm`` round trip:

* fail-fast BEFORE any BUY when the cleanup SELL cannot be safely executed
  (missing ``KIS_MOCK_SCALPING_ENABLED`` or an invalid cleanup exit reason);
* classifies a cleanup SELL submit rejection as an explicit cleanup anomaly
  (exit 3) rather than leaking out as an unexpected exception (exit 1);
* returns the entry/exit happy path to baseline (final delta 0);
* surfaces a residual position as an explicit anomaly.

stdlib + fakes only; no broker / network / secrets.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest

import scripts.kis_mock_holdings_delta_smoke as smoke


class _FakeSettings:
    """Minimal stand-in for the Settings object the gate returns."""

    def __init__(self, *, scalping_enabled: bool) -> None:
        self.kis_mock_scalping_enabled = scalping_enabled
        self.kis_mock_scalping_ws_enabled = True
        self.kis_mock_app_key = "k"
        self.kis_mock_app_secret = "s"
        self.kis_mock_account_no = "a"


def _confirm_args(extra: list[str] | None = None):
    argv = [
        "--confirm",
        "--symbol",
        "005930",
        "--max-poll",
        "1",
        "--poll-interval",
        "0",
    ]
    return smoke._parse_args(argv + (extra or []))


# --- fail-fast preflight (before any BUY) ----------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fails_fast_when_scalping_enabled_missing(monkeypatch):
    monkeypatch.setattr(
        smoke, "_gate_or_exit", lambda: _FakeSettings(scalping_enabled=False)
    )
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )

    rc = await smoke.run_confirm(_confirm_args())

    assert rc == 4
    created.assert_not_called()  # no BUY path was ever constructed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fails_fast_on_invalid_cleanup_reason(monkeypatch):
    monkeypatch.setattr(
        smoke, "_gate_or_exit", lambda: _FakeSettings(scalping_enabled=True)
    )
    created = mock.Mock()
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", created
    )

    rc = await smoke.run_confirm(_confirm_args(["--cleanup-reason", "bogus_reason"]))

    assert rc == 4
    created.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_preflight_accepts_default_stop_loss_reason(monkeypatch):
    # The default cleanup reason must be an allowed scalping-exit reason so the
    # gate passes and the BUY path is reached (we stop it at client creation).
    monkeypatch.setattr(
        smoke, "_gate_or_exit", lambda: _FakeSettings(scalping_enabled=True)
    )
    sentinel = RuntimeError("reached BUY path")

    def _boom(*_a, **_k):
        raise sentinel

    monkeypatch.setattr(
        "app.mcp_server.tooling.order_execution._create_kis_client", _boom
    )

    # main() swallows exceptions; here we call run_confirm directly and assert
    # the preflight let us through to client construction.
    with pytest.raises(RuntimeError, match="reached BUY path"):
        await smoke.run_confirm(_confirm_args())


# --- cleanup submit rejection -> explicit anomaly, not exit 1 --------------


class _RejectingBroker:
    async def _read_snapshot(self, _symbol):
        return Decimal("8"), Decimal("100")

    async def submit_exit_sell(self, **_kwargs):
        raise ValueError("invalid scalping_exit reason: smoke_cleanup")


class _FakeClient:
    async def inquire_orderbook(self, *_a, **_k):
        return {"askp1": "100", "bidp1": "99"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_submit_rejection_is_anomaly_not_unexpected():
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        _RejectingBroker(),
        _FakeClient(),
        _confirm_args(),
        "cid",
        Decimal("7"),  # baseline -> residual of 1
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup_sell_order_id"] is None
    assert "cleanup_error" in evidence
    assert evidence["final_position_delta_vs_baseline"] == "1"


# --- happy path: flattened back to baseline (final delta 0) ----------------


class _SuccessfulBroker:
    def __init__(self) -> None:
        # cleanup reads snapshot twice: current (8) then post-sell (7 == base).
        self._snapshots = [
            (Decimal("8"), Decimal("100")),
            (Decimal("7"), Decimal("110")),
        ]

    async def _read_snapshot(self, _symbol):
        return self._snapshots.pop(0)

    async def submit_exit_sell(self, **_kwargs):
        return {"odno": "0000000123"}

    async def confirm_fill(self, _submit):
        return object()  # non-None fill


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_happy_path_flattens_to_baseline():
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        _SuccessfulBroker(),
        _FakeClient(),
        _confirm_args(),
        "cid",
        Decimal("7"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 0
    assert evidence["cleanup"] == "flattened"
    assert evidence["cleanup_sell_order_id"] == "0000000123"
    assert evidence["final_position_delta_vs_baseline"] == "0"


# --- residual position remains -> explicit anomaly -------------------------


class _ResidualBroker:
    async def _read_snapshot(self, _symbol):
        return Decimal("8"), Decimal("100")  # stays at 8, never returns to 7

    async def submit_exit_sell(self, **_kwargs):
        return {"odno": "0000000999"}

    async def confirm_fill(self, _submit):
        return None  # exit never confirmed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_residual_position_is_explicit_anomaly():
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        _ResidualBroker(),
        _FakeClient(),
        _confirm_args(),
        "cid",
        Decimal("7"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup"] == "UNCONFIRMED_residual_position"
    assert evidence["final_position_delta_vs_baseline"] == "1"


# --- non-zero NEGATIVE delta must never be a clean exit (ROB-358) -----------


class _BelowBaselineBroker:
    async def _read_snapshot(self, _symbol):
        return Decimal("6"), Decimal("100")  # already below baseline 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_below_baseline_at_start_is_anomaly_not_clean():
    # cur_qty < base_qty at cleanup start: final delta is -1, so even with an
    # entry fill present this must NOT exit 0.
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        _BelowBaselineBroker(),
        _FakeClient(),
        _confirm_args(),
        "cid",
        Decimal("7"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["final_position_delta_vs_baseline"] == "-1"
    assert evidence["cleanup"] != "nothing_to_flatten"
    assert "cleanup_error" in evidence


class _OverFlattenBroker:
    def __init__(self) -> None:
        # current (8, residual of 1) then post-sell (6, BELOW baseline 7).
        self._snapshots = [
            (Decimal("8"), Decimal("100")),
            (Decimal("6"), Decimal("120")),
        ]

    async def _read_snapshot(self, _symbol):
        return self._snapshots.pop(0)

    async def submit_exit_sell(self, **_kwargs):
        return {"odno": "0000000777"}

    async def confirm_fill(self, _submit):
        return object()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_over_flatten_below_baseline_is_anomaly():
    # final_qty < base_qty after the cleanup SELL: final delta is -1, so it must
    # be an explicit anomaly and NOT report cleanup="flattened".
    evidence: dict[str, object] = {}
    rc = await smoke._cleanup_and_verify(
        _OverFlattenBroker(),
        _FakeClient(),
        _confirm_args(),
        "cid",
        Decimal("7"),
        evidence,
        entry_fill=object(),
    )
    assert rc == 3
    assert evidence["cleanup"] != "flattened"
    assert evidence["final_position_delta_vs_baseline"] == "-1"
    assert "cleanup_error" in evidence
