"""Map a KIND delisting reason to the closed ``TerminalOutcome`` vocabulary.

Rules are ordered; the first match wins.  An unmatched reason is
``UNCLASSIFIED`` and counts as unresolved in the coverage report.
"""

from __future__ import annotations

import re

from research.kr_delist_repair.model import PayoutSemantics, TerminalOutcome

_SPAC_NAME = re.compile(r"스팩|기업인수목적|SPAC|제\d+호", re.I)
_STATUTORY = (
    "감사의견",
    "감사범위",
    "자본잠식",
    "자본전액잠식",
    "부도",
    "계속성",
    "투명성",
    "건전성",
    "시가총액",
    "매출액",
    "영업손실",
    "미제출",
    "제출하지",
    "주식분포",
    "공시",
    "파산",
)
_PREFERRED_FLOAT = ("상장주식수", "주주수", "거래량", "시가총액", "유동성")


def is_preferred(symbol: str) -> bool:
    """KRX convention: a common share's code ends in ``0``."""
    return not symbol.endswith("0")


def classify(
    reason: str, *, name: str, symbol: str
) -> tuple[TerminalOutcome, PayoutSemantics]:
    r = re.sub(r"\s+", "", reason)
    preferred = is_preferred(symbol)
    if "이전상장" in r or r in {"유가증권시장상장", "코스닥시장상장"}:
        return TerminalOutcome.MARKET_TRANSFER, PayoutSemantics.CONTINUES_LISTED
    if "스팩소멸합병" in r:
        return TerminalOutcome.SPAC_MERGER, PayoutSemantics.CONSIDERATION
    if "합병" in r and "예비심사" not in r:
        return TerminalOutcome.MERGED_ABSORBED, PayoutSemantics.CONSIDERATION
    if any(k in r for k in ("완전자회사", "지주회사", "주식교환", "주식이전")):
        return TerminalOutcome.SHARE_EXCHANGE, PayoutSemantics.CONSIDERATION
    if "예비심사" in r and _SPAC_NAME.search(name):
        return TerminalOutcome.SPAC_DEADLINE, PayoutSemantics.TRUST_RETURN
    if "존속기간만료" in r or "존속기한만료" in r:
        if preferred:
            return (
                TerminalOutcome.PREFERRED_CONVERSION,
                PayoutSemantics.CONVERTED_TO_COMMON,
            )
        if _SPAC_NAME.search(name):
            return TerminalOutcome.SPAC_DEADLINE, PayoutSemantics.TRUST_RETURN
        return TerminalOutcome.FUND_TERM_EXPIRY, PayoutSemantics.AMBIGUOUS
    if "보통주" in r and "전환" in r and preferred:
        return TerminalOutcome.PREFERRED_CONVERSION, PayoutSemantics.CONVERTED_TO_COMMON
    if "신청" in r:
        return TerminalOutcome.VOLUNTARY, PayoutSemantics.TENDER_OR_LAST_PRICE
    if "해산" in r and "파산" not in r:
        # KIND uses 해산 사유 발생 both for absorption mergers and for real
        # liquidations; the list row cannot tell them apart.
        return (
            TerminalOutcome.DISSOLUTION_MERGER_OR_LIQUIDATION,
            PayoutSemantics.AMBIGUOUS,
        )
    if preferred and any(k in r for k in _PREFERRED_FLOAT):
        return TerminalOutcome.PREFERRED_FLOAT, PayoutSemantics.AMBIGUOUS
    if any(k in r for k in _STATUTORY) or "예비심사" in r:
        return (
            TerminalOutcome.STATUTORY_CAUSE,
            PayoutSemantics.RESIDUAL_AFTER_LIQUIDATION_TRADING,
        )
    return TerminalOutcome.UNCLASSIFIED, PayoutSemantics.AMBIGUOUS
