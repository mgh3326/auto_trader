"""Account source -> visual tone/badge mapping for /invest desktop UI."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict

Tone = Literal["navy", "gray", "purple", "green", "dashed"]
BadgeText = Literal["Live", "Mock", "Crypto", "Paper", "Manual"]


class AccountSourceVisual(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    tone: Tone
    badge: BadgeText
    displayName: str


_VISUAL_MAP: dict[str, tuple[Tone, BadgeText, str]] = {
    "kis": ("navy", "Live", "한국투자증권"),
    "kis_mock": ("gray", "Mock", "한국투자증권 모의"),
    "kiwoom_mock": ("gray", "Mock", "키움 모의"),
    "upbit": ("purple", "Crypto", "업비트"),
    "alpaca_paper": ("green", "Paper", "Alpaca Paper"),
    "toss_manual": ("dashed", "Manual", "토스 수동"),
    "pension_manual": ("dashed", "Manual", "연금 수동"),
    "isa_manual": ("dashed", "Manual", "ISA 수동"),
    "db_simulated": ("dashed", "Manual", "시뮬레이션"),
}


def visual_for(source: str) -> AccountSourceVisual:
    tone, badge, display = _VISUAL_MAP.get(source, ("gray", "Manual", source))
    return AccountSourceVisual(source=source, tone=tone, badge=badge, displayName=display)


def all_visuals() -> list[AccountSourceVisual]:
    return [visual_for(s) for s in _VISUAL_MAP]
