"""Verify the verifiers: every guarded rule has a check that passes on the real
module and fails -- with an AssertionError, not a crash -- on a mutant.

A mutant is the real source with one exact snippet replaced (the snippet must
occur exactly once, so the mutant cannot silently drift from the code). It is
executed as a fresh module object under the real module name; dependents are
re-executed after it so they bind to the mutant.
"""

from __future__ import annotations

import shutil
import sys
import types
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from research.rob1391_us_resubmit_shadow import engine as real_engine
from research.rob1391_us_resubmit_shadow import fills as real_fills
from research.rob1391_us_resubmit_shadow import parents as real_parents
from research.rob1391_us_resubmit_shadow import stats as real_stats
from research.rob1391_us_resubmit_shadow.market_data import (
    OK,
    UNKNOWN,
    AskEvidence,
    parse_bar,
)
from tests.research.rob1391_us_resubmit_shadow import test_isolation as iso
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CAL,
    FakeMarket,
    FakeSource,
    bar_json,
    context,
    et,
    quote_json,
    rung_for,
    seal,
    toss_row,
    universe_row,
)

pytestmark = pytest.mark.unit

PKG_NAME = "research.rob1391_us_resubmit_shadow"
PKG = iso.PKG
D0 = date(2025, 11, 17)
P = Decimal("99.50")
OPEN = CAL.session(CAL.offset(D0, 1)).open_utc
CLOSE = CAL.session(CAL.offset(D0, 1)).close_utc


def _exec(monkeypatch, stem: str, source: str) -> types.ModuleType:
    name = f"{PKG_NAME}.{stem}"
    mod = types.ModuleType(name)
    mod.__file__ = str(PKG / f"{stem}.py")
    monkeypatch.setitem(sys.modules, name, mod)
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def mutant(
    monkeypatch,
    stem: str,
    old: str | list[tuple[str, str]],
    new: str | None = None,
    *,
    dependents: tuple[str, ...] = (),
) -> dict[str, types.ModuleType]:
    """``old``/``new`` is one replacement, or ``old`` is a list of pairs."""

    pairs = old if isinstance(old, list) else [(old, new)]
    src = (PKG / f"{stem}.py").read_text(encoding="utf-8")
    for before, after in pairs:
        assert src.count(before) == 1, (
            f"mutation anchor drifted in {stem}.py: {before!r}"
        )
        src = src.replace(before, after)
    mods = {stem: _exec(monkeypatch, stem, src)}
    for dep in dependents:
        mods[dep] = _exec(
            monkeypatch, dep, (PKG / f"{dep}.py").read_text(encoding="utf-8")
        )
    return mods


def bar(minute: int, *, low: float, v: int = 50_000):
    return parse_bar(
        bar_json(OPEN + timedelta(minutes=minute), o=99.7, h=99.8, low=low, c=99.6, v=v)
    )


# --------------------------------------------------------------------- checks


def check_trade_through(f) -> None:
    assert f.conservative_candidate(bar(1, low=99.50), P) is False
    assert f.conservative_candidate(bar(1, low=99.49), P) is True
    out = f.simulate_conservative(
        bars=(bar(1, low=99.50),),
        submit_at=OPEN,
        close_utc=CLOSE,
        limit=P,
        remaining=3,
        affordable=lambda _x: 99,
        ask_lookup=lambda a, b: AskEvidence(OK, found=True),
    )
    assert out.qty == 0


def check_unknown_not_zero(f) -> None:
    out = f.simulate_conservative(
        bars=(bar(1, low=99.40),),
        submit_at=OPEN,
        close_utc=CLOSE,
        limit=P,
        remaining=3,
        affordable=lambda _x: 99,
        ask_lookup=lambda a, b: AskEvidence(UNKNOWN, reason="http_500"),
    )
    assert out.status == UNKNOWN and out.fills == []


