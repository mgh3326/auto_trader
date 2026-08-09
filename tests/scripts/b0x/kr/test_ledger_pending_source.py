"""KR 자기 미체결 = ``kis_mock_order_ledger`` — contract v1.6 ①~④.

Four things have to hold at once, and each is easy to break in a way that
still looks like it works:

1. The ledger answer really becomes ``own_pending`` and really blocks the
   symbol, across cycles — not just within one (the X-E1 lesson: 「상한이
   발화했다 ≠ 그 상한이 의도한 기간 동안 구속한다」). ``KR_TWO_CYCLE_SIM``
   below is the multi-cycle observation that can tell those apart.
2. A ledger that cannot answer lands back on ``PendingUnreadable`` — v1.6 ④.
   An empty tuple and a failed read must never be the same value.
3. The ledger never becomes position truth — v1.6 ②.
4. No submission path can exist that skips the ledger write — v1.6 ③'s new
   mutant. A ledger-based dedup gate is only as good as the guarantee that
   every order that goes out is in the ledger.

(3) and (4) are static: a behavioural test can prove what today's code does,
but only a source-level guard can catch *a new path* being added. Both styles
are here, and each detector is self-tested against synthetic source so "the
guard exists" is never mistaken for "the guard fires".
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.broker_truth import PendingUnreadable
from scripts.b0x.kr import pending_ledger as kr_pending_ledger
from scripts.b0x.kr.cycle import broker_state, run_kr_cycle
from scripts.b0x.kr.mock import KR_PENDING_UNREADABLE, FreshTruth, RawPosition
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table
from tests.scripts.b0x.kr._pending import (
    StatefulPendingLedger,
    foreign_traces,
    readable_pending,
)

pytestmark = pytest.mark.unit

#: Captured at import time, before ``conftest.py``'s autouse fixture replaces
#: the module attribute. The tests below that exercise the *real* reader's
#: empty-vs-unreadable behaviour have to call the production function; every
#: other test still goes through the guarded seam.
REAL_READ_OWN_PENDING = kr_pending_ledger.read_own_pending
REAL_READ_FOREIGN_TRACES = kr_pending_ledger.read_foreign_traces

REPO_ROOT = Path(__file__).resolve().parents[4]
KR_PACKAGE = REPO_ROOT / "scripts" / "b0x" / "kr"
KR_RUNNER = REPO_ROOT / "scripts" / "run_b0x_kr_cycle.py"
B0X_PACKAGE = REPO_ROOT / "scripts" / "b0x"
OTHER_RUNNERS = (
    REPO_ROOT / "scripts" / "run_b0x_cycle.py",
    REPO_ROOT / "scripts" / "run_b0x_cancel.py",
)

# 2026-08-10 Monday 02:00 UTC = 11:00 KST, inside the XKRX regular session.
CYCLE_1 = dt.datetime(2026, 8, 10, 2, 0, tzinfo=dt.UTC)
CYCLE_2 = dt.datetime(2026, 8, 10, 3, 0, tzinfo=dt.UTC)  # same KST day
CYCLE_3 = dt.datetime(2026, 8, 10, 4, 30, tzinfo=dt.UTC)  # same KST day
NEXT_DAY = dt.datetime(2026, 8, 11, 2, 0, tzinfo=dt.UTC)  # Tuesday, next KST day


def _kr_files() -> list[Path]:
    return sorted([*KR_PACKAGE.rglob("*.py"), KR_RUNNER])


def _non_kr_b0x_files() -> list[Path]:
    return sorted(
        [
            path
            for path in [*B0X_PACKAGE.rglob("*.py"), *OTHER_RUNNERS]
            if KR_PACKAGE not in path.parents and path != KR_PACKAGE
        ]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def table_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            market="kr",
            rows=[
                make_row(symbol="005930", previous_close="97000", buy_l1="94090"),
                make_row(symbol="000660", previous_close="200000", buy_l1="194000"),
            ],
            generated_at=CYCLE_1 - dt.timedelta(hours=1),
        ),
        market="kr",
    )
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "observations"


class _FakeKrClient:
    """Flat kis_mock account: cash only, no holdings."""

    def __init__(self, *, stocks: list[dict[str, Any]] | None = None) -> None:
        self._stocks = stocks or []

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return {"dnca_tot_amt": 5_000_000.0, "stck_cash_ord_psbl_amt": 5_000_000.0}

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return self._stocks

    async def close(self) -> None:  # pragma: no cover — caller-owned here
        pass


class _RecordingBroker:
    """``KisMockBroker`` stand-in that reports the ledger row it would cause.

    The real broker's ``submit_buy`` reaches ``_place_order_impl(is_mock=True)``
    → ``_execute_and_record``, whose first action on the mock branch is the
    pre-submit attribution gate that commits the ``correlation_id`` row. This
    fake models exactly that one property (a send produces a ledger row keyed
    by correlation id) and nothing else — no HTTP, no DB, no reservation.
    """

    def __init__(self, ledger: StatefulPendingLedger, now: dt.datetime) -> None:
        self._ledger = ledger
        self._now = now
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []

    def at(self, now: dt.datetime) -> _RecordingBroker:
        self._now = now
        return self

    async def submit_buy(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["confirm"] is True
        self.buy_calls.append(kwargs)
        self._ledger.record(
            now=self._now,
            symbol=kwargs["symbol"],
            correlation_id=kwargs["correlation_id"],
        )
        return {
            "success": True,
            "odno": f"FAKE-BUY-{len(self.buy_calls)}",
            "ledger_id": len(self.buy_calls),
        }

    async def submit_exit_sell(self, **kwargs: Any) -> dict[str, Any]:
        self.sell_calls.append(kwargs)
        self._ledger.record(
            now=self._now,
            symbol=kwargs["symbol"],
            correlation_id=kwargs["correlation_id"],
        )
        return {
            "success": True,
            "odno": f"FAKE-SELL-{len(self.sell_calls)}",
            "ledger_id": len(self.sell_calls),
        }


# ---------------------------------------------------------------------------
# 1. The ledger answer is the dedup input (v1.6 ①②)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ledger_named_symbol_is_refused_and_the_rest_still_derive(
    table_dir: Path, out_dir: Path
) -> None:
    """v1.6 ① — the exception supplies a *working* input, not a blanket block.

    X-E1's fail-close refused every symbol because nothing could be known.
    With the ledger answering, the refusal is symbol-scoped again: the named
    symbol is blocked with ``own_pending_order_exists`` (not
    ``own_pending_unreadable``), and every other row derives normally.
    """

    outcome = await run_kr_cycle(
        now=CYCLE_1,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(),
        pending_reader=readable_pending("005930"),
    )

    truth = outcome.record["broker_truth"]
    assert truth["own_pending_readable"] is True
    assert truth["own_pending"] == ["005930"]
    assert outcome.record["own_pending_source"].startswith("kis_mock_order_ledger")

    blocked = [s for s in outcome.record["skipped"] if s["symbol"] == "005930"]
    assert blocked and all(s["reason"] == "own_pending_order_exists" for s in blocked)
    assert {order["symbol"] for order in outcome.record["orders"]} == {"000660"}


@pytest.mark.asyncio
async def test_the_submission_boundary_also_refuses_a_ledger_named_symbol(
    table_dir: Path, out_dir: Path
) -> None:
    """The gate is at dispatch too, not only in derivation.

    Derivation already drops the row, so this asserts the *second* check by
    calling the submission helper directly with a plan derivation would never
    have produced — the shape a future caller bug would take.
    """

    from scripts.b0x.broker_truth import OwnPendingResubmitBlocked
    from scripts.b0x.kr import mock as kr_mock

    fresh = FreshTruth(cash=Decimal("0"), nav=Decimal("0"), positions=())
    truth = fresh.broker_truth(("005930",))
    planned = kr_mock.PlannedOrder(
        order_key="deadbeefdeadbeef",
        client_order_id="b0xk-deadbeefdeadbeef",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=94_000,
        quantity=1,
        notional=94_000,
    )
    broker = _RecordingBroker(StatefulPendingLedger(), CYCLE_1)

    with pytest.raises(OwnPendingResubmitBlocked):
        await kr_mock.submit_planned_order(
            broker, planned=planned, confirm=True, broker_truth=truth
        )
    assert broker.buy_calls == []


# ---------------------------------------------------------------------------
# 2. KR_TWO_CYCLE_SIM — the cap binds across cycles (v1.6 ①③)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kr_two_cycle_sim_the_same_symbol_is_never_submitted_twice(
    table_dir: Path, out_dir: Path
) -> None:
    """🔴 KR_TWO_CYCLE_SIM — fixed fixture, zero broker calls, zero network.

    Cycle 1 derives both rows, then this test models the pre-submit signal rows
    that the real chokepoint commits before a send. Cycle 2 and 3, on the same
    KRX trading day, read those rows back and derive **nothing**. Cycle 4
    crosses the KST day boundary (KRX day orders cannot survive it) and derives
    again. The confirm-path mutation-boundary re-read is covered separately
    below because confirmed production code intentionally uses wall-clock time
    rather than this deterministic replay clock.

    This is the observation X-E1 established is the only one that can tell
    「상한이 발화했다」 from 「상한이 구속한다」: a single cycle looks identical
    either way.
    """

    ledger = StatefulPendingLedger()
    reader = ledger.reader()
    derived_per_cycle: list[list[str]] = []

    for index, now in enumerate((CYCLE_1, CYCLE_2, CYCLE_3, NEXT_DAY)):
        # The table is regenerated each cycle so table age never becomes the
        # reason a later cycle derives nothing.
        write_table(
            table_dir,
            make_payload(
                market="kr",
                rows=[
                    make_row(symbol="005930", previous_close="97000", buy_l1="94090"),
                    make_row(symbol="000660", previous_close="200000", buy_l1="194000"),
                ],
                generated_at=now - dt.timedelta(hours=1),
            ),
            market="kr",
        )
        outcome = await run_kr_cycle(
            now=now,
            table_dir=table_dir,
            out_dir=out_dir / f"cycle{index}",
            confirm=False,
            client=_FakeKrClient(),
            pending_reader=reader,
        )
        assert outcome.zero_order_reason is None
        derived_per_cycle.append(
            sorted(order["symbol"] for order in outcome.record["orders"])
        )
        if index == 0:
            for planned in outcome.record["planned"]:
                ledger.record(
                    now=now,
                    symbol=planned["symbol"],
                    correlation_id=planned["client_order_id"],
                )

    assert derived_per_cycle == [
        ["000660", "005930"],  # cycle 1 — nothing recorded yet
        [],  # cycle 2 — both symbols held by the ledger
        [],  # cycle 3 — still held; no drift, no re-derivation
        ["000660", "005930"],  # next KST day — day orders cannot have survived
    ]
    assert ledger.reads == ["2026-08-10", "2026-08-10", "2026-08-10", "2026-08-11"]


@pytest.mark.asyncio
async def test_confirm_route_rechecks_and_observes_post_submit_dedup(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """The actual confirm boundary observes its forced pre-submit trace.

    Confirm uses a real wall clock for the five-minute preflight window, so
    the fake ledger records on that same wall-clock day. No network or DB is
    involved; ``_RecordingBroker`` models only the durable correlation row the
    sanctioned adapter creates before its mock POST.
    """

    ledger = StatefulPendingLedger()
    broker = _RecordingBroker(ledger, dt.datetime.now(dt.UTC))
    outcome = await run_kr_cycle(
        now=CYCLE_1,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(),
        broker=broker,
        pending_reader=ledger.reader(),
        foreign_trace_reader=foreign_traces(),
    )

    assert outcome.zero_order_reason is None
    assert len(broker.buy_calls) == 1
    assert outcome.record["post_submit_dedup"] == [
        {
            "symbol": outcome.record["planned"][0]["symbol"],
            "correlation_id": outcome.record["planned"][0]["client_order_id"],
            "observed": True,
        }
    ]
    assert outcome.record["submission_stopped"] == "acceptance_submission_limit=1"
    assert len(ledger.reads) == 3  # preflight, mutation boundary, post-submit
    assert armed_confirm == ["acquired", "released"]


# ---------------------------------------------------------------------------
# 3. Over-block direction (v1.6 ③) — nothing is released by inference
# ---------------------------------------------------------------------------

#: Names that could only appear in this module if it were trying to decide
#: whether a recorded order is *still* resting. v1.6 ③ forbids that: every such
#: inference can release a symbol that is in fact still working, which is the
#: 관대한 방향(누락). Over-blocking (a filled/cancelled order reading as pending)
#: is the sanctioned error.
FORBIDDEN_RELEASE_PREDICATES = (
    "lifecycle_state",
    "reconciled_at",
    "reconcile_attempts",
    "net_pnl",
    "order_no",
    "scalping_role",
    "outcome_state",
    "suppressed_reason",
)


def test_the_pending_reader_never_inspects_order_state() -> None:
    """v1.6 ③ — the reader must not filter on anything that could release."""

    source = (KR_PACKAGE / "pending_ledger.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    offenders = sorted(referenced & set(FORBIDDEN_RELEASE_PREDICATES))
    assert offenders == [], (
        "scripts/b0x/kr/pending_ledger.py references order-state fields "
        f"{offenders} — filtering on them would release symbols by inference "
        "(계약 v1.6 ③ 관대한 방향 금지)"
    )
    # `status` is the one shared word that also names something innocuous, so
    # assert on the query text rather than the identifier set.
    assert "status" not in source, (
        "an order-status predicate is a release-by-inference path (v1.6 ③)"
    )


def test_the_only_bound_is_the_kst_trading_day() -> None:
    """The window is structural (KRX day orders), not a guess about fills."""

    # 2026-08-10 02:00 UTC == 11:00 KST → the KST day starts at 00:00 KST,
    # which is 2026-08-09 15:00 UTC.
    start = kr_pending_ledger.kst_trading_day_start(CYCLE_1)
    assert start.utcoffset() == dt.timedelta(hours=9)
    assert start.isoformat() == "2026-08-10T00:00:00+09:00"
    assert kr_pending_ledger.kst_trading_day_label(CYCLE_1) == "2026-08-10"

    # 23:30 UTC is already the next KST day — a UTC-based window would put
    # these two instants on the same day and silently widen the release.
    late = dt.datetime(2026, 8, 10, 23, 30, tzinfo=dt.UTC)
    assert kr_pending_ledger.kst_trading_day_label(late) == "2026-08-11"

    # A naive timestamp is read as UTC (never as host-local), matching
    # app.core.timezone.trade_day_kst's own rule.
    naive = dt.datetime(2026, 8, 10, 23, 30)
    assert kr_pending_ledger.kst_trading_day_label(naive) == "2026-08-11"


# ---------------------------------------------------------------------------
# 4. Empty vs unreadable, exercised on the real reader (v1.6 ④)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[str]:
        return list(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    def __init__(self, results: list[list[Any]] | Exception) -> None:
        self._results = results
        self.statements: list[Any] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, statement: Any) -> _FakeResult:
        if isinstance(self._results, Exception):
            raise self._results
        self.statements.append(statement)
        return _FakeResult(self._results[len(self.statements) - 1])


@pytest.mark.asyncio
async def test_a_ledger_that_answers_nothing_is_readable_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Monday-first-cycle case: both tables answer, and name nothing."""

    session = _FakeSession([[], []])
    monkeypatch.setattr(kr_pending_ledger, "AsyncSessionLocal", lambda: session)

    result = await REAL_READ_OWN_PENDING(now=CYCLE_1, correlation_prefix="b0xk-")
    assert result == ()
    assert not isinstance(result, PendingUnreadable)
    # Both ledgers were queried — the pre-submit signal row is what closes the
    # lost-native-write hole, so dropping that query is a real regression.
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_both_ledgers_contribute_to_the_pending_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An order row alone is not enough — the order write can be lost."""

    session = _FakeSession([["005930"], ["000660", "005930"]])
    monkeypatch.setattr(kr_pending_ledger, "AsyncSessionLocal", lambda: session)

    result = await REAL_READ_OWN_PENDING(now=CYCLE_1, correlation_prefix="b0xk-")
    assert result == ("000660", "005930")


@pytest.mark.asyncio
async def test_a_failing_query_is_unreadable_never_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.6 ④ on the real function — the fault path cannot produce ``()``."""

    monkeypatch.setattr(
        kr_pending_ledger,
        "AsyncSessionLocal",
        lambda: _FakeSession(RuntimeError("boom: postgres://user:secret@host/db")),
    )

    result = await REAL_READ_OWN_PENDING(now=CYCLE_1, correlation_prefix="b0xk-")
    assert isinstance(result, PendingUnreadable)
    assert result.reason == kr_pending_ledger.LEDGER_UNREADABLE_REASON
    assert "RuntimeError" in result.detail
    assert "secret" not in result.detail  # type name only, never the message


