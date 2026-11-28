"""hk 7118 — table-driven tests for the pure autonomous decision module."""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.execution_intent_gateway import autonomous_decisions as ad
from app.services.execution_intent_gateway.autonomous_decisions import (
    Arming,
    Book,
    ChildOrder,
    DecisionInputs,
    SpreadLimits,
)
from app.services.execution_intent_gateway.autonomous_decisions import (
    AutonomousRefusal as R,
)
from app.services.execution_intent_gateway.autonomous_decisions import (
    SessionClass as S,
)

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
SPREADS = SpreadLimits(Decimal("30"), Decimal("15"), Decimal("20"))
MODULE = Path(ad.__file__)


def _book(bid: str = "9990", ask: str = "10000", age: float = 1.0) -> Book:
    at = NOW - timedelta(seconds=age)
    return Book(Decimal(bid), Decimal(ask), at, at)


def _child(**kw) -> ChildOrder:
    base = {
        "kind": "submit",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("10"),
        "limit_price": Decimal("10000"),
    }
    base.update(kw)
    return ChildOrder(**base)


def _inputs(**kw) -> DecisionInputs:
    base = {
        "account": "kis_live",
        "session": S.KRX_REGULAR,
        "product": "cash",
        "child": _child(),
        "arming": Arming.LIVE_FULL,
        "fx_krw_per_unit": None,
        "working_orders": 0,
        "plan_version": 3,
        "latest_plan_change_seq": 3,
        "confirmed_net_quantity": Decimal("100"),
        "working_sell_quantity": Decimal("0"),
        "budget_remaining_quantity": Decimal("100"),
        "budget_remaining_notional_krw": Decimal("100000000"),
        "amend_target_quantity": None,
        "amend_target_notional_krw": None,
        "halt_suspect": False,
        "trading_flag_blocked": False,
        "price_kind": "last_trade",
        "book": _book(),
        "in_fill_window": True,
        "spread_limits": SPREADS,
        "toss_us_sessions": frozenset({"regular"}),
        "now": NOW,
    }
    base.update(kw)
    return DecisionInputs(**base)


def _code(inputs: DecisionInputs):
    return ad.decide(inputs).refusal


# --- locked constants (independent literals) --------------------------------


def test_locked_constants_match_independent_literals():
    assert ad.PER_ORDER_CAP_KRW == Decimal(2_000_000)
    assert ad.HALF_CAP_KRW == Decimal(1_000_000)
    assert ad.ONE_SHARE_CEILING_KRW == Decimal(10_000_000)
    assert ad.MAX_WORKING_ORDERS_PER_ACCOUNT == 4
    assert ad.BOOK_MAX_AGE == timedelta(seconds=5)
    ad.assert_limits_locked(ad.LOCKED_LIMITS)
    with pytest.raises(ad.LimitsNotLocked):
        ad.assert_limits_locked(
            dataclasses.replace(ad.LOCKED_LIMITS, per_order_cap_krw=Decimal(3_000_000))
        )
    with pytest.raises(ad.LimitsNotLocked):
        ad.assert_limits_locked(object())


def test_book_age_matches_proposal_gate():
    from app.services.mandate.proposal_gate import ASK_MAX_AGE

    assert ad.BOOK_MAX_AGE == ASK_MAX_AGE


def test_existing_5m_bounds_unchanged():
    from app.schemas.execution_intent import MAX_INTENT_NOTIONAL_KRW
    from app.services.kis_mock_runner.envelope import LOCKED_ENVELOPE

    assert LOCKED_ENVELOPE.max_single_notional_krw == 5_000_000
    assert MAX_INTENT_NOTIONAL_KRW == 5_000_000


# --- static boundary ---------------------------------------------------------


def test_module_imports_only_stdlib():
    tree = ast.parse(MODULE.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "typing",
    }
    source = MODULE.read_text()
    for forbidden in ("os.environ", "getenv", "open(", "settings"):
        assert forbidden not in source


