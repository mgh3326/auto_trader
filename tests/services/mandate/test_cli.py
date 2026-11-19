"""hk 3795 T2: the operator CLI ``scripts/mandate.py`` without a database.

Covers the closed event list, preview versus commit (through injected fakes),
the operator-actor gate that runs before any database access, the state-derived
event fields, closed inputs, the static boundary (no order or broker import) and
the runbook's commands against the real argparse. The database round trips live in
``test_cli_db.py`` and run in CI.
"""

from __future__ import annotations

import ast
import asyncio
import json
import shlex
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mandate.fold import PlanState, TrancheState
from scripts import mandate as cli

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/mandate.py"
RUNBOOK = ROOT / "docs/runbooks/mandate-cli.md"
DB = ["--database-url-env", "MANDATE_TEST_DB"]
ACTOR = ["--actor", "operator:cli:tester"]
TERMS_JSON = {
    "basket_key": "basket-1",
    "account_ref": "acct-live-1",
    "universe_rule": "test universe",
    "allowed_symbols": ["005930"],
    "entry_max_premium_bps": "30",
    "per_order_cap": "2000000",
    "daily_cap": "5000000",
    "sector_cap_pct": "35",
    "basket_drawdown_freeze_pct": "25",
    "account_drawdown_freeze_pct": "25",
    "tranche_rule": {
        "weights": ["0.4", "0.3", "0.3"],
        "triggers_pct_from_first": ["0", "-15", "-30"],
        "min_gap_sessions": 20,
    },
    "expires_at": "2027-06-30T00:00:00+09:00",
    "budget_cap": "10000000",
    "loss_limit": "500000",
    "prereg": {
        "metric": "cost-net result at H minus baseline",
        "min_effect_pct": "1",
        "horizon_sessions": 20,
        "max_duration_days": 90,
    },
}


