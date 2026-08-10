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
    CRYPTO_SHADOW_ENVELOPE,
    CRYPTO_SHADOW_FX_KRW_PER_USDT,
    CRYPTO_SHADOW_MARKET_KEY,
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


# ---------------------------------------------------------------------------
# crypto 본선 (shadow) envelope — SHADOW-ALIGN
# ---------------------------------------------------------------------------


def test_crypto_shadow_envelope_is_krw_denominated_and_distinct_from_the_sidecar() -> (
    None
):
    """Lane separation (mutant ③): the shadow lane gets its own market key
    and currency, and the sidecar's USDT envelope is completely untouched.
    """

    assert CRYPTO_SHADOW_ENVELOPE.quote_currency == "KRW"
    assert CRYPTO_SHADOW_ENVELOPE.market == CRYPTO_SHADOW_MARKET_KEY
    assert CRYPTO_SHADOW_ENVELOPE.market != CRYPTO_SIDECAR_ENVELOPE.market

    # The sidecar column is unaffected by the shadow lane gaining its own.
    assert CRYPTO_SIDECAR_ENVELOPE.quote_currency == "USDT"
    assert CRYPTO_SIDECAR_ENVELOPE.market == "crypto"
    assert load_envelope("crypto") == CRYPTO_SIDECAR_ENVELOPE
    assert load_envelope(CRYPTO_SHADOW_MARKET_KEY) == CRYPTO_SHADOW_ENVELOPE


def test_crypto_shadow_envelope_matches_the_documented_krw_literals() -> None:
    """Fixed literals: 10/50/5 USDT x 1400 KRW/USDT = 14000/70000/7000 KRW.
    Position/entry counts are unit-less and carry over unconverted.
    """

    assert CRYPTO_SHADOW_FX_KRW_PER_USDT == Decimal("1400")
    assert CRYPTO_SHADOW_ENVELOPE.per_order_notional == Decimal("14000")
    assert CRYPTO_SHADOW_ENVELOPE.per_symbol_total_notional == Decimal("70000")
    assert CRYPTO_SHADOW_ENVELOPE.daily_loss_kill == Decimal("7000")
    assert (
        CRYPTO_SHADOW_ENVELOPE.max_concurrent_positions
        == CRYPTO_SIDECAR_ENVELOPE.max_concurrent_positions
    )
    assert (
        CRYPTO_SHADOW_ENVELOPE.max_new_entries_per_utc_day
        == CRYPTO_SIDECAR_ENVELOPE.max_new_entries_per_utc_day
    )
    assert CRYPTO_SHADOW_ENVELOPE.daily_loss_kill_basis == "absolute"


def test_crypto_shadow_envelope_cap_meaning_is_not_widened() -> None:
    """CAP_MEANING_PRESERVED: converting every currency-denominated field back
    to USDT at the real, observed rate this literal was floored from
    (1420.84 KRW/USD, 2026-08-10T19:13:23Z, source=toss, USDT~=USD) must land
    at-or-below the original sidecar figure it replaces — never above. A
    fixed conservative (understating) rate is what makes this an invariant
    of the literals themselves, not a property that depends on where the
    market rate wanders after the fact.
    """

    observed_rate = Decimal("1420.84")
    assert CRYPTO_SHADOW_FX_KRW_PER_USDT < observed_rate

    pairs = (
        (
            CRYPTO_SHADOW_ENVELOPE.daily_loss_kill,
            CRYPTO_SIDECAR_ENVELOPE.daily_loss_kill,
        ),
        (
            CRYPTO_SHADOW_ENVELOPE.per_order_notional,
            CRYPTO_SIDECAR_ENVELOPE.per_order_notional,
        ),
        (
            CRYPTO_SHADOW_ENVELOPE.per_symbol_total_notional,
            CRYPTO_SIDECAR_ENVELOPE.per_symbol_total_notional,
        ),
    )
    for krw_value, usdt_original in pairs:
        usdt_equivalent_at_observed_rate = krw_value / observed_rate
        assert usdt_equivalent_at_observed_rate <= usdt_original, (
            f"{krw_value} KRW converts back to "
            f"{usdt_equivalent_at_observed_rate} USDT at the observed rate, "
            f"which exceeds the original {usdt_original} USDT cap — widened"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("per_order_notional", Decimal("14001")),
        ("per_symbol_total_notional", Decimal("700000")),
        ("max_concurrent_positions", 4),
        ("max_new_entries_per_utc_day", 3),
        ("daily_loss_kill", Decimal("7001")),
    ],
)
def test_widened_shadow_envelope_fails_closed(field: str, value: object) -> None:
    """Mutant ② made permanent: any single-field widening of the shadow
    envelope — including the tiniest possible increase to
    ``daily_loss_kill`` — must be rejected by the same lock every other
    market envelope is held to.
    """

    widened = replace(CRYPTO_SHADOW_ENVELOPE, **{field: value})
    assert widened != CRYPTO_SHADOW_ENVELOPE
    with pytest.raises(EnvelopeNotLocked):
        assert_envelope_locked(widened)