def test_import_loads_no_db_or_broker_modules():
    code = (
        "import sys; import app.services.execution_intent_gateway.autonomous_decisions;"
        "bad=[m for m in sys.modules if m.startswith(('sqlalchemy','httpx','app.core.config',"
        "'app.services.brokers','app.models','redis'))]; print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- baseline / cancel ------------------------------------------------------


def test_baseline_allows():
    d = ad.decide(_inputs())
    assert d.allowed
    assert d.notional_krw == Decimal(100_000)
    assert d.cap_in_force_krw == ad.PER_ORDER_CAP_KRW


def test_cancel_always_allowed_even_with_unknowns():
    inputs = _inputs(
        child=_child(kind="cancel"),
        arming=None,
        working_orders=None,
        plan_version=None,
        book=None,
        halt_suspect=None,
        session=None,
    )
    assert ad.decide(inputs).allowed


def test_unknown_child_kind_refused():
    assert _code(_inputs(child=_child(kind="replace"))) is R.CHILD_INVALID


# --- I1 / I1a / I1b caps ------------------------------------------------------

CAP_CASES = [
    # id, arming, qty, price, expected
    ("full_at_cap", Arming.LIVE_FULL, "200", "10000", None),
    ("full_over_cap", Arming.LIVE_FULL, "201", "10000", R.ORDER_CAP_EXCEEDED),
    ("half_at_cap", Arming.LIVE_HALF, "100", "10000", None),
    ("half_over_half", Arming.LIVE_HALF, "101", "10000", R.ARMING_HALF_CAP),
    ("half_over_full", Arming.LIVE_HALF, "201", "10000", R.ORDER_CAP_EXCEEDED),
    ("one_share_exception", Arming.LIVE_FULL, "1", "6000000", None),
    ("one_share_at_ceiling", Arming.LIVE_FULL, "1", "10000000", None),
    (
        "one_share_over_ceiling",
        Arming.LIVE_FULL,
        "1",
        "10000001",
        R.ONE_SHARE_EXCEPTION_INVALID,
    ),
    ("one_share_under_half", Arming.LIVE_HALF, "1", "1500000", None),
    ("price_eq_cap_qty1", Arming.LIVE_FULL, "1", "2000000", None),
    ("price_eq_cap_qty2", Arming.LIVE_FULL, "2", "2000000", R.ORDER_CAP_EXCEEDED),
    ("qty2_exception_never", Arming.LIVE_FULL, "2", "1500000", R.ORDER_CAP_EXCEEDED),
    ("qty2_half", Arming.LIVE_HALF, "2", "600000", R.ARMING_HALF_CAP),
    ("frozen", Arming.FROZEN, "1", "10000", R.ARMING_NOT_LIVE),
    ("mock_proving", Arming.MOCK_PROVING, "1", "10000", R.ARMING_NOT_LIVE),
    ("missing", None, "1", "10000", R.ARMING_NOT_LIVE),
    ("unknown_value", "armed", "1", "10000", R.ARMING_NOT_LIVE),
]


@pytest.mark.parametrize(("case", "arming", "qty", "price", "expected"), CAP_CASES)
def test_caps(case, arming, qty, price, expected):
    inputs = _inputs(
        arming=arming,
        child=_child(quantity=Decimal(qty), limit_price=Decimal(price)),
        book=_book(str(Decimal(price) - 1), price),
        budget_remaining_quantity=Decimal("1000"),
    )
    assert _code(inputs) is expected, case


US_CASES = [
    ("fx_missing", None, "10", "100", R.FX_SNAPSHOT_MISSING),
    ("fx_zero", Decimal("0"), "10", "100", R.FX_SNAPSHOT_MISSING),
    ("fx_within", Decimal("1400"), "14", "100", None),  # 1,960,000 KRW
    ("fx_over", Decimal("1400"), "15", "100", R.ORDER_CAP_EXCEEDED),  # 2,100,000
    ("fx_one_share", Decimal("1400"), "1", "5000", None),  # 7,000,000
    ("fx_one_share_over", Decimal("1400"), "1", "8000", R.ONE_SHARE_EXCEPTION_INVALID),
]


@pytest.mark.parametrize(("case", "fx", "qty", "price", "expected"), US_CASES)
def test_us_caps_use_publish_fx(case, fx, qty, price, expected):
    inputs = _inputs(
        account="kis_live",
        session=S.US_REGULAR,
        fx_krw_per_unit=fx,
        child=_child(quantity=Decimal(qty), limit_price=Decimal(price)),
        book=_book(str(Decimal(price) - Decimal("0.01")), price),
    )
    assert _code(inputs) is expected, case


@pytest.mark.parametrize(
    ("account", "sess", "fx"),
    [
        ("kis_live", S.KRX_REGULAR, None),
        ("toss_live", S.US_REGULAR, Decimal("1400")),
    ],
)
def test_market_orders_are_never_accepted(account, sess, fx):
    # No broker request enforces a price bound, so the cap cannot hold.
    inputs = _inputs(
        account=account,
        session=sess,
        fx_krw_per_unit=fx,
        child=_child(order_type="market", limit_price=None, quantity=Decimal("3")),
    )
    assert _code(inputs) is R.ORDER_FORM_NOT_ACCEPTED


@pytest.mark.parametrize(
    ("qty", "price", "arming", "expected"),
    [
        ("1", "6000000", Arming.LIVE_FULL, R.ORDER_CAP_EXCEEDED),
        ("1", "1500000", Arming.LIVE_HALF, R.ARMING_HALF_CAP),
        ("0.3", "6000000", Arming.LIVE_FULL, None),  # 1,800,000 KRW
    ],
)
def test_one_share_exception_is_shares_only(qty, price, arming, expected):
    inputs = _inputs(
        account="upbit_live",
        session=S.CRYPTO_24H,
        arming=arming,
        child=_child(quantity=Decimal(qty), limit_price=Decimal(price)),
        book=_book(str(Decimal(price) - 1), price),
    )
    assert _code(inputs) is expected


@pytest.mark.parametrize(
    ("account", "sess", "fx"),
    [
        ("toss_live", S.KRX_REGULAR, None),
        ("toss_live", S.US_REGULAR, Decimal("1400")),
    ],
)
def test_toss_amount_buy_not_accepted(account, sess, fx):
    # The broker picks the quantity, so the intent quantity cap is unprovable.
    inputs = _inputs(
        account=account,
        session=sess,
        fx_krw_per_unit=fx,
        child=_child(quantity=None, amount=Decimal("50000")),
        budget_remaining_quantity=Decimal("0"),
    )
    assert _code(inputs) is R.ORDER_FORM_NOT_ACCEPTED


@pytest.mark.parametrize(
    "child_kw",
    [{"side": "sell"}, {"kind": "amend"}],
)
def test_toss_amount_sell_or_amend_not_accepted(child_kw):
    # A sell by amount cannot bound the share quantity it sells (I8).
    inputs = _inputs(
        account="toss_live",
        session=S.KRX_REGULAR,
        child=_child(quantity=None, amount=Decimal("100000"), **child_kw),
        amend_target_quantity=Decimal("0"),
    )
    assert _code(inputs) is R.ORDER_FORM_NOT_ACCEPTED


# --- I2 working orders ------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, None),
        (3, None),
        (4, R.WORKING_ORDERS_LIMIT),
        (9, R.WORKING_ORDERS_LIMIT),
        (None, R.WORKING_ORDERS_UNKNOWN),
        (-1, R.WORKING_ORDERS_UNKNOWN),
        (True, R.WORKING_ORDERS_UNKNOWN),
    ],
)
def test_working_orders(count, expected):
    assert _code(_inputs(working_orders=count)) is expected