class FakeSession:
    """Stands in for an AsyncSession: counts commits and rollbacks, nothing else."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _parse(*argv: str):
    return cli._parser().parse_args([*DB, *argv])


def _state(*, authority: str = "active", **tranches) -> PlanState:
    rows = [TrancheState(i) for i in range(1, cli.v.MAX_TRANCHES + 1)]
    for index, row in tranches.items():
        rows[int(index[1:]) - 1] = replace(rows[int(index[1:]) - 1], **row)
    return PlanState(authority=authority, tranches=tuple(rows))


# ------------------------------------------------------------ closed surface
def test_operator_events_are_exactly_the_t2_list() -> None:
    assert cli.OPERATOR_EVENTS == (
        "suspended",
        "resumed",
        "retired",
        "tranche_closed_partial",
        "thesis_broken",
        "thesis_evidence_refreshed",
        "holdings_disposed",
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        "approved",
        "rejected",
        "released",
        "tranche_send_fenced",
        "tranche_filled",
        "orders_settled",
        "first_tranche_settled",
    ],
)
def test_the_parser_refuses_events_outside_the_operator_list(forbidden: str) -> None:
    with pytest.raises(SystemExit):
        _parse("event", "--plan-id", "1", "--event", forbidden, "--reason", "r", *ACTOR)


@pytest.mark.parametrize(
    "argv",
    [
        ["request-terms", "--sleeve-key", "s", "--input", "f", "--approval-ref", "r"],
        [
            "request-profile",
            "--sleeve-key",
            "s",
            "--terms-id",
            "1",
            "--max-tranche-index",
            "1",
            "--effective-session",
            "2026-11-23",
            "--expires-at",
            "2027-01-01T00:00:00+09:00",
            "--approval-ref",
            "r",
        ],
        ["register-plan", "--input", "f"],
        ["event", "--plan-id", "1", "--event", "suspended", "--reason", "r"],
    ],
    ids=["request-terms", "request-profile", "register-plan", "event"],
)
def test_every_write_is_a_preview_unless_commit_is_given(argv: list[str]) -> None:
    assert _parse(*argv, *ACTOR).commit is False
    assert _parse(*argv, *ACTOR, "--commit").commit is True


def test_status_needs_exactly_one_target() -> None:
    assert _parse("status", "--plan-id", "3").plan_id == 3
    assert _parse("status", "--sleeve-key", "s").sleeve_key == "s"
    with pytest.raises(SystemExit):
        _parse("status")
    with pytest.raises(SystemExit):
        _parse("status", "--plan-id", "3", "--sleeve-key", "s")


# ------------------------------------------------------- operator actor gate
@pytest.mark.parametrize(
    "actor",
    [
        "op",
        "system:mandate_projection",
        "system:sleeve_loss_gate",
        "operator:",
        "operator:   ",
        "",
        "operator:" + "x" * 200,
    ],
)
def test_a_non_operator_actor_is_refused(actor: str) -> None:
    with pytest.raises(cli.MandateCliError):
        cli._operator_actor(actor)


def test_an_operator_actor_is_accepted() -> None:
    assert cli._operator_actor("operator:cli:mgh") == "operator:cli:mgh"


@pytest.mark.parametrize(
    "argv",
    [
        ["request-terms", "--sleeve-key", "s", "--input", "f", "--approval-ref", "r"],
        [
            "request-profile",
            "--sleeve-key",
            "s",
            "--terms-id",
            "1",
            "--max-tranche-index",
            "1",
            "--effective-session",
            "2026-11-23",
            "--expires-at",
            "2027-01-01T00:00:00+09:00",
            "--approval-ref",
            "r",
        ],
        ["register-plan", "--input", "f"],
        ["event", "--plan-id", "1", "--event", "suspended", "--reason", "r"],
    ],
    ids=["request-terms", "request-profile", "register-plan", "event"],
)
def test_a_system_actor_is_refused_before_any_database_access(
    argv: list[str],
) -> None:
    """The factory must never be called: the refusal happens with zero DB contact."""

    def factory():
        raise AssertionError("the database was touched")

    args = _parse(*argv, "--actor", "system:mandate_projection")
    with pytest.raises(cli.MandateCliError):
        asyncio.run(cli.run(args, session_factory=factory))


# -------------------------------------------------- preview versus commit (fake)
def _spy(calls: list, result):
    async def store(*args, **kwargs):
        calls.append(kwargs)
        return result

    return store


def test_request_terms_previews_then_commits_through_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = tmp_path / "clause.json"
    clause.write_text(json.dumps(TERMS_JSON))
    calls: list = []
    request = SimpleNamespace(
        subject_kind="terms",
        id=7,
        content_sha256="a" * 64,
        approval_ref="hk:1",
        requested_by="operator:cli:tester",
    )
    sleeve = SimpleNamespace(id=3, sleeve_key="mock-kr", market="equity_kr")

    async def get_sleeve(session, key):
        return sleeve

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", get_sleeve)
    monkeypatch.setattr(cli, "global_caps", lambda market: ("caps", market))
    monkeypatch.setattr(cli.ms, "create_terms_request", _spy(calls, request))
    argv = [
        "request-terms",
        "--sleeve-key",
        "mock-kr",
        "--input",
        str(clause),
        "--approval-ref",
        "hk:1",
        *ACTOR,
    ]

    preview = FakeSession()
    code, payload = asyncio.run(cli.run(_parse(*argv), session_factory=lambda: preview))
    assert code == cli.EXIT_OK
    assert (preview.commits, preview.rollbacks) == (0, 1)
    assert payload["mode"] == "preview" and payload["request_id"] == 7

    committed = FakeSession()
    code, payload = asyncio.run(
        cli.run(_parse(*argv, "--commit"), session_factory=lambda: committed)
    )
    assert (committed.commits, committed.rollbacks) == (1, 0)
    assert payload["mode"] == "commit"
    # both runs asked the same store for the same checks
    assert len(calls) == 2 and calls[0]["requested_by"] == "operator:cli:tester"


def test_event_previews_then_commits_through_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    row = SimpleNamespace(event="suspended", seq=4, actor="operator:cli:tester")

    async def get_plan(session, plan_id):
        return SimpleNamespace(id=plan_id)

    async def plan_state(session, plan_id):
        return _state()

    monkeypatch.setattr(cli.ms, "get_plan", get_plan)
    monkeypatch.setattr(cli.ms, "plan_state", plan_state)
    monkeypatch.setattr(cli.ms, "append_plan_event", _spy(calls, row))
    argv = [
        "event",
        "--plan-id",
        "2",
        "--event",
        "suspended",
        "--reason",
        "hold",
        *ACTOR,
    ]

    preview = FakeSession()
    asyncio.run(cli.run(_parse(*argv), session_factory=lambda: preview))
    assert (preview.commits, preview.rollbacks) == (0, 1)
    assert calls[0]["evidence"] == {"reason_code": "hold"}

    committed = FakeSession()
    asyncio.run(cli.run(_parse(*argv, "--commit"), session_factory=lambda: committed))
    assert (committed.commits, committed.rollbacks) == (1, 0)


# ------------------------------------------------ state-derived event fields
def _event_args(*argv: str):
    return _parse("event", "--plan-id", "1", "--reason", "why", *ACTOR, *argv)


def test_resume_target_comes_from_the_state_not_the_operator() -> None:
    args = _event_args("--event", "resumed", "--effective-session", "2026-11-24")
    fields, evidence = cli.event_fields("resumed", _state(authority="suspended"), args)
    assert fields == {
        "effective_session": date(2026, 11, 24),
        "resume_target": "approved",
    }
    assert evidence == {"reason_code": "why"}

    done = _state(authority="suspended", t1={"completed": True})
    fields, _ = cli.event_fields("resumed", done, args)
    assert fields["resume_target"] == "active"


def test_resumed_needs_an_effective_session() -> None:
    with pytest.raises(cli.MandateCliError, match="effective_session"):
        cli.event_fields(
            "resumed", _state(authority="suspended"), _event_args("--event", "resumed")
        )


def test_tranche_closed_partial_copies_only_the_first_tranche_fills() -> None:
    state = _state(t1={"filled_amount": "300000", "filled_quantity": "3"})
    first = _event_args("--event", "tranche_closed_partial", "--tranche-index", "1")
    fields, _ = cli.event_fields("tranche_closed_partial", state, first)
    assert fields["first_fill_amount"] == state.tranche(1).filled_amount
    assert fields["first_fill_quantity"] == state.tranche(1).filled_quantity

    second = _event_args("--event", "tranche_closed_partial", "--tranche-index", "2")
    fields, _ = cli.event_fields("tranche_closed_partial", state, second)
    assert "first_fill_amount" not in fields and "first_fill_quantity" not in fields


def test_thesis_refresh_points_at_the_latest_thesis_broken() -> None:
    state = replace(_state(authority="retired"), thesis_broken_seqs=(5, 9))
    args = _event_args(
        "--event",
        "thesis_evidence_refreshed",
        "--retrospective-id",
        "42",
        "--approval-issue-id",
        "hk:5600",
    )
    fields, evidence = cli.event_fields("thesis_evidence_refreshed", state, args)
    assert fields == {"thesis_seq": 9}
    assert evidence == {
        "reason_code": "why",
        "retrospective_id": 42,
        "approval_issue_id": "hk:5600",
    }
    with pytest.raises(cli.MandateCliError, match="갱신할 thesis_broken"):
        cli.event_fields("thesis_evidence_refreshed", _state(), args)


def test_thesis_broken_needs_both_evidence_ids() -> None:
    args = _event_args("--event", "thesis_broken", "--retrospective-id", "42")
    with pytest.raises(cli.MandateCliError, match="approval_issue_id"):
        cli.event_fields("thesis_broken", _state(), args)


def test_a_flag_the_event_does_not_take_is_refused() -> None:
    args = _event_args("--event", "suspended", "--tranche-index", "1")
    with pytest.raises(cli.MandateCliError, match="받지 않습니다"):
        cli.event_fields("suspended", _state(), args)


def test_holdings_disposed_is_always_kept_outside_basket() -> None:
    args = _event_args("--event", "holdings_disposed")
    _, evidence = cli.event_fields("holdings_disposed", _state(), args)
    assert evidence == {
        "reason_code": "why",
        "holdings_disposition": "kept_outside_basket",
    }


# ------------------------------------------------------------- closed inputs
def _json(tmp_path: Path, data) -> str:
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_an_unknown_input_key_is_refused(tmp_path: Path) -> None:
    path = _json(tmp_path, {**TERMS_JSON, "status": "approved"})
    with pytest.raises(cli.MandateCliError, match="모르는 입력 키"):
        cli._read_input(path, cli.TERMS_KEYS, cli.TERMS_REQUIRED)


def test_a_missing_input_key_is_refused(tmp_path: Path) -> None:
    data = dict(TERMS_JSON)
    del data["budget_cap"]
    with pytest.raises(cli.MandateCliError, match="빠진 입력 키"):
        cli._read_input(_json(tmp_path, data), cli.TERMS_KEYS, cli.TERMS_REQUIRED)


def test_a_json_float_stays_an_exact_decimal_string(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text('{"budget_cap": 10000000.1234}')
    raw = cli._read_input(
        str(path), frozenset({"budget_cap"}), frozenset({"budget_cap"})
    )
    assert raw["budget_cap"] == "10000000.1234"


def test_the_clause_needs_a_zoned_expiry_and_a_full_prereg(tmp_path: Path) -> None:
    naive = {**TERMS_JSON, "expires_at": "2027-06-30T00:00:00"}
    raw = cli._read_input(_json(tmp_path, naive), cli.TERMS_KEYS, cli.TERMS_REQUIRED)
    with pytest.raises(cli.MandateCliError, match="시간대"):
        cli._terms_input(raw)
    partial = {**TERMS_JSON, "prereg": {"metric": "m"}}
    raw = cli._read_input(_json(tmp_path, partial), cli.TERMS_KEYS, cli.TERMS_REQUIRED)
    with pytest.raises(cli.MandateCliError, match="prereg에는"):
        cli._terms_input(raw)


def test_the_profile_expiry_must_carry_a_zone() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                *DB,
                "request-profile",
                "--sleeve-key",
                "s",
                "--terms-id",
                "1",
                "--max-tranche-index",
                "1",
                "--effective-session",
                "2026-11-23",
                "--expires-at",
                "2027-01-01T00:00:00",
                "--approval-ref",
                "r",
                *ACTOR,
            ]
        )


# ------------------------------------------------------------ static boundary
FORBIDDEN = (
    "app.services.brokers",
    "app.mcp_server",
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.auto_approve",
    "app.services.order_proposals.service",
    "app.services.order_proposals.telegram_callback",
    "app.services.mechanical_loss_cut",
    "app.services.execution_intent_gateway",
    "app.tasks",
)


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def test_the_cli_imports_no_order_or_broker_module() -> None:
    assert [n for n in _imports(SCRIPT) if n.startswith(FORBIDDEN)] == []


def _module_path(name: str) -> Path | None:
    base = ROOT / name.replace(".", "/")
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def test_the_cli_import_closure_reaches_no_order_or_broker_module() -> None:
    seen: set[str] = set()
    pending = [i for i in _imports(SCRIPT) if i.startswith("app.")]
    offenders: list[str] = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_path(name)
        if path is None:
            continue
        for imported in _imports(path):
            if imported.startswith(FORBIDDEN):
                offenders.append(f"{name} -> {imported}")
            elif imported.startswith("app.services."):
                pending.append(imported)
    assert offenders == []


def test_the_cli_writes_only_through_the_store() -> None:
    text = SCRIPT.read_text()
    for banned in ("session.add(", "insert(", "delete(", "update("):
        assert banned not in text


# ------------------------------------------------------------ runbook vs argparse
def _runbook_commands() -> list[list[str]]:
    """Every ``uv run python -m scripts.mandate ...`` line of the runbook's bash blocks."""
    commands: list[list[str]] = []
    in_block = False
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_block = line.strip() == "```bash"
            continue
        if in_block and "scripts.mandate" in line:
            tokens = shlex.split(line.split("#", 1)[0].strip())
            start = tokens.index("scripts.mandate") + 1
            commands.append(tokens[start:])
    return commands


