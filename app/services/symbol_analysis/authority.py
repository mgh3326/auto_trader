"""필드별 권위 레지스트리 (ROB-397).

fallback 으로 치환할 때는 반드시 source 가 바뀌고 is_stale=True 가 동반된다
(freshness.py). Toss/Naver/browser 는 reference 로만 등재 — authority 대체 금지.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthoritySpec:
    primary: str
    fallback: str | None = None
    reference: tuple[str, ...] = field(default_factory=tuple)


CATEGORIES: tuple[str, ...] = (
    "price",
    "valuation",
    "technicals",
    "consensus",
    "flow",
)

CORE_CATEGORIES: tuple[str, ...] = ("price", "consensus", "technicals")

# authority 로 절대 승격 불가 (reference/calibration 전용).
NON_AUTHORITY_SOURCES: frozenset[str] = frozenset(
    {
        "naver_finance",
        "toss_screen",
        "naver_remote_debug",
        "toss_remote_debug",
        "browser_probe",
    }
)

AUTHORITY: dict[str, AuthoritySpec] = {
    "price": AuthoritySpec(primary="kis_live", fallback="stock_info"),
    "valuation": AuthoritySpec(primary="stock_info", reference=("naver_finance",)),
    "technicals": AuthoritySpec(primary="kis_live"),
    "consensus": AuthoritySpec(primary="kis_live", reference=("naver_finance",)),
    "flow": AuthoritySpec(
        primary="investor_flow_snapshots", reference=("naver_finance",)
    ),
}
