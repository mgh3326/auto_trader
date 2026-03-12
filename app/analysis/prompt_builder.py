from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from app.analysis.prompt import build_json_prompt, build_prompt


class PromptBuilder:
    def __init__(
        self,
        *,
        text_builder: Callable[..., str] = build_prompt,
        json_builder: Callable[..., str] = build_json_prompt,
    ) -> None:
        self._text_builder = text_builder
        self._json_builder = json_builder

    def build_text_prompt(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        *,
        currency: str = "₩",
        unit_shares: str = "주",
        fundamental_info: dict[str, Any] | None = None,
        position_info: dict[str, Any] | None = None,
        minute_candles: dict[str, pd.DataFrame] | None = None,
    ) -> str:
        del instrument_type
        return self._text_builder(
            df,
            symbol,
            name,
            currency,
            unit_shares,
            fundamental_info,
            position_info,
            minute_candles,
        )

    def build_json_prompt(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        *,
        currency: str = "₩",
        unit_shares: str = "주",
        fundamental_info: dict[str, Any] | None = None,
        position_info: dict[str, Any] | None = None,
        minute_candles: dict[str, pd.DataFrame] | None = None,
    ) -> str:
        del instrument_type
        return self._json_builder(
            df,
            symbol,
            name,
            currency,
            unit_shares,
            fundamental_info,
            position_info,
            minute_candles,
        )