@pytest.mark.asyncio
async def test_a_session_that_cannot_be_opened_is_also_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode() -> Any:
        raise OSError("no database configured")

    monkeypatch.setattr(kr_pending_ledger, "AsyncSessionLocal", _explode)
    result = await REAL_READ_OWN_PENDING(now=CYCLE_1, correlation_prefix="b0xk-")
    assert isinstance(result, PendingUnreadable)
    assert "OSError" in result.detail


class _LedgerTraceRow:
    def __init__(self, *, symbol: str, correlation_id: str | None) -> None:
        self.symbol = symbol
        self.correlation_id = correlation_id


@pytest.mark.asyncio
async def test_foreign_trace_reader_separates_b0x_from_other_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NW-B4: only another correlation is contamination; values stay redacted."""

    session = _FakeSession(
        [
            [
                _LedgerTraceRow(symbol="005930", correlation_id="b0xk-own"),
                _LedgerTraceRow(symbol="000660", correlation_id="other-order"),
            ],
            [
                _LedgerTraceRow(symbol="005930", correlation_id=None),
                _LedgerTraceRow(symbol="035420", correlation_id="b0xk-other"),
            ],
        ]
    )
    monkeypatch.setattr(kr_pending_ledger, "AsyncSessionLocal", lambda: session)

    result = await REAL_READ_FOREIGN_TRACES(now=CYCLE_1, correlation_prefix="b0xk-")

    assert not isinstance(result, PendingUnreadable)
    assert result.canonical() == {
        "symbols": ["000660", "005930"],
        "order_trace_count": 1,
        "signal_trace_count": 1,
        "trace_count": 2,
    }
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_foreign_trace_reader_failure_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kr_pending_ledger,
        "AsyncSessionLocal",
        lambda: _FakeSession(RuntimeError("boom: postgres://user:secret@host/db")),
    )

    result = await REAL_READ_FOREIGN_TRACES(now=CYCLE_1, correlation_prefix="b0xk-")

    assert isinstance(result, PendingUnreadable)
    assert result.reason == kr_pending_ledger.LEDGER_UNREADABLE_REASON
    assert "RuntimeError" in result.detail
    assert "secret" not in result.detail


def test_pending_unreadable_is_still_the_default_without_a_reader() -> None:
    """v1.6 ④ — X-E1's state was kept, and it is what an un-wired caller gets."""

    fresh = FreshTruth(cash=0, nav=0, positions=())
    assert fresh.broker_truth().own_pending is KR_PENDING_UNREADABLE
    assert fresh.broker_truth().pending_unreadable is not None
    assert broker_state(fresh=fresh).broker_truth.pending_unreadable is not None
    assert fresh.status_only()["own_pending_readable"] is False


