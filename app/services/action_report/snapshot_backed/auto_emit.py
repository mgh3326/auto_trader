"""ROB-278 Phase 2 — deterministic evidence-driven report-item proposer.

Reads the persisted snapshot bundle and proposes ``IngestReportItem``
drafts for the report generator. This proposer is **deterministic**
and uses Phase 2 collector enrichments (portfolio v2, symbol
quote/orderbook, candidate_universe usefulness, news symbol citations)
to gate candidate emission. Per ROB-287 it is the only in-process
report-item writer; LLM reasoning/composition lives in Hermes and
ingests through the dedicated Hermes-result path. The two paths are
not co-mingled against the same bundle.

Lockdown invariants:

* Every emitted item is ``operation="review"`` +
  ``apply_policy="requires_user_approval"``. The proposer never
  pre-classifies anything as auto-executable.
* Buy candidates are emitted only when the per-symbol quote evidence
  reports ``status="ok"`` with non-zero best bid + best ask + depth.
  Missing or unavailable quote evidence → no buy candidate.
* Sell candidates are emitted only when the portfolio snapshot has
  ``primary_source="kis"`` and the held row reports a positive
  ``sellable_quantity``. Manual/reference holdings never produce sell
  candidates here.
* Watch candidates are emitted when candidate/news/quote evidence
  exists but action grounds are insufficient — these surface as review
  items so the operator can validate the thesis.
* Each emitted item carries ``evidence_snapshot`` provenance pointing
  to the source snapshot UUID(s) + kind(s) so an audit can trace the
  proposed candidate back to the bundle rows that justified it.
* No broker / order / watch / order-intent mutation paths are reached;
  the static import guard test continues to assert this.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.schemas.investment_reports import IngestReportItem
from app.schemas.validated_run_card import (
    build_run_card_citation,
    build_run_card_evidence,
)
from app.services.action_report.snapshot_backed.action_verdict import (
    VERDICT_TO_BUCKET,
    classify_candidate_symbol,
    classify_held_symbol,
    demote_for_budget,
    demote_for_quality,
)
from app.services.market_events.catalyst.contract import CatalystEvent
from app.services.market_events.catalyst.guard import evaluate_catalyst_guard
from app.services.market_events.catalyst.polarity import (
    CATALYST_CATEGORIES,
    resolve_polarity,
)

_KST = ZoneInfo("Asia/Seoul")

_REASON_KO: dict[str, str] = {
    "low_liquidity": "저유동성",
    "beyond_candidate_budget": "후보 예산 초과",
    "screener_stale": "스크리너 stale",
    "penny": "저가주",
    "illiquid": "초저유동성",
    "abnormal_spike": "비정상 급등",
    "non_common_stock": "일반주 아님(ETF/우선주 등)",
    "common_stock_unknown": "종목 분류 미확인",
    "quote_missing": "호가 스냅샷 없음",
    "budget_gap": "신규매수 예산 부족",
    "fx_required": "달러(USD) 환전 필요",
    "operator_budget_required": "운영자 예산 설정 필요",
}
CATALYST_GUARD_WITHIN_DAYS = 7


def _stamp(item: IngestReportItem, verdict: str) -> IngestReportItem:
    """Attach the ActionPacket sub-verdict + its locked decision_bucket."""
    item.evidence_snapshot["action_verdict"] = verdict
    item.decision_bucket = VERDICT_TO_BUCKET[verdict]
    return item


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    payload = getattr(snapshot, "payload_json", None)
    return payload if isinstance(payload, dict) else {}


def _snapshot_uuid(snapshot: Any) -> str | None:
    value = getattr(snapshot, "snapshot_uuid", None)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _make_evidence(
    snapshot: Any, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": "auto_emit",
        "snapshot_kind": getattr(snapshot, "snapshot_kind", None),
        "snapshot_uuid": _snapshot_uuid(snapshot),
        "symbol": getattr(snapshot, "symbol", None),
    }
    if extra:
        evidence.update(extra)
    return evidence


def _catalyst_events_for_symbol(
    market_payload: dict[str, Any] | None,
    symbol: str,
    *,
    now_date: dt.date,
    within_days: int,
) -> list[CatalystEvent]:
    """frozen market 스냅샷 events → 해당 symbol의 catalyst CatalystEvent 리스트.

    category ∈ CATALYST_CATEGORIES, event_date ∈ [now_date, now_date+within_days].
    frozen 이벤트엔 raw_payload 없음 → polarity는 category-default. 파싱 실패는 skip.
    """
    if not market_payload:
        return []
    events = market_payload.get("events") or []
    horizon = now_date + dt.timedelta(days=within_days)
    out: list[CatalystEvent] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("symbol") != symbol:
            continue
        category = ev.get("category")
        if category not in CATALYST_CATEGORIES:
            continue
        raw_date = ev.get("event_date")
        try:
            event_date = (
                raw_date
                if isinstance(raw_date, dt.date)
                else dt.date.fromisoformat(str(raw_date))
            )
        except ValueError:
            continue
        if not (now_date <= event_date <= horizon):
            continue
        out.append(
            CatalystEvent(
                symbol=symbol,
                category=category,
                title=ev.get("title"),
                event_date=event_date,
                days_until=(event_date - now_date).days,
                polarity=resolve_polarity(category, None),
                source=ev.get("source"),
            )
        )
    out.sort(key=lambda e: (e.days_until, e.category))
    return out


def _catalyst_brief(e: CatalystEvent) -> dict[str, Any]:
    return {
        "symbol": e.symbol,
        "category": e.category,
        "event_date": e.event_date.isoformat(),
        "days_until": e.days_until,
    }


def _attach_catalyst_guard(
    item: IngestReportItem,
    *,
    market_payload: dict[str, Any] | None,
    side: str,
    now_date: dt.date,
    within_days: int = CATALYST_GUARD_WITHIN_DAYS,
) -> None:
    """item.symbol의 frozen catalyst에 가드 적용 — flag 있으면 evidence_snapshot에 부착.
    verdict/side/intent 불변(경고만)."""
    symbol = item.symbol
    if not symbol or not market_payload:
        return
    events = _catalyst_events_for_symbol(
        market_payload, symbol, now_date=now_date, within_days=within_days
    )
    if not events:
        return
    guard = evaluate_catalyst_guard(events, side=side, within_days=within_days)
    if guard.flag is None:
        return
    item.evidence_snapshot["upcoming_catalyst"] = {
        "flag": guard.flag,
        "nearest_days": guard.nearest_days,
        "reason": guard.reason,
        "positive": [_catalyst_brief(e) for e in guard.positive],
        "negative": [_catalyst_brief(e) for e in guard.negative],
    }


def _quote_is_actionable(quote: dict[str, Any]) -> bool:
    if not isinstance(quote, dict):
        return False
    if quote.get("status") != "ok":
        return False
    best_bid = quote.get("best_bid") or 0
    best_ask = quote.get("best_ask") or 0
    bid_depth = quote.get("bid_depth") or 0
    ask_depth = quote.get("ask_depth") or 0
    return best_bid > 0 and best_ask > 0 and (bid_depth > 0 or ask_depth > 0)


def _held_kis_symbols(
    portfolio_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return KIS-primary held holdings keyed by ticker. Empty dict when
    primary_source is not 'kis' — manual rows are never promoted here."""
    if portfolio_payload.get("primary_source") != "kis":
        return {}
    holdings = portfolio_payload.get("holdings") or []
    out: dict[str, dict[str, Any]] = {}
    if isinstance(holdings, list):
        for holding in holdings:
            if not isinstance(holding, dict):
                continue
            ticker = holding.get("ticker")
            if isinstance(ticker, str):
                out[ticker] = holding
    return out


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_rank(candidate: dict[str, Any]) -> int | None:
    return _to_int(candidate.get("candidate_rank") or candidate.get("rank"))


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, float, str]:
    rank = _candidate_rank(candidate)
    score = _to_float(candidate.get("score"))
    symbol = str(candidate.get("symbol") or "")
    return (rank if rank is not None else 1_000_000, -(score or 0.0), symbol)


