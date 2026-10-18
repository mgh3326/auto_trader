"""Isolation guards (brief §격리 가드): imports, SQL, read-only txn, host pin.

Each static checker is a plain function so ``test_mutants.py`` can run the
same function on a mutated copy and prove it goes RED.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from research.rob1391_us_resubmit_shadow import db_source, market_data
from research.rob1391_us_resubmit_shadow.db_source import (
    READ_ONLY_BEGIN,
    ReadOnlyViolation,
    _assert_select,
    run_read_only,
)

pytestmark = pytest.mark.unit

PKG = Path(__file__).resolve().parents[3] / "research" / "rob1391_us_resubmit_shadow"

THIRD_PARTY = {"numpy", "pandas", "exchange_calendars"}
# Exact allowlist of non-stdlib, non-package imports, per module. Everything
# else -- order/proposal/watch/ledger writers, every broker client -- is out.
PER_MODULE = {
    "db_source.py": {"sqlalchemy", "sqlalchemy.engine", "sqlalchemy.ext.asyncio"},
    "market_data.py": {"research.us_intraday_corpus.alpaca_data", "requests"},
    "parents.py": {"app.core.symbol"},
    "guards.py": {"app.services.trading_policy_service", "subprocess"},
}
# stdlib modules that open sockets, spawn processes or import dynamically are
# not "stdlib = fine": only the per-module exceptions above may use them.
STDLIB_FORBIDDEN = {
    "importlib",
    "builtins",
    "ctypes",
    "socket",
    "ssl",
    "http",
    "urllib",
    "ftplib",
    "smtplib",
    "xmlrpc",
    "subprocess",
    "multiprocessing",
    "runpy",
    "pkgutil",
    "code",
}
FORBIDDEN_NAMES = {
    "__import__",
    "__builtins__",
    "eval",
    "exec",
    "compile",
    "globals",
    "breakpoint",
}
PROCESS_ATTRS = {
    "system",
    "popen",
    "spawnl",
    "spawnlp",
    "spawnv",
    "spawnvp",
    "execv",
    "execve",
    "execl",
    "execlp",
    "execvp",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "startfile",
    "Popen",
    "check_output",
    "call",
    "check_call",
    "getoutput",
    "getstatusoutput",
}
OWN = "research.rob1391_us_resubmit_shadow"
HTTP_VERBS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "request",
    "send",
    "urlopen",
}


def _fold(node: ast.AST) -> str | None:
    """Constant-fold ``"a" + "b"`` chains (and parenthesised constants)."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif (
                isinstance(v, ast.FormattedValue)
                and getattr(v.value, "id", None) == "DATA_HOST"
            ):
                parts.append("data.alpaca.markets")
            else:
                parts.append("\x00")  # unknown at analysis time
        return "".join(parts)
    return None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                out.add(id(body[0].value))
    return out


def _strings(tree: ast.AST, *, docstrings: bool = True) -> list[tuple[int, str]]:
    skip = set() if docstrings else _docstring_nodes(tree)
    # f-string pieces are judged as the whole folded f-string, not alone
    skip |= {
        id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values
    }
    return [
        (n.lineno, v)
        for n in ast.walk(tree)
        if isinstance(n, ast.expr) and id(n) not in skip and (v := _fold(n)) is not None
    ]


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def import_violations(pkg: Path) -> list[str]:
    bad: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = PER_MODULE.get(path.name, set())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    bad.append(f"{path.name}: relative import")
                    continue
                names = [node.module or ""]
            elif (
                isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.modules"
            ):
                bad.append(f"{path.name}:{node.lineno}: sys.modules lookup")
                continue
            elif isinstance(node, ast.Attribute) and node.attr in PROCESS_ATTRS:
                bad.append(f"{path.name}:{node.lineno}: process API .{node.attr}")
                continue
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                bad.append(f"{path.name}:{node.lineno}: forbidden name {node.id}")
                continue
            elif isinstance(node, ast.Attribute) and node.attr in {
                "import_module",
                "__import__",
                "__builtins__",
            }:
                bad.append(f"{path.name}:{node.lineno}: dynamic import")
                continue
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"builtins", "importlib", "sys", "__builtins__"}
            ):
                bad.append(f"{path.name}:{node.lineno}: getattr on {node.args[0].id}")
                continue
            for name in names:
                top = name.split(".")[0]
                if name == OWN or name.startswith(OWN + "."):
                    continue
                if name in allowed or top in allowed:
                    continue
                if top in sys.stdlib_module_names and top not in STDLIB_FORBIDDEN:
                    continue
                if top in THIRD_PARTY:
                    continue
                bad.append(f"{path.name}: {name}")
    return bad