# ---------------------------------------------------------------------------
# MUTANT ② — the exception must not spread to other lanes
# ---------------------------------------------------------------------------

#: Modules whose import outside ``scripts/b0x/kr/**`` would mean the kis_mock
#: self-record exception had leaked into a lane that can read its venue.
KR_LEDGER_ONLY_MODULES = (
    "scripts.b0x.kr.pending_ledger",
    "app.models.review",
    "app.mcp_server.tooling.kis_mock_ledger",
    "app.services.brokers.kis.mock_scalping_exec.ledger_state",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _non_kr_b0x_files(), ids=lambda p: p.name)
def test_no_other_lane_imports_the_kis_mock_ledger_source(path: Path) -> None:
    """v1.6 ① 🔴 crypto·US 로 확대 금지 — enforced statically, not by habit."""

    offenders = sorted(
        module
        for module in _imported_modules(path)
        for forbidden in KR_LEDGER_ONLY_MODULES
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], (
        f"{path.relative_to(REPO_ROOT)} imports the kis_mock-only pending "
        f"source {offenders} — 계약 v1.6 ① limits the self-record exception to "
        "kis_mock; every other lane reads its venue"
    )


def test_the_non_kr_scan_actually_covers_the_crypto_lane() -> None:
    """A guard over an empty file list passes vacuously."""

    scanned = {path.name for path in _non_kr_b0x_files()}
    assert {"sidecar.py", "shadow.py", "cycle.py", "broker_truth.py"} <= scanned


