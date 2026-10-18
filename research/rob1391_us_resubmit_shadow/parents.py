"""Parent orders: normalisation, exclusion reasons, episode dedupe (prereg §1).

Input is a list of source rows -- the exact column set selected by
``db_source`` from the three US-buy ledgers, joined in Python with proposal
rungs and the US symbol universe. Nothing here talks to a database or the
network; every decision is a pure function of those rows and the calendar.

Exclusions are never silent: every candidate that does not become a parent
carries one or more reason codes that end up in the ``register`` event.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.symbol import to_db_symbol
from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow.us_calendar import CalendarError, USCalendar

SOURCE_TOSS = "toss_live_order_ledger"
SOURCE_KIS_US = "live_order_ledger"
SOURCE_KIS_KR_TABLE = "kis_live_order_ledger"
SOURCES = (SOURCE_TOSS, SOURCE_KIS_US, SOURCE_KIS_KR_TABLE)

# Proposal account_mode that owns each ledger (rung <-> order join guard).
ACCOUNT_MODE_OF_SOURCE = {SOURCE_TOSS: "toss_live", SOURCE_KIS_US: "kis_live"}

_OCC_OPTION = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_OTC_MARKERS = ("OTC", "PINK", "GREY", "GRAY")
# Unknown listing vocabulary is not a reason to exclude (halts are also caught
# from the bars); only explicit inactive states are.
_INACTIVE_LISTING = frozenset({"DELISTED", "SUSPENDED", "HALTED", "INACTIVE"})
_LEVERAGE_NAME = re.compile(
    r"(\b[1-9](\.\d+)?X\b|\bULTRA|\bINVERSE\b|\bBEAR\b|\bSHORT\b|\bLEVERAGED?\b"
    r"|\bDAILY\s+(BULL|BEAR)\b|\b-[1-9]X\b)",
    re.IGNORECASE,
)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        out = value
    else:
        try:
            out = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if out.tzinfo is None:
        return None  # naive timestamps have no defined session -> incomplete
    return out.astimezone(UTC)


def _is_integral(value: Decimal) -> bool:
    return value == value.to_integral_value()


def tick_size(price: Decimal) -> Decimal:
    """US equity minimum increment: $0.01 at/above $1, $0.0001 below."""

    return K.TICK_AT_OR_ABOVE_1 if price >= K.PRICE_ONE_DOLLAR else K.TICK_BELOW_1


@dataclass(frozen=True, slots=True)
class Universe:
    exchange: str | None
    name_en: str | None
    is_active: bool | None
    is_common_stock: bool | None
    is_common_share: bool | None
    security_type: str | None
    listing_status: str | None
    leverage_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class Rung:
    broker_order_id: str
    account_mode: str | None
    proposal_root: str | None
    rung_index: int | None
    quantity: Decimal | None
    limit_price: Decimal | None
    notional: Decimal | None
    order_policy_version: str | None
    order_policy_content_hash: str | None


@dataclass(frozen=True, slots=True)
class SourceOrder:
    source: str
    source_id: str
    broker_order_id: str | None
    symbol: str
    market: str | None
    side: str | None
    order_type: str | None
    operation_kind: str | None
    time_in_force: str | None
    quantity: Decimal | None
    price: Decimal | None
    currency: str | None
    status: str | None
    requires_manual_review: bool
    ordered_at: datetime | None
    expired_at: datetime | None
    filled_qty: Decimal | None
    avg_fill_price: Decimal | None
    exchange: str | None
    universe: Universe | None = None
    rung: Rung | None = None
    duplicate_rows: int = 1
    duplicate_rungs: int = 0

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.source_id}"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SourceOrder:
        return cls(
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            broker_order_id=(
                str(row["broker_order_id"]) if row.get("broker_order_id") else None
            ),
            symbol=to_db_symbol(str(row.get("symbol") or "").strip().upper()),
            market=row.get("market"),
            side=(row.get("side") or None),
            order_type=(row.get("order_type") or None),
            operation_kind=(row.get("operation_kind") or None),
            time_in_force=(row.get("time_in_force") or None),
            quantity=_dec(row.get("quantity")),
            price=_dec(row.get("price")),
            currency=(row.get("currency") or None),
            status=(row.get("status") or None),
            requires_manual_review=bool(row.get("requires_manual_review") or False),
            ordered_at=_ts(row.get("ordered_at")),
            expired_at=_ts(row.get("expired_at")),
            filled_qty=_dec(row.get("filled_qty")),
            avg_fill_price=_dec(row.get("avg_fill_price")),
            exchange=(row.get("exchange") or None),
        )


def universe_from_row(row: dict[str, Any]) -> Universe:
    return Universe(
        exchange=row.get("exchange"),
        name_en=row.get("name_en"),
        is_active=row.get("is_active"),
        is_common_stock=row.get("is_common_stock"),
        is_common_share=row.get("is_common_share"),
        security_type=row.get("security_type"),
        listing_status=row.get("listing_status"),
        leverage_factor=_dec(row.get("leverage_factor")),
    )


def _order_policy_stamp(
    raw: Any, rung_index: int | None
) -> tuple[str | None, str | None]:
    """(version, content_hash) the auto-approver recorded for this rung.

    Source: ``order_proposals.source_asof.auto_approved.cap_observations[]``
    (auto_approve_audit, one entry per eligible rung with policy_version and
    content_hash). Anything malformed or ambiguous -> (None, None).
    """

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, None
    if not isinstance(raw, list) or rung_index is None:
        return None, None
    hits = [o for o in raw if isinstance(o, dict) and o.get("rung_index") == rung_index]
    if len(hits) != 1:
        return None, None
    version, digest = hits[0].get("policy_version"), hits[0].get("content_hash")
    if not (
        isinstance(version, str) and version and isinstance(digest, str) and digest
    ):
        return None, None
    return version, digest


def rung_from_row(row: dict[str, Any]) -> Rung:
    idx = row.get("rung_index")
    return Rung(
        broker_order_id=str(row["broker_order_id"]),
        account_mode=row.get("account_mode"),
        proposal_root=(str(row["proposal_root"]) if row.get("proposal_root") else None),
        rung_index=int(idx) if idx is not None else None,
        quantity=_dec(row.get("rung_quantity")),
        limit_price=_dec(row.get("rung_limit_price")),
        notional=_dec(row.get("rung_notional")),
        **dict(
            zip(
                ("order_policy_version", "order_policy_content_hash"),
                _order_policy_stamp(
                    row.get("cap_observations"), int(idx) if idx is not None else None
                ),
                strict=True,
            )
        ),
    )


def assemble_orders(
    order_rows: list[dict[str, Any]],
    rung_rows: list[dict[str, Any]],
    universe_rows: list[dict[str, Any]],
) -> list[SourceOrder]:
    """Join ledger rows with their proposal rung and universe row.

    A rung joins only when its proposal ``account_mode`` owns that ledger, so a
    Toss broker id can never borrow a KIS rung. Duplicate ledger rows for one
    broker order and duplicate rungs are counted, not collapsed -- they make
    the order "unclear" downstream.
    """

    universe = {
        to_db_symbol(str(r["symbol"]).strip().upper()): universe_from_row(r)
        for r in universe_rows
    }
    rungs: dict[tuple[str, str], list[Rung]] = defaultdict(list)
    for raw in rung_rows:
        rung = rung_from_row(raw)
        rungs[(str(rung.account_mode), rung.broker_order_id)].append(rung)
    orders = [SourceOrder.from_row(r) for r in order_rows]
    dup: dict[tuple[str, str], int] = defaultdict(int)
    for o in orders:
        if o.broker_order_id:
            dup[(o.source, o.broker_order_id)] += 1
    out: list[SourceOrder] = []
    for o in orders:
        matched: list[Rung] = []
        mode = ACCOUNT_MODE_OF_SOURCE.get(o.source)
        if o.broker_order_id and mode:
            matched = rungs.get((mode, o.broker_order_id), [])
        out.append(
            replace(
                o,
                universe=universe.get(o.symbol),
                rung=matched[0] if len(matched) == 1 else None,
                duplicate_rows=dup.get((o.source, o.broker_order_id or ""), 1),
                duplicate_rungs=len(matched) if len(matched) > 1 else 0,
            )
        )
    return out


# --------------------------------------------------------------------------
# Candidate normalisation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """A source order that passed every row-level rule; may still be deduped."""

    order: SourceOrder
    d0: date
    expiry_at: datetime
    expiry_basis: str
    p: Decimal
    q: int
    filled_d0: int
    avg_fill_d0: Decimal | None
    remaining: int
    budget: Decimal
    budget_basis: str
    policy_version_at_order: str | None
    policy_hash_at_order: str | None


def _instrument_reasons(o: SourceOrder) -> list[str]:
    reasons: list[str] = []
    if _OCC_OPTION.match(o.symbol.replace(".", "")):
        reasons.append("option")
    if o.exchange and o.exchange.upper() not in {"NASD", "NYSE", "AMEX"}:
        reasons.append("non_listed_exchange")
    u = o.universe
    if u is None:
        reasons.append("instrument_unverified_no_universe_row")
        return reasons
    exchange = (u.exchange or "").upper()
    if any(marker in exchange for marker in _OTC_MARKERS):
        reasons.append("otc")
    if u.is_active is False or (u.listing_status or "").upper() in _INACTIVE_LISTING:
        reasons.append("inactive_listing")
    security_type = (u.security_type or "").upper()
    is_etf = "ETF" in security_type and "ETN" not in security_type
    is_common = u.is_common_stock is True or u.is_common_share is True
    if not (is_common or is_etf):
        reasons.append("not_common_stock_or_etf")
    if u.leverage_factor is not None and (
        u.leverage_factor < 0 or abs(u.leverage_factor) != 1
    ):
        reasons.append("leveraged_or_inverse_etf")
    elif (
        is_etf and u.leverage_factor is None and _LEVERAGE_NAME.search(u.name_en or "")
    ):
        reasons.append("leveraged_or_inverse_etf_by_name")
    return reasons


def _expiry(
    o: SourceOrder, d0: date, cal: USCalendar
) -> tuple[datetime | None, str, list[str]]:
    """DAY-expiry evidence. Returns (expiry instant, basis, reasons)."""

    status = (o.status or "").lower()
    filled = o.filled_qty or Decimal("0")
    session = cal.session(d0)
    if o.source == SOURCE_TOSS:
        if status == "expired" and o.expired_at is not None:
            nxt = cal.session(cal.offset(d0, 1))
            if not (session.close_utc <= o.expired_at < nxt.open_utc):
                return None, "", ["expiry_outside_d0_window"]
            return o.expired_at, "toss_expired_at", []
        if status == "expired":
            return None, "", ["expiry_timestamp_missing"]
    elif o.source == SOURCE_KIS_US:
        # KIS overseas ledger keeps no expiry instant. Only an unfilled expiry
        # is broker-proven (live_order_evidence: filled==0 and remaining==0);
        # a partial fill followed by the DAY sweep is not distinguishable.
        if status == "expired" and filled == 0:
            return session.close_utc, "kis_day_close_inferred", []
        if status == "expired":
            return None, "", ["kis_partial_expiry_unproven"]
        if status == "partial":
            return None, "", ["kis_partial_expiry_unproven"]
    if status == "filled":
        return None, "", ["no_residual_filled"]
    if status in {
        "cancelled",
        "rejected",
        "replaced",
        "cancel_rejected",
        "replace_rejected",
    }:
        return None, "", [f"not_day_expiry_{status}"]
    if status in {"accepted", "pending", "partial", ""}:
        return None, "", ["unreconciled"]
    return None, "", [f"unclear_status_{status}"]


# Deviation ids (manifest.DEVIATIONS) that relax a strict prereg default here.
DEV_POLICY_EPOCH = "D4_policy_epoch_invariant_basis"
DEV_OUTSIDE_SESSION = "D5_accept_outside_regular_session"
DEV_LEDGER_BUDGET = "D6_ledger_budget_basis"


def normalise(
    o: SourceOrder, cal: USCalendar, *, deviations: frozenset[str] = frozenset()
) -> tuple[Candidate | None, list[str]]:
    """Apply every row-level rule. Returns (candidate, reasons); reasons empty
    iff a candidate is returned.

    Strict by default (prereg §1): the order must be accepted inside a
    regular session, carry DAY evidence, an approved proposal rung (B_i) and
    an order-time policy stamp. Each relaxation needs an approved deviation.
    """

    reasons: list[str] = []
    if o.source == SOURCE_KIS_KR_TABLE:
        return None, ["unexpected_source_kis_live_order_ledger_equity_us"]
    if o.market not in {"us", "equity_us"}:
        reasons.append("not_us_market")
    if (o.currency or "").upper() != "USD":
        reasons.append("currency_not_usd" if o.currency else "currency_missing")
    if (o.side or "").lower() != "buy":
        reasons.append("not_buy")
    if (o.order_type or "").lower() != "limit":
        reasons.append("not_limit")
    if o.operation_kind and o.operation_kind.lower() != "place":
        reasons.append("not_place")
    if o.time_in_force and o.time_in_force.lower() != "day":
        reasons.append("not_day_tif")
    elif o.source == SOURCE_TOSS and not o.time_in_force:
        # Toss records the TIF it sent (DAY|CLS); absence is not DAY evidence.
        # KIS overseas limit orders (ORD_DVSN 00) are DAY-only by the API.
        reasons.append("day_tif_unproven")
    if o.price is None or o.quantity is None or o.ordered_at is None or not o.symbol:
        reasons.append("incomplete_ledger_row")
    if o.requires_manual_review or (o.status or "").lower() == "anomaly":
        reasons.append("unclear_ledger_manual_review")
    if o.duplicate_rows > 1 or o.duplicate_rungs > 1:
        reasons.append("duplicate_ledger_or_rung")
    reasons.extend(_instrument_reasons(o))
    if o.quantity is not None and (o.quantity <= 0 or not _is_integral(o.quantity)):
        reasons.append(
            "fractional_order" if o.quantity > 0 else "incomplete_ledger_row"
        )
    if o.price is not None and (o.price <= 0 or o.price % tick_size(o.price) != 0):
        reasons.append("price_off_tick")
    if reasons:
        return None, sorted(set(reasons))

    assert o.price is not None and o.quantity is not None and o.ordered_at is not None
    try:
        d0 = cal.working_session(o.ordered_at)
        cal.offset(d0, 1)
    except CalendarError:
        return None, ["calendar_unmappable"]
    session = cal.session(d0)
    if not (session.open_utc <= o.ordered_at < session.close_utc) and (
        DEV_OUTSIDE_SESSION not in deviations
    ):
        return None, ["accepted_outside_regular_session"]
    expiry_at, basis, expiry_reasons = _expiry(o, d0, cal)
    if expiry_reasons:
        return None, expiry_reasons
    assert expiry_at is not None

    filled = o.filled_qty or Decimal("0")
    if filled < 0 or not _is_integral(filled):
        return None, ["fractional_fill"]
    if filled > o.quantity:
        return None, ["quantity_mismatch_filled_gt_ordered"]
    if filled > 0 and (o.avg_fill_price is None or o.avg_fill_price <= 0):
        return None, ["fill_price_missing"]
    q = int(o.quantity)
    remaining = q - int(filled)
    if remaining <= 0:
        return None, ["no_residual"]

    budget_basis = "ledger_price_x_quantity"
    budget = o.price * o.quantity
    rung = o.rung
    policy_version = policy_hash = None
    if rung is None and DEV_LEDGER_BUDGET not in deviations:
        return None, ["approved_budget_unavailable"]
    if rung is not None:
        policy_version = rung.order_policy_version
        policy_hash = rung.order_policy_content_hash
        if rung.limit_price is not None and rung.limit_price != o.price:
            return None, ["rung_price_mismatch"]
        if rung.quantity is not None and rung.quantity != o.quantity:
            return None, ["rung_quantity_mismatch"]
        if rung.notional is not None:
            budget, budget_basis = rung.notional, "rung_notional"
        elif rung.limit_price is not None and rung.quantity is not None:
            budget, budget_basis = (
                rung.limit_price * rung.quantity,
                "rung_limit_x_quantity",
            )
    if budget < o.price * o.quantity:
        return None, ["budget_below_order_notional"]
    if not (policy_version and policy_hash) and DEV_POLICY_EPOCH not in deviations:
        return None, ["order_policy_stamp_missing"]
    return (
        Candidate(
            order=o,
            d0=d0,
            expiry_at=expiry_at,
            expiry_basis=basis,
            p=o.price,
            q=q,
            filled_d0=int(filled),
            avg_fill_d0=o.avg_fill_price if filled > 0 else None,
            remaining=remaining,
            budget=budget,
            budget_basis=budget_basis,
            policy_version_at_order=policy_version,
            policy_hash_at_order=policy_hash,
        ),
        [],
    )


# --------------------------------------------------------------------------
# Episode dedupe
# --------------------------------------------------------------------------


def episode_id_for(anchor: SourceOrder) -> str:
    return "ep-" + hashlib.sha256(anchor.source_ref.encode()).hexdigest()[:16]


@dataclass(slots=True)
class Episode:
    episode_id: str
    symbol: str
    anchor: SourceOrder
    anchor_price: Decimal
    anchor_session: date
    last_session: date
    members: list[SourceOrder] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EpisodeAssignment:
    episodes: dict[str, Episode]
    episode_of: dict[str, str]  # source_ref -> episode_id
    quarantined: dict[str, str]  # source_ref -> reason


def build_episodes(orders: list[SourceOrder], cal: USCalendar) -> EpisodeAssignment:
    """Group consecutive same-symbol buys into parent episodes.

    Rule (prereg §1, v1 definition): an order joins the symbol's latest
    episode iff its working session is at most 5 US sessions after the
    episode's previous order AND its limit is within +-5% of the episode's
    first anchor. Rungs of one proposal always share the episode (ladder
    children). An order without an anchor price or a calendar mapping is
    quarantined, never counted as a new parent.
    """

    episodes: dict[str, Episode] = {}
    episode_of: dict[str, str] = {}
    quarantined: dict[str, str] = {}
    by_root: dict[str, str] = {}
    latest_by_symbol: dict[str, str] = {}

    def sort_key(o: SourceOrder) -> tuple:
        return (
            o.symbol,
            o.ordered_at or datetime.max.replace(tzinfo=UTC),
            o.source_ref,
        )

    for o in sorted(orders, key=sort_key):
        if o.ordered_at is None:
            quarantined[o.source_ref] = "anchor_time_missing"
            continue
        try:
            session = cal.working_session(o.ordered_at)
        except CalendarError:
            quarantined[o.source_ref] = "calendar_unmappable"
            continue
        root = o.rung.proposal_root if o.rung else None
        if root and root in by_root:
            ep = episodes[by_root[root]]
            if ep.symbol == o.symbol:
                ep.members.append(o)
                ep.last_session = max(ep.last_session, session)
                episode_of[o.source_ref] = ep.episode_id
                continue
        if o.price is None or o.price <= 0:
            quarantined[o.source_ref] = "anchor_price_missing"
            continue
        latest = episodes.get(latest_by_symbol.get(o.symbol, ""))
        if (
            latest is not None
            and cal.gap(latest.last_session, session) <= K.DEDUPE_MAX_SESSION_GAP
            and abs(o.price / latest.anchor_price - 1) <= K.DEDUPE_ANCHOR_BAND
        ):
            latest.members.append(o)
            latest.last_session = max(latest.last_session, session)
            episode_of[o.source_ref] = latest.episode_id
            if root:
                by_root[root] = latest.episode_id
            continue
        ep = Episode(
            episode_id=episode_id_for(o),
            symbol=o.symbol,
            anchor=o,
            anchor_price=o.price,
            anchor_session=session,
            last_session=session,
            members=[o],
        )
        episodes[ep.episode_id] = ep
        episode_of[o.source_ref] = ep.episode_id
        latest_by_symbol[o.symbol] = ep.episode_id
        if root:
            by_root[root] = ep.episode_id
    return EpisodeAssignment(
        episodes=episodes, episode_of=episode_of, quarantined=quarantined
    )
