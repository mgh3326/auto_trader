"""Fail-closed approval and live-submission window policy.

The policy is deliberately split into two layers:

* proposal validity (``valid_until``), evaluated first and without I/O;
* broker/market submission capability, backed by the existing exchange and
  Toss market calendars.

That ordering is load-bearing. An already-expired proposal never needs a
calendar lookup and can never degrade into a session defer. The returned
decision is typed and side-effect free; top-level callers own expiry
convergence and transaction boundaries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.services.brokers.toss.market_calendar import (
    TossKrMarketDay,
    TossSessionWindow,
    TossUsMarketDay,
    get_toss_market_calendar,
    kr_toss_session_for,
    us_toss_session_for,
)
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.market_events.session_calendar import (
    next_trading_session,
    regular_session_bounds,
)
from app.services.order_proposals.approval_window_contract import (
    ApprovalWindowCode,
    valid_until_block,
)

POLICY_VERSION = "order-proposal-approval-window-v1"

_KST = ZoneInfo("Asia/Seoul")
_NEW_YORK = ZoneInfo("America/New_York")
_NXT_SESSIONS = frozenset({"nxt_premarket", "nxt_after"})


@dataclass(frozen=True)
class SubmissionSessionEvidence:
    """Authoritative session/capability evidence for one proposal."""

    known: bool
    source: str
    current_session: str
    allowed_sessions: tuple[str, ...]
    allowed_now: bool
    allowed_until: datetime | None = None
    next_allowed_at: datetime | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("allowed_until", self.allowed_until),
            ("next_allowed_at", self.next_allowed_at),
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ApprovalWindowDecision:
    code: ApprovalWindowCode
    observed_at: datetime
    valid_until: datetime | None
    policy_stamp: str
    market: str
    account_mode: str
    action: str
    order_type: str
    evidence: SubmissionSessionEvidence | None = None
    detail: str | None = None

    @property
    def allowed(self) -> bool:
        return self.code is ApprovalWindowCode.ALLOW

    def to_dict(self) -> dict[str, Any]:
        evidence = self.evidence
        return {
            "code": self.code.value,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": (
                self.valid_until.isoformat() if self.valid_until is not None else None
            ),
            "policy_stamp": self.policy_stamp,
            "market": self.market,
            "account_mode": self.account_mode,
            "action": self.action,
            "order_type": self.order_type,
            "detail": self.detail,
            "session_evidence": (
                {
                    "known": evidence.known,
                    "source": evidence.source,
                    "current_session": evidence.current_session,
                    "allowed_sessions": list(evidence.allowed_sessions),
                    "allowed_now": evidence.allowed_now,
                    "allowed_until": (
                        evidence.allowed_until.isoformat()
                        if evidence.allowed_until is not None
                        else None
                    ),
                    "next_allowed_at": (
                        evidence.next_allowed_at.isoformat()
                        if evidence.next_allowed_at is not None
                        else None
                    ),
                    "detail": evidence.detail,
                }
                if evidence is not None
                else None
            ),
        }


SessionResolver = Callable[..., Awaitable[SubmissionSessionEvidence]]
WindowEvaluator = Callable[..., Awaitable[ApprovalWindowDecision]]


def _contract_fields(group: Any) -> tuple[str, str, str, str]:
    return (
        str(getattr(group, "market", "") or ""),
        str(getattr(group, "account_mode", "") or ""),
        str(getattr(group, "action", None) or "place"),
        str(getattr(group, "order_type", "") or ""),
    )


def _policy_stamp(group: Any, evidence: SubmissionSessionEvidence | None = None) -> str:
    market, account_mode, action, order_type = _contract_fields(group)
    allowed = ",".join(evidence.allowed_sessions) if evidence is not None else "none"
    material = "|".join(
        (
            POLICY_VERSION,
            account_mode,
            market,
            action,
            order_type,
            "DAY",
            allowed,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{POLICY_VERSION}:{digest}"


def _containing_window(
    windows: Sequence[TossSessionWindow], now: datetime
) -> TossSessionWindow | None:
    return next((window for window in windows if window.contains(now)), None)


def _next_window_start(
    windows: Sequence[TossSessionWindow],
    *,
    after: datetime,
) -> datetime | None:
    candidates = [window.start for window in windows if window.start >= after]
    return min(candidates) if candidates else None


def _toss_us_regular_windows(calendar: Any) -> list[TossSessionWindow]:
    return sorted(
        (
            day.regular_market
            for day in calendar.days
            if isinstance(day, TossUsMarketDay) and day.regular_market is not None
        ),
        key=lambda window: window.start,
    )


def _toss_kr_windows(calendar: Any, *, allow_nxt: bool) -> list[TossSessionWindow]:
    windows: list[TossSessionWindow] = []
    for day in calendar.days:
        if not isinstance(day, TossKrMarketDay):
            continue
        if allow_nxt and day.pre_market is not None:
            windows.append(day.pre_market)
        if day.regular_market is not None:
            windows.append(day.regular_market)
        if allow_nxt and day.after_market is not None:
            windows.append(day.after_market)
    return sorted(windows, key=lambda window: window.start)


async def _resolve_toss_us_session(
    group: Any, now: datetime
) -> SubmissionSessionEvidence:
    local = now.astimezone(_KST)
    calendar = await get_toss_market_calendar("us", local.date())
    if calendar is None:
        return SubmissionSessionEvidence(
            known=False,
            source="toss_market_calendar:us",
            current_session="unknown",
            allowed_sessions=("regular",),
            allowed_now=False,
            detail="calendar_unavailable",
        )
    regular_windows = _toss_us_regular_windows(calendar)
    session = us_toss_session_for(local, calendar=calendar) or "closed"
    current = _containing_window(regular_windows, local)
    next_open = _next_window_start(
        regular_windows,
        after=current.end if current is not None else local,
    )
    if not regular_windows or (session != "regular" and next_open is None):
        return SubmissionSessionEvidence(
            known=False,
            source="toss_market_calendar:us",
            current_session=session,
            allowed_sessions=("regular",),
            allowed_now=False,
            detail="regular_window_unavailable",
        )
    return SubmissionSessionEvidence(
        known=True,
        source="toss_market_calendar:us",
        current_session=session,
        allowed_sessions=("regular",),
        allowed_now=session == "regular",
        allowed_until=current.end
        if session == "regular" and current is not None
        else None,
        next_allowed_at=next_open,
    )


def _resolve_xnys_session(group: Any, now: datetime) -> SubmissionSessionEvidence:
    local = now.astimezone(_NEW_YORK)
    bounds = regular_session_bounds("us", local.date())
    next_day = next_trading_session("us", local.date())
    next_bounds = (
        regular_session_bounds("us", next_day) if next_day is not None else None
    )
    next_open: datetime | None = None
    if bounds is not None:
        session_open, session_close = bounds
        if session_open <= now.astimezone(UTC) < session_close:
            return SubmissionSessionEvidence(
                known=True,
                source="exchange_calendars:XNYS",
                current_session="regular",
                allowed_sessions=("regular",),
                allowed_now=True,
                allowed_until=session_close,
                next_allowed_at=next_bounds[0] if next_bounds is not None else None,
            )
        if now.astimezone(UTC) < session_open:
            next_open = session_open
    if next_open is None:
        next_open = next_bounds[0] if next_bounds is not None else None
    if next_open is None:
        return SubmissionSessionEvidence(
            known=False,
            source="exchange_calendars:XNYS",
            current_session="closed",
            allowed_sessions=("regular",),
            allowed_now=False,
            detail="next_regular_window_unavailable",
        )
    return SubmissionSessionEvidence(
        known=True,
        source="exchange_calendars:XNYS",
        current_session="closed",
        allowed_sessions=("regular",),
        allowed_now=False,
        next_allowed_at=next_open,
    )


async def _resolve_kr_session(group: Any, now: datetime) -> SubmissionSessionEvidence:
    local = now.astimezone(_KST)
    krx_bounds = regular_session_bounds("kr", local.date())
    in_krx_regular = (
        krx_bounds is not None and krx_bounds[0] <= now.astimezone(UTC) < krx_bounds[1]
    )
    calendar = await get_toss_market_calendar("kr", local.date())
    session = (
        kr_toss_session_for(local, calendar=calendar) or "closed"
        if calendar is not None
        else "unknown"
    )
    nxt_known = False
    nxt_tradable = False
    nxt_detail = "nxt_capability_unavailable"
    try:
        tradability = (
            await get_kr_nxt_tradability([str(getattr(group, "symbol", "") or "")])
        ).get(str(getattr(group, "symbol", "") or ""))
        if tradability is not None and not tradability.is_stale(now=local):
            nxt_known = True
            nxt_tradable = tradability.nxt_tradable
            nxt_detail = (
                "nxt_tradable"
                if nxt_tradable
                else (
                    "nxt_trading_suspended"
                    if tradability.nxt_trading_suspended is True
                    else "not_nxt_eligible"
                )
            )
        elif tradability is not None:
            nxt_detail = "nxt_capability_stale"
    except Exception:  # noqa: BLE001 - capability uncertainty is data, not allow
        nxt_detail = "nxt_capability_lookup_failed"

    if in_krx_regular:
        allow_nxt = calendar is not None and nxt_known and nxt_tradable
        windows = (
            _toss_kr_windows(calendar, allow_nxt=allow_nxt)
            if calendar is not None
            else []
        )
        current = _containing_window(windows, local)
        next_open = _next_window_start(
            windows,
            after=current.end if current is not None else krx_bounds[1],
        )
        if next_open is None:
            next_day = next_trading_session("kr", local.date())
            next_bounds = (
                regular_session_bounds("kr", next_day) if next_day is not None else None
            )
            next_open = next_bounds[0] if next_bounds is not None else None
        return SubmissionSessionEvidence(
            known=True,
            source="exchange_calendars:XKRX+toss_nxt_capability",
            current_session="regular",
            allowed_sessions=(
                ("nxt_premarket", "regular", "nxt_after") if allow_nxt else ("regular",)
            ),
            allowed_now=True,
            allowed_until=krx_bounds[1],
            next_allowed_at=next_open,
            detail=nxt_detail,
        )

    if calendar is None:
        return SubmissionSessionEvidence(
            known=False,
            source="exchange_calendars:XKRX+toss_market_calendar:kr_integrated",
            current_session="unknown",
            allowed_sessions=("regular",),
            allowed_now=False,
            detail="integrated_calendar_unavailable",
        )

    if session == "regular":
        return SubmissionSessionEvidence(
            known=False,
            source="exchange_calendars:XKRX+toss_market_calendar:kr_integrated",
            current_session=session,
            allowed_sessions=("regular",),
            allowed_now=False,
            detail="regular_calendar_disagreement",
        )

    if session in _NXT_SESSIONS and not nxt_known:
        return SubmissionSessionEvidence(
            known=False,
            source="toss_market_calendar:kr_integrated+kr_symbol_universe",
            current_session=session,
            allowed_sessions=("regular",),
            allowed_now=False,
            detail=nxt_detail,
        )

    allow_nxt = nxt_known and nxt_tradable
    allowed_sessions = (
        ("nxt_premarket", "regular", "nxt_after") if allow_nxt else ("regular",)
    )
    allowed_now = session in allowed_sessions
    windows = _toss_kr_windows(calendar, allow_nxt=allow_nxt)
    current = _containing_window(windows, local) if allowed_now else None
    next_open = _next_window_start(
        windows,
        after=current.end if current is not None else local,
    )
    if next_open is None:
        next_day = next_trading_session("kr", local.date())
        next_bounds = (
            regular_session_bounds("kr", next_day) if next_day is not None else None
        )
        next_open = next_bounds[0] if next_bounds is not None else None
    if not allowed_now and next_open is None:
        return SubmissionSessionEvidence(
            known=False,
            source="toss_market_calendar:kr_integrated+kr_symbol_universe",
            current_session=session,
            allowed_sessions=allowed_sessions,
            allowed_now=False,
            detail="next_allowed_window_unavailable",
        )
    return SubmissionSessionEvidence(
        known=True,
        source="toss_market_calendar:kr_integrated+kr_symbol_universe",
        current_session=session,
        allowed_sessions=allowed_sessions,
        allowed_now=allowed_now,
        allowed_until=current.end if current is not None else None,
        next_allowed_at=next_open,
        detail=nxt_detail,
    )


async def resolve_submission_session(
    group: Any, *, now: datetime
) -> SubmissionSessionEvidence:
    """Resolve broker/market-aware submission capability.

    Proposal orders have DAY semantics and no persisted extended-hours
    capability bit. US live proposals are therefore regular-session only.
    KR retains KRX regular plus NXT carry only when the existing symbol
    universe positively proves current NXT tradability. Crypto remains 24/7.
    """
    market, account_mode, _action, _order_type = _contract_fields(group)
    if market == "crypto" and account_mode == "upbit":
        return SubmissionSessionEvidence(
            known=True,
            source="crypto:24x7",
            current_session="24x7",
            allowed_sessions=("24x7",),
            allowed_now=True,
        )
    if market == "equity_us" and account_mode == "toss_live":
        return await _resolve_toss_us_session(group, now)
    if market == "equity_us" and account_mode == "kis_live":
        return _resolve_xnys_session(group, now)
    if market == "equity_kr" and account_mode in {"kis_live", "toss_live"}:
        return await _resolve_kr_session(group, now)
    return SubmissionSessionEvidence(
        known=False,
        source="unsupported_submission_contract",
        current_session="unknown",
        allowed_sessions=(),
        allowed_now=False,
        detail=f"unsupported:{account_mode}/{market}",
    )


async def evaluate_approval_window(
    group: Any,
    *,
    now: datetime,
    session_resolver: SessionResolver = resolve_submission_session,
) -> ApprovalWindowDecision:
    """Return the fail-closed decision for dispatch/callback/submit."""
    market, account_mode, action, order_type = _contract_fields(group)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("approval-window now must be timezone-aware")

    raw_valid_until = getattr(group, "valid_until", None)
    valid_until = raw_valid_until if isinstance(raw_valid_until, datetime) else None
    common = {
        "observed_at": now,
        "valid_until": valid_until,
        "market": market,
        "account_mode": account_mode,
        "action": action,
        "order_type": order_type,
    }
    validity_block = valid_until_block(raw_valid_until, now=now)
    if validity_block is not None:
        code, detail = validity_block
        return ApprovalWindowDecision(
            code=code,
            policy_stamp=_policy_stamp(group),
            detail=detail,
            **common,
        )
    assert isinstance(raw_valid_until, datetime)

    try:
        evidence = await session_resolver(group, now=now)
    except Exception as exc:  # noqa: BLE001 - live session uncertainty fails closed
        evidence = SubmissionSessionEvidence(
            known=False,
            source="session_resolver",
            current_session="unknown",
            allowed_sessions=(),
            allowed_now=False,
            detail=f"resolver_error:{type(exc).__name__}",
        )
    stamp = _policy_stamp(group, evidence)
    if not evidence.known:
        return ApprovalWindowDecision(
            code=ApprovalWindowCode.CALENDAR_UNKNOWN,
            policy_stamp=stamp,
            evidence=evidence,
            detail=evidence.detail or "session_evidence_unknown",
            **common,
        )
    if evidence.allowed_now:
        return ApprovalWindowDecision(
            code=ApprovalWindowCode.ALLOW,
            policy_stamp=stamp,
            evidence=evidence,
            **common,
        )
    if (
        evidence.next_allowed_at is not None
        and evidence.next_allowed_at >= raw_valid_until
    ):
        return ApprovalWindowDecision(
            code=ApprovalWindowCode.NO_EXECUTABLE_WINDOW,
            policy_stamp=stamp,
            evidence=evidence,
            detail="valid_until_not_after_next_allowed_session",
            **common,
        )
    return ApprovalWindowDecision(
        code=ApprovalWindowCode.DEFER_SESSION_CLOSED,
        policy_stamp=stamp,
        evidence=evidence,
        detail="submission_session_closed",
        **common,
    )


def bind_approval_window_policy(
    decision: ApprovalWindowDecision,
    expected_policy_stamp: str | None,
) -> ApprovalWindowDecision:
    """Bind fresh evidence to the policy advertised on the approval surface.

    Expiry/invalidity outrank policy metadata. Every other live decision
    requires an exact stamp; a missing stamp is not an implicit trust path.
    """
    if decision.code in {
        ApprovalWindowCode.EXPIRED,
        ApprovalWindowCode.INVALID_VALID_UNTIL,
    }:
        return decision
    if expected_policy_stamp is not None and (
        decision.policy_stamp == expected_policy_stamp
    ):
        return decision
    return replace(
        decision,
        code=ApprovalWindowCode.CALENDAR_UNKNOWN,
        detail=(
            "approval_window_policy_stamp_missing"
            if expected_policy_stamp is None
            else "approval_window_policy_stamp_mismatch:"
            f"expected={expected_policy_stamp}:actual={decision.policy_stamp}"
        ),
    )


def recheck_approval_window_decision(
    group: Any,
    decision: ApprovalWindowDecision,
    *,
    now: datetime,
) -> ApprovalWindowDecision:
    """Re-check an awaited decision at the completion/send instant.

    Calendar resolution can straddle an expiry or session-close edge. The
    evidence therefore carries the authoritative end of the currently allowed
    interval. This function performs no I/O and never extends that interval.
    """
    raw_valid_until = getattr(group, "valid_until", None)
    validity_block = valid_until_block(raw_valid_until, now=now)
    if validity_block is not None:
        code, detail = validity_block
        return replace(
            decision,
            code=code,
            observed_at=now,
            valid_until=(
                raw_valid_until if isinstance(raw_valid_until, datetime) else None
            ),
            detail=detail,
        )
    if not decision.allowed:
        return replace(decision, observed_at=now)

    evidence = decision.evidence
    if evidence is None or not evidence.known:
        return replace(
            decision,
            code=ApprovalWindowCode.CALENDAR_UNKNOWN,
            observed_at=now,
            detail="session_evidence_missing_at_completion",
        )
    if evidence.allowed_sessions == ("24x7",):
        return replace(decision, observed_at=now)
    if evidence.allowed_until is None:
        return replace(
            decision,
            code=ApprovalWindowCode.CALENDAR_UNKNOWN,
            observed_at=now,
            evidence=replace(
                evidence,
                current_session="unknown",
                allowed_now=False,
            ),
            detail="allowed_interval_end_missing",
        )
    if now < evidence.allowed_until:
        return replace(decision, observed_at=now)

    closed_evidence = replace(
        evidence,
        current_session="closed",
        allowed_now=False,
        allowed_until=None,
    )
    next_allowed_at = evidence.next_allowed_at
    if next_allowed_at is None or next_allowed_at <= now:
        return replace(
            decision,
            code=ApprovalWindowCode.CALENDAR_UNKNOWN,
            observed_at=now,
            evidence=closed_evidence,
            detail="next_allowed_window_stale_or_unavailable",
        )
    assert isinstance(raw_valid_until, datetime)
    if next_allowed_at >= raw_valid_until:
        return replace(
            decision,
            code=ApprovalWindowCode.NO_EXECUTABLE_WINDOW,
            observed_at=now,
            evidence=closed_evidence,
            detail="valid_until_not_after_next_allowed_session",
        )
    return replace(
        decision,
        code=ApprovalWindowCode.DEFER_SESSION_CLOSED,
        observed_at=now,
        evidence=closed_evidence,
        detail="submission_session_closed_during_evaluation",
    )


async def evaluate_approval_window_boundary(
    group: Any,
    *,
    window_evaluator: WindowEvaluator,
    now_fn: Callable[[], datetime],
    expected_policy_stamp: str | None = None,
    require_policy_stamp: bool = True,
) -> ApprovalWindowDecision:
    """Evaluate and then re-sample at the exact caller boundary."""
    decision = await window_evaluator(group, now=now_fn())
    if require_policy_stamp:
        decision = bind_approval_window_policy(decision, expected_policy_stamp)
    return recheck_approval_window_decision(group, decision, now=now_fn())


def approval_window_operator_text(decision: ApprovalWindowDecision) -> str:
    if decision.code is ApprovalWindowCode.EXPIRED:
        return "⌛ 제안 만료"
    if decision.code is ApprovalWindowCode.INVALID_VALID_UNTIL:
        return "⛔ 제안 유효기간 오류 — 승인 차단"
    if decision.code is ApprovalWindowCode.CALENDAR_UNKNOWN:
        return "⛔ 시장 세션 확인 불가 — 승인 차단"
    if decision.code is ApprovalWindowCode.NO_EXECUTABLE_WINDOW:
        prefix = "⌛ 다음 주문 가능 세션 전에 만료 —"
    elif decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED:
        prefix = "🌙 주문 가능 세션 아님 —"
    else:
        return "승인 가능"
    if decision.code in {
        ApprovalWindowCode.NO_EXECUTABLE_WINDOW,
        ApprovalWindowCode.DEFER_SESSION_CLOSED,
    }:
        next_at = (
            decision.evidence.next_allowed_at.isoformat()
            if decision.evidence is not None
            and decision.evidence.next_allowed_at is not None
            else "확인 불가"
        )
        return f"{prefix} 다음 허용 세션: {next_at}"
    return "승인 가능"


def approval_window_rung_result(
    decision: ApprovalWindowDecision,
) -> Literal[
    "expired",
    "invalid_valid_until",
    "defer_session_closed",
    "calendar_unknown",
    "no_executable_window",
]:
    return {
        ApprovalWindowCode.EXPIRED: "expired",
        ApprovalWindowCode.INVALID_VALID_UNTIL: "invalid_valid_until",
        ApprovalWindowCode.DEFER_SESSION_CLOSED: "defer_session_closed",
        ApprovalWindowCode.CALENDAR_UNKNOWN: "calendar_unknown",
        ApprovalWindowCode.NO_EXECUTABLE_WINDOW: "no_executable_window",
    }[decision.code]