def test_the_crypto_lane_still_reads_pending_from_its_venue() -> None:
    """The sidecar's own-pending set must still come from the venue's own
    open-orders answer keyed on ``b0xc`` — untouched by this round."""

    sidecar = (B0X_PACKAGE / "crypto" / "sidecar.py").read_text(encoding="utf-8")
    assert "own_order_symbols" in sidecar
    assert "CLIENT_ORDER_ID_PREFIX" in sidecar
    assert "kis_mock" not in sidecar


# ---------------------------------------------------------------------------
# MUTANT ③ — the ledger must never become position truth
# ---------------------------------------------------------------------------

#: Every name by which a pending-ledger answer travels through this package.
#: None of them may appear inside a position-valued argument.
PENDING_DERIVED_NAMES = frozenset(
    {
        "own_pending",
        "pending_reader",
        "read_own_pending",
        "pending_ledger",
        "kr_pending_ledger",
    }
)

#: Keywords whose value *is* position truth. v1.6 ②: 포지션 진실은 계속 브로커
#: 조회 — so nothing pending-derived may be passed here, in any lane file.
POSITION_KEYWORDS = frozenset({"positions", "position_symbols"})


def find_pending_as_position_truth(tree: ast.AST) -> list[str]:
    """v1.6 ② — a pending-derived value used where a position belongs."""

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in POSITION_KEYWORDS:
                continue
            names = {
                inner.id
                for inner in ast.walk(keyword.value)
                if isinstance(inner, ast.Name)
            } | {
                inner.attr
                for inner in ast.walk(keyword.value)
                if isinstance(inner, ast.Attribute)
            }
            leaked = sorted(names & PENDING_DERIVED_NAMES)
            if leaked:
                violations.append(
                    f"line {node.lineno}: {keyword.arg}= is fed by {leaked} — "
                    "the ledger is a dedup input only (계약 v1.6 ②)"
                )
    return violations


