"""Render invariant declarations for CIO coin briefing markdown."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Invariant:
    name: str
    description: str
    check: Callable[[str], bool]


def _parser_not_implemented(_: str) -> bool:
    raise NotImplementedError("Render invariant parser lands in ROB-221 E-4.")


RENDER_INVARIANTS: list[Invariant] = [
    Invariant(
        name="funding_rows_split",
        description="exchange_krw 행과 unverified_cap 행이 §3 자금 현황 안에서 각 1회씩 등장",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="runway_excludes_unverified_cap",
        description="runway 산식 (현재 runway / TC preliminary 의 runway) 에 unverified_cap.amount 가 포함되지 않음",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="framing_ab_anchor",
        description="FRAMING_AB_PATH_NON_EXCLUSIVE anchor 가 Framing 에 존재",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="path_section_ab_anchor",
        description="PATH_SECTION_AB_REPEAT anchor 가 §7 말미에 존재",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="board_questions_split",
        description="§10 보드 질문이 [funding] (또는 [funding-confirmation]) 1 행 + [action] 1 행, 총 2 행 분리",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="g2_phrase_mutual_exclusivity",
        description="G2_RUNWAY_FUEL_LINES 와 G2_NEW_BUDGET_LINES 중 정확히 하나만 삽입 (둘 다 불가, 둘 다 없음 불가)",
        check=_parser_not_implemented,
    ),
    Invariant(
        name="immediate_buy_requires_g2_to_g5_pass",
        description="CIO 권고가 '(1) 즉시 매수' 이면 G2~G5 모두 pass — 하나라도 fail/대기/차단이면 assertion fail",
        check=_parser_not_implemented,
    ),
]
