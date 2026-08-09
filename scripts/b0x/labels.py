"""B0-X observation labels — fixed header, never per-run editable.

The three ``policy_table.v1`` trust labels are **reused verbatim** from
``scripts/policy_table/core/trust_labels.py`` (the ROB-1230 design doc §0
source of record) rather than re-typed, so a wording drift in one place
cannot silently diverge here. B0-X adds its own experiment-identity labels
on top; the three inherited ones always come first and always all three
(the acceptance gate checks 3/3 exact-string presence).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from scripts.b0x.scope import (
    ALPACA_PAPER_LAB_SCOPE_KEY,
    BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY,
    KIS_MOCK_SCOPE_KEY,
    KNOWN_B0X_SCOPE_KEYS,
    UPBIT_SHADOW_SCOPE_KEY,
)
from scripts.policy_table.core.trust_labels import TRUST_LABELS

#: B0-X §1 identity — every artifact carries these after the inherited three.
B0X_IDENTITY_LABELS: tuple[str, ...] = (
    (
        "PROSPECTIVE_EXPERIMENT_ONLY — B0-X 는 관측이지 검증이 아니다. 이 산출물은 "
        "어떤 전략의 채점·승격 근거도 아니며 D3 prospective confirmatory 가 아니다."
    ),
    (
        "WRITER_SINGLETON — B0-X 측 주문 생성 주체는 B0-X 어댑터 하나다. "
        "방어는 오염 게이트의 fail-closed 관측에 둔다. 수동·외부 주문이 감지되면 "
        "해당 구간은 CONTAMINATED 로 분리 표시된다."
    ),
)

# Account-specific evidence belongs in a lane-injected caveat, not in the
# fixed identity header. In particular, the Binance disarm is not a KR or
# shadow fact (contract v1.4 §3 and §8 v1.4 ③).
WRITER_SINGLETON_SCOPE_CAVEATS: Final[dict[str, str]] = {
    BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY: (
        "WRITER_SINGLETON_SCOPE_CAVEAT — Binance Spot Demo 사이드카 계좌의 "
        "배타성은 프로덕션 데모 스캘핑 봇 disarm 운영 조치(2026-08-09)로 확보됐다."
    ),
    KIS_MOCK_SCOPE_KEY: (
        "WRITER_SINGLETON_SCOPE_CAVEAT — kis_mock(KR) 계좌 배타성의 근거는 "
        "operator account map의 exclusive_lane 및 B0-X 단일 writer 할당이다. "
        "posture-v1 shadow와 방향성 랩은 관측 전용으로 공존한다."
    ),
    UPBIT_SHADOW_SCOPE_KEY: (
        "WRITER_SINGLETON_SCOPE_CAVEAT — Upbit shadow-sim은 실계좌가 아닌 "
        "합성 체결이다. 이 레인에는 계좌 배타성 근거가 없으므로 계좌 배타성을 "
        "주장하지 않는다."
    ),
    ALPACA_PAPER_LAB_SCOPE_KEY: (
        "WRITER_SINGLETON_SCOPE_CAVEAT — operator_contract.yaml의 "
        "account_lanes.alpaca_paper_lab=B0-X-US 이고, "
        "strategy_order_exceptions.b0x-adapter-orders-20260808의 "
        "writer=b0x_adapter_single 및 surfaces에 alpaca_paper_lab이 등재돼 있다."
    ),
}

#: Account-property caveat, deliberately not part of the fixed trust header.
#: The value is a lane/account scope key so widening the scope is one explicit
#: data change, not a change to the label-selection logic.
SHARED_HISTORY_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY}
)

SHARED_ACCOUNT_HISTORY: Final[str] = (
    "SHARED_ACCOUNT_HISTORY — 이 계좌는 B0-X 이전에 다른 주체가 사용한 이력이 있다"
    "(ROB-298 스모크가 남긴 BTC·SOL dust 및 2026-07-29 ROB-1150 사고 기록 4건). "
    "프로덕션 데모 스캘핑 봇과 자격증명을 공유해 왔고 2026-08-09 disarm 됐다. "
    "따라서 이 계좌의 체결·잔고 이력 전부를 B0-X 산출로 읽으면 안 된다."
)


def _unknown_scope_keys(accounts: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted(set(accounts) - KNOWN_B0X_SCOPE_KEYS))


def _assert_known_scope_key(account: str) -> None:
    if account not in KNOWN_B0X_SCOPE_KEYS:
        known = ", ".join(sorted(KNOWN_B0X_SCOPE_KEYS))
        raise ValueError(f"unknown B0-X scope key {account!r}; known keys: {known}")


def account_history_labels(
    account: str, *, accounts: Collection[str] | None = None
) -> tuple[str, ...]:
    """Return account-history caveats for the explicitly scoped account."""

    _assert_known_scope_key(account)
    active_accounts = SHARED_HISTORY_ACCOUNTS if accounts is None else accounts
    unknown = _unknown_scope_keys(active_accounts)
    if unknown:
        raise ValueError(
            "unknown B0-X shared-history scope key(s): " + ", ".join(unknown)
        )
    if account in active_accounts:
        return (SHARED_ACCOUNT_HISTORY,)
    return ()


def writer_singleton_scope_labels(lane: str) -> tuple[str, ...]:
    """Return the writer evidence that is true for exactly this lane."""

    _assert_known_scope_key(lane)
    return (WRITER_SINGLETON_SCOPE_CAVEATS[lane],)


#: Sidecar-only caveat. Kept out of the fixed header (which stays 3/3 + identity)
#: and recorded in the artifact body, because it describes a venue transfer
#: rather than a trust property of the table itself.
CROSS_QUOTE_RATIO_TRANSFER = (
    "CROSS_QUOTE_RATIO_TRANSFER — 정책표는 KRW 호가 기준이고 Binance Spot Demo 는 "
    "USDT 호가다. 사이드카는 표의 절대가격이 아니라 표가 정의한 무차원 비율"
    "(레벨/직전종가)만 이식해 Binance 기준가에 적용한다. 이 이식 자체는 미검증이다."
)

#: Shadow-only caveat (계약 §3 crypto 본선 행 + job 브리프).
SHADOW_SYNTHETIC_FILL = (
    "SHADOW_SYNTHETIC_FILL — Upbit shadow 는 실계좌가 아니라 합성 체결이다. "
    "체결 증거는 약하고(터치≠체결: 호가 대기열·부분체결 미모형), 신호·타이밍 증거만 유효하다."
)


def header_labels(*, lane: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return fixed labels plus the lane-specific writer evidence."""

    return (
        *TRUST_LABELS,
        *B0X_IDENTITY_LABELS,
        *writer_singleton_scope_labels(lane),
        *extra,
    )


def render_header(labels: tuple[str, ...]) -> str:
    """Markdown blockquote header used by every human-readable artifact."""

    return "\n".join(f"> {label}" for label in labels)


__all__ = [
    "TRUST_LABELS",
    "B0X_IDENTITY_LABELS",
    "SHARED_HISTORY_ACCOUNTS",
    "SHARED_ACCOUNT_HISTORY",
    "account_history_labels",
    "CROSS_QUOTE_RATIO_TRANSFER",
    "SHADOW_SYNTHETIC_FILL",
    "header_labels",
    "render_header",
    "writer_singleton_scope_labels",
]
