"""Gate phrase constants extracted from board_briefing_v2.md."""

from __future__ import annotations

import re

G2_LINE_RUNWAY = "- G2 입금 목적: **운영 runway 복구** (신규 risk budget 아님)"
G2_LINE_NEW_BUDGET = "- G2 입금 목적: **신규 risk budget** (운영 runway 는 이미 충족)"

G2_RECOMMENDATION_FIXED = "CIO 권고: **(3) 현금 비중 유지**"

G2_RUNWAY_FUEL_LINES = [
    "- 이번 {amount} 원은 **운영 연료** 로 귀속 — coinmoogi DCA {days} 일 지속분 + 만기 cushion.",
    "- 신규 매수 여력으로 전용 금지. G2 에서 차단.",
]

G2_NEW_BUDGET_LINES = [
    "- 이번 {amount} 원은 G3 (runway/obligation) 통과 후 신규 risk budget 후보.",
    "- 이 경우에도 G4 시장 regime → G5 volatility halt → G6 보조지표 통과 여부 추가 판정 필요.",
]

HARD_GATE_REMINDER = (
    "- {symbol} 부분매도는 별도 Hard Gate critique 으로 계속 진행 "
    "(경로 B 는 concentration 문제를 여전히 해결해야 함)."
)

FRAMING_AB_PATH_NON_EXCLUSIVE = (
    "경로 A (입금) 와 경로 B (현물 부분매도) 는 **상호배타 아님**. 병행 가능합니다."
)

PATH_SECTION_AB_REPEAT = "**A 와 B 는 상호배타 아님 — 병행 가능.**"

BOARD_QUESTIONS_TEMPLATE = """### 보드에게 질문 (응답 요청, 분리)
1) **[funding]** manual_cash 중 오늘 실제 입금 가능액이 있습니까? 있다면 얼마, 언제까지?
2) **[action]** {hard_gate_symbol} 현물 {quantity_range} 부분매도를 Hard Gate critique 에 올려 실행하시겠습니까?"""

FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pattern)
    for pattern in [
        r"\[funding\].*\[action\]",
        r"가용\s*현금[^(]*\d",
        r"Planning\s*cash",
        r"\b유휴\s*자금\b",
        r"\b예비\s*자금\b",
        r"\b대기\s*자금\b",
        r"\b대기\s*cash\b",
        r"\b입금\s*여력\b",
        r"\b천만\s*원\s*(현금|cash|가용)",
        r"A\s*(또는|혹은|or)\s*B\s*(중|에서)\s*택1?",
        r"입금\s*(또는|혹은)\s*매도\s*(중|에서)\s*택1?",
    ]
]
