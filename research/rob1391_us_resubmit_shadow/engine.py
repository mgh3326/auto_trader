"""register / step: the per-session state machine (prereg §1, §3, §4).

Session order is strict and enforced, so the ledger is the same whoever runs
it and whenever: for each official session S in turn,

    step(S)      -- D1..D3 virtual resubmissions due on S, reconciliation of
                    every tracked parent against its source row, closes,
                    D+20 valuation, loss check;
    register(S)  -- parents whose original DAY order expired at the close of S
                    (D0 = S), only while S is inside the sealed 20-session window.

``advance --through S`` runs the pending pairs in order. Every command is
idempotent: a session already recorded is a no-op.

Latches (persisted ``latch`` events, never cleared):
  operational   -- write path reachable, duplicate parent, B_i exceeded,
                   quantity mismatch, look-ahead, reconciliation mismatch,
                   calendar/constant drift
  policy_change -- STOPPED_BY_POLICY_CHANGE
  loss          -- T equity <= E0*(1-5%) or a parent net <= -5% B_i
Any latch stops new assignments and resubmissions; marks and D+20 valuation
keep running read-only, so stopped books are still observed to the end.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow import guards
from research.rob1391_us_resubmit_shadow.costs import CostModel, FeeSchedule
from research.rob1391_us_resubmit_shadow.db_source import ParentSource
from research.rob1391_us_resubmit_shadow.fills import (
    simulate_conservative,
    simulate_optimistic,
)
from research.rob1391_us_resubmit_shadow.ledger import Ledger
from research.rob1391_us_resubmit_shadow.manifest import (
    BUDGET_RULES,
    verify_against_runtime,
)
from research.rob1391_us_resubmit_shadow.market_data import OK, Bar, MarketData
from research.rob1391_us_resubmit_shadow.parents import (
    Candidate,
    SourceOrder,
    assemble_orders,
    build_episodes,
    normalise,
)
from research.rob1391_us_resubmit_shadow.state import ARMS, D, ParentState, State, fold
from research.rob1391_us_resubmit_shadow.us_calendar import ET, USCalendar
from research.rob1391_us_resubmit_shadow.valuation import (
    arm_net,
    arm_spent,
    session_affordable,
)

# A session's data is read only after its close plus this settle delay.
SETTLE_DELAY = timedelta(minutes=30)
# Snapshot fields that must not change after registration (reconciliation).
RECONCILED_FIELDS = ("status", "quantity", "price", "filled_qty", "avg_fill_price")


class OrderingError(RuntimeError):
    """A command was run out of session order or too early."""


class PendingReconcile(RuntimeError):
    """D0 candidates are not yet broker-reconciled; rerun later."""


class StoppedError(RuntimeError):
    """A guard latched the epoch during this command."""


@dataclass
class Context:
    ledger: Ledger
    market: MarketData
    source: ParentSource
    now: Callable[[], datetime]
    # Resolved at call time, so the guard functions cannot be bypassed by
    # binding an old reference at import.
    code_sha: Callable[[], str] = field(default=lambda: guards.git_code_sha())
    policy_stamp: Callable[[], dict[str, str]] = field(
        default=lambda: guards.current_policy_stamp()
    )
    loaded_modules: Callable[[], dict] = field(default=lambda: sys.modules)

    # filled by load()
    state: State | None = None
    cal: USCalendar | None = None

    def load(self) -> State:
        manifest, digest = self.ledger.read_manifest()
        self.state = fold(manifest, digest, self.ledger.load())
        days = [s["day"] for s in manifest["calendar"]["sessions"]]
        self.cal = USCalendar(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
        return self.state

    @property
    def manifest(self) -> dict[str, Any]:
        assert self.state is not None
        return self.state.manifest

    def fee_schedule(self) -> FeeSchedule | None:
        block = self.manifest.get("fee_schedule")
        return FeeSchedule.parse(block["content"]) if block else None

    def cost_model(
        self, source: str, slippage_bp: Decimal = K.SLIPPAGE_BP_MAIN
    ) -> CostModel:
        return CostModel(
            schedule=self.fee_schedule(), source=source, slippage_bp=slippage_bp
        )

    def include_cost_in_budget(self) -> bool:
        return BUDGET_RULES[self.manifest["budget_rule"]]


# ----------------------------------------------------------------------------
# guards and latches
# ----------------------------------------------------------------------------


def latch(
    ctx: Context,
    latch_type: str,
    reason: str,
    *,
    session: str | None,
    detail: Any = None,
) -> None:
    assert ctx.state is not None
    event = ctx.ledger.append(
        "latch",
        {
            "latch_type": latch_type,
            "reason": reason,
            "session": session,
            "detail": detail,
        },
        now=ctx.now(),
    )
    ctx.state.apply(event)


def preflight(ctx: Context, session: str | None) -> None:
    """Checks every command runs first. Latches what is an experiment event;
    raises on an operator mistake (wrong checkout)."""

    state = ctx.state
    assert state is not None
    sha = ctx.code_sha()
    if sha != ctx.manifest["code_git_sha"]:
        raise guards.CodeShaError(
            f"code SHA {sha[:12]} differs from the sealed {ctx.manifest['code_git_sha'][:12]}"
        )
    forbidden = guards.loaded_forbidden_modules(ctx.loaded_modules())
    if forbidden and "write_path_reachable" not in _latch_reasons(state):
        latch(
            ctx,
            "operational",
            "write_path_reachable",
            session=session,
            detail=forbidden[:20],
        )
    stamp = ctx.policy_stamp()
    if stamp != ctx.manifest["policy"] and "policy_change" not in state.latch_types():
        latch(
            ctx,
            "policy_change",
            "STOPPED_BY_POLICY_CHANGE",
            session=session,
            detail={"sealed": ctx.manifest["policy"], "now": stamp},
        )
    for problem in verify_against_runtime(ctx.manifest):
        if problem not in _latch_reasons(state):
            latch(ctx, "operational", problem, session=session)


def _latch_reasons(state: State) -> set[str]:
    return {latch_["reason"] for latch_ in state.latches}


def _require_closed(ctx: Context, day: date) -> None:
    assert ctx.cal is not None
    close = ctx.cal.session(day).close_utc
    if ctx.now() < close + SETTLE_DELAY:
        raise OrderingError(
            f"session {day} is not closed+settled yet (look-ahead guard); "
            f"run after {(close + SETTLE_DELAY).isoformat()}"
        )


def _horizon_sessions(state: State) -> list[str]:
    return [s["day"] for s in state.manifest["calendar"]["sessions"]]


def _expected_next(state: State) -> tuple[str, str] | None:
    """The next (command, session) in the strict order, or None when done."""

    window = set(state.window)
    start = state.window[0]
    for day in _horizon_sessions(state):
        if day < start:
            continue
        if day not in state.stepped:
            return ("step", day)
        if day in window and day not in state.registered:
            return ("register", day)
    return None


def _check_order(state: State, command: str, session: str) -> bool:
    """True when this (command, session) is the next due; False when already
    done (idempotent no-op); raises when out of order."""

    if command == "step" and session in state.stepped:
        return False
    if command == "register" and session in state.registered:
        return False
    due = _expected_next(state)
    if due != (command, session):
        raise OrderingError(f"{command} {session} is out of order; next due is {due}")
    return True


# ----------------------------------------------------------------------------
# source and market-data helpers
# ----------------------------------------------------------------------------


def _source_since(ctx: Context) -> datetime:
    assert ctx.cal is not None
    first = date.fromisoformat(ctx.manifest["calendar"]["sessions"][0]["day"])
    return ctx.cal.session(first).open_utc - timedelta(days=3)


def _orders(ctx: Context) -> list[SourceOrder]:
    snap = ctx.source.fetch(_source_since(ctx))
    return assemble_orders(snap.order_rows, snap.rung_rows, snap.universe_rows)


def _daily(
    ctx: Context, symbol: str, days: list[date], adjustment: str = "raw"
) -> tuple[str, dict[date, Bar], list[str], str | None]:
    result = ctx.market.daily_bars(symbol, days[0], days[-1], adjustment=adjustment)
    if result.status != OK:
        return result.status, {}, list(result.digests), result.reason
    by_day = {b.start_utc.astimezone(ET).date(): b for b in result.bars}
    return OK, {d: by_day[d] for d in days if d in by_day}, list(result.digests), None


def _working(cal: USCalendar, o: SourceOrder) -> date | None:
    if o.ordered_at is None:
        return None
    try:
        return cal.working_session(o.ordered_at)
    except Exception:
        return None


def _snapshot(o: SourceOrder) -> dict[str, str | None]:
    return {
        "status": o.status,
        "quantity": str(o.quantity) if o.quantity is not None else None,
        "price": str(o.price) if o.price is not None else None,
        "filled_qty": str(o.filled_qty or Decimal("0")),
        "avg_fill_price": str(o.avg_fill_price)
        if o.avg_fill_price is not None
        else None,
    }


def _same_snapshot(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for key in RECONCILED_FIELDS:
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            if x != y:
                return False
        elif key == "status":
            if x != y:
                return False
        elif Decimal(str(x)) != Decimal(str(y)):
            return False
    return True


# ----------------------------------------------------------------------------
# register
# ----------------------------------------------------------------------------


def register(ctx: Context, session: date) -> dict[str, Any] | None:
    state, cal = ctx.state, ctx.cal
    assert state is not None and cal is not None
    s = session.isoformat()
    if s not in state.window:
        raise OrderingError(f"{s} is outside the sealed 20-session window")
    if not _check_order(state, "register", s):
        return None
    _require_closed(ctx, session)
    preflight(ctx, s)

    seal_at = datetime.fromisoformat(ctx.manifest["sealed_at_utc"])
    orders = _orders(ctx)
    # Only orders whose working session is <= S shape the episodes, so the
    # result does not depend on when register runs.
    working = {o.source_ref: _working(cal, o) for o in orders}
    visible = [
        o
        for o in orders
        if working[o.source_ref] is not None and working[o.source_ref] <= session
    ]
    episodes = build_episodes(visible, cal)
    deadline = cal.session(cal.offset(session, 1)).close_utc

    exclusions: list[dict[str, Any]] = []
    eligible: list[tuple[Candidate, str]] = []
    pending: list[str] = []
    data_digests: list[str] = []
    liquidity_by_ref: dict[str, dict[str, Any]] = {}
    for o in visible:
        if working[o.source_ref] != session:
            continue
        ep_id = episodes.episode_of.get(o.source_ref)
        base = {"source_ref": o.source_ref, "symbol": o.symbol, "episode_id": ep_id}
        if o.source_ref in episodes.quarantined:
            exclusions.append({**base, "reasons": [episodes.quarantined[o.source_ref]]})
            continue
        ep = episodes.episodes[ep_id]
        if ep.anchor.source_ref != o.source_ref:
            exclusions.append({**base, "reasons": ["episode_member_not_anchor"]})
            continue
        if o.ordered_at < seal_at:
            exclusions.append({**base, "reasons": ["pre_seal_order"]})
            continue
        cand, reasons = normalise(o, cal)
        if reasons == ["unreconciled"]:
            if ctx.now() < deadline:
                pending.append(o.source_ref)
                continue
            reasons = ["unreconciled_at_deadline"]
        if cand is None:
            exclusions.append({**base, "reasons": reasons})
            continue
        if (
            cand.policy_version_at_order
            and cand.policy_version_at_order != ctx.manifest["policy"]["version"]
        ):
            if "policy_change" not in state.latch_types():
                latch(
                    ctx,
                    "policy_change",
                    "STOPPED_BY_POLICY_CHANGE",
                    session=s,
                    detail={"order_policy_version": cand.policy_version_at_order},
                )
            exclusions.append({**base, "reasons": ["policy_version_differs_at_order"]})
            continue
        reasons, liquidity = _liquidity_and_spread(ctx, cand, data_digests)
        if reasons:
            exclusions.append({**base, "reasons": reasons, "liquidity": liquidity})
            continue
        eligible.append((cand, ep_id))
        liquidity_by_ref[cand.order.source_ref] = liquidity
    if pending:
        raise PendingReconcile(
            f"{len(pending)} D0 candidate(s) not yet reconciled; rerun after the broker "
            f"reconcile or after {deadline.isoformat()}"
        )

    assigned, assignment_log = _assign(
        ctx, session, eligible, episodes, liquidity_by_ref
    )
    payload = {
        "session": s,
        "assigned": assigned,
        "exclusions": exclusions + [e for e in assignment_log if e.get("reasons")],
        "assignment_log": assignment_log,
        "orders_seen": len(visible),
        # never silently dropped: rows whose instant maps to no session
        "calendar_unmappable_orders": sorted(
            r for r, w in working.items() if w is None
        ),
        "data_digests": sorted(set(data_digests)),
    }
    event = ctx.ledger.append("register", payload, now=ctx.now())
    state.apply(event)
    duplicates = [e for e in assignment_log if e.get("reasons") == ["duplicate_parent"]]
    if duplicates and "duplicate_parent" not in _latch_reasons(state):
        latch(ctx, "operational", "duplicate_parent", session=s, detail=duplicates[:10])
    return payload


def _liquidity_and_spread(
    ctx: Context, cand: Candidate, digests: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """Registration filter, identical for both arms (prereg §1)."""

    cal = ctx.cal
    assert cal is not None
    sym = cand.order.symbol
    lookback = cal.previous(cand.d0, K.LIQUIDITY_LOOKBACK_SESSIONS)
    status, bars, dg, reason = _daily(ctx, sym, [*lookback, cand.d0])
    digests.extend(dg)
    info: dict[str, Any] = {}
    if status != OK:
        return [f"liquidity_data_unavailable:{reason}"], info
    dollar = []
    for day in lookback:
        bar = bars.get(day)
        if bar is None:
            dollar.append(Decimal("0"))  # complete response, no bar == no trade
            continue
        if bar.volume is None or (bar.vwap is None and bar.close is None):
            return ["liquidity_data_unavailable:bar_fields"], info
        dollar.append((bar.vwap if bar.vwap is not None else bar.close) * bar.volume)
    median = statistics.median(dollar)
    info["median_dollar_volume_20"] = str(median)
    reasons: list[str] = []
    if median < K.MIN_MEDIAN_DOLLAR_VOLUME_USD:
        reasons.append("liquidity_below_min")
    d0_bar = bars.get(cand.d0)
    if d0_bar is None or not d0_bar.volume:
        reasons.append("halt_or_stale_d0")
    session = cal.session(cand.d0)
    start = max(cand.order.ordered_at, session.open_utc)
    quote = ctx.market.first_valid_quote(sym, start, session.close_utc)
    digests.extend(quote.digests)
    if quote.status != OK:
        reasons.append(f"spread_data_unavailable:{quote.reason}")
    elif quote.quote is None:
        reasons.append("no_valid_quote_d0")
    else:
        info["spread_bp_at_submission"] = str(quote.quote.spread_bp)
        info["submission_quote_at"] = quote.quote.at_utc.isoformat()
        if quote.quote.spread_bp > K.MAX_SPREAD_BP:
            reasons.append("spread_gate_at_submission")
    return reasons, info


def _open_parents(state: State, session: date) -> list[ParentState]:
    return [p for p in state.parents.values() if p.d0 <= session < p.d20]


def _assign(
    ctx: Context,
    session: date,
    eligible: list[tuple[Candidate, str]],
    episodes: Any,
    liquidity_by_ref: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state, cal = ctx.state, ctx.cal
    assert state is not None and cal is not None
    ordered = sorted(eligible, key=lambda item: (item[0].expiry_at, item[1]))
    assigned: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    open_now = _open_parents(state, session)
    open_symbols = {p.rec["symbol"] for p in open_now}
    reserved = sum((p.budget for p in open_now), Decimal("0"))
    total = len(state.parents)
    taken_ids = set(state.parents)
    taken_refs = {p.rec["source_ref"] for p in state.parents.values()}
    for cand, ep_id in ordered:
        sym = cand.order.symbol
        entry = {
            "episode_id": ep_id,
            "source_ref": cand.order.source_ref,
            "symbol": sym,
        }
        if state.latched:
            log.append({**entry, "reasons": ["latched"]})
            continue
        if ep_id in taken_ids or cand.order.source_ref in taken_refs:
            # B_i is counted once per episode: a second parent is an
            # operational stop (prereg §4 중복 parent), never a new row.
            log.append({**entry, "reasons": ["duplicate_parent"]})
            continue
        if sym in open_symbols:
            log.append({**entry, "reasons": ["symbol_has_open_parent"]})
            continue
        if total >= K.MAX_PARENTS_TOTAL:
            log.append({**entry, "reasons": ["total_cap"]})
            continue
        if len(assigned) >= K.MAX_PARENTS_PER_SESSION:
            log.append({**entry, "reasons": ["daily_cap"]})
            continue
        if K.E0_USD - reserved < cand.budget:
            log.append({**entry, "reasons": ["insufficient_virtual_cash"]})
            continue
        ep = episodes.episodes[ep_id]
        rec = {
            "episode_id": ep_id,
            "assignment_seq": total + 1,
            "symbol": sym,
            "source": cand.order.source,
            "source_ref": cand.order.source_ref,
            "broker_order_id": cand.order.broker_order_id,
            "proposal_root": cand.order.rung.proposal_root if cand.order.rung else None,
            "ordered_at": cand.order.ordered_at.isoformat(),
            "d0": cand.d0.isoformat(),
            "resubmit_sessions": [
                cal.offset(cand.d0, k).isoformat()
                for k in range(1, K.RESUBMIT_SESSIONS + 1)
            ],
            "d20": cal.offset(cand.d0, K.EVAL_SESSION_OFFSET).isoformat(),
            "last_tracked": cal.offset(
                cand.d0, K.RESUBMIT_SESSIONS + K.AUX_FILL_OFFSET
            ).isoformat(),
            "expiry_at": cand.expiry_at.isoformat(),
            "expiry_basis": cand.expiry_basis,
            "p": str(cand.p),
            "q": cand.q,
            "filled_d0": cand.filled_d0,
            "avg_fill_d0": str(cand.avg_fill_d0)
            if cand.avg_fill_d0 is not None
            else None,
            "remaining_d0": cand.remaining,
            "budget": str(cand.budget),
            "budget_basis": cand.budget_basis,
            "policy_version_at_order": cand.policy_version_at_order,
            "policy_basis": "order_stamp"
            if cand.policy_version_at_order
            else "epoch_invariant",
            "linked_refs": [
                m.source_ref
                for m in ep.members
                if m.source_ref != cand.order.source_ref
            ],
            "source_snapshot": _snapshot(cand.order),
            **liquidity_by_ref.get(cand.order.source_ref, {}),
        }
        assigned.append(rec)
        log.append({**entry, "reasons": [], "assigned": True})
        taken_ids.add(ep_id)
        taken_refs.add(cand.order.source_ref)
        open_symbols.add(sym)
        reserved += cand.budget
        total += 1
    return assigned, log


# ----------------------------------------------------------------------------
# step
# ----------------------------------------------------------------------------


def step(ctx: Context, session: date) -> dict[str, Any] | None:
    state, cal = ctx.state, ctx.cal
    assert state is not None and cal is not None
    s = session.isoformat()
    if not _check_order(state, "step", s):
        return None
    _require_closed(ctx, session)
    preflight(ctx, s)

    tracked = [p for p in state.parents.values() if p.d0 < session <= p.last_tracked]
    orders = {o.source_ref: o for o in _orders(ctx)} if tracked else {}
    entries: dict[str, dict[str, Any]] = {}
    operational: list[tuple[str, Any]] = []
    for parent in sorted(tracked, key=lambda p: p.rec["assignment_seq"]):
        entry, problems = _step_parent(ctx, parent, session, orders)
        entries[parent.episode_id] = entry
        operational.extend(problems)

    payload: dict[str, Any] = {"session": s, "parents": entries}
    event = ctx.ledger.append("step", payload, now=ctx.now())
    state.apply(event)

    for reason, detail in operational:
        if reason not in _latch_reasons(state):
            latch(ctx, "operational", reason, session=s, detail=detail)
    _invariants(ctx, s)
    loss = loss_check(ctx, session)
    if loss["triggered"] and "loss" not in state.latch_types():
        latch(ctx, "loss", loss["triggered"][0], session=s, detail=loss)
    return payload


def _step_parent(
    ctx: Context, parent: ParentState, session: date, orders: dict[str, SourceOrder]
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    cal, state = ctx.cal, ctx.state
    assert cal is not None and state is not None
    s = session.isoformat()
    sym = parent.rec["symbol"]
    problems: list[tuple[str, Any]] = []
    entry: dict[str, Any] = {"symbol": sym}

    # --- reconciliation against the source row (prereg §4 원장 reconciliation)
    row = orders.get(parent.rec["source_ref"])
    if row is None or not _same_snapshot(_snapshot(row), parent.rec["source_snapshot"]):
        entry["reconciliation"] = "mismatch"
        problems.append(("reconciliation_mismatch", {"episode_id": parent.episode_id}))
    else:
        entry["reconciliation"] = "ok"
    expiry = datetime.fromisoformat(parent.rec["expiry_at"])
    followups = sorted(
        ref
        for ref, o in orders.items()
        if o.symbol == sym
        and o.ordered_at is not None
        and expiry < o.ordered_at <= cal.session(session).close_utc
        and (o.side or "").lower() == "buy"
        and ref not in parent.real_followups
    )
    entry["new_real_followups"] = followups

    # --- daily bars D0..S: marks, halt, split/merge review (no look-ahead:
    # a split *after* S scales raw/split-adjusted alike for every day, so only a
    # change of that ratio inside D0..S is a corporate action in the window).
    days = [cal.offset(parent.d0, k) for k in range(cal.gap(parent.d0, session) + 1)]
    status, raw_bars, digests, reason = _daily(ctx, sym, days)
    split_status, split_bars, split_digests, split_reason = _daily(
        ctx, sym, days, adjustment="split"
    )
    entry["data_digests"] = [*digests, *split_digests]
    raw_close = raw_bars.get(session).close if raw_bars.get(session) else None
    factor = None
    if status == OK and split_status == OK:
        factor = split_factor(raw_bars, split_bars, parent.d0, session)
    entry["raw_close"] = str(raw_close) if raw_close is not None else None
    entry["split_factor_since_d0"] = str(factor) if factor is not None else None
    if raw_close is not None and factor is not None:
        entry["close"] = str(raw_close * factor)
        entry["close_status"] = "ok"
    else:
        entry["close"] = None
        entry["close_status"] = (
            f"unknown:{reason or split_reason or 'no_bar_or_split_check'}"
        )
    if factor is not None and factor != 1:
        entry["review_block"] = "corporate_action"

    if s in parent.resubmit_sessions:
        entry["resubmission"] = _resubmit(
            ctx,
            parent,
            session,
            raw_bars if status == OK and factor is not None else None,
            entry,
        )
    return entry, problems


def split_factor(
    raw: dict[date, Bar], adjusted: dict[date, Bar], d0: date, session: date
) -> Decimal | None:
    """Shares-per-D0-share multiplier from splits/merges inside (D0, S].

    ``r(day) = raw_close / split_adjusted_close`` is the cumulative factor of
    splits after ``day`` as of the fetch; ``r(D0) / r(S)`` isolates the ones
    between D0 and S. None when either close is missing.
    """

    pairs = []
    for day in (d0, session):
        a, b = raw.get(day), adjusted.get(day)
        if a is None or b is None or not a.close or not b.close:
            return None
        pairs.append(a.close / b.close)
    ratio = pairs[0] / pairs[1]
    return Decimal("1") if abs(ratio - 1) <= Decimal("0.000001") else ratio


def _resubmit(
    ctx: Context,
    parent: ParentState,
    session: date,
    raw_bars: dict[date, Bar] | None,
    entry: dict[str, Any],
) -> dict[str, Any]:
    cal, state = ctx.cal, ctx.state
    assert cal is not None and state is not None
    s = session.isoformat()
    sym = parent.rec["symbol"]
    k = parent.resubmit_sessions.index(s) + 1
    result: dict[str, Any] = {"day_index": k}

    def skip(reason: str, status: str = "skipped") -> dict[str, Any]:
        for arm in ("T", "T_opt"):
            result[arm] = {"status": status, "reason": reason, "fills": []}
            if status == "unknown":
                result[arm]["unknown_reason"] = reason
        return result

    if state.latched:
        return skip("latched")
    if parent.review_block:
        return skip(f"review_block:{parent.review_block}")
    if raw_bars is None:
        return skip("daily_bars_unavailable", "unknown")
    if entry.get("review_block"):
        return skip(f"{entry['review_block']}_review")
    bar = raw_bars.get(session)
    if bar is None or not bar.volume:
        entry["review_block"] = "halt_or_stale"
        return skip("halt_or_stale_review")

    sess = cal.session(session)
    quote = ctx.market.first_valid_quote(sym, sess.open_utc, sess.close_utc)
    entry["data_digests"].extend(quote.digests)
    if quote.status != OK:
        return skip(f"first_quote_unavailable:{quote.reason}", "unknown")
    if quote.quote is None:
        return skip("no_valid_quote")
    q = quote.quote
    result["submit_at"] = q.at_utc.isoformat()
    result["first_quote"] = {
        "bid": str(q.bid),
        "ask": str(q.ask),
        "spread_bp": str(q.spread_bp),
    }
    if q.spread_bp > K.MAX_SPREAD_BP:
        return skip("spread_gate")

    minute = ctx.market.minute_bars(sym, sess.open_utc, sess.close_utc)
    entry["data_digests"].extend(minute.digests)
    price = parent.p
    model = ctx.cost_model(parent.rec["source"])
    for arm in ("T", "T_opt"):
        book = parent.arms[arm]
        remaining = parent.remaining(arm)
        if book.unknown:
            result[arm] = {"status": "skipped", "reason": "prior_unknown", "fills": []}
            continue
        if remaining <= 0:
            result[arm] = {"status": "skipped", "reason": "no_remaining", "fills": []}
            continue
        affordable = session_affordable(
            book=book,
            session=s,
            price=price,
            budget=parent.budget,
            model=model,
            include_cost=ctx.include_cost_in_budget(),
        )
        if affordable(0) <= 0:
            result[arm] = {
                "status": "skipped",
                "reason": "insufficient_parent_budget",
                "fills": [],
            }
            continue
        if minute.status != OK:
            result[arm] = {
                "status": "unknown",
                "unknown_reason": f"minute_bars_unavailable:{minute.reason}",
                "fills": [],
            }
            continue
        if arm == "T":
            sim = simulate_conservative(
                bars=minute.bars,
                submit_at=q.at_utc,
                close_utc=sess.close_utc,
                limit=price,
                remaining=remaining,
                affordable=affordable,
                ask_lookup=lambda a, b: ctx.market.ask_at_or_below(sym, a, b, price),
            )
            entry["data_digests"].extend(sim.ask_digests)
        else:
            sim = simulate_optimistic(
                bars=minute.bars,
                submit_at=q.at_utc,
                close_utc=sess.close_utc,
                limit=price,
                remaining=remaining,
                affordable=affordable,
            )
        result[arm] = {
            "status": sim.status,
            "unknown_reason": sim.unknown_reason,
            "candidate_minutes": sim.candidate_minutes,
            "fills": [
                {
                    "at": f.at_utc.isoformat(),
                    "qty": f.qty,
                    "price": str(f.price),
                    "bar_volume": str(f.bar_volume)
                    if f.bar_volume is not None
                    else None,
                }
                for f in sim.fills
            ],
        }
    return result


# ----------------------------------------------------------------------------
# invariants and loss check
# ----------------------------------------------------------------------------


def _invariants(ctx: Context, s: str) -> None:
    """Operational stops computed from the folded books (prereg §4)."""

    state = ctx.state
    assert state is not None
    problems: list[tuple[str, Any]] = []
    seen_refs: dict[str, str] = {}
    for parent in state.parents.values():
        ref = parent.rec["source_ref"]
        if ref in seen_refs:
            problems.append(("duplicate_parent", {"episode_id": parent.episode_id}))
        seen_refs[ref] = parent.episode_id
        model = ctx.cost_model(parent.rec["source"])
        for arm in ARMS:
            book = parent.arms[arm]
            if book.shares > int(parent.rec["q"]):
                problems.append(
                    ("quantity_mismatch", {"episode_id": parent.episode_id, "arm": arm})
                )
            spent = (
                arm_spent(book, model)
                if ctx.include_cost_in_budget()
                else book.fill_notional
            )
            virtual = any(f["kind"] == "virtual" for f in book.fills)
            if virtual and spent > parent.budget:
                problems.append(
                    ("budget_exceeded", {"episode_id": parent.episode_id, "arm": arm})
                )
            for fill in book.fills:
                if fill["kind"] != "virtual":
                    continue
                if fill["session"] not in parent.resubmit_sessions:
                    problems.append(
                        (
                            "fill_outside_resubmit_window",
                            {"episode_id": parent.episode_id},
                        )
                    )
                at = datetime.fromisoformat(fill["at"])
                assert ctx.cal is not None
                if at >= ctx.cal.session(date.fromisoformat(fill["session"])).close_utc:
                    problems.append(
                        (
                            "look_ahead_fill_after_close",
                            {"episode_id": parent.episode_id},
                        )
                    )
        real_c = [f for f in parent.arms["C"].fills if f["kind"] == "real_d0"]
        if sum(int(f["qty"]) for f in real_c) != int(parent.rec["filled_d0"]):
            problems.append(
                ("quantity_mismatch", {"episode_id": parent.episode_id, "arm": "C"})
            )
        if any(f["kind"] == "virtual" for f in parent.arms["C"].fills):
            problems.append(
                ("control_arm_virtual_fill", {"episode_id": parent.episode_id})
            )
    for reason, detail in problems:
        if reason not in _latch_reasons(state):
            latch(ctx, "operational", reason, session=s, detail=detail)


def latest_mark(parent: ParentState, upto: date) -> tuple[str | None, Decimal | None]:
    """Close used for valuation at ``upto``: D+20 close once matured."""

    target = min(upto, parent.d20).isoformat()
    close = parent.marks.get(target)
    return target, (D(close) if close is not None else None)


def loss_check(ctx: Context, session: date) -> dict[str, Any]:
    state = ctx.state
    assert state is not None
    equity = K.E0_USD
    unknown: list[str] = []
    triggered: list[str] = []
    per_parent: dict[str, str] = {}
    for parent in state.parents.values():
        if parent.d0 >= session:
            continue
        _, close = latest_mark(parent, session)
        if close is None:
            unknown.append(parent.episode_id)
            continue
        net = arm_net(parent.arms["T"], close, ctx.cost_model(parent.rec["source"]))
        per_parent[parent.episode_id] = str(net)
        equity += net
        if net <= -K.LOSS_STOP_PARENT_FRACTION * parent.budget:
            triggered.append("parent_net_loss_ge_5pct_budget")
    if equity <= K.E0_USD * (1 - K.LOSS_STOP_EQUITY_FRACTION):
        triggered.insert(0, "t_equity_le_e0_minus_5pct")
    return {
        "session": session.isoformat(),
        "t_equity": str(equity),
        "unknown_marks": unknown,
        "triggered": triggered,
        "per_parent_net_t": per_parent,
    }


# ----------------------------------------------------------------------------
# advance
# ----------------------------------------------------------------------------


def advance(ctx: Context, through: date) -> list[tuple[str, str]]:
    """Run every due (step, register) pair up to and including ``through``."""

    state = ctx.state
    assert state is not None
    done: list[tuple[str, str]] = []
    while True:
        due = _expected_next(state)
        if due is None or due[1] > through.isoformat():
            return done
        command, day = due
        if (
            ctx.now()
            < ctx.cal.session(date.fromisoformat(day)).close_utc + SETTLE_DELAY
        ):
            return done
        if command == "step":
            step(ctx, date.fromisoformat(day))
        else:
            register(ctx, date.fromisoformat(day))
        done.append(due)


def now_utc() -> datetime:
    return datetime.now(UTC)