@pytest.mark.parametrize("path", _kr_files(), ids=lambda p: p.name)
def test_the_ledger_is_never_passed_as_position_truth(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert find_pending_as_position_truth(tree) == [], f"{path.name}: v1.6 ② violation"


def test_detector_catches_pending_used_as_position_truth() -> None:
    assert find_pending_as_position_truth(ast.parse("f(position_symbols=own_pending)"))
    assert find_pending_as_position_truth(ast.parse("f(positions=own_pending)"))
    assert find_pending_as_position_truth(
        ast.parse("f(positions=tuple(sorted(set(own_pending))))")
    )
    assert find_pending_as_position_truth(
        ast.parse("f(position_symbols=await read_own_pending(now=n))")
    )


def test_detector_allows_the_real_position_sources() -> None:
    assert (
        find_pending_as_position_truth(
            ast.parse("BrokerTruth(position_symbols=self.non_dust_position_symbols())")
        )
        == []
    )
    assert find_pending_as_position_truth(ast.parse("f(positions=positions)")) == []


def test_the_two_position_sources_are_the_broker_read_and_nothing_else() -> None:
    """Pin the actual expressions, so a rename cannot quietly re-point them."""

    mock_tree = ast.parse((KR_PACKAGE / "mock.py").read_text(encoding="utf-8"))
    position_args = [
        keyword.value
        for node in ast.walk(mock_tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "position_symbols"
    ]
    assert position_args, "FreshTruth.broker_truth stopped naming position_symbols"
    for value in position_args:
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Attribute)
        assert value.func.attr == "non_dust_position_symbols"

    cycle_tree = ast.parse((KR_PACKAGE / "cycle.py").read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(cycle_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "broker_state"
    )
    iterated = {
        inner.func.attr
        for node in ast.walk(func)
        if isinstance(node, ast.GeneratorExp)
        for generator in node.generators
        for inner in ast.walk(generator.iter)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
    } | {
        inner.attr
        for node in ast.walk(func)
        if isinstance(node, ast.GeneratorExp)
        for generator in node.generators
        for inner in ast.walk(generator.iter)
        if isinstance(inner, ast.Attribute)
    }
    assert "positions" in iterated, (
        "broker_state stopped building positions from the holdings snapshot"
    )
    assert not (iterated & PENDING_DERIVED_NAMES)


@pytest.mark.asyncio
async def test_a_ledger_named_symbol_is_not_a_position(
    table_dir: Path, out_dir: Path
) -> None:
    """v1.6 ② behaviourally: the ledger cannot manufacture a holding.

    A symbol the ledger names, with an account that holds nothing, must leave
    the position count at zero — otherwise the 동시 포지션 cap (and averaging /
    sell sizing, which read ``positions``) would be driven by self-record.
    """

    outcome = await run_kr_cycle(
        now=CYCLE_1,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=[]),
        pending_reader=readable_pending("005930", "000660"),
    )

    truth = outcome.record["broker_truth"]
    assert truth["own_pending"] == ["000660", "005930"]
    assert truth["position_symbols"] == []
    assert outcome.record["fresh_truth"]["non_dust_position_symbols"] == []


def test_positions_are_built_from_holdings_even_when_pending_disagrees() -> None:
    """The two views stay independent in both directions."""

    fresh = FreshTruth(
        cash=1_000_000,
        nav=1_100_000,
        positions=(
            RawPosition(
                symbol="035720",
                quantity=10,
                average_price=50_000,
                evaluation_amount=520_000,
            ),
        ),
    )
    state = broker_state(fresh=fresh, own_pending=("005930",))

    assert [pos.symbol for pos in state.positions] == ["035720"]
    assert state.broker_truth.position_symbols == ("035720",)
    assert state.broker_truth.own_pending_symbols == ("005930",)
    assert state.broker_truth.concurrent_position_count == 1


# ---------------------------------------------------------------------------
# MUTANT ① — no submission path may bypass the ledger write (v1.6 ③ new)
# ---------------------------------------------------------------------------

#: Callables that put an order at a venue. If one of these is reached from
#: anywhere but the single wired chokepoint, an order can leave without a
#: ledger row — and a ledger-based dedup gate is only as strong as that.
SUBMISSION_CALLEES = {
    "submit_buy",
    "submit_sell",
    "submit_exit_sell",
    "submit_order",
    "submit_planned",
    "place_order",
    "send_order",
    "_place_order_impl",
    "order_korea_stock",
    "sell_korea_stock",
    "modify_korea_order",
    "cancel_korea_order",
}

#: The one function allowed to contain them. It is in ``scripts/b0x/kr/mock.py``
#: and it dispatches only through ``KisMockBroker``, whose every send path runs
#: the ``order_execution`` pre-submit gate that writes the ledger.
LEDGER_WRITING_CHOKEPOINT = "submit_planned_order"


def find_submission_calls_outside_the_chokepoint(tree: ast.AST) -> list[str]:
    sanctioned: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == LEDGER_WRITING_CHOKEPOINT
        ):
            sanctioned.update(id(inner) for inner in ast.walk(node))

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if callee is None and isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee in SUBMISSION_CALLEES and id(node) not in sanctioned:
            violations.append(
                f"line {node.lineno}: {callee}() outside "
                f"{LEDGER_WRITING_CHOKEPOINT}() — a send that skips the ledger "
                "write breaks the v1.6 ① dedup source"
            )
    return violations


