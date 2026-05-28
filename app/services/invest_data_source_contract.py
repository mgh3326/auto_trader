"""/invest data-source contract (ROB-340).

Single typed source of truth for *how /invest data flows*: which product
surface reads which DB/read-model, at what trust tier, with what freshness/
fetch policy, and whether a source may influence buy/sell ranking.

This module is **declarative and dependency-free**. It does NOT import the
runtime collector registry, broker clients, or ``Settings`` — the contract
describes intent; enforcement lives elsewhere:

  - Collector wiring / mutation-path blocking:
      app/services/action_report/snapshot_backed/collectors/registry.py
      (``production_collector_registry``; read-only adapters block order paths)
  - Freshness / stale / coverage semantics:
      app/services/action_report/common/{stale_gate,diagnostics,snapshot_bundle}.py
  - Report-evidence freeze:
      investment_snapshots (immutable-ish bundle; NOT a Naver/Toss cache)

Two invariants are enforced by tests (tests/test_invest_data_source_contract.py),
NOT by this module, so the contract stays cheap to read:

  1. AUTHORITY-MIXING GUARD (the load-bearing rule). Toss/Naver-derived sources
     must never be ``authority_tier="primary"`` and must have
     ``may_affect_ranking=False``. KIS live remains the sole authority for
     account/orderability; Toss/Naver are supplementary cross-check only.
  2. BIDIRECTIONAL DRIFT GUARD. The set of ``collector_snapshot_kind`` values
     present here equals ``production_collector_registry(...).list_kinds()``.
     Adding/removing a runtime collector without updating this contract — or
     declaring a collector here that does not run — fails CI.

Note on ``collector_snapshot_kind`` vs ``authority_tier``: every registered
collector gets an entry, *including* the fail-open Naver/Toss/browser stubs.
Those stubs are collector-wired (so they participate in the drift guard) but
are ``low_trust_attention`` (so the authority guard still pins them down). The
drift link is "is this wired to a runtime collector", not "is this canonical".

Note on ``surface``: the 5 values are the /invest product pages. Several
collectors freeze report-internal evidence (market events, the invest-page
snapshot, browser probe) rather than back a single product page; those map to
``surface="reports"``. This mapping is a judgment call — adjust the entry's
``surface`` if a collector's home page changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# --- Controlled vocabularies -------------------------------------------------

# /invest product surfaces. Aspirational route names map to the live routes:
#   news    -> /trading/.../invest/feed/news
#   screener-> /trading/.../invest/screener/results
#   stocks  -> /trading/.../invest/stock-detail/{market}/{symbol}
#   my      -> /trading/.../invest/account-panel
#   reports -> /trading/.../invest/reports (consumer of frozen evidence)
Surface = Literal["news", "screener", "stocks", "my", "reports"]

# Trust tier. ``primary`` is authoritative; ``supplementary`` enriches/cross-
# checks; ``low_trust_attention`` is retail-attention signal only (Toss/Naver).
AuthorityTier = Literal["primary", "supplementary", "low_trust_attention"]

# When a value is fetched relative to the report-generation path.
#   pre_collected        - accumulated by background jobs; read DB-only in path
#   report_time_on_demand- bounded fresh fetch at report time, then frozen
#   never_request_path   - must never be fetched during a request/report path
#   frozen_in_bundle     - consumed as already-frozen report evidence
FetchPolicy = Literal[
    "pre_collected",
    "report_time_on_demand",
    "never_request_path",
    "frozen_in_bundle",
]

# Korean-UI label used when a value is unavailable/unverified. ``확인 불가`` is
# the canonical "could not verify" copy for unverified items.
UnavailableLabel = Literal["확인 불가", "stale", "unavailable", "partial"]


@dataclass(frozen=True, slots=True)
class DataSourceContractEntry:
    """One (surface, source) row of the /invest data-source contract."""

    surface: Surface
    source_name: str
    authority_tier: AuthorityTier
    reusable_table: str | None  # DB table / read-model; None = live/no durable table
    fetch_policy: FetchPolicy
    # Concrete TTL (seconds) is deferred (ROB-340 follow-up). ``None`` means
    # "policy locked, value TBD" — intentionally non-blocking for PR1.
    freshness_ttl: int | None
    fallback_source: str | None
    may_affect_ranking: bool
    unavailable_label: UnavailableLabel
    # Set when this source is wired to a runtime snapshot collector; drives the
    # bidirectional drift guard. ``None`` for ingestion/fill sources that feed a
    # durable table but are not themselves a registered collector.
    collector_snapshot_kind: str | None = None


# --- The contract ------------------------------------------------------------
#
# Collector-wired entries (collector_snapshot_kind set) MUST cover exactly the
# kinds registered by production_collector_registry. The 12 below mirror it:
#   portfolio, journal, watch_context, market, news, symbol,
#   candidate_universe, invest_page, pending_orders,
#   naver_remote_debug, toss_remote_debug, browser_probe
# Non-collector entries (collector_snapshot_kind=None) document fill/fallback
# and durable read-models the report path reads but no collector owns yet.

INVEST_DATA_SOURCE_CONTRACT: tuple[DataSourceContractEntry, ...] = (
    # --- my (account truth = KIS live; Toss = cross-check only) ---
    DataSourceContractEntry(
        surface="my",
        source_name="kis_live",
        authority_tier="primary",
        reusable_table=None,  # live broker read; frozen into bundle at report time
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=True,
        unavailable_label="확인 불가",
        collector_snapshot_kind="portfolio",
    ),
    DataSourceContractEntry(
        surface="my",
        source_name="kis_live",
        authority_tier="primary",
        reusable_table=None,
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="확인 불가",
        collector_snapshot_kind="pending_orders",
    ),
    DataSourceContractEntry(
        surface="my",
        source_name="trade_journal_db",
        authority_tier="primary",
        reusable_table="trade_journal",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind="journal",
    ),
    DataSourceContractEntry(
        surface="my",
        source_name="watchlist_db",
        authority_tier="primary",
        reusable_table="watch_context",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind="watch_context",
    ),
    DataSourceContractEntry(
        surface="my",
        source_name="toss_screen",
        authority_tier="low_trust_attention",
        reusable_table=None,  # short-TTL cross-check; account_screen_* table is follow-up
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="확인 불가",
        collector_snapshot_kind="toss_remote_debug",
    ),
    # --- news (news_ingestor primary; Naver fill = supplementary) ---
    DataSourceContractEntry(
        surface="news",
        source_name="news_ingestor",
        authority_tier="primary",
        reusable_table="news_articles",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source="naver_finance",
        may_affect_ranking=True,
        unavailable_label="unavailable",
        collector_snapshot_kind="news",
    ),
    DataSourceContractEntry(
        surface="news",
        source_name="naver_finance",
        authority_tier="supplementary",
        reusable_table="news_articles",  # fill path inserts/upserts here, not request-path scraping
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="확인 불가",
        collector_snapshot_kind=None,  # ingestion source; no dedicated collector (follow-up)
    ),
    # --- screener (durable snapshots primary; Naver rank = follow-up) ---
    DataSourceContractEntry(
        surface="screener",
        source_name="invest_screener_snapshots",
        authority_tier="primary",
        reusable_table="invest_screener_snapshots",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=True,
        unavailable_label="stale",
        collector_snapshot_kind="candidate_universe",
    ),
    DataSourceContractEntry(
        surface="screener",
        source_name="investor_flow_snapshots",
        authority_tier="supplementary",
        reusable_table="investor_flow_snapshots",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=True,
        unavailable_label="stale",
        collector_snapshot_kind=None,  # read-model; folded into candidate_universe / future collector
    ),
    DataSourceContractEntry(
        surface="screener",
        source_name="upbit_live",
        authority_tier="primary",
        reusable_table="invest_crypto_screener_snapshots",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=True,
        unavailable_label="stale",
        collector_snapshot_kind=None,  # crypto screener read-model; collector is follow-up
    ),
    # --- stocks (KIS live quote primary; metadata pre-collected; Naver cross-check) ---
    DataSourceContractEntry(
        surface="stocks",
        source_name="kis_live",
        authority_tier="primary",
        reusable_table=None,  # quote/orderbook live; short TTL, frozen at report time
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source="stock_info",
        may_affect_ranking=True,
        unavailable_label="확인 불가",
        collector_snapshot_kind="symbol",
    ),
    DataSourceContractEntry(
        surface="stocks",
        source_name="stock_info",
        authority_tier="primary",
        reusable_table="stock_info",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind=None,  # metadata/valuation read paths; no dedicated collector
    ),
    DataSourceContractEntry(
        surface="stocks",
        source_name="naver_finance",
        authority_tier="low_trust_attention",
        reusable_table=None,
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="확인 불가",
        collector_snapshot_kind="naver_remote_debug",
    ),
    # --- reports (consumer of frozen evidence + report-internal collectors) ---
    DataSourceContractEntry(
        surface="reports",
        source_name="investment_snapshots",
        authority_tier="primary",
        reusable_table="investment_snapshots",
        fetch_policy="frozen_in_bundle",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind=None,  # report evidence freeze; not a collector
    ),
    DataSourceContractEntry(
        surface="reports",
        source_name="market_events_db",
        authority_tier="primary",
        reusable_table="market_events",
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind="market",  # MarketEventsSnapshotCollector (events, not quotes)
    ),
    DataSourceContractEntry(
        surface="reports",
        source_name="invest_page_db",
        authority_tier="supplementary",
        reusable_table=None,
        fetch_policy="pre_collected",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="unavailable",
        collector_snapshot_kind="invest_page",
    ),
    DataSourceContractEntry(
        surface="reports",
        source_name="browser_probe",
        authority_tier="low_trust_attention",
        reusable_table=None,
        fetch_policy="report_time_on_demand",
        freshness_ttl=None,
        fallback_source=None,
        may_affect_ranking=False,
        unavailable_label="확인 불가",
        collector_snapshot_kind="browser_probe",
    ),
)


# --- Accessors ---------------------------------------------------------------


def entries_for_surface(surface: Surface) -> tuple[DataSourceContractEntry, ...]:
    """All contract entries for one product surface."""
    return tuple(e for e in INVEST_DATA_SOURCE_CONTRACT if e.surface == surface)


def entries_for_authority(
    tier: AuthorityTier,
) -> tuple[DataSourceContractEntry, ...]:
    """All contract entries at one authority tier."""
    return tuple(e for e in INVEST_DATA_SOURCE_CONTRACT if e.authority_tier == tier)


def collector_wired_kinds() -> set[str]:
    """``snapshot_kind`` values wired to a runtime collector.

    Compared for equality against
    ``production_collector_registry(...).list_kinds()`` by the drift guard.
    """
    return {
        e.collector_snapshot_kind
        for e in INVEST_DATA_SOURCE_CONTRACT
        if e.collector_snapshot_kind is not None
    }


def render_contract_matrix_markdown() -> str:
    """Render the contract as a deterministic GitHub-flavored markdown table.

    This is the single source for the matrix table embedded in
    ``docs/invest/data-source-contract.md``; a diff-test asserts the doc's
    generated block equals this output, so the doc cannot drift from code.
    Rows are sorted (surface, source_name, collector_snapshot_kind) for a
    stable diff regardless of declaration order.
    """

    def cell(value: object) -> str:
        if value is None:
            return "—"
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return str(value)

    header = (
        "| surface | source | authority | table | fetch_policy | "
        "freshness_ttl | may_affect_ranking | unavailable | collector |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|"
    rows = [
        "| "
        + " | ".join(
            cell(v)
            for v in (
                e.surface,
                e.source_name,
                e.authority_tier,
                e.reusable_table,
                e.fetch_policy,
                e.freshness_ttl,
                e.may_affect_ranking,
                e.unavailable_label,
                e.collector_snapshot_kind,
            )
        )
        + " |"
        for e in sorted(
            INVEST_DATA_SOURCE_CONTRACT,
            key=lambda e: (e.surface, e.source_name, e.collector_snapshot_kind or ""),
        )
    ]
    return "\n".join([header, separator, *rows])


__all__ = [
    "INVEST_DATA_SOURCE_CONTRACT",
    "AuthorityTier",
    "DataSourceContractEntry",
    "FetchPolicy",
    "Surface",
    "UnavailableLabel",
    "collector_wired_kinds",
    "entries_for_authority",
    "entries_for_surface",
    "render_contract_matrix_markdown",
]
