"""US contract constants and static safety guards."""

from __future__ import annotations

import ast
import datetime as dt
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.b0x.broker_truth import BrokerTruth
from scripts.b0x.derivation import derive_orders
from scripts.b0x.envelope import (
    US_ALPACA_PAPER_LAB_ENVELOPE,
    EnvelopeNotLocked,
    assert_envelope_locked,
)
from scripts.b0x.kill_switch import MissingNavForRatioKill, evaluate
from scripts.b0x.labels import SHARED_HISTORY_ACCOUNTS, header_labels
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import PolicyTable
from scripts.b0x.us import alpaca
from scripts.b0x.us.contract import (
    ACCOUNT_MAP_COMMIT_UNAVAILABLE,
    ACCOUNT_MAP_FACTS,
    CONTRACT_FILE_SHA256_REFERENCE,
    CONTRACT_VERSION,
    account_map_stamp,
    contract_stamp,
)
from scripts.policy_table.core.trust_labels import (
    CROSS_MARKET_TRANSFER_UNVALIDATED,
    TRUST_LABELS,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 10, 15, 0, tzinfo=dt.UTC)
ROOT = Path(__file__).resolve().parents[4]
US_PACKAGE = ROOT / "scripts" / "b0x" / "us"
RUNNER = ROOT / "scripts" / "run_b0x_us_cycle.py"
V17_SCHEDULER_CLAUSE = (
    "「스케줄러 등록 없음」 개정 — **스케줄러 (Prefect)는 시각만 소유한다**: "
    "표 빌드 실행(KR 07:45·US 22:00)과 orch 기상 nudge(사이클 슬롯)에 한정. "
    "전략 판단·주문 파생·dispatch·워커 실행은 불변(orch/워커 소유, "
    "harvest-before-dispatch 유지). 근거 = 수동 원샷 장전 누락 4회 실측. "
    "실행 표면·envelope·승격 절차 무변경."
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_operator_table_repo(tmp_path: Path) -> tuple[Path, Path]:
    repository_path = tmp_path / "operator.tbl"
    repository_path.mkdir()
    _git(repository_path, "init", "-q")
    _git(repository_path, "config", "user.email", "test@example.invalid")
    _git(repository_path, "config", "user.name", "B0-X test")
    (repository_path / "operator_contract.yaml").write_text("contract: test\n")
    _git(repository_path, "add", "operator_contract.yaml")
    _git(repository_path, "commit", "-q", "-m", "operator contract")
    _git(repository_path, "branch", "-M", "main")
    _git(repository_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    table_dir = repository_path / "policy-tables"
    table_dir.mkdir()
    return repository_path, table_dir


def _state(*, pnl: Decimal, nav: Decimal | None) -> LaneAccountState:
    return LaneAccountState(
        lane=alpaca.LANE,
        quote_currency="USD",
        cash=Decimal("10000"),
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        realized_pnl_today=pnl,
        nav=nav,
    )


def test_us_envelope_matches_section_4_and_is_locked() -> None:
    envelope = US_ALPACA_PAPER_LAB_ENVELOPE
    assert envelope.market == "us"
    assert envelope.quote_currency == "USD"
    assert envelope.per_order_notional == Decimal("450")
    assert envelope.per_symbol_total_notional == Decimal("2250")
    assert envelope.max_concurrent_positions == 10
    assert envelope.max_new_entries_per_utc_day == 3
    assert envelope.daily_loss_kill == Decimal("0.025")
    assert envelope.daily_loss_kill_basis == "pct_of_nav"
    assert_envelope_locked(envelope)
    with pytest.raises(EnvelopeNotLocked):
        assert_envelope_locked(replace(envelope, per_order_notional=Decimal("451")))


def test_us_contract_stamp_is_v17_plus_quoted_clauses_with_reference_digest() -> None:
    stamp = contract_stamp()
    assert stamp["version"] == CONTRACT_VERSION == "v1.7"
    assert stamp["file_sha256_reference_only"] == CONTRACT_FILE_SHA256_REFERENCE
    assert {
        "§1",
        "§2-2 v1.1",
        "§4 US",
        "§8 v1.5 ①",
        "§8 v1.6",
        "§8 v1.7",
    } <= set(stamp["clauses"])
    assert stamp["clauses"]["§8 v1.7"] == V17_SCHEDULER_CLAUSE


def test_us_account_map_stamp_binds_the_injected_table_worktree(
    tmp_path: Path,
) -> None:
    repository_path, table_dir = _make_operator_table_repo(tmp_path)
    expected_head = _git(repository_path, "rev-parse", "HEAD")

    stamp = account_map_stamp(account_map_path=table_dir)

    assert stamp["repo"] == "auto_trader-operator"
    assert stamp["commit"] == expected_head
    assert stamp["branch"] == "main"
    assert stamp["reachable_from_origin_main"] is True
    assert stamp["commit_status"] == "available"
    assert stamp["commit_reason"] is None
    assert stamp["canonical_surface"] == "operator_contract.yaml"
    assert stamp["source_path"] == str(table_dir)
    for key, value in ACCOUNT_MAP_FACTS.items():
        assert stamp[key] == value


def test_us_account_map_stamp_fails_closed_but_keeps_us_facts(tmp_path: Path) -> None:
    source_path = tmp_path / "not-an-operator-worktree"
    source_path.mkdir()

    stamp = account_map_stamp(account_map_path=source_path)

    assert ACCOUNT_MAP_COMMIT_UNAVAILABLE == "UNAVAILABLE"
    assert stamp["commit"] == "UNAVAILABLE"
    assert stamp["branch"] == "UNAVAILABLE"
    assert stamp["commit_status"] == "unavailable"
    assert stamp["commit_reason"] == "account_map_source_not_git_repository"
    assert stamp["reachable_from_origin_main"] is None
    for key, value in ACCOUNT_MAP_FACTS.items():
        assert stamp[key] == value


def test_us_ratio_kill_uses_nav_and_missing_nav_fails_closed() -> None:
    with pytest.raises(MissingNavForRatioKill):
        evaluate(
            state=_state(pnl=Decimal("-1"), nav=None),
            envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
        )

    under = evaluate(
        state=_state(pnl=Decimal("-24.99"), nav=Decimal("1000")),
        envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
    )
    at = evaluate(
        state=_state(pnl=Decimal("-25"), nav=Decimal("1000")),
        envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
    )
    assert under.tripped is False
    assert at.tripped is True
    assert at.daily_loss_kill == Decimal("25")
    assert at.daily_loss_kill_basis == "pct_of_nav"
    assert at.nav_snapshot == Decimal("1000")


def test_us_table_selected_usd_amount_reaches_generic_derivation() -> None:
    table = PolicyTable(
        market="us",
        path=Path("/tmp/latest-us.json"),
        payload={
            "config": {
                "new_entry_notional_usd": "300",
                "averaging_k_levels": [],
                "loss_guard_multiplier": "1",
            },
            "sizing": {},
            "rows": [
                {
                    "symbol": "AAPL",
                    "insufficient_history": False,
                    "previous_close": "100",
                    "A_buy_side": {"buy_l1": {"price": "97"}, "buy_l2": None},
                    "B_sell_side": {"sell_r1": None, "sell_r2": None},
                }
            ],
        },
        policy_table_hash="sha256:us",
        artifact_sha256="sha256:artifact",
        generated_at=NOW,
        age=dt.timedelta(0),
    )
    state = _state(pnl=Decimal("0"), nav=Decimal("10000"))
    result = derive_orders(
        table=table,
        state=state,
        envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
        kill_switch=evaluate(state=state, envelope=US_ALPACA_PAPER_LAB_ENVELOPE),
    )
    assert len(result.orders) == 1
    assert result.orders[0].notional == Decimal("300")


def test_us_headers_have_exact_three_base_labels_plus_us_extra_but_not_shared_history() -> (
    None
):
    labels = header_labels(lane=alpaca.LANE, extra=(CROSS_MARKET_TRANSFER_UNVALIDATED,))
    assert labels[:3] == TRUST_LABELS
    assert CROSS_MARKET_TRANSFER_UNVALIDATED in labels
    assert all("SHARED_ACCOUNT_HISTORY" not in label for label in labels)
    assert alpaca.LANE not in SHARED_HISTORY_ACCOUNTS


def test_us_adapter_never_uses_kr_ledger_pending_exception_or_default_account_literal() -> (
    None
):
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in US_PACKAGE.rglob("*.py")
    )
    assert "PendingUnreadable" not in source
    assert "kis_mock_order_ledger" not in source
    # The one literal ``alpaca_paper`` is the generic execution *venue* field
    # required by the existing packet contract—not an account selector.  Every
    # actual read/mutation call must still name the imported lab scope key.
    tree = ast.parse(source)
    default_account_mode_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "account_mode"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "alpaca_paper"
            ):
                default_account_mode_calls.append(node.lineno)
    assert default_account_mode_calls == []