def test_amend_does_not_count_as_new_working_order():
    assert ad.decide(
        _inputs(
            child=_child(kind="amend"),
            working_orders=4,
            amend_target_quantity=Decimal("10"),
            amend_target_notional_krw=Decimal("100000"),
        )
    ).allowed


# --- I7 plan supersession ---------------------------------------------------


@pytest.mark.parametrize(
    ("version", "latest", "kind", "expected"),
    [
        (3, 3, "submit", None),
        (3, 4, "submit", R.PLAN_VERSION_STALE),
        (3, 4, "amend", R.PLAN_VERSION_STALE),
        (3, 4, "cancel", None),
        (None, 3, "submit", R.PLAN_GUARD_UNAVAILABLE),
        (3, None, "submit", R.PLAN_GUARD_UNAVAILABLE),
    ],
)
def test_plan_version(version, latest, kind, expected):
    inputs = _inputs(
        plan_version=version, latest_plan_change_seq=latest, child=_child(kind=kind)
    )
    assert _code(inputs) is expected


# --- I8 exits / I9 budget ---------------------------------------------------


@pytest.mark.parametrize(
    ("confirmed", "expected"),
    [
        (Decimal("10"), None),
        (Decimal("9"), R.EXIT_EXCEEDS_CONFIRMED_QTY),
        (Decimal("0"), R.EXIT_EXCEEDS_CONFIRMED_QTY),
        (None, R.EXIT_QUANTITY_UNVERIFIED),
        (Decimal("NaN"), R.EXIT_QUANTITY_UNVERIFIED),
    ],
)
def test_exit_quantity(confirmed, expected):
    inputs = _inputs(child=_child(side="sell"), confirmed_net_quantity=confirmed)
    assert _code(inputs) is expected


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (Decimal("10"), None),
        (Decimal("9"), R.BUDGET_EXCEEDED),
        (None, R.BUDGET_UNVERIFIED),
        (Decimal("Infinity"), R.BUDGET_UNVERIFIED),
    ],
)
def test_budget(remaining, expected):
    assert _code(_inputs(budget_remaining_quantity=remaining)) is expected