def check_volume_cap(f) -> None:
    assert f.volume_cap(Decimal(199)) == 1 and f.volume_cap(Decimal(99)) == 0
    out = f.simulate_conservative(
        bars=(bar(1, low=99.40, v=150),),
        submit_at=OPEN,
        close_utc=CLOSE,
        limit=P,
        remaining=3,
        affordable=lambda _x: 99,
        ask_lookup=lambda a, b: AskEvidence(OK, found=True),
    )
    assert out.qty == 1


def check_dedupe_boundaries(p) -> None:
    def eps(*specs):
        rows = [
            toss_row(100 + i, "SYMA", ordered_at=et(d, 10, 5), price=px, qty=2)
            for i, (d, px) in enumerate(specs)
        ]
        return len(
            p.build_episodes(
                p.assemble_orders(rows, [], [universe_row("SYMA")]), CAL
            ).episodes
        )

    assert eps((D0, "100.00"), (CAL.offset(D0, 5), "100.00")) == 1
    assert eps((D0, "100.00"), (CAL.offset(D0, 6), "100.00")) == 2
    assert eps((D0, "100.00"), (CAL.offset(D0, 1), "105.00")) == 1
    assert eps((D0, "100.00"), (CAL.offset(D0, 1), "105.01")) == 2


def check_holm(s) -> None:
    assert s.holm({"m1": 0.04, "m2": 0.02}) == {"m2": True, "m1": True}
    assert s.holm({"m1": 0.026, "m2": 0.03}) == {"m1": False, "m2": False}


def _flow(tmp_path, eng, rows):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    src.order_rows.extend(rows)
    src.rung_rows.extend(rung_for(r) for r in rows)
    src.universe_rows.extend(universe_row(s) for s in {r["symbol"] for r in rows})
    ctx = context(ledger, src, mkt, eng=eng)
    return ctx, src, mkt, ledger


def check_budget_counted_once(tmp_path, eng) -> None:
    rows = [
        toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2),
        toss_row(
            2, "SYMA", ordered_at=et(CAL.offset(D0, 2), 10, 5), price="98.00", qty=2
        ),
    ]
    ctx, *_ = _flow(tmp_path, eng, rows)
    eng.advance(ctx, CAL.offset(D0, 2))
    assert len(ctx.state.parents) == 1
    assert sum(p.budget for p in ctx.state.parents.values()) == Decimal("199.00")


def check_latch_persists(tmp_path, eng) -> None:
    rows = [toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2)]
    ctx, src, mkt, ledger = _flow(tmp_path, eng, rows)
    eng.advance(ctx, D0)
    changed = context(
        ledger, src, mkt, policy={"version": "x", "content_hash": "y"}, eng=eng
    )
    eng.step(changed, CAL.offset(D0, 1))
    again = context(ledger, src, mkt, eng=eng)
    src.order_rows.append(
        toss_row(
            2, "SYMB", ordered_at=et(CAL.offset(D0, 1), 10, 5), price="20.00", qty=2
        )
    )
    src.rung_rows.append(rung_for(src.order_rows[-1]))
    src.universe_rows.append(universe_row("SYMB"))
    eng.register(again, CAL.offset(D0, 1))
    assert again.state.registered[CAL.offset(D0, 1).isoformat()]["assigned"] == []
    (parent,) = [p for p in again.state.parents.values() if p.rec["symbol"] == "SYMA"]
    assert (
        parent.session_log[CAL.offset(D0, 1).isoformat()]["resubmission"]["T"]["reason"]
        == "latched"
    )


def check_session_offsets(tmp_path, eng) -> None:
    rows = [
        toss_row(
            1, "SYMA", ordered_at=et(date(2025, 11, 25), 10, 5), price="99.50", qty=2
        )
    ]
    ctx, *_ = _flow(tmp_path, eng, rows)
    eng.advance(ctx, date(2025, 11, 25))
    (parent,) = ctx.state.parents.values()
    assert parent.resubmit_sessions == ["2025-11-26", "2025-11-28", "2025-12-01"]
    assert parent.rec["d20"] == "2025-12-24"


