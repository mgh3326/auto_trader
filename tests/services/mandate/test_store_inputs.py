"""Store input validation that needs no database (the DB tests cover the writes)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.services.mandate import hashing
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.strategy_sleeves.store import PreRegistration

pytestmark = pytest.mark.unit
D = Decimal


def raises(code: v.MandateRefusal):
    return pytest.raises(ms.MandateError, match=code.value)


# ----------------------------------------------------------- per-event field matrix
def test_event_field_matrix_requires_and_forbids_columns() -> None:
    assert ms._event_field_problem("proposed", {}) is None
    assert ms._event_field_problem("approved", {}) is not None  # terms + session needed
    ok = {"terms_id": 1, "terms_sha256": "a" * 64, "effective_session": "2026-11-23"}
    assert ms._event_field_problem("approved", ok) is None
    assert ms._event_field_problem("suspended", ok) is not None  # takes no terms
    assert (
        ms._event_field_problem("tranche_armed", {}) is not None
    )  # needs tranche_index
    assert ms._event_field_problem("tranche_armed", {"tranche_index": 1}) is None
    assert (
        ms._event_field_problem("retired", {"terms_id": 1, "terms_sha256": "a" * 64})
        is None
    )


def test_fence_and_profile_columns_travel_together() -> None:
    fence = {
        "tranche_index": 1,
        "proposal_id": uuid4(),
        "notional": D("1"),
        "kst_day": "2026-11-23",
        "approval_mode": "auto",
        "account_ref": "a",
        "attempt_id": uuid4(),
    }
    assert "activation_profile_id" in ms._event_field_problem(
        "tranche_send_fenced", fence
    )
    assert (
        ms._event_field_problem(
            "tranche_send_fenced", {**fence, "activation_profile_id": 3}
        )
        is None
    )
    human = {**fence, "approval_mode": "human"}
    assert ms._event_field_problem("tranche_send_fenced", human) is None
    assert ms._event_field_problem(
        "tranche_send_fenced", {**human, "activation_profile_id": 3}
    )


def test_first_fill_belongs_to_the_first_tranche_completion_only() -> None:
    base = {
        "tranche_index": 2,
        "proposal_id": uuid4(),
        "fill_amount": D("1"),
        "fill_quantity": D("1"),
    }
    assert ms._event_field_problem("tranche_filled", base) is None
    assert ms._event_field_problem(
        "tranche_filled",
        {**base, "first_fill_amount": D("1"), "first_fill_quantity": D("1")},
    )
    first = {**base, "tranche_index": 1}
    assert ms._event_field_problem("tranche_filled", first)
    assert (
        ms._event_field_problem(
            "tranche_filled",
            {**first, "first_fill_amount": D("1"), "first_fill_quantity": D("1")},
        )
        is None
    )


# ------------------------------------------------------------------ tranche rule
GOOD_RULE = {
    "weights": ["0.4", "0.3", "0.3"],
    "triggers_pct_from_first": ["0", "-15", "-30"],
    "min_gap_sessions": 20,
}


def test_tranche_rule_is_closed_and_canonical() -> None:
    assert ms._tranche_rule(GOOD_RULE) == {
        "weights": ["0.4", "0.3", "0.3"],
        "triggers_pct_from_first": ["0", "-15", "-30"],
        "min_gap_sessions": 20,
    }
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "extra": 1})
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "weights": ["0.5", "0.3", "0.3"]})
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "triggers_pct_from_first": ["0", "-30", "-15"]})
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "triggers_pct_from_first": ["-5", "-15", "-30"]})
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "min_gap_sessions": -1})


# ---------------------------------------------------------------------- principal
@pytest.mark.parametrize("bad", ["0", "01", "-1", "1.0", " 1", "", 5, None, str(2**52)])
def test_principal_is_a_canonical_decimal_user_id(bad) -> None:
    with raises(v.MandateRefusal.APPROVER_NOT_ALLOWED):
        ms._check_principal(bad)


def test_principal_accepts_the_largest_exact_id() -> None:
    assert ms._check_principal(str(2**52 - 1)) == str(2**52 - 1)
    assert ms._check_principal("123456789") == "123456789"


# ------------------------------------------------------------- adopted evidence
SCOPE = ["acct-kis", "acct-toss"]


def _evidence(**over):
    base = {
        "checked_at": "2026-11-20T00:00:00Z",
        "holdings": [
            {"account_ref": "acct-kis", "quantity": "10", "amount": "150000"},
            {"account_ref": "acct-toss", "quantity": "0", "amount": "0"},
        ],
        "price": "15000",
        "price_source": "daily_candles",
    }
    base.update(over)
    return base


def test_adopted_evidence_lists_every_scope_account_even_at_zero() -> None:
    assert (
        ms._check_adopted_evidence(_evidence(), SCOPE, D("150000"))["price"] == "15000"
    )
    with raises(v.MandateRefusal.SCOPE_UNKNOWN):
        ms._check_adopted_evidence(
            _evidence(holdings=_evidence()["holdings"][:1]), SCOPE, D("150000")
        )


def test_adopted_evidence_must_sum_to_the_adopted_amount_with_closed_keys() -> None:
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._check_adopted_evidence(_evidence(), SCOPE, D("149999"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._check_adopted_evidence(_evidence(note="x"), SCOPE, D("150000"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._check_adopted_evidence({"price": "1"}, SCOPE, D("0"))


# ------------------------------------------------------------- holdings evidence
def test_i19_zero_holdings_must_read_the_registered_scope_accounts() -> None:
    ok = {
        "holdings_disposition": "zero_holdings",
        "holdings_account_refs": ["acct-toss", "acct-kis"],
        "holdings_quantity": "0",
    }
    ms.validate_holdings_evidence(
        ok, scope_account_refs=SCOPE, actor="system:mandate_projection"
    )
    # the configured KIS account changed after registration: refs differ
    with raises(v.MandateRefusal.SCOPE_UNKNOWN):
        ms.validate_holdings_evidence(
            {**ok, "holdings_account_refs": ["acct-toss", "acct-kis-NEW"]},
            scope_account_refs=SCOPE,
            actor="system:mandate_projection",
        )
    with raises(v.MandateRefusal.SCOPE_UNKNOWN):
        ms.validate_holdings_evidence(
            {**ok, "holdings_account_refs": ["acct-toss"]},
            scope_account_refs=SCOPE,
            actor="system:mandate_projection",
        )
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms.validate_holdings_evidence(
            {**ok, "holdings_quantity": "1"}, scope_account_refs=SCOPE, actor="op"
        )


def test_i19_kept_outside_basket_is_an_operator_record() -> None:
    kept = {"holdings_disposition": "kept_outside_basket"}
    ms.validate_holdings_evidence(kept, scope_account_refs=SCOPE, actor="operator:cli")
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms.validate_holdings_evidence(
            kept, scope_account_refs=SCOPE, actor="system:mandate_projection"
        )


# --------------------------------------------------------------------- terms input
def _sleeve(account_mode="kis_live", market="equity_kr"):
    return SimpleNamespace(
        id=1, account_mode=account_mode, market=market, currency="KRW"
    )


def _data(**over):
    base = {
        "basket_key": "basket-1",
        "account_ref": "acct-kis",
        "universe_rule": "rule",
        "allowed_symbols": ["005930", "000270"],
        "entry_max_premium_bps": "30",
        "per_order_cap": "2000000",
        "daily_cap": "5000000",
        "sector_cap_pct": "35",
        "basket_drawdown_freeze_pct": "25",
        "account_drawdown_freeze_pct": "25",
        "tranche_rule": GOOD_RULE,
        "expires_at": datetime(2026, 12, 1, tzinfo=UTC),
        "budget_cap": "9750000",
        "loss_limit": "500000",
        "prereg": PreRegistration("m", D("1"), 20, 90),
    }
    base.update(over)
    return ms.TermsInput(**base)


def test_i17_parking_symbols_cannot_be_in_a_clause() -> None:
    with raises(v.MandateRefusal.PARKING_SYMBOL):
        ms._terms_columns(_sleeve(), _data(allowed_symbols=["005930", "459580"]))
    cols = ms._terms_columns(
        _sleeve("kis_live", "equity_us"), _data(allowed_symbols=["brk-b", "AAPL"])
    )
    assert cols["allowed_symbols"] == ["AAPL", "BRK.B"]


def test_a_sleeve_without_a_mandate_realm_or_market_is_refused() -> None:
    with raises(v.MandateRefusal.UNSUPPORTED):
        ms._terms_columns(_sleeve("upbit", "crypto"), _data())
    with raises(v.MandateRefusal.UNSUPPORTED):
        ms.realm_and_market(_sleeve("kis_live", "crypto"))
    assert ms.realm_and_market(_sleeve("kiwoom_mock", "equity_kr")) == (
        "mock",
        "equity_kr",
    )


def test_clause_amounts_are_checked_and_decimals_canonical() -> None:
    cols = ms._terms_columns(_sleeve(), _data(per_order_cap="2000000.0000"))
    assert cols["per_order_cap"] == D("2000000")
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._terms_columns(_sleeve(), _data(sector_cap_pct="101"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._terms_columns(_sleeve(), _data(daily_cap="0"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._terms_columns(_sleeve(), _data(entry_max_premium_bps="0.00001"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._terms_columns(_sleeve(), _data(loss_sell_mode="auto"))


def test_i21_hash_recomputed_from_row_values_equals_the_request_hash() -> None:
    """The request content hashes ``30``; the numeric(10,4) column returns ``30.0000``."""
    cols = ms._terms_columns(_sleeve(), _data())
    prereg = PreRegistration("m", D("1"), 20, 90)
    content = ms._content_from_terms(
        1,
        cols,
        state="live",
        budget_cap=D("9750000"),
        loss_limit=D("500000"),
        prereg=prereg,
    )
    terms = SimpleNamespace(
        terms_version=cols["terms_version"],
        basket_key="basket-1",
        account_ref="acct-kis",
        universe_rule="rule",
        allowed_symbols=["000270", "005930"],
        entry_max_premium_bps=D("30.0000"),
        per_order_cap=D("2000000.0000"),
        daily_cap=D("5000000.0000"),
        sector_cap_pct=D("35.0000"),
        basket_drawdown_freeze_pct=D("25.0000"),
        account_drawdown_freeze_pct=D("25.0000"),
        tranche_rule={
            "weights": ["0.4", "0.3", "0.3"],
            "triggers_pct_from_first": ["0", "-15", "-30"],
            "min_gap_sessions": 20,
        },
        loss_sell_mode="human_card",
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
    )
    version = SimpleNamespace(
        sleeve_id=1,
        state="live",
        budget_cap=D("9750000.0000"),
        loss_limit=D("500000.0000"),
        prereg_metric="m",
        prereg_min_effect_pct=D("1.0000"),
        prereg_horizon_sessions=20,
        prereg_max_duration_days=90,
    )
    assert hashing.content_sha256(ms.terms_content_from_rows(terms, version)) == (
        hashing.content_sha256(content)
    )
    terms.daily_cap = D("5000001")
    assert hashing.content_sha256(ms.terms_content_from_rows(terms, version)) != (
        hashing.content_sha256(content)
    )


# ---------------------------------------------------------------- db error mapping
def _db_error(message: str) -> DBAPIError:
    return DBAPIError("stmt", {}, Exception(message))


def test_database_refusals_get_a_closed_code() -> None:
    assert (
        ms.map_db_error(
            _db_error("this symbol already has an unreleased plan in this realm")
        ).code
        is v.MandateRefusal.SYMBOL_RESERVED
    )
    assert (
        ms.map_db_error(
            _db_error("the plan does not hold sending authority (retired)")
        ).code
        is v.MandateRefusal.PLAN_NOT_ACTIVE
    )
    assert (
        ms.map_db_error(_db_error("the proposal does not match its mandate plan")).code
        is v.MandateRefusal.BINDING_MISMATCH
    )
    assert (
        ms.map_db_error(_db_error("anything else")).code
        is v.MandateRefusal.INVALID_TRANSITION
    )


def test_the_system_loss_gate_actor_is_not_an_operator() -> None:
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._operator("system:sleeve_loss_gate")
    assert ms._operator("operator:cli") == "operator:cli"


def test_column_bounds_match_the_numeric_columns() -> None:
    """NUMERIC(10,4) holds < 1e6 and Integer < 2**31: overflow is INVALID_FIELDS."""
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._terms_columns(_sleeve(), _data(entry_max_premium_bps="2000000"))
    assert ms._terms_columns(_sleeve(), _data(entry_max_premium_bps="999999.9999"))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule(
            {**GOOD_RULE, "triggers_pct_from_first": ["0", "-2000000", "-3000000"]}
        )
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._tranche_rule({**GOOD_RULE, "min_gap_sessions": 2**31})
    assert ms._tranche_rule({**GOOD_RULE, "min_gap_sessions": 2**31 - 1})


@pytest.mark.asyncio
async def test_a_refused_terms_approval_writes_nothing(monkeypatch) -> None:
    """The basket check runs before the first write (MandateError: nothing written)."""
    from unittest.mock import AsyncMock, MagicMock

    class Row:
        def first(self):
            return None

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    session = MagicMock()
    session.begin_nested = Savepoint
    added: list[object] = []
    session.add.side_effect = added.append
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=Row())
    content = {"basket_key": "b", "expires_at": "2099-01-01T00:00:00.000000Z"}
    request = SimpleNamespace(
        id=1,
        subject_kind="terms",
        sleeve_id=7,
        base_sleeve_version=1,
        content=content,
        content_sha256=hashing.content_sha256(content),
    )
    session.get = AsyncMock(return_value=request)

    async def done(*_a, **_k):
        return None

    async def sleeve(*_a, **_k):
        return _sleeve()

    async def current(*_a, **_k):
        return SimpleNamespace(version=1)

    async def mismatch(*_a, **_k):
        raise ms.MandateError(v.MandateRefusal.INVALID_FIELDS, "basket mismatch")

    monkeypatch.setattr(ms, "acquire_basket_lock", done)
    monkeypatch.setattr(ms, "acquire_sleeve_lock", done)
    monkeypatch.setattr(ms, "_sleeve", sleeve)
    monkeypatch.setattr(ms, "_check_basket_market", mismatch)
    monkeypatch.setattr(ms.sleeve_store, "current_version", current)
    with raises(v.MandateRefusal.INVALID_FIELDS):
        await ms.record_approval(
            session,
            request_id=1,
            decision="approved",
            principal="123",
            nonce_digest="a" * 64,
        )
    assert added == []


def test_prereg_must_fit_its_columns_exactly() -> None:
    """1.23456 would round to 1.2346 in NUMERIC(10,4) and break the approved hash."""
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._prereg(PreRegistration("m", D("1.23456"), 20, 90))
    with raises(v.MandateRefusal.INVALID_FIELDS):
        ms._prereg(PreRegistration("m", D("1"), 2**31, 90))
    assert ms._prereg(PreRegistration("m", D("1.2346"), 20, 90)).min_effect_pct == D(
        "1.2346"
    )


def test_a_hash_breach_is_not_a_closed_refusal() -> None:
    assert not issubclass(ms.MandateIntegrityError, ms.MandateError)
    assert not issubclass(ms.MandateIntegrityError, ValueError)


@pytest.mark.asyncio
async def test_a_fence_cannot_cite_another_sleeves_activation_profile(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    plan = SimpleNamespace(
        id=1, sleeve_id=7, symbol="X", account_ref="a", scope_account_refs=["a", "b"]
    )
    profile = SimpleNamespace(sleeve_id=99)
    session = MagicMock()
    session.get = AsyncMock(return_value=profile)

    async def done(*_a, **_k):
        return None

    async def get_plan(*_a, **_k):
        return plan

    async def sleeve(*_a, **_k):
        return _sleeve()

    monkeypatch.setattr(ms, "get_plan", get_plan)
    monkeypatch.setattr(ms, "_sleeve", sleeve)
    monkeypatch.setattr(ms, "acquire_sleeve_lock", done)
    with raises(v.MandateRefusal.BINDING_MISMATCH):
        await ms.append_plan_event(
            session,
            plan_id=1,
            event="tranche_send_fenced",
            actor="o",
            tranche_index=1,
            proposal_id=uuid4(),
            notional=D("1"),
            kst_day=datetime(2026, 11, 23).date(),
            approval_mode="auto",
            account_ref="a",
            attempt_id=uuid4(),
            activation_profile_id=5,
        )