# --- I10 halt / stale / expected --------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"halt_suspect": True}, R.HALT_SUSPECT),
        ({"halt_suspect": None}, R.HALT_STATE_UNKNOWN),
        ({"trading_flag_blocked": True}, R.TRADING_FLAG_BLOCKED),
        ({"trading_flag_blocked": None}, R.HALT_STATE_UNKNOWN),
        ({"price_kind": "expected"}, R.EXPECTED_PRICE),
        ({"price_kind": None}, R.QUOTE_STALE),
        ({"price_kind": "unavailable"}, R.QUOTE_STALE),
        ({"book": _book(age=5.0)}, None),
        ({"book": _book(age=5.1)}, R.QUOTE_STALE),
        ({"book": _book(age=-1)}, R.QUOTE_STALE),
    ],
)
def test_halt_and_quote(overrides, expected):
    assert _code(_inputs(**overrides)) is expected


# --- I11 cells, shapes, book, spread ----------------------------------------


@pytest.mark.parametrize(
    ("account", "sess"),
    [
        ("kis_live", S.US_PRE),
        ("kis_live", S.US_POST),
        ("toss_live", S.US_DAY),
        ("upbit_live", S.KRX_REGULAR),
        ("kis_mock", S.KRX_REGULAR),
        ("kis_live", None),
        ("kis_live", "bogus"),
    ],
)
def test_unsupported_cells(account, sess):
    assert _code(_inputs(account=account, session=sess)) is R.CELL_UNSUPPORTED


# spread = (ask-bid)/mid*1e4; bid 9970 ask 10000 -> ~30.05bp; 9971 -> ~29.04bp
SPREAD_CASES = [
    ("regular_ok", "kis_live", S.KRX_REGULAR, "9971", None),
    ("regular_wide", "kis_live", S.KRX_REGULAR, "9970", R.SPREAD_TOO_WIDE),
    ("extended_ok", "kis_live", S.NXT_AFTER, "9986", None),  # ~14.01bp
    ("extended_wide", "kis_live", S.NXT_AFTER, "9984", R.SPREAD_TOO_WIDE),  # ~16.01bp
    ("krx_after_wide", "toss_live", S.KRX_AFTER, "9980", R.SPREAD_TOO_WIDE),
    ("upbit_ok", "upbit_live", S.CRYPTO_24H, "9981", None),  # ~19.02bp
    ("upbit_wide", "upbit_live", S.CRYPTO_24H, "9979", R.SPREAD_TOO_WIDE),
]


@pytest.mark.parametrize(("case", "account", "sess", "bid", "expected"), SPREAD_CASES)
def test_spread_per_session_class(case, account, sess, bid, expected):
    inputs = _inputs(account=account, session=sess, book=_book(bid, "10000"))
    assert _code(inputs) is expected, case