def _candidate_item(
    *,
    symbol_snapshot: Any,
    candidate_snapshot: Any,
    sym: str,
    cand: dict[str, Any],
    quote: dict[str, Any] | None,
    verdict: str,
    priority: int,
    reject_or_wait_reason: str | None,
    candidate_usefulness: str | None,
    news_match_count: int,
    candidate_universe_evidence: dict[str, Any] | None = None,
    budget_evidence: dict[str, Any] | None = None,
) -> IngestReportItem:
    is_buy = verdict == "buy_review"
    is_gap = verdict == "data_gap"
    q = quote if isinstance(quote, dict) else {}
    extra: dict[str, Any] = {
        "candidate_snapshot_uuid": _snapshot_uuid(candidate_snapshot),
        "candidate_usefulness": candidate_usefulness,
        "candidate_rank": priority,
        "candidate_score": cand.get("score"),
        "candidate_reasons": cand.get("reasons"),
        "candidate_source": cand.get("source"),
        # ROB-359 Scope E — screener evidence lineage so the report can show why
        # a new-buy candidate surfaced (preset hit / freshness / Toss parity).
        "candidate_source_preset": cand.get("source_preset"),
        "candidate_data_state": cand.get("data_state"),
        "candidate_toss_parity_status": cand.get("toss_parity_status"),
        # ROB-346 quality fields
        "quality_flags": cand.get("quality_flags"),
        "confidence_cap": cand.get("confidence_cap"),
        "candidate_priority_score": cand.get("priority_score"),
        "news_matches": news_match_count,
        "quote_status": q.get("status") if quote is not None else "no_snapshot",
        "best_bid": q.get("best_bid"),
        "best_ask": q.get("best_ask"),
        "spread_bps": q.get("spread_bps"),
        "proposer": (
            "auto_emit/buy_from_candidate"
            if is_buy
            else f"auto_emit/candidate_{verdict}"
        ),
    }
    if reject_or_wait_reason is not None:
        extra["reject_or_wait_reason"] = reject_or_wait_reason

    if candidate_universe_evidence:
        extra.update(candidate_universe_evidence)

    if budget_evidence:
        extra.update(budget_evidence)

    if is_buy:
        rationale = (
            f"신규 매수 검토 {priority}순위 — {sym} "
            f"(candidate {candidate_usefulness}, score {cand.get('score')}, "
            f"quote best_bid {q.get('best_bid')}, spread_bps {q.get('spread_bps')})"
        )
    elif is_gap:
        if reject_or_wait_reason == "common_stock_unknown":
            rationale = f"신규 후보 판단 보류 — {sym} (종목 분류 미확인)"
        else:
            rationale = f"신규 후보 판단 보류 — {sym} (호가 스냅샷 없음)"
    else:
        reason_ko = _REASON_KO.get(reject_or_wait_reason or "", "관망")
        rationale = f"신규 후보 관망 {priority}순위 — {sym} ({reason_ko})"

    return IngestReportItem(
        client_item_key=(f"auto-buy-{sym}" if is_buy else f"auto-cand-{verdict}-{sym}"),
        item_kind="action" if is_buy else "risk",
        symbol=sym,
        side="buy" if is_buy else None,
        intent=(
            "buy_review"
            if is_buy
            else "risk_review"
            if is_gap
            else "trend_recovery_review"
        ),
        priority=priority,
        rationale=rationale,
        operation="review",
        apply_policy="requires_user_approval",
        evidence_snapshot=_make_evidence(symbol_snapshot, extra=extra),
    )


