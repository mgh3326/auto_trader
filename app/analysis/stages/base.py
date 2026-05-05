# app/analysis/stages/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from app.schemas.research_pipeline import StageOutput


@dataclass(frozen=True)
class StageContext:
    session_id: int
    symbol: str
    instrument_type: str
    user_id: int | None = None


class BaseStageAnalyzer(ABC):
    stage_type: ClassVar[str]  # override in subclass

    @abstractmethod
    async def analyze(self, ctx: StageContext) -> StageOutput: ...

    async def run(self, ctx: StageContext) -> StageOutput:
        out = await self.analyze(ctx)
        if out.stage_type != self.stage_type:
            raise ValueError(
                f"stage_type mismatch: analyzer={self.stage_type} output={out.stage_type}"
            )
        return out