def test_spread_limits_absent_fail_closed():
    assert _code(_inputs(spread_limits=None)) is R.SPREAD_LIMIT_UNAVAILABLE


@pytest.mark.parametrize(
    "book",
    [
        None,
        Book(None, Decimal("1"), NOW, NOW),
        Book(Decimal("10"), Decimal("10"), NOW, NOW),
        Book(Decimal("11"), Decimal("10"), NOW, NOW),
    ],
)
def test_book_incomplete(book):
    assert _code(_inputs(book=book)) is R.BOOK_INCOMPLETE


def test_one_side_stale_refused():
    book = Book(Decimal("9990"), Decimal("10000"), NOW, NOW - timedelta(seconds=6))
    assert _code(_inputs(book=book)) is R.QUOTE_STALE


TOSS_US_EXT = [
    ("limit_int", {"quantity": Decimal("3"), "limit_price": Decimal("100")}, None),
    (
        "fractional",
        {"quantity": Decimal("1.5"), "limit_price": Decimal("100")},
        R.ORDER_FORM_NOT_ACCEPTED,
    ),
    (
        "amount",
        {"quantity": None, "amount": Decimal("100"), "limit_price": Decimal("100")},
        R.ORDER_FORM_NOT_ACCEPTED,
    ),
    (
        "market",
        {"order_type": "market", "limit_price": None},
        R.ORDER_FORM_NOT_ACCEPTED,
    ),
]


@pytest.mark.parametrize("sess", [S.US_PRE, S.US_POST])
@pytest.mark.parametrize(("case", "child_kw", "expected"), TOSS_US_EXT)
def test_toss_us_pre_post_limit_integer_no_amount(sess, case, child_kw, expected):
    inputs = _inputs(
        account="toss_live",
        session=sess,
        fx_krw_per_unit=Decimal("1400"),
        child=_child(**child_kw),
        book=_book("99.99", "100"),
        toss_us_sessions=frozenset({"regular", "pre", "post"}),
    )
    assert _code(inputs) is expected, case


@pytest.mark.parametrize(
    ("sess", "allowed"),
    [
        (S.US_PRE, frozenset({"regular"})),
        (S.US_POST, frozenset({"regular", "pre"})),
        (S.US_PRE, None),
        (S.US_POST, frozenset()),
    ],
)
def test_toss_us_pre_post_require_session_allowlist(sess, allowed):
    inputs = _inputs(
        account="toss_live",
        session=sess,
        fx_krw_per_unit=Decimal("1400"),
        child=_child(quantity=Decimal("3"), limit_price=Decimal("100")),
        book=_book("99.99", "100"),
        toss_us_sessions=allowed,
    )
    assert _code(inputs) is R.CELL_UNSUPPORTED


def test_toss_us_regular_allows_fraction_not_amount():
    base = {
        "account": "toss_live",
        "session": S.US_REGULAR,
        "fx_krw_per_unit": Decimal("1400"),
        "book": _book("99.99", "100"),
    }
    assert ad.decide(
        _inputs(
            child=_child(quantity=Decimal("1.5"), limit_price=Decimal("100")), **base
        )
    ).allowed
    sell_amount = _child(side="sell", quantity=None, amount=Decimal("100"))
    assert _code(_inputs(child=sell_amount, **base)) is R.ORDER_FORM_NOT_ACCEPTED
    buy_amount = _child(quantity=None, amount=Decimal("100"))
    assert _code(_inputs(child=buy_amount, **base)) is R.ORDER_FORM_NOT_ACCEPTED


def test_kr_nxt_qty_limit_only():
    nxt = {"account": "toss_live", "session": S.NXT_PRE, "book": _book("9990", "10000")}
    assert ad.decide(_inputs(**nxt)).allowed
    assert (
        _code(_inputs(child=_child(quantity=None, amount=Decimal("1000")), **nxt))
        is R.ORDER_FORM_NOT_ACCEPTED
    )
    assert (
        _code(
            _inputs(
                child=_child(order_type="market", limit_price=None),
                **dict(nxt, account="kis_live"),
            )
        )
        is R.ORDER_FORM_NOT_ACCEPTED
    )
    assert (
        _code(_inputs(child=_child(quantity=Decimal("1.5"))))
        is R.ORDER_FORM_NOT_ACCEPTED
    )