def test_the_runbook_has_commands() -> None:
    commands = _runbook_commands()
    assert {cli._parser().parse_args(c).command for c in commands} == {
        "request-terms",
        "request-profile",
        "register-plan",
        "event",
        "status",
    }


def test_every_runbook_command_parses_against_the_real_argparse() -> None:
    for tokens in _runbook_commands():
        cli._parser().parse_args(tokens)


def test_every_subcommand_and_flag_is_named_in_the_runbook() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    parser = cli._parser()
    subparsers = next(
        a
        for a in parser._actions
        if getattr(a, "choices", None) and a.dest == "command"
    )
    for name, sub in subparsers.choices.items():
        assert f"`{name}`" in text or f" {name} " in text, name
        for action in sub._actions:
            for option in action.option_strings:
                if option in ("-h", "--help"):
                    continue
                assert option in text, (name, option)
    for action in parser._actions:
        for option in action.option_strings:
            if option in ("-h", "--help"):
                continue
            assert option in text, option


# ------------------------------------------- the remaining command bodies (fake)
class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSessionWithRows(FakeSession):
    """Answers ``execute`` from a queue, one result per query the command makes."""

    def __init__(self, results: list) -> None:
        super().__init__()
        self._results = list(results)

    async def execute(self, statement):
        return _Rows(self._results.pop(0))


