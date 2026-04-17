"""Prompt v2 phrase constants used by board brief renderers."""

G2_RUNWAY_FUEL_LINES = [
    "- 이번 {amount} 원은 **운영 연료** 로 귀속 — coinmoogi DCA {days} 일 지속분 + 만기 cushion.",
    "- 신규 매수 여력으로 전용 금지. G2 에서 차단.",
]

G2_NEW_BUDGET_LINES = [
    "- 이번 {amount} 원은 G3 (runway/obligation) 통과 후 신규 risk budget 후보.",
    "- 이 경우에도 G4 시장 regime → G5 volatility halt → G6 보조지표 통과 여부 추가 판정 필요.",
]

FRAMING_AB_PATH_NON_EXCLUSIVE = (
    "경로 A (입금) 와 경로 B (현물 부분매도) 는 **상호배타 아님**. 병행 가능합니다."
)

PATH_SECTION_AB_REPEAT = "**A 와 B 는 상호배타 아님 — 병행 가능.**"

BOARD_QUESTIONS_TEMPLATE = """질문 (Step 1 답변 반영 — 재질문 아님)
1) **[funding-confirmation]** manual_cash 중 오늘 실제 입금 가능액이 있습니까? 있다면 얼마, 언제까지?
2) **[action]** 부분매도를 Hard Gate critique 에 올려 실행하시겠습니까?"""