@pytest.mark.parametrize("path", _kr_files(), ids=lambda p: p.name)
def test_no_submission_path_bypasses_the_ledger_writing_chokepoint(
    path: Path,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert find_submission_calls_outside_the_chokepoint(tree) == [], f"{path.name}"


def test_detector_catches_a_bypassing_submission_path() -> None:
    bypass = "async def sneak(b):\n    return await b.submit_buy(symbol='005930')\n"
    assert find_submission_calls_outside_the_chokepoint(ast.parse(bypass)) != []


def test_detector_allows_the_chokepoint_itself() -> None:
    ok = (
        "async def submit_planned_order(b):\n"
        "    return await b.submit_buy(symbol='005930')\n"
    )
    assert find_submission_calls_outside_the_chokepoint(ast.parse(ok)) == []


def test_the_chokepoint_rechecks_the_pending_gate_before_dispatching() -> None:
    """Order matters: the re-check must precede every submit call in there."""

    tree = ast.parse((KR_PACKAGE / "mock.py").read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == LEDGER_WRITING_CHOKEPOINT
    )
    gate_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_resubmit_allowed"
    ]
    submit_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SUBMISSION_CALLEES
    ]
    assert gate_lines, "the submission chokepoint dropped its resubmit re-check"
    assert submit_lines, "the guard is scanning the wrong function"
    assert max(gate_lines) < min(submit_lines)


def test_the_kr_scan_actually_has_files() -> None:
    assert len(_kr_files()) >= 4, _kr_files()
