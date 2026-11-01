"""Closed vocabulary and record shapes for the KR delisting repair."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class TerminalOutcome(StrEnum):
    """Why a listing ended.  ``UNCLASSIFIED`` counts as unresolved."""

    MERGED_ABSORBED = "merged_absorbed"
    SPAC_MERGER = "spac_merger"
    SHARE_EXCHANGE = "share_exchange"
    VOLUNTARY = "voluntary"
    STATUTORY_CAUSE = "statutory_cause"
    SPAC_DEADLINE = "spac_deadline"
    DISSOLUTION_MERGER_OR_LIQUIDATION = "dissolution_merger_or_liquidation"
    FUND_TERM_EXPIRY = "fund_term_expiry"
    PREFERRED_CONVERSION = "preferred_conversion"
    PREFERRED_FLOAT = "preferred_float"
    MARKET_TRANSFER = "market_transfer"
    UNCLASSIFIED = "unclassified"


class PayoutSemantics(StrEnum):
    """What a holder of the last session's close plausibly received.

    This is a labelling hint, not a payout amount.  No payout value is
    asserted anywhere in this package.
    """

    CONSIDERATION = "consideration_shares_or_cash"
    TENDER_OR_LAST_PRICE = "tender_or_last_price"
    RESIDUAL_AFTER_LIQUIDATION_TRADING = "residual_after_liquidation_trading"
    TRUST_RETURN = "trust_return"
    CONVERTED_TO_COMMON = "converted_to_common"
    CONTINUES_LISTED = "continues_listed"
    AMBIGUOUS = "ambiguous"


class DateBasis(StrEnum):
    KIND_DELIST_LIST = "kind_delist_list"
    KIND_DISCLOSURE_BODY = "kind_disclosure_body"
    KIND_PARENT_ROW = "kind_parent_row"


class AvailabilityViolation(RuntimeError):
    """A join or read would use a row before its ``available_at``."""


class ExportConflict(RuntimeError):
    """Inputs contradict each other; the export refuses to guess."""


@dataclass(frozen=True)
class KindDelistRow:
    """One row of the KIND 상장폐지현황 Excel export."""

    market: str
    symbol: str
    name: str
    delist_date: date
    reason: str
    note: str


@dataclass(frozen=True)
class KindDisclosure:
    """One KRX market-action disclosure body: ``상장폐지`` for one issue."""

    acptno: str
    symbol: str
    issue_name: str
    delist_date: date
    reason: str
    filed_at: str