def test_us_ast_guard_allows_only_reviewed_alpaca_tooling_and_no_dynamic_bypass() -> (
    None
):
    allowed = {
        "app.core.symbol",
        "app.mcp_server.tooling.alpaca_paper",
        "app.mcp_server.tooling.alpaca_paper_ledger_read",
        "app.services.alpaca_paper_account_modes",
        "app.services.alpaca_paper_submit_service",
        "app.services.paper_approval_packet",
    }
    guarded_prefixes = ("app.mcp_server.tooling.alpaca_paper", "app.services.alpaca")
    files = [*US_PACKAGE.rglob("*.py"), RUNNER]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                modules = []
            for module in modules:
                if module.startswith(guarded_prefixes):
                    assert module in allowed, (
                        f"{path}: unreviewed Alpaca import {module}"
                    )
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {
                "__import__",
                "exec",
                "eval",
            }:
                pytest.fail(f"{path}: dynamic bypass {node.func.id}()")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ):
                pytest.fail(f"{path}: dynamic import bypass")
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                assert len(node.args) < 2 or isinstance(node.args[1], ast.Constant)


def test_us_adapter_imports_only_read_only_alpaca_surfaces() -> None:
    allowed_tool_imports = {
        "app.mcp_server.tooling.alpaca_paper": {
            "alpaca_paper_get_account",
            "alpaca_paper_list_orders",
            "alpaca_paper_list_positions",
        },
        "app.mcp_server.tooling.alpaca_paper_ledger_read": {
            "alpaca_paper_ledger_list_recent",
        },
    }
    for path in [*US_PACKAGE.rglob("*.py"), RUNNER]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module == "app.mcp_server.tooling.alpaca_paper_orders":
                pytest.fail(f"{path}: US adapter may not import order mutation tooling")
            allowed_names = allowed_tool_imports.get(node.module)
            if allowed_names is not None:
                imported = {alias.name for alias in node.names}
                assert imported <= allowed_names, (
                    f"{path}: non-read-only Alpaca tooling import "
                    f"{sorted(imported - allowed_names)}"
                )