_STATEMENT = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|TRUNCATE\s|ALTER\s+TABLE|DROP\s+TABLE|"
    r"CREATE\s+(TEMP\s+)?TABLE|MERGE\s+INTO|COPY\s+\S+\s+FROM|SELECT\s+.*\bFOR\s+UPDATE|SELECT\s+.*\bINTO\b)",
    re.IGNORECASE | re.DOTALL,
)
_EXECUTORS = {
    "execute",
    "exec_driver_sql",
    "executemany",
    "fetchrow",
    "fetchval",
    "copy_records_to_table",
}


def sql_violations(pkg: Path) -> list[str]:
    bad: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in _strings(tree, docstrings=False):
            if _STATEMENT.search(value):
                bad.append(f"{path.name}:{lineno}: write SQL")
        callees = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        # an executor (or, in db_source, sqlalchemy ``text``) may only appear as
        # a direct callee: binding it to an alias would bypass the checks below
        watched = _EXECUTORS | ({"text"} if path.name == "db_source.py" else set())
        for node in ast.walk(tree):
            ident = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if ident in watched and id(node) not in callees:
                bad.append(
                    f"{path.name}:{node.lineno}: SQL executor referenced, not called"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if path.name != "db_source.py":
                if name in _EXECUTORS or name == "text":
                    bad.append(
                        f"{path.name}:{node.lineno}: SQL execution outside db_source"
                    )
                continue
            # db_source: every executed statement goes through the runtime guard
            if name == "text" and not (
                node.args
                and isinstance(node.args[0], ast.Call)
                and getattr(node.args[0].func, "id", None) == "_assert_select"
            ):
                bad.append(f"db_source.py:{node.lineno}: text() without _assert_select")
            if name == "exec_driver_sql" and not (
                node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "READ_ONLY_BEGIN"
            ):
                bad.append(f"db_source.py:{node.lineno}: raw exec_driver_sql")
            if name == "execute" and not (
                node.args
                and isinstance(node.args[0], ast.Call)
                and getattr(node.args[0].func, "id", None) == "text"
            ):
                bad.append(
                    f"db_source.py:{node.lineno}: execute() of a non-text() statement"
                )
        if path.name == "db_source.py":
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id.startswith("SQL_")
                    for t in node.targets
                ):
                    try:
                        _assert_select(ast.literal_eval(node.value))
                    except ReadOnlyViolation as exc:
                        bad.append(f"db_source.py:{node.lineno}: {exc}")
    return bad


def host_violations(pkg: Path) -> list[str]:
    bad: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for lineno, value in _strings(tree):
            for host in re.findall(r"(?:https?|wss?)://([A-Za-z0-9.-]*)", value):
                if host != "data.alpaca.markets":
                    bad.append(f"{path.name}:{lineno}: host {host!r}")
        for node in ast.walk(tree):
            # "https://" + <expression>: a host assembled at run time
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = _fold(node.left)
                if (
                    left is not None
                    and left.endswith("://")
                    and _fold(node.right) is None
                ):
                    bad.append(f"{path.name}:{node.lineno}: dynamic host")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in HTTP_VERBS - {"get"}:
                bad.append(
                    f"{path.name}:{node.lineno}: HTTP verb .{node.attr} referenced"
                )
        gated_get_seen = False
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                ):
                    continue
                verb, recv = node.func.attr, node.func.value
                recv_name = (
                    recv.attr
                    if isinstance(recv, ast.Attribute)
                    else getattr(recv, "id", "")
                )
                if verb not in HTTP_VERBS:
                    continue
                if recv_name in {"requests", "urllib", "httpx", "urllib3"}:
                    bad.append(
                        f"{path.name}:{node.lineno}: module-level HTTP {recv_name}.{verb}("
                    )
                elif "session" in recv_name.lower() or recv_name in {
                    "http",
                    "client",
                    "conn",
                }:
                    if (
                        path.name == "market_data.py"
                        and func.name == "_get"
                        and verb == "get"
                    ):
                        gate = [
                            c.lineno
                            for c in ast.walk(func)
                            if isinstance(c, ast.Call)
                            and getattr(c.func, "id", None) == "assert_data_host"
                        ]
                        if gate and min(gate) < node.lineno:
                            gated_get_seen = True
                            continue
                    bad.append(
                        f"{path.name}:{node.lineno}: ungated {recv_name}.{verb}("
                    )
        if path.name == "market_data.py" and not gated_get_seen:
            bad.append("market_data.py: no host-gated session.get found")
    return bad


def test_imports_are_allowlisted():
    assert import_violations(PKG) == []


def test_no_write_sql_and_every_query_is_select():
    assert sql_violations(PKG) == []
    names = {n for n in dir(db_source) if n.startswith("SQL_")}
    assert {getattr(db_source, n) for n in names} == set(db_source.ALL_QUERIES)