def _record(calls: list, result):
    async def store(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    return store


def _plan_input_file(tmp_path: Path) -> str:
    data = {
        "sleeve_key": "mock-kr",
        "symbol": "005930",
        "planned_amount": "2350000",
        "adopted_amount": "0",
        "adopted_evidence": {
            "checked_at": "2026-11-20T00:00:00Z",
            "holdings": [
                {"account_ref": "acct-a", "quantity": "0", "amount": "0"},
                {"account_ref": "acct-b", "quantity": "0", "amount": "0"},
            ],
        },
        "account_ref": "acct-a",
        "scope_account_refs": ["acct-a", "acct-b"],
        "thesis": "test thesis",
        "invalidation_conditions": ["x falls"],
        "sector_key": "auto",
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data))
    return str(path)


def _sleeve(market: str = "equity_kr"):
    return SimpleNamespace(id=3, sleeve_key="mock-kr", market=market)


def test_register_plan_sizes_one_share_from_the_last_close_and_writes_via_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    registered = SimpleNamespace(
        plan=SimpleNamespace(
            id=9,
            symbol="005930",
            planned_amount=cli.Decimal("2350000"),
            adopted_amount=cli.Decimal("0"),
        ),
        tranches=(SimpleNamespace(tranche_index=1, amount=cli.Decimal("940000")),),
        reservation=SimpleNamespace(realm="mock", market="equity_kr", symbol="005930"),
    )

    async def get_sleeve(session, key):
        return _sleeve()

    async def marks(session, market, symbols, now):
        return {symbols[0]: cli.Decimal("10000")}

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", get_sleeve)
    monkeypatch.setattr(cli, "latest_close_marks", marks)
    monkeypatch.setattr(cli.ms, "register_plan", _record(calls, registered))
    argv = ["register-plan", "--input", _plan_input_file(tmp_path), *ACTOR]

    preview = FakeSession()
    _, payload = asyncio.run(cli.run(_parse(*argv), session_factory=lambda: preview))
    assert (preview.commits, preview.rollbacks) == (0, 1)
    assert payload["plan_id"] == 9 and payload["authority"] == "proposed"
    (_, data), _kw = calls[0]
    assert data.one_share_price == cli.Decimal("10000")
    assert data.created_by == "operator:cli:tester"
    assert data.symbol == "005930"

    committed = FakeSession()
    asyncio.run(cli.run(_parse(*argv, "--commit"), session_factory=lambda: committed))
    assert (committed.commits, committed.rollbacks) == (1, 0)
    assert len(calls) == 2