@pytest.mark.parametrize(
    ("side", "window", "expected"),
    [
        ("buy", False, R.ENTRY_NO_FILL_WINDOW),
        ("sell", False, None),
        ("buy", None, R.ENTRY_NO_FILL_WINDOW),
        ("buy", True, None),
    ],
)
def test_entry_no_fill_window(side, window, expected):
    assert _code(_inputs(child=_child(side=side), in_fill_window=window)) is expected


# --- I14 product -------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "side", "expected"),
    [
        ("cash", "buy", None),
        ("margin", "buy", R.PRODUCT_NOT_ALLOWED),
        ("futures", "sell", R.PRODUCT_NOT_ALLOWED),
        (None, "buy", R.PRODUCT_NOT_ALLOWED),
        ("cash", "short", R.PRODUCT_NOT_ALLOWED),
    ],
)
def test_product(product, side, expected):
    assert _code(_inputs(product=product, child=_child(side=side))) is expected


# --- spread policy keys -------------------------------------------------------


def _policy(regular=30, extended=15, upbit=20):
    return {
        "autonomous_orders": {
            "spread_gate_bp": {"regular": regular, "extended": extended, "upbit": upbit}
        }
    }


def test_spread_limits_from_policy():
    assert ad.spread_limits_from_policy(_policy()) == SPREADS
    assert ad.spread_limits_from_policy(None) is None
    assert ad.spread_limits_from_policy({}) is None
    assert ad.spread_limits_from_policy(_policy(upbit=None)) is None
    assert ad.spread_limits_from_policy(_policy(regular=0)) is None
    assert ad.spread_limits_from_policy(_policy(extended=True)) is None
    assert ad.spread_limits_from_policy(_policy(regular="x")) is None


def test_policy_file_has_no_spread_keys_yet_so_decision_fails_closed():
    import yaml

    raw = yaml.safe_load(Path("config/trading_policy.yaml").read_text())
    limits = ad.spread_limits_from_policy(raw)
    if limits is None:
        assert _code(_inputs(spread_limits=limits)) is R.SPREAD_LIMIT_UNAVAILABLE


# --- review round 1 (hk 7118) -------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "qty", "working", "target", "expected"),
    [
        ("submit", "10", "0", None, None),
        ("submit", "10", "10", None, R.EXIT_EXCEEDS_CONFIRMED_QTY),  # second sell
        ("submit", "5", "5", None, None),
        ("submit", "10", None, None, R.EXIT_QUANTITY_UNVERIFIED),
        ("submit", "10", "-1", None, R.EXIT_QUANTITY_UNVERIFIED),
        ("amend", "10", "10", "10", None),  # reprice the only working sell
        ("amend", "11", "10", "10", R.EXIT_EXCEEDS_CONFIRMED_QTY),
        ("amend", "10", "10", None, R.EXIT_QUANTITY_UNVERIFIED),
        ("amend", "10", "5", "6", R.EXIT_QUANTITY_UNVERIFIED),  # target > working
    ],
)
def test_exit_counts_working_sells(kind, qty, working, target, expected):
    inputs = _inputs(
        child=_child(kind=kind, side="sell", quantity=Decimal(qty)),
        confirmed_net_quantity=Decimal("10"),
        working_sell_quantity=None if working is None else Decimal(working),
        amend_target_quantity=None if target is None else Decimal(target),
    )
    assert _code(inputs) is expected


@pytest.mark.parametrize(
    ("qty", "target", "expected"),
    [
        ("10", "10", None),  # price-only amend of a fully reserved order
        ("5", "10", None),  # reduce
        ("11", "10", R.BUDGET_EXCEEDED),
        ("10", None, R.BUDGET_UNVERIFIED),
    ],
)
def test_amend_budget_reuses_target_reservation(qty, target, expected):
    inputs = _inputs(
        child=_child(kind="amend", quantity=Decimal(qty)),
        budget_remaining_quantity=Decimal("0"),
        budget_remaining_notional_krw=Decimal("0"),
        amend_target_quantity=None if target is None else Decimal(target),
        amend_target_notional_krw=Decimal("100000"),
    )
    assert _code(inputs) is expected


