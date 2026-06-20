"""ROB-315 Phase 1 — service layer for the scalping review loop.

The **only** write path for ``scalping_daily_reviews`` /
``scalping_review_actions``. It rolls a day's raw ``scalp_trade_analytics``
rows into a draft review (idempotent per key) and lets the operator edit the
human-judgment fields — but it never edits raw analytics rows, and never
touches any broker / order / scheduler surface.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.models.scalping_reviews import (
    ACTION_STATUSES,
    ACTION_TYPES,
    REVIEW_DECISIONS,
    REVIEW_STATUSES,
    SCALPING_REVIEW_ACCOUNT_SCOPE,
    ScalpingDailyReview,
    ScalpingReviewAction,
)
from app.services.scalping_reviews.rollup import RollupResult, build_rollup

# Operator-editable review fields (everything else is rollup-owned).
_REVIEW_TEXT_FIELDS = ("observation", "root_cause", "improvement", "next_run_plan")
# Operator-editable action fields.
_ACTION_TEXT_FIELDS = (
    "title",
    "rationale",
    "target_component",
    "proposed_change",
    "expected_effect",
)

_UNSET: Any = object()


class ScalpingReviewError(ValueError):
    """Invalid review/action input (bad scope, decision, status, ...)."""


def _require_demo_scope(account_scope: str) -> None:
    if account_scope != SCALPING_REVIEW_ACCOUNT_SCOPE:
        raise ScalpingReviewError(
            f"account_scope must be {SCALPING_REVIEW_ACCOUNT_SCOPE!r} for the demo "
            f"scalping review loop, got {account_scope!r}. Demo scalping review "
            "state must never carry a KIS/Upbit live execution scope."
        )


class ScalpingReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Draft build (idempotent rollup)
    # ------------------------------------------------------------------
    async def build_draft(
        self,
        *,
        review_date: dt.date,
        product: str,
        now: dt.datetime,
        session_tag: str = "",
        account_scope: str = SCALPING_REVIEW_ACCOUNT_SCOPE,
    ) -> ScalpingDailyReview:
        """Build or refresh the draft review for a key from that day's
        ``scalp_trade_analytics`` rows. Idempotent per
        ``(review_date, product, account_scope, session_tag)`` — re-running
        refreshes the rollup metrics in place, preserves operator inputs, and
        leaves a ``locked`` review untouched."""
        _require_demo_scope(account_scope)
        rollup = await self._rollup_for(review_date, product)

        existing = await self._get_by_key(
            review_date, product, account_scope, session_tag
        )
        if existing is not None:
            if existing.status != "locked":
                self._apply_rollup(existing, rollup)
                existing.updated_at = now
            return existing

        review = ScalpingDailyReview(
            review_date=review_date,
            product=product,
            account_scope=account_scope,
            session_tag=session_tag,
            decision="review",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        self._apply_rollup(review, rollup)
        self._session.add(review)
        await self._session.flush()
        await self._session.refresh(review)
        return review

    async def _get_by_key(
        self,
        review_date: dt.date,
        product: str,
        account_scope: str,
        session_tag: str,
    ) -> ScalpingDailyReview | None:
        return await self._session.scalar(
            select(ScalpingDailyReview).where(
                ScalpingDailyReview.review_date == review_date,
                ScalpingDailyReview.product == product,
                ScalpingDailyReview.account_scope == account_scope,
                ScalpingDailyReview.session_tag == session_tag,
            )
        )

    async def list_analytics(
        self, *, review_date: dt.date, product: str
    ) -> list[ScalpTradeAnalytics]:
        """Raw scalp_trade_analytics round-trip rows for a day/product, oldest
        first. Read-only — the per-trade table renders these; the review UI
        never edits them."""
        start = dt.datetime.combine(review_date, dt.time.min, tzinfo=dt.UTC)
        end = start + dt.timedelta(days=1)
        return list(
            (
                await self._session.scalars(
                    select(ScalpTradeAnalytics)
                    .where(
                        ScalpTradeAnalytics.product == product,
                        ScalpTradeAnalytics.created_at >= start,
                        ScalpTradeAnalytics.created_at < end,
                    )
                    .order_by(ScalpTradeAnalytics.created_at)
                )
            ).all()
        )

    async def _rollup_for(self, review_date: dt.date, product: str) -> RollupResult:
        rows = await self.list_analytics(review_date=review_date, product=product)
        return build_rollup(rows)

    @staticmethod
    def _apply_rollup(review: ScalpingDailyReview, rollup: RollupResult) -> None:
        review.trade_count = rollup.trade_count
        review.win_count = rollup.win_count
        review.loss_count = rollup.loss_count
        review.anomaly_count = rollup.anomaly_count
        review.gross_pnl_usdt = rollup.gross_pnl_usdt
        review.net_pnl_usdt = rollup.net_pnl_usdt
        review.net_return_bps = rollup.net_return_bps
        review.avg_slippage_bps = rollup.avg_slippage_bps
        review.avg_spread_bps = rollup.avg_spread_bps
        review.avg_mae_bps = rollup.avg_mae_bps
        review.avg_mfe_bps = rollup.avg_mfe_bps
        review.avg_holding_seconds = rollup.avg_holding_seconds
        review.exit_reason_counts = rollup.exit_reason_counts
        review.source_payload = rollup.source_payload

    async def set_benchmark(
        self,
        *,
        review_date: dt.date,
        product: str,
        value: Decimal | None,
        now: dt.datetime,
        session_tag: str = "",
        account_scope: str = SCALPING_REVIEW_ACCOUNT_SCOPE,
        detail: dict[str, Any] | None = None,
    ) -> ScalpingDailyReview | None:
        """Store the daily buy&hold benchmark on an existing review row.

        Separate from ``build_draft`` (rollup-only, never imports a market-data
        client) so the market-data-aware ``benchmark_runner`` computes the value
        out of band and persists it here. No-op on a missing row (``None``) or a
        ``locked`` review (returned untouched). ``detail`` (per-symbol audit) is
        merged under ``source_payload['benchmark']``."""
        _require_demo_scope(account_scope)
        review = await self._get_by_key(
            review_date, product, account_scope, session_tag
        )
        if review is None or review.status == "locked":
            return review
        review.benchmark_return_bps = value
        if detail is not None:
            review.source_payload = {
                **(review.source_payload or {}),
                "benchmark": detail,
            }
        review.updated_at = now
        await self._session.flush()
        return review

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, review_id: int) -> ScalpingDailyReview | None:
        return await self._session.get(ScalpingDailyReview, review_id)

    async def list_reviews(
        self,
        *,
        review_date: dt.date | None = None,
        product: str | None = None,
    ) -> list[ScalpingDailyReview]:
        stmt = select(ScalpingDailyReview)
        if review_date is not None:
            stmt = stmt.where(ScalpingDailyReview.review_date == review_date)
        if product is not None:
            stmt = stmt.where(ScalpingDailyReview.product == product)
        stmt = stmt.order_by(
            ScalpingDailyReview.review_date.desc(), ScalpingDailyReview.product
        )
        return list((await self._session.scalars(stmt)).all())

    async def list_actions(self, review_id: int) -> list[ScalpingReviewAction]:
        return list(
            (
                await self._session.scalars(
                    select(ScalpingReviewAction)
                    .where(ScalpingReviewAction.review_id == review_id)
                    .order_by(ScalpingReviewAction.id)
                )
            ).all()
        )

    # ------------------------------------------------------------------
    # Operator edits (review fields only — never raw analytics)
    # ------------------------------------------------------------------
    async def update_review(
        self,
        review_id: int,
        *,
        now: dt.datetime,
        observation: str | None = _UNSET,
        root_cause: str | None = _UNSET,
        improvement: str | None = _UNSET,
        next_run_plan: str | None = _UNSET,
        decision: str | None = _UNSET,
        status: str | None = _UNSET,
    ) -> ScalpingDailyReview | None:
        review = await self.get(review_id)
        if review is None:
            return None
        local = locals()
        for f in _REVIEW_TEXT_FIELDS:
            if local[f] is not _UNSET:
                setattr(review, f, local[f])
        if decision is not _UNSET:
            if decision not in REVIEW_DECISIONS:
                raise ScalpingReviewError(f"invalid decision {decision!r}")
            review.decision = decision
        if status is not _UNSET:
            if status not in REVIEW_STATUSES:
                raise ScalpingReviewError(f"invalid status {status!r}")
            review.status = status
        review.updated_at = now
        await self._session.flush()
        return review

    async def add_action(
        self,
        review_id: int,
        *,
        action_type: str,
        title: str,
        now: dt.datetime,
        rationale: str | None = None,
        target_component: str | None = None,
        proposed_change: str | None = None,
        expected_effect: str | None = None,
    ) -> ScalpingReviewAction:
        if action_type not in ACTION_TYPES:
            raise ScalpingReviewError(f"invalid action_type {action_type!r}")
        action = ScalpingReviewAction(
            review_id=review_id,
            action_type=action_type,
            title=title,
            rationale=rationale,
            target_component=target_component,
            proposed_change=proposed_change,
            expected_effect=expected_effect,
            status="open",
            created_at=now,
            updated_at=now,
        )
        self._session.add(action)
        await self._session.flush()
        await self._session.refresh(action)
        return action

    async def update_action(
        self,
        action_id: int,
        *,
        now: dt.datetime,
        status: str | None = _UNSET,
        title: str | None = _UNSET,
        rationale: str | None = _UNSET,
        target_component: str | None = _UNSET,
        proposed_change: str | None = _UNSET,
        expected_effect: str | None = _UNSET,
    ) -> ScalpingReviewAction | None:
        action = await self._session.get(ScalpingReviewAction, action_id)
        if action is None:
            return None
        local = locals()
        for f in _ACTION_TEXT_FIELDS:
            if local[f] is not _UNSET:
                setattr(action, f, local[f])
        if status is not _UNSET:
            if status not in ACTION_STATUSES:
                raise ScalpingReviewError(f"invalid action status {status!r}")
            action.status = status
        action.updated_at = now
        await self._session.flush()
        return action
