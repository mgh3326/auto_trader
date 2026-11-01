"""Resolve delist date, terminal outcome and identity for each target symbol.

Target set ("delisted" denominator): corpus symbols that are absent from
today's public KIS master files, which are the same source the
``kr_symbol_universe`` table is synced from.  Resolution order per symbol:

1. KIND 상장폐지현황 row with the exact six-character code;
2. KRX ``상장폐지`` notice body (KIND disclosure) naming the exact code;
3. the parent common share's KIND row on the same session (preferred shares
   delisted together with their company).

A resolved date must agree with the corpus calendar: for a symbol whose
membership ended inside the main scope, the delist date must be the next
corpus session after its last membership session.  Disagreement is never
"fixed"; the row stays unresolved with the reason.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from research.kr_delist_repair.model import (
    DateBasis,
    KindDelistRow,
    KindDisclosure,
    PayoutSemantics,
    TerminalOutcome,
)
from research.kr_delist_repair.outcome import classify, is_preferred

KST = ZoneInfo("Asia/Seoul")
KIND_DELIST_URL = (
    "https://kind.krx.co.kr/investwarn/delcompany.do?method=searchDelCompanyMain"
)
KIND_VIEWER_URL = (
    "https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={acptno}"
)


@dataclass(frozen=True)
class Presence:
    """One contiguous run of KRX membership sessions for a code (an issue segment).

    A code whose membership stops and later restarts (code reuse after a
    delisting) has two segments; each is its own listing.
    """

    symbol: str
    first_session: date
    last_session: date
    markets: tuple[str, ...]
    has_bars: bool
    segment: int = 0

    @property
    def listing_id(self) -> str:
        return listing_id(self.symbol, self.segment)


def listing_id(symbol: str, segment: int) -> str:
    return f"{symbol}#{segment}"


@dataclass(frozen=True)
class Activity:
    """Per-symbol trading activity from the corpus (valid bars + no-trade days)."""

    last_traded_session: date | None
    no_trade_sessions: int
    no_trade_in_final_20: int
    longest_no_trade_run: int
    last_close: int | None


@dataclass
class Resolution:
    listing_id: str
    symbol: str
    segment: int
    name: str
    markets: str
    in_bar_frame: bool
    corpus_first_session: str
    corpus_last_session: str
    alive_at_main_end: bool
    listing_date: str
    listing_date_basis: str
    delist_date: str = ""
    date_basis: str = ""
    delist_reason: str = ""
    terminal_outcome: str = ""
    payout_semantics: str = ""
    corporate_action: str = ""
    identity_note: str = ""
    parent_symbol: str = ""
    last_traded_session: str = ""
    no_trade_sessions: int = 0
    no_trade_in_final_20: int = 0
    longest_no_trade_run: int = 0
    corpus_date_check: str = ""
    source: str = ""
    source_ref: str = ""
    information_available_at: str = ""
    resolved: bool = False
    unresolved_reason: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def row(self) -> dict[str, object]:
        out = asdict(self)
        out.pop("extra")
        return out


_CORPORATE_ACTION = {
    TerminalOutcome.MERGED_ABSORBED: "merger",
    TerminalOutcome.SPAC_MERGER: "spac_merger",
    TerminalOutcome.SHARE_EXCHANGE: "share_exchange_or_transfer",
    TerminalOutcome.PREFERRED_CONVERSION: "conversion_to_common",
    TerminalOutcome.DISSOLUTION_MERGER_OR_LIQUIDATION: "dissolution",
    TerminalOutcome.FUND_TERM_EXPIRY: "fund_liquidation",
}


def next_session(calendar: list[date], session: date) -> date | None:
    for d in calendar:
        if d > session:
            return d
    return None


def _kst_midnight(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=KST).isoformat()


def _filed_at(filed: str) -> str:
    return datetime.strptime(filed, "%Y-%m-%d %H:%M").replace(tzinfo=KST).isoformat()


def resolve_all(
    *,
    targets: list[Presence],
    calendar: list[date],
    activity: dict[str, Activity],
    kind_rows: list[KindDelistRow],
    disclosures: list[KindDisclosure],
    master_names: dict[str, str],
) -> list[Resolution]:
    main_end = calendar[-1]
    kind_by_symbol: dict[str, list[KindDelistRow]] = defaultdict(list)
    for row in kind_rows:
        kind_by_symbol[row.symbol].append(row)
    disc_by_symbol: dict[str, list[KindDisclosure]] = defaultdict(list)
    for d in disclosures:
        disc_by_symbol[d.symbol].append(d)

    out: list[Resolution] = []
    for p in sorted(targets, key=lambda x: (x.symbol, x.segment)):
        act = activity.get(p.listing_id, Activity(None, 0, 0, 0, None))
        alive = p.last_session == main_end
        listed_before = p.first_session == calendar[0]
        res = Resolution(
            listing_id=p.listing_id,
            symbol=p.symbol,
            segment=p.segment,
            name="",
            markets="|".join(p.markets),
            in_bar_frame=p.has_bars,
            corpus_first_session=p.first_session.isoformat(),
            corpus_last_session=p.last_session.isoformat(),
            alive_at_main_end=alive,
            listing_date="" if listed_before else p.first_session.isoformat(),
            listing_date_basis=(
                "listed_before_corpus_start"
                if listed_before
                else "krx_membership_first_session"
            ),
            last_traded_session=(
                act.last_traded_session.isoformat() if act.last_traded_session else ""
            ),
            no_trade_sessions=act.no_trade_sessions,
            no_trade_in_final_20=act.no_trade_in_final_20,
            longest_no_trade_run=act.longest_no_trade_run,
        )
        expected = None if alive else next_session(calendar, p.last_session)

        floor = main_end if alive else p.last_session

        # A market-transfer row is only used when no real delisting row exists.
        chosen_kind = sorted(
            (r for r in kind_by_symbol.get(p.symbol, []) if r.delist_date > floor),
            key=lambda r: (
                classify(r.reason, name=r.name, symbol=p.symbol)[0]
                is TerminalOutcome.MARKET_TRANSFER,
                r.delist_date,
            ),
        )
        chosen_disc = sorted(
            (d for d in disc_by_symbol.get(p.symbol, []) if d.delist_date > floor),
            key=lambda d: d.delist_date,
        )
        parent = p.symbol[:5] + "0"
        parent_rows = (
            [r for r in kind_by_symbol.get(parent, []) if r.delist_date == expected]
            if is_preferred(p.symbol) and expected is not None
            else []
        )

        if chosen_kind:
            k = chosen_kind[0]
            res.name = k.name
            res.delist_date = k.delist_date.isoformat()
            res.date_basis = DateBasis.KIND_DELIST_LIST
            res.delist_reason = k.reason
            res.source = f"KIND 상장폐지현황 ({k.market})"
            res.source_ref = KIND_DELIST_URL
            res.information_available_at = _kst_midnight(k.delist_date)
            if k.note and k.note != k.name:
                res.identity_note = f"kind_note:{k.note}"
            outcome, payout = classify(k.reason, name=k.name, symbol=p.symbol)
        elif chosen_disc:
            d = chosen_disc[0]
            res.name = d.issue_name
            res.delist_date = d.delist_date.isoformat()
            res.date_basis = DateBasis.KIND_DISCLOSURE_BODY
            res.delist_reason = d.reason
            res.source = "KIND KRX 상장폐지 notice"
            res.source_ref = KIND_VIEWER_URL.format(acptno=d.acptno)
            res.information_available_at = _filed_at(d.filed_at)
            outcome, payout = classify(d.reason, name=d.issue_name, symbol=p.symbol)
        elif parent_rows:
            k = parent_rows[0]
            res.name = f"{k.name} (preferred)"
            res.parent_symbol = parent
            res.delist_date = k.delist_date.isoformat()
            res.date_basis = DateBasis.KIND_PARENT_ROW
            res.delist_reason = f"parent {parent}: {k.reason}"
            res.source = f"KIND 상장폐지현황 ({k.market}) parent row"
            res.source_ref = KIND_DELIST_URL
            res.information_available_at = _kst_midnight(k.delist_date)
            outcome, payout = classify(k.reason, name=k.name, symbol=parent)
        else:
            res.name = master_names.get(parent, "")
            res.unresolved_reason = (
                "no KIND list row, no KRX notice body and no same-session parent row"
            )
            out.append(res)
            continue

        res.terminal_outcome = outcome.value
        res.payout_semantics = payout.value
        res.corporate_action = _CORPORATE_ACTION.get(outcome, "")
        if len(p.markets) > 1:
            res.identity_note = "|".join(
                x for x in (res.identity_note, "markets:" + "+".join(p.markets)) if x
            )
        when = date.fromisoformat(res.delist_date)
        if alive:
            res.corpus_date_check = "not_checkable_main_scope_alive"
        elif when == expected:
            res.corpus_date_check = "equals_next_session"
        else:
            res.corpus_date_check = f"conflict_expected_{expected}"

        if outcome is TerminalOutcome.UNCLASSIFIED:
            res.unresolved_reason = (
                f"reason not in closed vocabulary: {res.delist_reason}"
            )
        elif outcome is TerminalOutcome.MARKET_TRANSFER:
            res.unresolved_reason = (
                "only a market-transfer row; no terminal event found"
            )
        elif res.corpus_date_check.startswith("conflict"):
            res.unresolved_reason = (
                f"delist date {res.delist_date} disagrees with corpus calendar"
            )
        else:
            res.resolved = True
        out.append(res)
    return out


def payout_counts(rows: list[Resolution]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.resolved:
            counts[r.payout_semantics or PayoutSemantics.AMBIGUOUS.value] += 1
    return dict(sorted(counts.items()))