def test_envelope_module_has_no_runtime_fx_dependency() -> None:
    """Mutant ④ made permanent: the shadow envelope's KRW figures must be
    literal Decimal arithmetic evaluated once at import time — not a call to
    any FX/network/env source. Extends the existing no-environment AST guard
    with network- and FX-shaped names specific to this literal's derivation.
    """

    import scripts.b0x.envelope as envelope_module

    tree = ast.parse(Path(envelope_module.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden_imports = {"httpx", "requests", "aiohttp", "asyncio"}
    assert not forbidden_imports & imported, (
        f"envelope module must not import network/async machinery: "
        f"{forbidden_imports & imported}"
    )

    forbidden_names = {
        "get_fx_rate",
        "fetch_ohlcv",
        "exchange_rate",
        "ExchangeRateService",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, (
                f"envelope module references a live FX lookup: {node.id}"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_names, (
                f"envelope module references a live FX lookup: {node.attr}"
            )

    # The rate assignment's RHS must be exactly ``Decimal("<literal>")`` — a
    # call to *any other* name (a helper function, a service client, an
    # os.environ-backed wrapper) is what "computed at runtime" looks like in
    # the AST, regardless of what it is named. This is deliberately shape-
    # based rather than name-based, so it cannot be dodged by picking an
    # innocuous-sounding helper name.
    assign_values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "CRYPTO_SHADOW_FX_KRW_PER_USDT"
            for target in node.targets
        )
    ] + [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CRYPTO_SHADOW_FX_KRW_PER_USDT"
        and node.value is not None
    ]
    assert len(assign_values) == 1, (
        "expected exactly one module-level assignment to "
        f"CRYPTO_SHADOW_FX_KRW_PER_USDT, found {len(assign_values)}"
    )
    value_node = assign_values[0]
    assert isinstance(value_node, ast.Call), (
        "CRYPTO_SHADOW_FX_KRW_PER_USDT must be assigned directly from a "
        f"call expression, got {ast.dump(value_node)}"
    )
    assert isinstance(value_node.func, ast.Name) and value_node.func.id == "Decimal", (
        "CRYPTO_SHADOW_FX_KRW_PER_USDT must be assigned via Decimal(...) "
        f"directly, not {ast.dump(value_node.func)}"
    )
    assert len(value_node.args) == 1 and isinstance(value_node.args[0], ast.Constant), (
        "Decimal(...) must be called with a single literal constant argument, "
        f"got {[ast.dump(a) for a in value_node.args]}"
    )

    # And the runtime value matches — the rate is a bare Decimal literal, not
    # the output of a function call.
    assert isinstance(CRYPTO_SHADOW_FX_KRW_PER_USDT, Decimal)


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