def test_register_plan_refuses_a_missing_price_with_no_store_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []

    async def get_sleeve(session, key):
        return _sleeve()

    async def no_close(session, market, symbols, now):
        return {symbols[0]: None}

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", get_sleeve)
    monkeypatch.setattr(cli, "latest_close_marks", no_close)
    monkeypatch.setattr(cli.ms, "register_plan", _record(calls, None))
    argv = ["register-plan", "--input", _plan_input_file(tmp_path), "--commit", *ACTOR]

    with pytest.raises(cli.ms.MandateError, match="mandate_invalid_fields"):
        asyncio.run(cli.run(_parse(*argv), session_factory=FakeSession))
    assert calls == []


def test_request_profile_previews_then_commits_through_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    request = SimpleNamespace(
        subject_kind="activation_profile",
        id=5,
        content_sha256="b" * 64,
        approval_ref="hk:1",
        requested_by="operator:cli:tester",
    )

    async def get_sleeve(session, key):
        return _sleeve()

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", get_sleeve)
    monkeypatch.setattr(cli.ms, "create_profile_request", _record(calls, request))
    argv = [
        "request-profile",
        "--sleeve-key",
        "mock-kr",
        "--terms-id",
        "12",
        "--max-tranche-index",
        "1",
        "--effective-session",
        "2026-11-23",
        "--expires-at",
        "2027-01-01T00:00:00+09:00",
        "--approval-ref",
        "hk:1",
        *ACTOR,
    ]

    preview = FakeSession()
    _, payload = asyncio.run(cli.run(_parse(*argv), session_factory=lambda: preview))
    assert (preview.commits, preview.rollbacks) == (0, 1)
    assert payload["subject_kind"] == "activation_profile"
    (_, kwargs) = calls[0]
    assert kwargs["terms_id"] == 12 and kwargs["max_tranche_index"] == 1
    assert kwargs["effective_session"] == date(2026, 11, 23)
    assert kwargs["expires_at"].utcoffset() is not None

    committed = FakeSession()
    asyncio.run(cli.run(_parse(*argv, "--commit"), session_factory=lambda: committed))
    assert (committed.commits, committed.rollbacks) == (1, 0)


