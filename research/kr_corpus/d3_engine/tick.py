"""Pure-data KRX tick-table loader and Decimal alignment."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import yaml

from research.kr_corpus.d3_engine.constants import TICK_TABLE_SHA256


class InvalidTickTable(ValueError):
    code = "RUN_INVALID_TICK_TABLE"


@dataclass(frozen=True, slots=True)
class TickBand:
    lower_inclusive: Decimal
    upper_exclusive: Decimal | None
    tick: Decimal


@dataclass(frozen=True, slots=True)
class TickTable:
    bands: tuple[TickBand, ...]
    schema_version: str = "d3.krx_tick_table.v1"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> TickTable:
        schema = str(payload.get("schema_version", payload.get("schema", "")))
        raw_bands = payload.get("bands")
        if schema and schema != "d3.krx_tick_table.v1":
            raise InvalidTickTable(f"unsupported schema {schema}")
        if not isinstance(raw_bands, Sequence) or isinstance(raw_bands, (str, bytes)):
            raise InvalidTickTable("bands must be a non-empty sequence")
        bands: list[TickBand] = []
        for raw in raw_bands:
            if not isinstance(raw, Mapping):
                raise InvalidTickTable("band must be a mapping")
            try:
                lower = Decimal(str(raw["lower_inclusive"]))
                upper_raw = raw.get("upper_exclusive")
                upper = None if upper_raw is None else Decimal(str(upper_raw))
                tick = Decimal(str(raw["tick"]))
            except (KeyError, TypeError, ArithmeticError) as exc:
                raise InvalidTickTable("invalid band scalar") from exc
            bands.append(TickBand(lower, upper, tick))
        table = cls(tuple(bands))
        table.validate()
        return table

    def validate(self) -> None:
        if not self.bands:
            raise InvalidTickTable("empty bands")
        if self.bands[0].lower_inclusive != 0:
            raise InvalidTickTable(f"gap [0,{self.bands[0].lower_inclusive})")
        for index, band in enumerate(self.bands):
            if band.tick <= 0 or not band.tick.is_finite():
                raise InvalidTickTable("non-positive tick")
            if band.lower_inclusive < 0 or not band.lower_inclusive.is_finite():
                raise InvalidTickTable("invalid lower bound")
            if band.upper_exclusive is not None:
                if (
                    not band.upper_exclusive.is_finite()
                    or band.upper_exclusive <= band.lower_inclusive
                ):
                    raise InvalidTickTable("non-monotonic upper bound")
            if index == len(self.bands) - 1:
                if band.upper_exclusive is not None:
                    raise InvalidTickTable("final band must be open-ended")
                continue
            if band.upper_exclusive is None:
                raise InvalidTickTable("only final band may be open-ended")
            following = self.bands[index + 1]
            if following.lower_inclusive < band.upper_exclusive:
                raise InvalidTickTable(
                    f"overlap [{following.lower_inclusive},{band.upper_exclusive})"
                )
            if following.lower_inclusive > band.upper_exclusive:
                raise InvalidTickTable(
                    f"gap [{band.upper_exclusive},{following.lower_inclusive})"
                )

    def band_for(self, price: Decimal) -> TickBand:
        if not price.is_finite() or price < 0:
            raise ValueError("price must be finite and non-negative")
        for band in self.bands:
            if price < band.lower_inclusive:
                continue
            if band.upper_exclusive is None or price < band.upper_exclusive:
                return band
        raise InvalidTickTable(f"no band for price {price}")

    def is_valid_price(self, price: Decimal) -> bool:
        band = self.band_for(price)
        return (price - band.lower_inclusive) % band.tick == 0

    def align_buy(self, raw: Decimal) -> Decimal:
        band = self.band_for(raw)
        steps = (raw / band.tick).to_integral_value(rounding=ROUND_FLOOR)
        aligned = steps * band.tick
        if not self.is_valid_price(aligned):
            raise InvalidTickTable(f"buy alignment not table-valid: {aligned}")
        return aligned

    def align_sell(self, raw: Decimal) -> Decimal:
        band = self.band_for(raw)
        steps = (raw / band.tick).to_integral_value(rounding=ROUND_CEILING)
        aligned = steps * band.tick
        if not self.is_valid_price(aligned):
            # A ceil can cross into a band with a larger tick. Re-align there.
            next_band = self.band_for(aligned)
            steps = (aligned / next_band.tick).to_integral_value(rounding=ROUND_CEILING)
            aligned = steps * next_band.tick
        if not self.is_valid_price(aligned):
            raise InvalidTickTable(f"sell alignment not table-valid: {aligned}")
        return aligned

    def sell_limit(self, raw_target: Decimal) -> Decimal:
        target = self.align_sell(raw_target)
        tick = self.band_for(target).tick
        candidate = target - tick if tick <= raw_target * Decimal("0.0005") else target
        if candidate < 0 or not self.is_valid_price(candidate):
            raise InvalidTickTable(f"sell minus-one-tick result invalid: {candidate}")
        return candidate


def load_tick_table(
    path: Path, *, expected_sha256: str = TICK_TABLE_SHA256
) -> TickTable:
    """Hash-check and parse YAML. The provenance Python module is never imported."""

    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise InvalidTickTable(f"sha drift {actual}!={expected_sha256}")
    payload = yaml.safe_load(raw)
    if not isinstance(payload, Mapping):
        raise InvalidTickTable("tick YAML root must be a mapping")
    return TickTable.from_mapping(payload)