def check_budget_never_exceeded(tmp_path, eng) -> None:
    # B_i = 2 x 99.50 = 199.00; costs > 0, so the prereg rule leaves room for
    # at most 1 virtual share (99.50 + costs) after nothing was filled.
    rows = [toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2)]
    ctx, src, mkt, _ = _flow(tmp_path, eng, rows)
    d1 = CAL.offset(D0, 1)
    sess = CAL.session(d1)
    bars = list(mkt._session_minutes("SYMA", d1))
    t = sess.open_utc + timedelta(minutes=30)
    bars[30] = bar_json(t, o=99.6, h=99.7, low=99.40, c=99.55, v=1_000_000)
    mkt.minutes[("SYMA", d1)] = bars
    mkt.quotes[("SYMA", d1)] = [
        quote_json(sess.open_utc + timedelta(seconds=1), bid=99.98, ask=100.00),
        quote_json(t + timedelta(seconds=5), bid=99.49, ask=99.50),
    ]
    eng.advance(ctx, d1)
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].shares == 1
    assert not ctx.state.latched


# ------------------------------------------------------------- real modules pass


def test_checks_pass_on_real_modules(tmp_path):
    check_trade_through(real_fills)
    check_unknown_not_zero(real_fills)
    check_volume_cap(real_fills)
    check_dedupe_boundaries(real_parents)
    check_holm(real_stats)
    for i, check in enumerate(
        (
            check_budget_counted_once,
            check_latch_persists,
            check_session_offsets,
            check_budget_never_exceeded,
            check_cash_reservation,
        )
    ):
        sub = tmp_path / str(i)
        sub.mkdir()
        check(sub, real_engine)


# ------------------------------------------------------------- mutants fail

FILL_MUTANTS = [
    (
        "relaxed_trade_through",
        "return bar.low <= limit - tick_size(limit)",
        "return bar.low <= limit",
        check_trade_through,
    ),
    (
        "unknown_ask_as_zero",
        "        if evidence.status != OK:\n",
        "        if False:\n",
        check_unknown_not_zero,
    ),
    (
        "volume_cap_ceiling",
        "rounding=ROUND_FLOOR",
        "rounding=__import__('decimal').ROUND_CEILING",
        check_volume_cap,
    ),
]


@pytest.mark.parametrize(
    ("name", "old", "new", "check"), FILL_MUTANTS, ids=[m[0] for m in FILL_MUTANTS]
)
def test_fill_mutants_are_red(monkeypatch, name, old, new, check):
    mods = mutant(monkeypatch, "fills", old, new)
    with pytest.raises(AssertionError):
        check(mods["fills"])


DEDUPE_MUTANTS = [
    ("gap_strict", "<= K.DEDUPE_MAX_SESSION_GAP", "< K.DEDUPE_MAX_SESSION_GAP"),
    ("band_strict", "<= K.DEDUPE_ANCHOR_BAND", "< K.DEDUPE_ANCHOR_BAND"),
    ("gap_widened", "<= K.DEDUPE_MAX_SESSION_GAP", "<= K.DEDUPE_MAX_SESSION_GAP + 1"),
]


@pytest.mark.parametrize(
    ("name", "old", "new"), DEDUPE_MUTANTS, ids=[m[0] for m in DEDUPE_MUTANTS]
)
def test_dedupe_boundary_mutants_are_red(monkeypatch, name, old, new):
    mods = mutant(monkeypatch, "parents", old, new)
    with pytest.raises(AssertionError):
        check_dedupe_boundaries(mods["parents"])


def test_holm_order_mutant_is_red(monkeypatch):
    mods = mutant(
        monkeypatch,
        "stats",
        "for name, alpha in zip(order, alphas, strict=True):",
        "for name, alpha in zip(order[::-1], alphas, strict=True):",
    )
    with pytest.raises(AssertionError):
        check_holm(mods["stats"])