class EvidenceAutoEmitter:
    """Deterministic proposer that surfaces review-only candidates from the
    persisted snapshot bundle."""

    def __init__(
        self, *, max_buy_candidates: int = 10, intraday_floor: bool = False
    ) -> None:
        self._max_buy_candidates = max(0, max_buy_candidates)
        self._intraday_floor = intraday_floor

    def propose(
        self,
        *,
        snapshots: list[Any],
        request_market: str,
        account_scope: str | None,
        budget_basis: str = "available_usd",
        operator_budget_override_usd: Any | None = None,
        now: dt.datetime | None = None,
    ) -> list[IngestReportItem]:
        """Emit review-only ``IngestReportItem`` drafts from the bundle's
        evidence. Returns an empty list when no evidence supports an
        actionable proposal — never fabricates candidates."""
        now_dt = now or dt.datetime.now(_KST)
        now_date = now_dt.astimezone(_KST).date() if now_dt.tzinfo else now_dt.date()
        portfolio_payload: dict[str, Any] = {}
        market_payload: dict[str, Any] = {}
        symbol_quotes: dict[str, tuple[Any, dict[str, Any]]] = {}
        news_matches: dict[str, int] = {}
        candidate_usefulness: str | None = None
        candidate_by_symbol: dict[str, dict[str, Any]] = {}
        candidate_order: list[dict[str, Any]] = []
        candidate_universe_evidence: dict[str, Any] = {}
        portfolio_snapshot: Any | None = None
        candidate_snapshot: Any | None = None
        news_snapshot: Any | None = None
        run_card_evidence_by_symbol: dict[str, dict[str, Any]] = {}

        for snapshot in snapshots:
            kind = getattr(snapshot, "snapshot_kind", None)
            payload = _snapshot_payload(snapshot)
            if kind == "portfolio":
                portfolio_snapshot = snapshot
                portfolio_payload = payload
            elif kind == "symbol":
                sym = getattr(snapshot, "symbol", None) or payload.get("symbol")
                quote = payload.get("quote")
                if isinstance(sym, str) and isinstance(quote, dict):
                    symbol_quotes[sym] = (snapshot, quote)
            elif kind == "candidate_universe":
                candidate_snapshot = snapshot
                candidate_usefulness = (
                    payload.get("usefulness") if isinstance(payload, dict) else None
                )
                candidate_universe_evidence = {
                    key: payload.get(key)
                    for key in (
                        "pool_size",
                        "displayed_count",
                        "candidate_limit",
                        "universe_count",
                        "capped",
                        "freshness_status",
                        "fresh_count",
                        "stale_count",
                        "expected_baseline_date",
                        "latest_partition_date",
                        "days_stale",
                    )
                    if isinstance(payload, dict) and key in payload
                }
                raw_candidates = (
                    payload.get("candidates", []) if isinstance(payload, dict) else []
                )
                for idx, cand in enumerate(raw_candidates, start=1):
                    if isinstance(cand, dict) and isinstance(cand.get("symbol"), str):
                        ranked_cand = dict(cand)
                        ranked_cand.setdefault("rank", idx)
                        ranked_cand.setdefault(
                            "candidate_rank", ranked_cand.get("rank")
                        )
                        candidate_by_symbol[ranked_cand["symbol"]] = ranked_cand
                        candidate_order.append(ranked_cand)
            elif kind == "market":
                market_payload = payload
            elif kind == "news":
                news_snapshot = snapshot
                matches = payload.get("symbol_matches") or {}
                if isinstance(matches, dict):
                    for sym, count in matches.items():
                        if isinstance(sym, str) and isinstance(count, int):
                            news_matches[sym] = count
            elif kind == "validated_run_card":
                snap_uuid = _snapshot_uuid(snapshot)
                citation = build_run_card_citation(payload)
                if snap_uuid is not None and citation.symbols:
                    evidence = build_run_card_evidence(
                        snapshot_uuid=snap_uuid, citation=citation
                    )
                    for sym in citation.symbols:
                        run_card_evidence_by_symbol.setdefault(sym, evidence)

        buying_power = portfolio_payload.get("buying_power") or {}
        budget_state = {
            "basis": budget_basis,
            "override_usd": _to_float(operator_budget_override_usd),
            "usd": _to_float(buying_power.get("usd")),
            "krw": _to_float(buying_power.get("krw")),
        }

        held = _held_kis_symbols(portfolio_payload)
        candidate_actionable = candidate_usefulness == "useful"

        items: list[IngestReportItem] = []

        # Sell candidates — held + sellable + quote evidence supports liquidity.
        for ticker, holding in held.items():
            sellable = holding.get("sellable_quantity") or 0
            if sellable <= 0:
                continue
            symbol_pair = symbol_quotes.get(ticker)
            if symbol_pair is None:
                continue
            symbol_snapshot, quote = symbol_pair
            if not _quote_is_actionable(quote):
                continue
            evidence = _make_evidence(
                symbol_snapshot,
                extra={
                    "portfolio_snapshot_uuid": _snapshot_uuid(portfolio_snapshot),
                    "sellable_quantity": sellable,
                    "quote_status": quote.get("status"),
                    "best_bid": quote.get("best_bid"),
                    "best_ask": quote.get("best_ask"),
                    "spread_bps": quote.get("spread_bps"),
                    "proposer": "auto_emit/sell_from_held",
                },
            )
            sell_item = _stamp(
                IngestReportItem(
                    client_item_key=f"auto-sell-{ticker}",
                    item_kind="action",
                    symbol=ticker,
                    side="sell",
                    intent="sell_review",
                    rationale=(
                        f"보유 종목 {ticker} sell 검토 — sellable {sellable}, "
                        f"best_bid {quote.get('best_bid')}, "
                        f"spread_bps {quote.get('spread_bps')}"
                    ),
                    operation="review",
                    apply_policy="requires_user_approval",
                    evidence_snapshot=evidence,
                ),
                "sell_review",
            )
            _attach_catalyst_guard(
                sell_item,
                market_payload=market_payload,
                side="trim",
                now_date=now_date,
            )
            items.append(sell_item)

        # Candidate classification — every non-held screener candidate gets
        # exactly ONE honest verdict (buy_review / watch_only / data_gap). No
        # candidate is silently dropped (ROB-350). Always-on whenever a
        # candidate_universe snapshot is present, independent of intraday_floor.
        buy_emitted = 0
        demoted_emitted = 0
        _MAX_DEMOTED_SHOWN = 10
        for cand in sorted(candidate_order, key=_candidate_sort_key):
            sym = cand.get("symbol")
            if not isinstance(sym, str) or sym in held:
                continue  # held names handled by held_and_trending below
            symbol_pair = symbol_quotes.get(sym)
            quote = symbol_pair[1] if symbol_pair else None
            base_verdict = classify_candidate_symbol(
                quote,
                universe_useful=candidate_actionable,
                quote_snapshot_present=symbol_pair is not None,
                candidate_fresh=(cand.get("data_state") or "fresh") == "fresh",
            )
            # ROB-346 — quality demotion (pure, no signature change).
            quality_flags = frozenset(cand.get("quality_flags") or [])
            verdict, reject_or_wait_reason = demote_for_quality(
                base_verdict, quality_flags
            )
            # ROB-347 — budget demotion (buy_review only; never fabricates USD).
            # Applies only to US market under kis_live account scope.
            if request_market == "us" and account_scope == "kis_live":
                verdict, budget_reasons = demote_for_budget(verdict, budget_state)
                if budget_reasons and reject_or_wait_reason is None:
                    reject_or_wait_reason = budget_reasons[0]
            else:
                budget_reasons = []

            if verdict == "data_gap" and reject_or_wait_reason is None:
                reject_or_wait_reason = "quote_missing"
            elif verdict == "watch_only" and reject_or_wait_reason is None:
                reject_or_wait_reason = (
                    "low_liquidity"
                    if symbol_pair is not None and not _quote_is_actionable(quote)
                    else "screener_stale"
                )
            elif verdict == "buy_review":
                if buy_emitted >= self._max_buy_candidates:
                    verdict = "watch_only"
                    reject_or_wait_reason = "beyond_candidate_budget"
                else:
                    buy_emitted += 1

            if verdict != "buy_review":
                if demoted_emitted >= _MAX_DEMOTED_SHOWN:
                    continue  # 노이즈 방지: 상위 N개 데모션만 카드화(집계는 candidate snapshot)
                demoted_emitted += 1

            budget_evidence = {
                "budget_basis": budget_state["basis"],
                "available_usd": budget_state["usd"],
                "krw_orderable_reference": budget_state["krw"],
                "operator_budget_override_usd": budget_state["override_usd"],
                "budget_reasons": budget_reasons,
                "budget_fit": verdict == "buy_review" and not budget_reasons,
            }

            candidate_rank = _candidate_rank(cand)
            priority = candidate_rank if candidate_rank is not None else buy_emitted
            cand_item = _stamp(
                _candidate_item(
                    symbol_snapshot=(
                        symbol_pair[0] if symbol_pair else candidate_snapshot
                    ),
                    candidate_snapshot=candidate_snapshot,
                    sym=sym,
                    cand=cand,
                    quote=quote,
                    verdict=verdict,
                    priority=priority,
                    reject_or_wait_reason=reject_or_wait_reason,
                    candidate_usefulness=candidate_usefulness,
                    news_match_count=news_matches.get(sym, 0),
                    candidate_universe_evidence=candidate_universe_evidence,
                    budget_evidence=budget_evidence,
                ),
                verdict,
            )
            if verdict == "buy_review":
                _attach_catalyst_guard(
                    cand_item,
                    market_payload=market_payload,
                    side="buy",
                    now_date=now_date,
                )
            items.append(cand_item)

        # Watch candidates — news-active symbols whose quote evidence is
        # missing or whose candidate evidence is stale/empty (action
        # grounds insufficient). Skip symbols already held + with sell
        # candidate emitted, and symbols already proposed as buy.
        already_proposed = {item.symbol for item in items if item.symbol}
        for sym, count in news_matches.items():
            if count <= 0:
                continue
            if sym in already_proposed:
                continue
            quote_pair = symbol_quotes.get(sym)
            quote_status = quote_pair[1].get("status") if quote_pair else "no_snapshot"
            if quote_pair is not None and _quote_is_actionable(quote_pair[1]):
                # Quote actionable but candidate usefulness wasn't useful —
                # caller's evidence is mixed; surface as watch review.
                if candidate_actionable:
                    continue
            evidence = _make_evidence(
                quote_pair[0] if quote_pair else news_snapshot,
                extra={
                    "news_snapshot_uuid": _snapshot_uuid(news_snapshot),
                    "news_match_count": count,
                    "quote_status": quote_status,
                    "candidate_usefulness": candidate_usefulness,
                    "proposer": "auto_emit/watch_from_news",
                },
            )
            items.append(
                _stamp(
                    IngestReportItem(
                        client_item_key=f"auto-watch-{sym}",
                        item_kind="watch",
                        symbol=sym,
                        intent="trend_recovery_review",
                        rationale=(
                            f"뉴스 관심 종목 watch 검토 — {sym} "
                            f"(news_matches {count}, quote_status {quote_status})"
                        ),
                        operation="review",
                        apply_policy="requires_user_approval",
                        evidence_snapshot=evidence,
                    ),
                    "watch_only",
                )
            )

        # Held-and-trending — held names that also surface in the screener
        # candidate universe. Review-only awareness signal (held names are
        # excluded from buy candidates above); no broker mutation.
        already_proposed = {item.symbol for item in items if item.symbol}
        for sym, cand in candidate_by_symbol.items():
            if sym not in held or sym in already_proposed:
                continue
            reasons = cand.get("reasons") or []
            candidate_rank = _candidate_rank(cand)
            priority = candidate_rank if candidate_rank is not None else 0
            items.append(
                _stamp(
                    IngestReportItem(
                        client_item_key=f"auto-hold-trend-{sym}",
                        item_kind="watch",
                        symbol=sym,
                        intent="trend_recovery_review",
                        priority=priority,
                        rationale=(
                            f"보유 종목 {sym}가 스크리너 추세 상위에 등장 — 관망/추가검토 "
                            f"(rank {priority}, score {cand.get('score')}, {', '.join(reasons)})"
                        ),
                        operation="review",
                        apply_policy="requires_user_approval",
                        evidence_snapshot=_make_evidence(
                            candidate_snapshot,
                            extra={
                                "candidate_snapshot_uuid": _snapshot_uuid(
                                    candidate_snapshot
                                ),
                                "candidate_rank": priority,
                                "candidate_score": cand.get("score"),
                                "candidate_reasons": reasons,
                                "candidate_source": cand.get("source"),
                                "held": True,
                                "proposer": "auto_emit/held_and_trending",
                            },
                        ),
                    ),
                    "watch_only",
                )
            )

        # ROB-335 — intraday floor: classify EVERY held KIS symbol (not just
        # sellable+actionable) so held_actions is never empty, and surface an
        # explicit no-new-buy reason when the screener universe is not useful.
        if self._intraday_floor:
            already = {i.symbol for i in items if i.symbol}
            for ticker, holding in held.items():
                if ticker in already:
                    continue
                quote_pair = symbol_quotes.get(ticker)
                quote = quote_pair[1] if quote_pair else None
                verdict = classify_held_symbol(
                    holding, quote, in_candidate_universe=ticker in candidate_by_symbol
                )
                items.append(
                    _stamp(
                        IngestReportItem(
                            client_item_key=f"auto-held-{ticker}",
                            item_kind="risk" if verdict == "data_gap" else "action",
                            symbol=ticker,
                            side="sell" if verdict == "sell_review" else None,
                            intent=(
                                "sell_review"
                                if verdict == "sell_review"
                                else "risk_review"
                                if verdict == "data_gap"
                                else "rebalance_review"
                            ),
                            rationale=(
                                f"보유 종목 {ticker} {verdict} — sellable "
                                f"{holding.get('sellable_quantity')}, "
                                f"quote {quote.get('status') if quote else 'none'}"
                            ),
                            operation="review",
                            apply_policy="requires_user_approval",
                            evidence_snapshot=_make_evidence(
                                quote_pair[0] if quote_pair else portfolio_snapshot,
                                extra={
                                    "portfolio_snapshot_uuid": _snapshot_uuid(
                                        portfolio_snapshot
                                    ),
                                    "sellable_quantity": holding.get(
                                        "sellable_quantity"
                                    ),
                                    "quote_status": quote.get("status")
                                    if quote
                                    else "no_snapshot",
                                    "proposer": "auto_emit/intraday_held_floor",
                                },
                            ),
                        ),
                        verdict,
                    )
                )

            if candidate_usefulness != "useful":
                missing = (
                    candidate_snapshot is not None
                    and _snapshot_payload(candidate_snapshot).get("missing_data")
                ) or {}
                reason = (
                    f"{missing.get('what', '신규 매수 후보 없음')} "
                    f"{missing.get('next', '')}".strip()
                    if isinstance(missing, dict)
                    else "신규 매수 후보 없음"
                )
                items.append(
                    _stamp(
                        IngestReportItem(
                            client_item_key="auto-no-new-buy",
                            item_kind="risk",
                            symbol=None,
                            intent="risk_review",
                            rationale=reason,
                            operation="review",
                            apply_policy="requires_user_approval",
                            evidence_snapshot=_make_evidence(
                                candidate_snapshot,
                                extra={
                                    "candidate_usefulness": candidate_usefulness,
                                    "proposer": "auto_emit/no_new_buy_floor",
                                },
                            ),
                        ),
                        "no_new_buy_candidates",
                    )
                )

        # ROB-332 — cite a bundle-resident validated_run_card on items whose
        # symbol matches the run card's symbols (consume-when-present; no-op
        # when no run card is in the bundle or no symbol overlaps).
        if run_card_evidence_by_symbol:
            for item in items:
                if item.symbol and item.symbol in run_card_evidence_by_symbol:
                    item.evidence_snapshot["run_card"] = run_card_evidence_by_symbol[
                        item.symbol
                    ]

        return items
