"""§4 envelope is a constant, and §2-3 writer=1 is enforced by a real lock."""

from __future__ import annotations

import ast
import multiprocessing as mp
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.b0x.envelope import (
    CRYPTO_SIDECAR_ENVELOPE,
    EnvelopeNotLocked,
    assert_envelope_locked,
    load_envelope,
)
from scripts.b0x.ledger import WriterLockUnavailable, writer_lock
from scripts.run_b0x_cycle import _parse_args

pytestmark = pytest.mark.unit


def test_crypto_envelope_matches_contract_section_4() -> None:
    """주문 10 USDT · 종목 총 50 · 동시 ≤3 · 일 신규 ≤2 · 일 손실 5 → kill."""

    envelope = CRYPTO_SIDECAR_ENVELOPE
    assert envelope.quote_currency == "USDT"
    assert envelope.per_order_notional == Decimal("10")
    assert envelope.per_symbol_total_notional == Decimal("50")
    assert envelope.max_concurrent_positions == 3
    assert envelope.max_new_entries_per_utc_day == 2
    assert envelope.daily_loss_kill == Decimal("5")


@pytest.mark.parametrize(
    "field,value",
    [
        ("per_order_notional", Decimal("11")),
        ("per_symbol_total_notional", Decimal("500")),
        ("max_concurrent_positions", 4),
        ("max_new_entries_per_utc_day", 3),
        ("daily_loss_kill", Decimal("50")),
    ],
)
def test_widened_envelope_fails_closed(field: str, value: object) -> None:
    """Every single-field widening is rejected — not just wholesale replacement."""

    widened = replace(CRYPTO_SIDECAR_ENVELOPE, **{field: value})
    assert widened != CRYPTO_SIDECAR_ENVELOPE
    with pytest.raises(EnvelopeNotLocked):
        assert_envelope_locked(widened)


def test_narrowed_envelope_also_fails_closed() -> None:
    """The lock is equality, not an upper bound: a *tighter* cap is still not
    the contract value, and silently running a different experiment is the
    thing being prevented."""

    narrowed = replace(CRYPTO_SIDECAR_ENVELOPE, per_order_notional=Decimal("1"))
    with pytest.raises(EnvelopeNotLocked):
        assert_envelope_locked(narrowed)


def test_unknown_market_has_no_envelope() -> None:
    with pytest.raises(EnvelopeNotLocked):
        load_envelope("forex")


def test_no_cli_flag_can_reach_an_envelope_value() -> None:
    """The runner exposes no dest that names or feeds an envelope field."""

    envelope_fields = set(CRYPTO_SIDECAR_ENVELOPE.canonical())
    args = _parse_args(["--lane", "shadow"])
    dests = set(vars(args))
    assert not (dests & envelope_fields), (
        f"CLI exposes a dest colliding with an envelope field: {dests & envelope_fields}"
    )
    # And nothing cap-shaped by any other spelling.
    forbidden_substrings = ("notional", "cap", "limit", "envelope", "max_", "loss")
    offenders = [
        dest
        for dest in dests
        if any(token in dest.lower() for token in forbidden_substrings)
    ]
    assert offenders == [], f"CLI exposes cap-shaped flags: {offenders}"


def test_environment_variables_cannot_move_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "B0X_PER_ORDER_NOTIONAL",
        "B0X_MAX_CONCURRENT_POSITIONS",
        "B0X_DAILY_LOSS_KILL",
        "B0X_ENVELOPE",
        "BINANCE_SPOT_DEMO_MAX_NOTIONAL",
        "BINANCE_DEMO_MAX_NOTIONAL_USDT",
        "PER_ORDER_NOTIONAL",
    ):
        monkeypatch.setenv(name, "99999")
    assert load_envelope("crypto") == CRYPTO_SIDECAR_ENVELOPE
    assert_envelope_locked(load_envelope("crypto"))


def test_envelope_module_reads_no_environment() -> None:
    """AST guard: the envelope module has no code path that reads config/env.

    Substring matching would trip on the module's own prose, so this walks the
    parsed tree and looks at imports and attribute access only.
    """

    import scripts.b0x.envelope as envelope_module

    tree = ast.parse(Path(envelope_module.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "os" not in imported, "envelope module must not import os"
    assert not {"dotenv", "configparser"} & imported

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "environ",
                "getenv",
            }, f"envelope module reads {node.attr}"
        if isinstance(node, ast.Name):
            assert node.id != "getenv"


# ---------------------------------------------------------------------------
# writer = 1
# ---------------------------------------------------------------------------


def _child_tries_lock(
    root: str, queue: mp.Queue
) -> None:  # pragma: no cover - subprocess
    try:
        with writer_lock(lane="upbit_shadow", root=Path(root)):
            queue.put("acquired")
    except WriterLockUnavailable:
        queue.put("blocked")
    except Exception as exc:  # noqa: BLE001
        queue.put(f"error:{type(exc).__name__}")


def test_second_process_cannot_hold_the_same_lane_lock(tmp_path: Path) -> None:
    """flock is per-open-file-description, so this needs a real second process."""

    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    with writer_lock(lane="upbit_shadow", root=tmp_path):
        child = ctx.Process(target=_child_tries_lock, args=(str(tmp_path), queue))
        child.start()
        child.join(timeout=30)
        assert queue.get(timeout=5) == "blocked"

    # Released — a later process may take it.
    child2 = ctx.Process(target=_child_tries_lock, args=(str(tmp_path), queue))
    child2.start()
    child2.join(timeout=30)
    assert queue.get(timeout=5) == "acquired"


def test_lock_records_the_holding_pid(tmp_path: Path) -> None:
    with writer_lock(lane="sidecar", root=tmp_path) as path:
        assert path.read_text().strip() == f"pid={os.getpid()}"