def check_cash_reservation(tmp_path, eng) -> None:
    rows = [
        toss_row(i, f"SYM{i}", ordered_at=et(D0, 10, i), price="400.00", qty=10)
        for i in range(1, 4)
    ]
    ctx, *_ = _flow(tmp_path, eng, rows)
    eng.advance(ctx, D0)
    assert len(ctx.state.parents) == 2  # 2 x 4000 of E0 10000; third excluded


_STATE_DEPENDENTS = ("valuation", "report", "engine")

ENGINE_MUTANTS = [
    # B_i double count, realistic bug class: dedupe never joins (a member
    # becomes its own episode) AND the open-symbol block is gone.
    (
        "dedupe_broken_budget_counted_twice",
        [
            ("parents", [("<= K.DEDUPE_MAX_SESSION_GAP", "< 0")]),
            ("engine", [("        if sym in open_symbols:\n", "        if False:\n")]),
        ],
        check_budget_counted_once,
    ),
    (
        "reservation_double_counted",
        [
            (
                "engine",
                [
                    (
                        "        reserved += cand.budget\n",
                        "        reserved += 2 * cand.budget\n",
                    )
                ],
            )
        ],
        check_cash_reservation,
    ),
    (
        "latch_released",
        [
            (
                "state",
                [("        return bool(self.latches)\n", "        return False\n")],
            ),
            ("valuation", []),
            ("report", []),
            ("engine", []),
        ],
        check_latch_persists,
    ),
    (
        "resubmit_from_d0",
        [
            (
                "engine",
                [
                    (
                        "for k in range(1, K.RESUBMIT_SESSIONS + 1)",
                        "for k in range(0, K.RESUBMIT_SESSIONS)",
                    )
                ],
            )
        ],
        check_session_offsets,
    ),
    (
        "budget_cap_bypassed",
        [
            (
                "engine",
                [
                    (
                        "                affordable=affordable,\n                ask_lookup=",
                        "                affordable=lambda _f: 10**9,\n                ask_lookup=",
                    )
                ],
            )
        ],
        check_budget_never_exceeded,
    ),
]


@pytest.mark.parametrize(
    ("name", "steps", "check"), ENGINE_MUTANTS, ids=[m[0] for m in ENGINE_MUTANTS]
)
def test_engine_mutants_are_red(monkeypatch, tmp_path, name, steps, check):
    """Steps run in order; each re-executes its module so later ones bind to
    earlier mutants (an empty pair list re-executes the original source)."""

    mods: dict[str, types.ModuleType] = {}
    for stem, pairs in steps:
        mods.update(
            mutant(monkeypatch, stem, pairs)
            if pairs
            else {
                stem: _exec(
                    monkeypatch, stem, (PKG / f"{stem}.py").read_text(encoding="utf-8")
                )
            }
        )
    with pytest.raises(AssertionError):
        check(tmp_path, mods["engine"])


def test_all_engine_dedupe_guards_removed_still_refused_by_state_fold(
    monkeypatch, tmp_path
):
    """Fourth layer: the fold refuses a re-registered episode outright."""

    mods = mutant(
        monkeypatch,
        "engine",
        [
            (
                "        if ep.anchor.source_ref != o.source_ref:\n",
                "        if False:\n",
            ),
            ("        if sym in open_symbols:\n", "        if False:\n"),
            (
                "        if ep_id in taken_ids or cand.order.source_ref in taken_refs:\n",
                "        if False:\n",
            ),
        ],
    )
    with pytest.raises(ValueError, match="registered twice"):
        check_budget_counted_once(tmp_path, mods["engine"])


