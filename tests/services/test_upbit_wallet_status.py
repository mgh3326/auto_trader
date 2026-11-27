"""hk 6372: wallet-status parser, closed hold reasons, and import boundary."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import upbit_wallet_status as ws

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "upbit_wallet_status_response.json"


def _fixture_rows() -> tuple[ws.WalletNetworkRow, ...]:
    return ws.parse_wallet_status(json.loads(FIXTURE.read_text()))


def _row(currency: str, state: str, net: str = "NET") -> ws.WalletNetworkRow:
    return ws.WalletNetworkRow(
        currency=currency,
        wallet_state=state,
        net_type=net,
        network_name=net,
        block_state=None,
        block_height=None,
        block_updated_at=None,
        block_elapsed_minutes=None,
    )


def test_documented_response_parses_closed_states() -> None:
    rows = _fixture_rows()
    states = {row.currency: row.wallet_state for row in rows}
    assert states == {
        "KLAY": "working",
        "BTG": "deposit_only",
        "ADD": "withdraw_only",
        "IOTA": "paused",
        "ETH": "unsupported",
        "NEO": "working",
    }
    neo = next(row for row in rows if row.currency == "NEO")
    assert neo.net_type == "NEO"
    assert neo.network_name == "NEO N3"
    assert neo.block_height == 7235123
    assert neo.block_updated_at == "2019-02-18T07:08:50.499+00:00"


def test_missing_currency_is_wallet_status_unknown() -> None:
    statuses = ws.currency_statuses(_fixture_rows())
    missing = ws.status_for_currency(statuses, "DOGE")
    assert missing.hold_reason == ws.HOLD_WALLET_STATUS_UNKNOWN
    assert missing.rows == ()


def test_hold_reasons_from_documented_states() -> None:
    statuses = ws.currency_statuses(_fixture_rows())
    assert ws.status_for_currency(statuses, "KLAY").hold_reason is None
    assert ws.status_for_currency(statuses, "NEO").hold_reason is None
    assert ws.status_for_currency(statuses, "BTG").hold_reason == ws.HOLD_DEPOSIT_ONLY
    assert ws.status_for_currency(statuses, "ADD").hold_reason == ws.HOLD_WITHDRAW_ONLY
    assert (
        ws.status_for_currency(statuses, "IOTA").hold_reason
        == ws.HOLD_DEPOSIT_WITHDRAW_PAUSED
    )
    # unsupported = 입출금 미지원: neither direction is possible.
    assert (
        ws.status_for_currency(statuses, "ETH").hold_reason
        == ws.HOLD_DEPOSIT_WITHDRAW_PAUSED
    )


def test_unrecognized_or_missing_wallet_state_is_unknown_never_working() -> None:
    rows = ws.parse_wallet_status(
        [
            {"currency": "XXX", "wallet_state": "maintenance"},
            {"currency": "YYY"},
            {"currency": "ZZZ", "wallet_state": 3},
        ]
    )
    assert [row.wallet_state for row in rows] == ["unknown"] * 3
    statuses = ws.currency_statuses(rows)
    for code in ("XXX", "YYY", "ZZZ"):
        assert (
            ws.status_for_currency(statuses, code).hold_reason
            == ws.HOLD_WALLET_STATUS_UNKNOWN
        )


def test_non_list_payload_is_rejected() -> None:
    with pytest.raises(ws.WalletStatusError):
        ws.parse_wallet_status({"currency": "BTC", "wallet_state": "working"})


def test_unattributable_rows_are_dropped() -> None:
    rows = ws.parse_wallet_status(
        [
            {"wallet_state": "working"},
            {"currency": "  "},
            "junk",
            {"currency": "btc", "wallet_state": "working"},
        ]
    )
    assert len(rows) == 1
    assert rows[0].currency == "BTC"


def test_multi_network_rollup() -> None:
    # One working network proves both directions; other networks may lag.
    assert (
        ws.hold_reason_for_rows(
            [_row("USDT", "working", "ETH"), _row("USDT", "paused", "TRX")]
        )
        is None
    )
    # Directional coverage can be split across networks.
    assert (
        ws.hold_reason_for_rows(
            [
                _row("USDT", "withdraw_only", "ETH"),
                _row("USDT", "deposit_only", "TRX"),
            ]
        )
        is None
    )
    assert (
        ws.hold_reason_for_rows(
            [_row("X", "withdraw_only"), _row("X", "withdraw_only", "B")]
        )
        == ws.HOLD_WITHDRAW_ONLY
    )
    assert ws.hold_reason_for_rows([_row("X", "paused")]) == (
        ws.HOLD_DEPOSIT_WITHDRAW_PAUSED
    )
    assert ws.hold_reason_for_rows([]) == ws.HOLD_WALLET_STATUS_UNKNOWN


def test_unknown_network_row_poisons_unless_both_directions_proven() -> None:
    assert (
        ws.hold_reason_for_rows(
            [_row("USDT", "paused", "ETH"), _row("USDT", "unknown", "TRX")]
        )
        == ws.HOLD_WALLET_STATUS_UNKNOWN
    )
    assert (
        ws.hold_reason_for_rows(
            [_row("USDT", "working", "ETH"), _row("USDT", "unknown", "TRX")]
        )
        is None
    )


def test_hold_reason_vocabulary_is_closed() -> None:
    for state in ws.KNOWN_WALLET_STATES | {ws.WALLET_STATE_UNKNOWN}:
        reason = ws.hold_reason_for_rows([_row("X", state)])
        assert reason is None or reason in ws.HOLD_REASONS
        assert reason != "working"


def test_held_currencies_from_accounts() -> None:
    payload = [
        {"currency": "KRW", "balance": "1000", "locked": "0"},
        {"currency": "BTC", "balance": "0.5", "locked": "0"},
        {"currency": "ETH", "balance": "0", "locked": "1.2"},
        {"currency": "XRP", "balance": "0", "locked": "0"},
        {"currency": "USDT", "balance": "unparseable", "locked": "0"},
        "junk",
        {"balance": "5"},
    ]
    assert ws.held_currencies(payload) == ["BTC", "ETH", "USDT"]
    with pytest.raises(ws.WalletStatusError):
        ws.held_currencies({"currency": "BTC"})


# --- static import boundary -------------------------------------------------

OWN_MODULES = ("app.services.upbit_wallet_status", "scripts.upbit_wallet_status")
FIRST_PARTY = ("app", "research", "scripts")

#: The only ``app.services.brokers`` member this surface may reach: the
#: credential firewall guard itself. Every broker subpackage (``upbit``'s
#: ``__init__`` imports order submission) and every order/ledger surface is
#: denied, as are DB, scheduler and MCP modules — this feature is read-only.
ALLOWED_EXACT = frozenset(
    {"app.services.brokers", "app.services.brokers.credential_firewall"}
)
DENIED_PREFIXES = (
    "app.services.order_proposals",
    "app.services.execution_intent_gateway",
    "app.services.execution_ledger",
    "app.services.strategy_sleeves",
    "app.services.mock_strategy",
    "app.services.mechanical_loss_cut",
    "app.services.decision_evidence",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.models",
    "app.core.db",
)
DENIED_WORDS = frozenset({"order", "orders", "ledger", "ledgers"})


def _denied(module: str) -> bool:
    if module in ALLOWED_EXACT:
        return False
    if module.split(".")[0] not in FIRST_PARTY:
        return False
    if module.startswith("app.services.brokers."):
        return True
    words = set(re.split(r"[._]", module))
    return module.startswith(DENIED_PREFIXES) or bool(words & DENIED_WORDS)


def _module_file(name: str) -> Path | None:
    base = REPO.joinpath(*name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def _imports(module: str, path: Path, *, deep: bool) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree) if deep else tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                drop = node.level - (1 if path.name == "__init__.py" else 0)
                parent = module.rsplit(".", drop)[0] if drop else module
                base = f"{parent}.{base}" if base else parent
            names = [base] + [
                f"{base}.{a.name}"
                for a in node.names
                if _module_file(f"{base}.{a.name}") is not None
            ]
        found.update(n for n in names if n.split(".")[0] in FIRST_PARTY)
    return found


def _closure(starts: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = list(starts)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        stack.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        path = _module_file(module)
        if path is not None:
            stack.extend(_imports(module, path, deep=module in starts))
    return seen


def test_the_import_closure_holds_no_order_ledger_or_broker_module() -> None:
    closure = _closure(OWN_MODULES)
    assert "app.services.brokers.credential_firewall" in closure
    assert sorted(m for m in closure if _denied(m)) == []


@pytest.mark.parametrize(
    "module",
    [
        "app.services.brokers.upbit.client",  # its package imports order submission
        "app.services.brokers.upbit.orders",
        "app.services.order_proposals.dispatch",
        "app.services.execution_ledger.fill_ingest",
        "app.core.db",
    ],
)
def test_the_guard_sees_a_forbidden_import(module: str) -> None:
    assert _denied(module)


def test_the_guard_allows_the_credential_firewall_only() -> None:
    assert not _denied("app.services.brokers.credential_firewall")
    assert not _denied("app.services.brokers")
    assert _denied("app.services.brokers.toss.client")


def test_importing_the_module_loads_no_denied_module(tmp_path: Path) -> None:
    code = (
        "import importlib, json, sys\n"
        f"for name in {list(OWN_MODULES)!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = {
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_path),
        "KIS_APP_KEY": "placeholder",
        "KIS_APP_SECRET": "placeholder",
        "OPENDART_API_KEY": "placeholder",
        "DATABASE_URL": "postgresql+asyncpg://p:p@127.0.0.1:1/p",
        "UPBIT_ACCESS_KEY": "placeholder",
        "UPBIT_SECRET_KEY": "placeholder",
        "SECRET_KEY": "Inert_Placeholder_Key_0123456789_Not_Real",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert [m for m in loaded if _denied(m)] == []