def test_only_the_data_host_and_only_get():
    assert host_violations(PKG) == []


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM review.toss_live_order_ledger",
        "SELECT 1; DELETE FROM x",
        "UPDATE x SET y=1",
        "SELECT * FROM review.order_proposals FOR UPDATE",
        "WITH a AS (INSERT INTO t VALUES (1) RETURNING 1) SELECT 1",
        "SELECT pg_advisory_lock(1)",
    ],
)
def test_assert_select_refuses_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        _assert_select(sql)


class _Result(list):
    pass


class FakeConn:
    def __init__(self):
        self.sql: list[str] = []

    def exec_driver_sql(self, sql):
        self.sql.append(sql)

    def execute(self, statement, params):
        self.sql.append(str(statement))
        return _Result()


def test_read_only_transaction_is_first_and_only_selects_follow():
    from datetime import UTC, datetime

    conn = FakeConn()
    run_read_only(conn, datetime(2025, 11, 1, tzinfo=UTC))
    assert conn.sql[0] == READ_ONLY_BEGIN
    assert all(re.match(r"^\s*SELECT\b", s) for s in conn.sql[1:])
    assert (
        len(conn.sql) == 1 + 4
    )  # 3 order queries + rungs (no symbols -> no universe query)


def test_dsn_comes_from_named_env_only_and_is_never_echoed(monkeypatch):
    monkeypatch.setenv(
        "ROB1391_TEST_DSN", "postgresql://u:s3cr3t-value@db.invalid:5432/prod"
    )
    url = db_source.database_url_from_env("ROB1391_TEST_DSN")
    assert url.startswith("postgresql+asyncpg://")
    monkeypatch.setenv("ROB1391_TEST_DSN", "mysql://u:s3cr3t-value@db.invalid/x")
    with pytest.raises(ValueError) as info:
        db_source.database_url_from_env("ROB1391_TEST_DSN")
    assert "s3cr3t" not in str(info.value)
    with pytest.raises(ValueError):
        db_source.database_url_from_env("not a name")


def test_selected_columns_exist_on_the_orm_models():
    """Catches a renamed/dropped column without a database."""

    from app.models.order_proposals import OrderProposal, OrderProposalRung
    from app.models.review import (
        KISLiveOrderLedger,
        LiveOrderLedger,
        TossLiveOrderLedger,
    )
    from app.models.us_symbol_universe import USSymbolUniverse

    tables = {
        "t": TossLiveOrderLedger,
        "l": LiveOrderLedger,
        "k": KISLiveOrderLedger,
        "r": OrderProposalRung,
        "p": OrderProposal,
        "u": USSymbolUniverse,
    }
    for sql in db_source.ALL_QUERIES:
        for alias, column in re.findall(r"\b([tlkrpu])\.([a-z_]+)\b", sql):
            assert column in tables[alias].__table__.columns, f"{alias}.{column}"
    assert TossLiveOrderLedger.__table__.schema == "review"
    assert USSymbolUniverse.__table__.schema is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_schema_selects_inside_read_only_transaction(db_session):
    """On the conftest-managed run-owned test database (real migrated schema):
    every SELECT runs, and PostgreSQL itself rejects a write in that txn.

    No row is written; the URL is validated as run-owned before connecting
    (never the local :5432 copy -- the conftest refuses unowned databases).
    """

    from datetime import UTC, datetime

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests._run_owned_database import validate_run_owned_database_url

    bind = db_session.bind
    url = validate_run_owned_database_url(getattr(bind, "url", None) or bind.engine.url)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            snap = await conn.run_sync(run_read_only, datetime(2100, 1, 1, tzinfo=UTC))
            assert snap.order_rows == [] and snap.rung_rows == []
            rows = await conn.execute(
                text(_assert_select(db_source.SQL_US_UNIVERSE)),
                {"symbols": ["ROB1391NONE"]},
            )
            assert rows.all() == []
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await conn.exec_driver_sql("CREATE TEMP TABLE rob1391_probe (x int)")
            await trans.rollback()
    finally:
        await engine.dispose()


def test_runtime_closure_loads_no_write_path_module():
    code = (
        "import sys\n"
        "import research.rob1391_us_resubmit_shadow.cli\n"
        "import research.rob1391_us_resubmit_shadow.guards as g\n"
        "g.current_policy_stamp()\n"
        "print('|'.join(g.loaded_forbidden_modules()))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=PKG.parents[1],
    )
    assert out.stdout.strip() == ""


# ------------------------------------------------------------------ Alpaca client


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params, timeout, allow_redirects=True):
        assert allow_redirects is False  # the client must never follow redirects
        self.calls.append((url, dict(params)))
        return self.responses.pop(0)


