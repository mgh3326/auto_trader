"""B0-X observation labels — fixed header, never per-run editable.

The three ``policy_table.v1`` trust labels are **reused verbatim** from
``scripts/policy_table/core/trust_labels.py`` (the ROB-1230 design doc §0
source of record) rather than re-typed, so a wording drift in one place
cannot silently diverge here. B0-X adds its own experiment-identity labels
on top; the three inherited ones always come first and always all three
(the acceptance gate checks 3/3 exact-string presence).
"""

from __future__ import annotations

from typing import Final

from scripts.policy_table.core.trust_labels import TRUST_LABELS

#: B0-X §1 identity — every artifact carries these after the inherited three.
B0X_IDENTITY_LABELS: tuple[str, ...] = (
    (
        "PROSPECTIVE_EXPERIMENT_ONLY — B0-X 는 관측이지 검증이 아니다. 이 산출물은 "
        "어떤 전략의 채점·승격 근거도 아니며 D3 prospective confirmatory 가 아니다."
    ),
    (
        "WRITER_SINGLETON — B0-X 측 주문 생성 주체는 B0-X 어댑터 하나다. "
        "계좌 배타성은 프로덕션 데모 스캘핑 봇 disarm 운영 조치로 확보했으며, "
        "방어는 오염 게이트의 fail-closed 관측에 둔다. 수동·외부 주문이 감지되면 "
        "해당 구간은 CONTAMINATED 로 분리 표시된다."
    ),
)

#: Account-property caveat, deliberately not part of the fixed trust header.
#: The value is a lane/account scope key so widening the scope is one explicit
#: data change, not a change to the label-selection logic.
SHARED_HISTORY_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {"binance_spot_demo_sidecar"}
)

SHARED_ACCOUNT_HISTORY: Final[str] = (
    "SHARED_ACCOUNT_HISTORY — 이 계좌는 B0-X 이전에 다른 주체가 사용한 이력이 있다"
    "(ROB-298 스모크가 남긴 BTC·SOL dust 및 2026-07-29 ROB-1150 사고 기록 4건). "
    "프로덕션 데모 스캘핑 봇과 자격증명을 공유해 왔고 2026-08-09 disarm 됐다. "
    "따라서 이 계좌의 체결·잔고 이력 전부를 B0-X 산출로 읽으면 안 된다."
)


def account_history_labels(
    account: str, *, accounts: frozenset[str] = SHARED_HISTORY_ACCOUNTS
) -> tuple[str, ...]:
    """Return account-history caveats for the explicitly scoped account."""

    if account in accounts:
        return (SHARED_ACCOUNT_HISTORY,)
    return ()


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


def header_labels(*, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return the fixed artifact header labels, inherited three first."""

    return (*TRUST_LABELS, *B0X_IDENTITY_LABELS, *extra)


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
]
