"""Deterministic synthetic-only CRS-24 coverage fixture."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from rob974_features import Bar4h
from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_contracts import DAY_MS, FOUR_HOUR_MS, UNIVERSE
from rob1040_crs24_feasibility import (
    EntryReference,
    ExitPresence,
    ReferenceKey,
    ReferenceSurface,
    is_horizon_eligible,
    scheduled_cutoffs,
)
from rob1040_crs24_features import CRSFeatureGenerator

from research_contracts.canonical_hash import canonical_sha256

SYNTHETIC_FIXTURE_VERSION = "rob1040.crs24.corr1.synthetic.v1"
SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256 = (
    "429c1e26490cef60652df6ef75f23f2fb386092815ea71019ea0041bf2a9f14f"
)
SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256 = (
    "5cc827b30d3a12ad5799f9da10cd866292b1e9708b4a734cf9296e3285157669"
)
SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256 = (
    "14a217e426ef8c6f4c02661f6bfbd128a467263e1121665b983223d6a306bf43"
)
SYNTHETIC_FIXTURE_CONTENT_SHA256 = (
    "718d1bd865cc77dc6b26d44c1da2859b8180ad6f4a8520dfdbef9f8ae35b5b49"
)

_BASE_REFERENCE = {
    "XRPUSDT": Decimal("0.5"),
    "DOGEUSDT": Decimal("0.2"),
    "SOLUSDT": Decimal("150"),
}


@dataclass(frozen=True, slots=True)
class SyntheticSymbolBars:
    symbol: str
    bars: tuple[Bar4h, ...]

    def __post_init__(self) -> None:
        if self.symbol not in UNIVERSE:
            raise ValueError("synthetic series symbol is outside the universe")
        if type(self.bars) is not tuple or any(
            type(bar) is not Bar4h for bar in self.bars
        ):
            raise TypeError("synthetic bars must be an exact Bar4h tuple")


@dataclass(frozen=True, slots=True)
class SyntheticFixture:
    version: str
    series: tuple[SyntheticSymbolBars, ...]
    references: ReferenceSurface

    def __post_init__(self) -> None:
        if self.version != SYNTHETIC_FIXTURE_VERSION:
            raise ValueError("synthetic fixture version drifted")
        if (
            type(self.series) is not tuple
            or tuple(item.symbol for item in self.series) != UNIVERSE
        ):
            raise ValueError("synthetic series order drifted")
        if type(self.references) is not ReferenceSurface:
            raise TypeError("synthetic references must be exact ReferenceSurface")

    def bars_by_symbol(self) -> dict[str, tuple[Bar4h, ...]]:
        return {item.symbol: item.bars for item in self.series}


def _return_components(index: int) -> tuple[float, float, float]:
    common = (
        0.00045 * math.sin(index * 0.173)
        + 0.00018 * math.cos(index * 0.037)
        + 0.00006 * math.sin(index * 0.011)
    )
    xrp_residual = 0.00110 * math.sin(index * 0.119) + 0.00031 * math.cos(index * 0.047)
    doge_residual = 0.00093 * math.cos(index * 0.137) - 0.00027 * math.sin(
        index * 0.059
    )
    sol_residual = -xrp_residual - doge_residual
    return (
        common + xrp_residual,
        common + doge_residual,
        common + sol_residual,
    )


def _synthetic_bars(
    *,
    symbol_index: int,
    start_close_ms: int,
    end_close_ms: int,
    base: float,
) -> tuple[Bar4h, ...]:
    prior_close = base
    bars: list[Bar4h] = []
    close_ts = start_close_ms
    index = 0
    while close_ts <= end_close_ms:
        raw_return = _return_components(index)[symbol_index]
        close = prior_close * math.exp(raw_return)
        high = max(prior_close, close) * 1.0007
        low = min(prior_close, close) * 0.9993
        bars.append(
            Bar4h(
                ts=close_ts - FOUR_HOUR_MS,
                close_ts=close_ts,
                open=prior_close,
                high=high,
                low=low,
                close=close,
                volume=1000.0 + symbol_index * 100.0 + index % 17,
                is_segment_start=index == 0,
            )
        )
        prior_close = close
        close_ts += FOUR_HOUR_MS
        index += 1
    return tuple(bars)


def build_synthetic_fixture() -> SyntheticFixture:
    folds = exact_h4_folds()
    first_cutoff = scheduled_cutoffs(folds[0])[0]
    last_cutoff = scheduled_cutoffs(folds[-1])[-1]
    start_close_ms = first_cutoff - 80 * DAY_MS
    if start_close_ms % FOUR_HOUR_MS:
        raise ValueError("synthetic start must remain UTC 4h aligned")
    series = tuple(
        SyntheticSymbolBars(
            symbol=symbol,
            bars=_synthetic_bars(
                symbol_index=index,
                start_close_ms=start_close_ms,
                end_close_ms=last_cutoff,
                base=float(_BASE_REFERENCE[symbol]),
            ),
        )
        for index, symbol in enumerate(UNIVERSE)
    )
    entry_rows: list[EntryReference] = []
    exit_rows: list[ExitPresence] = []
    for fold in folds:
        for cutoff_ms in scheduled_cutoffs(fold):
            if not is_horizon_eligible(fold, cutoff_ms):
                continue
            for symbol in UNIVERSE:
                entry_rows.append(
                    EntryReference(
                        ReferenceKey(symbol, cutoff_ms + 60_000),
                        _BASE_REFERENCE[symbol],
                    )
                )
                exit_rows.append(
                    ExitPresence(
                        ReferenceKey(symbol, cutoff_ms + DAY_MS + 60_000),
                        True,
                    )
                )
    references = ReferenceSurface(tuple(entry_rows), tuple(exit_rows))
    fixture = SyntheticFixture(SYNTHETIC_FIXTURE_VERSION, series, references)
    validate_frozen_synthetic_fixture(fixture)
    return fixture


def fixture_content_sha256(
    generator: CRSFeatureGenerator,
    references: ReferenceSurface,
) -> str:
    return canonical_sha256(
        {
            "version": SYNTHETIC_FIXTURE_VERSION,
            "complete_bar_snapshot_sha256": generator.snapshot_sha256,
            "entry_reference_source_sha256": references.entry_source_sha256,
            "exit_presence_source_sha256": references.exit_presence_source_sha256,
        }
    )


def validate_frozen_synthetic_fixture(
    fixture: SyntheticFixture,
) -> CRSFeatureGenerator:
    if type(fixture) is not SyntheticFixture:
        raise TypeError("fixture must be exact SyntheticFixture")
    generator = CRSFeatureGenerator(fixture.bars_by_symbol())
    if generator.snapshot_sha256 != SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256:
        raise ValueError("synthetic complete-bar content identity drifted")
    if (
        fixture.references.entry_source_sha256
        != SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256
    ):
        raise ValueError("synthetic entry-reference content identity drifted")
    if (
        fixture.references.exit_presence_source_sha256
        != SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256
    ):
        raise ValueError("synthetic exit-presence content identity drifted")
    if (
        fixture_content_sha256(generator, fixture.references)
        != SYNTHETIC_FIXTURE_CONTENT_SHA256
    ):
        raise ValueError("synthetic fixture content manifest drifted")
    return generator


__all__ = [
    "SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256",
    "SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256",
    "SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256",
    "SYNTHETIC_FIXTURE_CONTENT_SHA256",
    "SYNTHETIC_FIXTURE_VERSION",
    "SyntheticFixture",
    "SyntheticSymbolBars",
    "build_synthetic_fixture",
    "fixture_content_sha256",
    "validate_frozen_synthetic_fixture",
]