@pytest.mark.parametrize("kind", ["", "stale", "LAST_TRADE", "closing"])
def test_unknown_price_kinds_refused(kind):
    assert _code(_inputs(price_kind=kind)) is R.QUOTE_STALE


def test_mid_price_kind_allowed():
    assert ad.decide(_inputs(price_kind="mid")).allowed


def test_naive_timestamps_refused():
    naive = NOW.replace(tzinfo=None)
    both_naive = Book(Decimal("9990"), Decimal("10000"), naive, naive)
    assert _code(_inputs(book=both_naive, now=naive)) is R.QUOTE_STALE
    mixed = Book(Decimal("9990"), Decimal("10000"), naive, NOW)
    assert _code(_inputs(book=mixed)) is R.QUOTE_STALE


@pytest.mark.parametrize(
    "bad", [Decimal("Infinity"), Decimal("NaN"), Decimal("0"), Decimal("-5"), 30]
)
def test_spread_limit_validated_at_decision(bad):
    limits = SpreadLimits(bad, Decimal("15"), Decimal("20"))
    assert _code(_inputs(spread_limits=limits)) is R.SPREAD_LIMIT_UNAVAILABLE


def test_policy_schema_accepts_spread_section_and_optional_absence():
    import yaml

    from app.schemas.trading_policy import TradingPolicyDocument

    raw = yaml.safe_load(Path("config/trading_policy.yaml").read_text())
    assert TradingPolicyDocument.model_validate(raw).autonomous_orders is None
    raw["autonomous_orders"] = {
        "spread_gate_bp": {"regular": 30, "extended": 15, "upbit": 20}
    }
    doc = TradingPolicyDocument.model_validate(raw)
    assert doc.autonomous_orders is not None
    assert ad.spread_limits_from_policy(raw) == SPREADS
    raw["autonomous_orders"]["spread_gate_bp"]["regular"] = 0
    with pytest.raises(ValueError):
        TradingPolicyDocument.model_validate(raw)


# --- review round 2 (hk 7118) -------------------------------------------------


@pytest.mark.parametrize(
    ("price", "target_krw", "remaining_krw", "expected"),
    [
        ("10000", "100000", "0", None),  # same notional
        ("10400", "100000", "0", R.BUDGET_EXCEEDED),  # notional rises 4,000
        ("10400", "100000", "4000", None),
        ("10000", None, "0", R.BUDGET_UNVERIFIED),
    ],
)
def test_amend_budget_checks_notional_increase(
    price, target_krw, remaining_krw, expected
):
    inputs = _inputs(
        child=_child(kind="amend", quantity=Decimal("10"), limit_price=Decimal(price)),
        book=_book("10390", "10400"),
        budget_remaining_quantity=Decimal("0"),
        budget_remaining_notional_krw=Decimal(remaining_krw),
        amend_target_quantity=Decimal("10"),
        amend_target_notional_krw=None if target_krw is None else Decimal(target_krw),
    )
    assert _code(inputs) is expected


@pytest.mark.parametrize(
    ("remaining_krw", "expected"),
    [("100000", None), ("99999", R.BUDGET_EXCEEDED), (None, R.BUDGET_UNVERIFIED)],
)
def test_submit_budget_checks_notional(remaining_krw, expected):
    inputs = _inputs(
        budget_remaining_notional_krw=None
        if remaining_krw is None
        else Decimal(remaining_krw)
    )
    assert _code(inputs) is expected


def test_spread_extractor_reads_validated_policy_floats():
    import yaml

    from app.schemas.trading_policy import TradingPolicyDocument

    raw = yaml.safe_load(Path("config/trading_policy.yaml").read_text())
    raw["autonomous_orders"] = {
        "spread_gate_bp": {"regular": 30, "extended": 15.0, "upbit": 20}
    }
    dumped = TradingPolicyDocument.model_validate(raw).model_dump()
    assert ad.spread_limits_from_policy(dumped) == SPREADS
    assert ad.spread_limits_from_policy(raw) == SPREADS
    raw["autonomous_orders"]["spread_gate_bp"]["upbit"] = float("inf")
    assert ad.spread_limits_from_policy(raw) is None