@pytest.mark.parametrize(
    "only",
    [
        "        if ep.anchor.source_ref != o.source_ref:\n",
        "        if sym in open_symbols:\n",
        "        if ep_id in taken_ids or cand.order.source_ref in taken_refs:\n",
    ],
)
def test_each_single_dedupe_defence_removed_is_still_caught(
    monkeypatch, tmp_path, only
):
    """Documents the layered defence: removing any one of the three still
    counts B_i once."""

    mods = mutant(monkeypatch, "engine", only, "        if False:\n")
    check_budget_counted_once(tmp_path, mods["engine"])


# ------------------------------------------------------------- static guard mutants


def _pkg_copy(tmp_path) -> Path:
    dst = tmp_path / "pkg"
    shutil.copytree(PKG, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


@pytest.mark.parametrize(
    "line",
    [
        "from app.services.brokers.toss.client import TossReadClient\n",
        "import app.services.order_proposals.service\n",
        "from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService\n",
        "import alpaca.trading.client\n",
        "from app.mcp_server.tooling import orders_toss_variants\n",
    ],
)
def test_forbidden_import_mutant_is_red(tmp_path, line):
    pkg = _pkg_copy(tmp_path)
    target = pkg / "fills.py"
    target.write_text(target.read_text() + "\n" + line)
    with pytest.raises(AssertionError):
        assert iso.import_violations(pkg) == []


def test_write_sql_mutant_is_red(tmp_path):
    pkg = _pkg_copy(tmp_path)
    target = pkg / "db_source.py"
    target.write_text(
        target.read_text()
        + "\nSQL_FIX = \"UPDATE review.toss_live_order_ledger SET status = 'expired'\"\n"
    )
    with pytest.raises(AssertionError):
        assert iso.sql_violations(pkg) == []


def test_foreign_host_mutant_is_red(tmp_path):
    pkg = _pkg_copy(tmp_path)
    target = pkg / "market_data.py"
    target.write_text(
        target.read_text()
        + '\nORDERS_URL = "https://paper-api.alpaca.markets/v2/orders"\n'
    )
    with pytest.raises(AssertionError):
        assert iso.host_violations(pkg) == []


# r1 S2: evasions the first checkers missed -- each must now be RED.
EVASIONS = [
    (
        "fills.py",
        "import builtins\nf = getattr(builtins, '__im' + 'port__')\n",
        "import",
    ),
    ("fills.py", "f = __builtins__['__import__']\n", "import"),
    ("fills.py", "import importlib\n", "import"),
    ("fills.py", "import socket\n", "import"),
    ("fills.py", "import requests\n", "import"),
    (
        "db_source.py",
        "Q = 'UP' + 'DATE review.toss_live_order_ledger SET status = 1'\n",
        "sql",
    ),
    ("db_source.py", "Q = 'SELECT 1 INTO TEMP rob1391_probe'\n", "sql"),
    ("engine.py", "def _x(conn):\n    conn.execute('SELECT 1')\n", "sql"),
    ("db_source.py", "def _x(conn):\n    conn.exec_driver_sql('SELECT 1')\n", "sql"),
    (
        "market_data.py",
        "def _x(host):\n    return 'https://' + host + '/probe'\n",
        "host",
    ),
    (
        "market_data.py",
        "def _x(requests):\n    requests.request('POST', BARS_URL)\n",
        "host",
    ),
    ("market_data.py", "def _x(self):\n    self._session.post(BARS_URL)\n", "host"),
]


@pytest.mark.parametrize(
    ("target", "code", "kind"),
    EVASIONS,
    ids=[f"{k}-{i}" for i, (_, _, k) in enumerate(EVASIONS)],
)
def test_r1_evasion_probes_are_red(tmp_path, target, code, kind):
    pkg = _pkg_copy(tmp_path)
    path = pkg / target
    path.write_text(path.read_text() + "\n\n" + code)
    checker = {
        "import": iso.import_violations,
        "sql": iso.sql_violations,
        "host": iso.host_violations,
    }[kind]
    with pytest.raises(AssertionError):
        assert checker(pkg) == []
