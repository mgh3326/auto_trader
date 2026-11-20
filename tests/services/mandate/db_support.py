"""Seeding helpers for the hk 3795 T1 DB tests (run-owned test database only)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from app.models.order_proposals import OrderProposal
from app.services.mandate import store as ms
from app.services.strategy_sleeves.store import PreRegistration
from tests.services.strategy_sleeves.db_support import PREREG, SleeveSeed

RULE = "at3795.basket1_value_split_entry"
PRINCIPAL = "123456789"
EFFECTIVE = date(2026, 11, 23)
FAR = datetime.now(UTC) + timedelta(days=365)
TRANCHE_RULE = {
    "weights": ["0.4", "0.3", "0.3"],
    "triggers_pct_from_first": ["0", "-15", "-30"],
    "min_gap_sessions": 20,
}
GLOBAL_CAPS = ms.GlobalCaps(Decimal("2000000"), Decimal("5000000"))


def digest(label: str) -> str:
    return hashlib.sha256(f"{label}:{uuid.uuid4()}".encode()).hexdigest()


def new_symbol() -> str:
    return f"M{uuid.uuid4().hex[:5].upper()}"


class MandateSeed(SleeveSeed):
    """Creates uniquely named rows and removes every one of them afterwards."""

    def __init__(self, db) -> None:
        super().__init__(db)
        self.basket_keys: list[str] = []

    async def clause_sleeve(
        self,
        *,
        symbols: tuple[str, ...],
        basket_key: str | None = None,
        account_mode: str = "kis_live",
        market: str = "equity_kr",
        account_ref: str | None = None,
        budget_cap: str = "10000000",
        sector_cap_pct: str = "35",
        expires_at: datetime = FAR,
    ):
        """A shadow sleeve whose next live version and clause are operator-approved."""
        sleeve = await self.sleeve(
            state="shadow",
            budget_cap=budget_cap,
            loss_limit="500000",
            account_mode=account_mode,
            market=market,
            rule_id=RULE,
        )
        key = basket_key or f"b-{uuid.uuid4().hex[:10]}"
        self.basket_keys.append(key)
        request = await ms.create_terms_request(
            self.db,
            sleeve_id=sleeve.id,
            data=ms.TermsInput(
                basket_key=key,
                account_ref=account_ref
                or f"acct-{account_mode}-{uuid.uuid4().hex[:6]}",
                universe_rule="test universe",
                allowed_symbols=list(symbols),
                entry_max_premium_bps="30",
                per_order_cap="2000000",
                daily_cap="5000000",
                sector_cap_pct=sector_cap_pct,
                basket_drawdown_freeze_pct="25",
                account_drawdown_freeze_pct="25",
                tranche_rule=TRANCHE_RULE,
                expires_at=expires_at,
                budget_cap=budget_cap,
                loss_limit="500000",
                prereg=PreRegistration(
                    PREREG["metric"],
                    Decimal(PREREG["min_effect_pct"]),
                    int(PREREG["horizon_sessions"]),
                    int(PREREG["max_duration_days"]),
                ),
            ),
            global_caps=GLOBAL_CAPS,
            approval_ref="hk:test",
            requested_by="operator:test",
        )
        result = await ms.record_approval(
            self.db,
            request_id=request.id,
            decision="approved",
            principal=PRINCIPAL,
            nonce_digest=digest("nonce"),
        )
        await self.db.commit()
        assert result.terms is not None
        # Plain snapshots: a later rollback expires ORM objects, and an async
        # session cannot lazy-load them.
        return (
            SimpleNamespace(
                id=sleeve.id, market=sleeve.market, account_mode=sleeve.account_mode
            ),
            SimpleNamespace(
                id=result.terms.id,
                account_ref=result.terms.account_ref,
                basket_key=result.terms.basket_key,
            ),
        )

    def plan_input(
        self,
        sleeve,
        terms,
        symbol: str,
        *,
        planned: str = "2350000",
        adopted: str = "0",
        sector: str = "auto",
        scope: tuple[str, ...] | None = None,
        **over: Any,
    ) -> ms.PlanInput:
        refs = list(
            scope
            or (
                (terms.account_ref, f"other-{uuid.uuid4().hex[:6]}")
                if sleeve.account_mode != "kiwoom_mock"
                else (terms.account_ref,)
            )
        )
        holdings = [
            {
                "account_ref": r,
                "quantity": "0" if Decimal(adopted) == 0 or r != refs[0] else "1",
                "amount": "0" if Decimal(adopted) == 0 or r != refs[0] else adopted,
            }
            for r in refs
        ]
        base = {
            "sleeve_id": sleeve.id,
            "symbol": symbol,
            "planned_amount": planned,
            "adopted_amount": adopted,
            "adopted_evidence": {
                "checked_at": "2026-11-20T00:00:00Z",
                "holdings": holdings,
            },
            "account_ref": terms.account_ref,
            "scope_account_refs": refs,
            "thesis": "test thesis",
            "invalidation_conditions": ["x falls"],
            "sector_key": sector,
            "created_by": "operator:test",
            "one_share_price": "10000",
        }
        base.update(over)
        return ms.PlanInput(**base)

    async def register(self, sleeve, terms, symbol: str, **over) -> ms.RegisteredPlan:
        plan = await ms.register_plan(
            self.db, self.plan_input(sleeve, terms, symbol, **over)
        )
        await self.db.commit()
        return plan

    @staticmethod
    def plan_snapshot(plan) -> SimpleNamespace:
        return SimpleNamespace(
            id=plan.id,
            symbol=plan.symbol,
            account_ref=plan.account_ref,
            scope_account_refs=list(plan.scope_account_refs),
            sleeve_id=plan.sleeve_id,
        )

    async def approve_plan(self, plan_id: int, terms) -> None:
        await ms.append_plan_event(
            self.db,
            plan_id=plan_id,
            event="approved",
            actor="operator:test",
            terms_id=terms.id,
            effective_session=EFFECTIVE,
        )
        await self.db.commit()

    async def bound_proposal(
        self,
        *,
        plan,
        sleeve,
        symbol: str | None = None,
        account_mode: str | None = None,
        side: str = "buy",
        order_type: str = "limit",
        tranche_index: int | None = 1,
        plan_id: int | None | str = "plan",
    ) -> OrderProposal:
        pid = uuid.uuid4()
        row = OrderProposal(
            proposal_id=pid,
            root_proposal_id=pid,
            symbol=symbol or plan.symbol,
            market=sleeve.market,
            account_mode=account_mode or sleeve.account_mode,
            side=side,
            order_type=order_type,
            proposer="test",
            sleeve_id=sleeve.id,
            mandate_plan_id=plan.id if plan_id == "plan" else plan_id,
            mandate_tranche_index=tranche_index,
        )
        self.db.add(row)
        await self.db.flush()
        self.proposal_pks.append(row.id)
        return row

    async def loss_cut_proposal(
        self,
        *,
        symbol: str,
        market: str = "equity_kr",
        account_mode: str = "kis_live",
        lifecycle_state: str = "proposed",
    ) -> OrderProposal:
        pid = uuid.uuid4()
        row = OrderProposal(
            proposal_id=pid,
            root_proposal_id=pid,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side="sell",
            order_type="limit",
            proposer="test",
            exit_intent="loss_cut",
            lifecycle_state=lifecycle_state,
        )
        self.db.add(row)
        await self.db.commit()
        self.proposal_pks.append(row.id)
        return row

    async def cleanup(self) -> None:
        await self.db.rollback()
        await self.db.execute(sa.text("SET LOCAL session_replication_role = replica"))
        if self.sleeve_ids:
            ids = {"ids": self.sleeve_ids}
            plans = "(SELECT id FROM review.mandate_plans WHERE sleeve_id = ANY(:ids))"
            for statement in (
                "DELETE FROM review.mandate_symbol_reservation_releases WHERE reservation_id IN "
                f"(SELECT id FROM review.mandate_symbol_reservations WHERE plan_id IN {plans})",
                f"DELETE FROM review.mandate_symbol_reservations WHERE plan_id IN {plans}",
                f"DELETE FROM review.mandate_plan_events WHERE plan_id IN {plans}",
                f"DELETE FROM review.mandate_plan_tranches WHERE plan_id IN {plans}",
                "DELETE FROM review.order_proposals WHERE mandate_plan_id IN " + plans,
                "DELETE FROM review.mandate_plans WHERE sleeve_id = ANY(:ids)",
                "DELETE FROM review.mandate_activation_profiles WHERE sleeve_id = ANY(:ids)",
                "DELETE FROM review.mandate_terms WHERE sleeve_version_id IN "
                "(SELECT id FROM review.strategy_sleeve_versions WHERE sleeve_id = ANY(:ids))",
                "DELETE FROM review.mandate_approvals WHERE request_id IN "
                "(SELECT id FROM review.mandate_approval_requests WHERE sleeve_id = ANY(:ids))",
                "DELETE FROM review.mandate_approval_requests WHERE sleeve_id = ANY(:ids)",
                "DELETE FROM review.mandate_cap_mutex WHERE mutex_key LIKE 'sleeve:%' "
                "AND substring(mutex_key FROM 8)::bigint = ANY(:ids)",
            ):
                await self.db.execute(sa.text(statement), ids)
            if self.basket_keys:
                await self.db.execute(
                    sa.text(
                        "DELETE FROM review.mandate_basket_authority "
                        "WHERE basket_key = ANY(:keys)"
                    ),
                    {"keys": self.basket_keys},
                )
                await self.db.execute(
                    sa.text(
                        "DELETE FROM review.mandate_cap_mutex WHERE scope <> 'sleeve' "
                        "AND split_part(mutex_key, ':', 3) = ANY(:keys)"
                    ),
                    {"keys": self.basket_keys},
                )
        await self.db.commit()
        await super().cleanup()