def test_status_of_one_plan_is_read_only_and_folds_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = SimpleNamespace(
        id=9,
        symbol="005930",
        account_ref="acct-a",
        scope_account_refs=("acct-a", "acct-b"),
        planned_amount=cli.Decimal("2350000"),
        adopted_amount=cli.Decimal("0"),
        sector_key="auto",
    )

    async def get_plan(session, plan_id):
        return plan

    async def plan_state(session, plan_id):
        return _state(authority="approved", t1={"filled_amount": cli.Decimal("300000")})

    async def list_events(session, plan_id):
        return [SimpleNamespace(seq=1, event="proposed", actor="operator:cli:x")]

    monkeypatch.setattr(cli.ms, "get_plan", get_plan)
    monkeypatch.setattr(cli.ms, "plan_state", plan_state)
    monkeypatch.setattr(cli.ms, "list_plan_events", list_events)
    tranche = SimpleNamespace(tranche_index=1, amount=cli.Decimal("940000"))
    session = FakeSessionWithRows([[tranche]])

    code, payload = asyncio.run(
        cli.run(_parse("status", "--plan-id", "9"), session_factory=lambda: session)
    )
    assert code == cli.EXIT_OK
    assert payload["mode"] == "read" and payload["authority"] == "approved"
    assert payload["tranches"][0]["filled_amount"] == "300000"
    assert payload["events"] == [
        {"seq": 1, "event": "proposed", "actor": "operator:cli:x"}
    ]
    assert (session.commits, session.rollbacks) == (0, 1)


def test_status_of_a_sleeve_lists_its_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_sleeve(session, key):
        return _sleeve()

    async def plan_state(session, plan_id):
        return _state(authority="proposed")

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", get_sleeve)
    monkeypatch.setattr(cli.ms, "plan_state", plan_state)
    session = FakeSessionWithRows([[SimpleNamespace(id=9, symbol="005930")]])

    _, payload = asyncio.run(
        cli.run(
            _parse("status", "--sleeve-key", "mock-kr"), session_factory=lambda: session
        )
    )
    assert payload["sleeve_key"] == "mock-kr"
    assert payload["plans"] == [
        {
            "plan_id": 9,
            "symbol": "005930",
            "authority": "proposed",
            "cleanup": "none",
            "released": False,
        }
    ]