def client(responses, sink=None):
    sess = Session(responses)
    sunk: list = []

    def default_sink(endpoint, params, payload):
        sunk.append(endpoint)
        return f"sha-{len(sunk)}"

    c = market_data.AlpacaMarketData(
        feed="sip", sink=sink or default_sink, session=sess, min_interval_sec=0
    )
    return c, sess, sunk


BAR = {
    "t": "2025-11-18T15:00:00Z",
    "o": 99.6,
    "h": 99.7,
    "l": 99.49,
    "c": 99.55,
    "v": 50000,
    "n": 412,
    "vw": 99.58,
}


def test_bars_pagination_and_raw_pages_persisted():
    c, sess, sunk = client(
        [
            Resp(200, {"bars": {"SYMA": [BAR]}, "next_page_token": "tok"}),
            Resp(
                200,
                {
                    "bars": {"SYMA": [dict(BAR, t="2025-11-18T15:01:00Z")]},
                    "next_page_token": None,
                },
            ),
        ]
    )
    from datetime import UTC, datetime

    res = c.minute_bars(
        "SYMA",
        datetime(2025, 11, 18, 14, 30, tzinfo=UTC),
        datetime(2025, 11, 18, 21, tzinfo=UTC),
    )
    assert (
        res.status == "ok"
        and len(res.bars) == 2
        and res.bars[0].low == Decimal("99.49")
    )
    assert res.digests == ("sha-1", "sha-2") and sunk == ["bars", "bars"]
    assert sess.calls[1][1]["page_token"] == "tok"
    assert {p["feed"] for _, p in sess.calls} == {"sip"} and {
        p["adjustment"] for _, p in sess.calls
    } == {"raw"}
    assert {u for u, _ in sess.calls} == {"https://data.alpaca.markets/v2/stocks/bars"}


def test_http_error_and_pagination_cap_are_unknown_not_empty():
    from datetime import UTC, datetime

    t0, t1 = (
        datetime(2025, 11, 18, 14, 30, tzinfo=UTC),
        datetime(2025, 11, 18, 21, tzinfo=UTC),
    )
    c, _, _ = client([Resp(500, {})])
    assert c.minute_bars("SYMA", t0, t1).status == "unknown"
    c, _, _ = client(
        [Resp(200, {"quotes": {"SYMA": []}, "next_page_token": "again"})]
        * market_data.MAX_PAGES_PER_WINDOW
    )
    ev = c.ask_at_or_below("SYMA", t0, t1, Decimal("99.50"))
    assert ev.status == "unknown" and ev.reason == "pagination_cap"


def test_crossed_or_zero_quotes_are_not_valid_ask_evidence():
    from datetime import UTC, datetime

    quotes = [
        {
            "t": "2025-11-18T15:00:00.1Z",
            "ap": 99.40,
            "as": 1,
            "bp": 99.45,
            "bs": 1,
        },  # crossed
        {
            "t": "2025-11-18T15:00:00.2Z",
            "ap": 0,
            "as": 1,
            "bp": 99.0,
            "bs": 1,
        },  # zero ask
        {
            "t": "2025-11-18T15:00:00.3Z",
            "ap": 99.49,
            "as": 0,
            "bp": 99.0,
            "bs": 1,
        },  # no size
    ]
    c, _, _ = client([Resp(200, {"quotes": {"SYMA": quotes}, "next_page_token": None})])
    ev = c.ask_at_or_below(
        "SYMA",
        datetime(2025, 11, 18, 15, tzinfo=UTC),
        datetime(2025, 11, 18, 15, 1, tzinfo=UTC),
        Decimal("99.50"),
    )
    # invalid quotes are not evidence: no valid quote at all -> UNKNOWN, never "not found"
    assert (
        ev.status == "unknown"
        and ev.found is False
        and ev.reason == "no_quote_evidence"
    )


def test_host_pin_rejects_trading_host_before_any_request(monkeypatch):
    from datetime import UTC, datetime

    from research.us_intraday_corpus.alpaca_data import ForbiddenHostError

    c, sess, _ = client([])
    monkeypatch.setattr(
        market_data, "BARS_URL", "https://paper-api.alpaca.markets/v2/stocks/bars"
    )
    with pytest.raises(ForbiddenHostError):
        c.minute_bars(
            "SYMA",
            datetime(2025, 11, 18, 14, 30, tzinfo=UTC),
            datetime(2025, 11, 18, 21, tzinfo=UTC),
        )
    assert sess.calls == []


def test_feed_is_closed_vocabulary():
    with pytest.raises(ValueError):
        market_data.AlpacaMarketData(
            feed="otc", sink=lambda *a: "x", session=Session([])
        )
