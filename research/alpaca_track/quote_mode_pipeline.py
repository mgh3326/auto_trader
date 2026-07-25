"""ROB-1059 H1 remediation (S1: AC6/AC7/AC8 were unenforced dead code) — the
single call site that wires ``quote_mode.py`` into the actual corpus-building
pipeline.

Before this module existed, no production code imported ``quote_mode.py`` at
all: ``corpus_builder.py`` took a bare ``quote_mode: str`` from its caller
purely as a manifest label, and ``pit_universe_alpaca.py:105`` carried a
comment (``# from quote_mode.resolve_quote_mode(...)``) that was an
aspiration, never a real call. Consequently:

  - AC8 (fail closed on a sealed-universe-map mismatch) was exercised only by
    ``quote_mode.py``'s own unit tests, never by anything a real build run
    would hit.
  - AC6 (``SYNTH_USDC`` price reconstruction, ``P = P_USDT / P_USDCUSDT``) had
    no implemented path anywhere that actually called
    ``quote_mode.synth_usdc_price`` to build a corpus.
  - AC7 (record, per UTC day, the ``|USDCUSDT-1|>30bp`` flag for
    ``USDT_PROXY`` symbols) had a predicate (``usdcusdt_basis_drift_flag``)
    nobody called and no field anywhere to record the result into the corpus.

This module fixes all three: ``resolve_and_validate_candidate_quote_mode`` is
the real call site the ``pit_universe_alpaca.SymbolCandidate.binance_quote_mode``
comment now points at, and ``build_quote_mode_aware_corpus``/
``build_corpus_manifest_with_quote_modes`` are the real, quote-mode-dispatched
corpus-building call sites (AC6/AC7), each validating against the sealed
universe map before any network fetch is attempted (AC8).

Zero broker/order/fill/scheduler/DB wiring, same as every other module in this
package: only ``quote_mode``/``corpus_builder``/``corpus_manifest`` (local),
``rob941_kline_schema`` (composition, not a fork), and stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

import canonical_hash
import quote_mode as qm
import rob941_kline_schema as ks
import spot_archive_fetch as saf
from corpus_builder import build_symbol_corpus
from corpus_manifest import CorpusManifest, SymbolCorpusManifest

DAY_MS = 86_400_000
MINUTE_MS = 60_000

__all__ = [
    "QuoteModePipelineError",
    "CandidateQuoteModeSpec",
    "build_corpus_manifest_with_quote_modes",
    "build_quote_mode_aware_corpus",
    "resolve_and_validate_candidate_quote_mode",
]


class QuoteModePipelineError(ValueError):
    """Base for pipeline-level fail-closed rejections distinct from any
    single-symbol ``corpus_builder`` error."""


def _ms_to_utc_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date()


def resolve_and_validate_candidate_quote_mode(
    *,
    base: str,
    base_usdc_first_1m: date | None,
    base_usdt_first_1m: date | None,
    usdc_usdt_available: bool,
    required_backtest_start: date,
    sealed: dict[str, qm.SealedPairRecord],
) -> str:
    """AC8: the real call site ``pit_universe_alpaca.SymbolCandidate``'s
    ``binance_quote_mode`` comment names. Computes the §14.2 mapping via
    ``quote_mode.resolve_quote_mode`` and validates it against the sealed
    universe map BEFORE returning -- a mismatch raises
    ``quote_mode.SealedUniverseMapMismatchError`` (fail closed), never
    silently proceeds with an unvalidated mapping.
    """
    computed = qm.resolve_quote_mode(
        base_usdc_first_1m=base_usdc_first_1m,
        base_usdt_first_1m=base_usdt_first_1m,
        usdc_usdt_available=usdc_usdt_available,
        required_backtest_start=required_backtest_start,
    )
    qm.validate_against_sealed_universe_map(
        base=base,
        computed_quote_mode=computed,
        computed_usdc_first_1m=base_usdc_first_1m,
        computed_usdt_first_1m=base_usdt_first_1m,
        sealed=sealed,
    )
    return computed


def _usdcusdt_daily_drift_flags(
    usdcusdt_rows: Sequence[ks.NormalizedKline],
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[tuple[str, bool], ...]:
    """AC7: per-UTC-day ``True`` iff ANY observed USDCUSDT minute that day has
    ``|price-1|>30bp``. A day with zero observed USDCUSDT rows records
    ``False`` -- there is no evidence of drift from the available data, which
    is a conservative "not flagged" default, never a fabricated ``True``.
    """
    by_day: dict[int, list[ks.NormalizedKline]] = {}
    for row in usdcusdt_rows:
        day = (row.open_time_ms // DAY_MS) * DAY_MS
        by_day.setdefault(day, []).append(row)

    flags: list[tuple[str, bool]] = []
    day = (window_start_ms // DAY_MS) * DAY_MS
    while day < window_end_ms:
        day_rows = by_day.get(day, ())
        flagged = any(qm.usdcusdt_basis_drift_flag(row.close) for row in day_rows)
        flags.append((_ms_to_utc_date(day).isoformat(), flagged))
        day += DAY_MS
    return tuple(flags)


def build_quote_mode_aware_corpus(
    *,
    base: str,
    computed_quote_mode: str,
    computed_usdc_first_1m: date | None,
    computed_usdt_first_1m: date | None,
    sealed: dict[str, qm.SealedPairRecord],
    window_start_ms: int,
    window_end_ms: int,
    archive_opener: saf.Opener = saf.urllib_opener,
    rest_opener: saf.RestOpener = saf.rest_urllib_opener,
) -> tuple[list[ks.NormalizedKline], SymbolCorpusManifest]:
    """AC6/AC7/AC8: the real quote-mode-dispatched corpus-building call site.

    AC8 fail-closed gate FIRST, before any network fetch is attempted:
    ``quote_mode.validate_against_sealed_universe_map`` raises
    ``SealedUniverseMapMismatchError`` if the caller's computed mapping
    disagrees with the sealed universe map.

    Then dispatches by ``computed_quote_mode``:

      - ``USDC``:        fetch ``{base}USDC`` directly.
      - ``SYNTH_USDC``:  fetch ``{base}USDT`` and ``USDCUSDT``, divide
                         per-minute (``quote_mode.synth_usdc_price``); a
                         missing USDCUSDT minute makes THAT minute missing in
                         the synthesized series -- never forward-filled.
      - ``USDT_PROXY``:  fetch ``{base}USDT`` directly; ALSO fetch USDCUSDT
                         over the same window purely to compute+record the
                         per-UTC-day basis-drift flag onto the manifest
                         (recorded only, never applied/excluded, per AC7).
      - ``NO_MAPPING``:  never reachable here in a correct pipeline -- PIT
                         universe rule 3 excludes NO_MAPPING symbols before
                         any corpus fetch is attempted. Raises
                         ``QuoteModePipelineError`` defensively.
    """
    qm.validate_against_sealed_universe_map(
        base=base,
        computed_quote_mode=computed_quote_mode,
        computed_usdc_first_1m=computed_usdc_first_1m,
        computed_usdt_first_1m=computed_usdt_first_1m,
        sealed=sealed,
    )

    if computed_quote_mode == "USDC":
        return build_symbol_corpus(
            f"{base}USDC",
            "USDC",
            window_start_ms,
            window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )

    if computed_quote_mode == "SYNTH_USDC":
        usdt_rows, usdt_manifest = build_symbol_corpus(
            f"{base}USDT",
            "SYNTH_USDC",
            window_start_ms,
            window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )
        usdcusdt_rows, usdcusdt_manifest = build_symbol_corpus(
            "USDCUSDT",
            "SYNTH_USDC",
            window_start_ms,
            window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )
        usdcusdt_by_ts = {r.open_time_ms: r for r in usdcusdt_rows}
        synth_rows: list[ks.NormalizedKline] = []
        for row in usdt_rows:
            basis = usdcusdt_by_ts.get(row.open_time_ms)
            basis_close = basis.close if basis is not None else None
            close = qm.synth_usdc_price(row.close, basis_close)
            if close is None:
                # missing USDCUSDT minute -> this minute is missing in the
                # synthesized series, never forward-filled (AC6).
                continue
            synth_rows.append(
                ks.NormalizedKline(
                    symbol=f"{base}USDC",
                    open_time_ms=row.open_time_ms,
                    open=qm.synth_usdc_price(row.open, basis_close),
                    high=qm.synth_usdc_price(row.high, basis_close),
                    low=qm.synth_usdc_price(row.low, basis_close),
                    close=close,
                    base_volume=row.base_volume,
                    close_time_ms=row.close_time_ms,
                    quote_volume=row.quote_volume,
                    trade_count=row.trade_count,
                    taker_buy_volume=row.taker_buy_volume,
                    taker_buy_quote_volume=row.taker_buy_quote_volume,
                )
            )

        expected_count = (window_end_ms - window_start_ms) // MINUTE_MS
        synth_open_times = {r.open_time_ms for r in synth_rows}
        missing = tuple(
            sorted(
                t
                for t in range(window_start_ms, window_end_ms, MINUTE_MS)
                if t not in synth_open_times
            )
        )
        manifest = SymbolCorpusManifest(
            symbol=f"{base}USDC",
            quote_mode="SYNTH_USDC",
            sources=usdt_manifest.sources + usdcusdt_manifest.sources,
            row_count=len(synth_rows),
            expected_count=int(expected_count),
            missing_open_times_ms=missing,
            normalized_content_sha256=canonical_hash.canonical_sha256(
                [r.__dict__ for r in synth_rows]
            ),
        )
        return synth_rows, manifest

    if computed_quote_mode == "USDT_PROXY":
        usdt_rows, usdt_manifest = build_symbol_corpus(
            f"{base}USDT",
            "USDT_PROXY",
            window_start_ms,
            window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )
        usdcusdt_rows, _ = build_symbol_corpus(
            "USDCUSDT",
            "USDT_PROXY",
            window_start_ms,
            window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )
        drift_flags = _usdcusdt_daily_drift_flags(
            usdcusdt_rows, window_start_ms, window_end_ms
        )
        manifest = replace(usdt_manifest, usdcusdt_basis_drift_flags=drift_flags)
        return usdt_rows, manifest

    raise QuoteModePipelineError(
        f"{base}: build_quote_mode_aware_corpus must never be reached with "
        f"computed_quote_mode={computed_quote_mode!r} -- PIT universe rule 3 "
        f"must exclude NO_MAPPING symbols before any corpus fetch is attempted"
    )


@dataclass(frozen=True)
class CandidateQuoteModeSpec:
    """One candidate base's already-computed §14.2 mapping inputs, ready to be
    validated against the sealed map and dispatched (AC6/AC7/AC8)."""

    base: str
    computed_quote_mode: str
    computed_usdc_first_1m: date | None
    computed_usdt_first_1m: date | None


def build_corpus_manifest_with_quote_modes(
    candidates: Sequence[CandidateQuoteModeSpec],
    *,
    sealed: dict[str, qm.SealedPairRecord],
    window_start_ms: int,
    window_end_ms: int,
    archive_opener: saf.Opener = saf.urllib_opener,
    rest_opener: saf.RestOpener = saf.rest_urllib_opener,
) -> tuple[dict[str, list[ks.NormalizedKline]], CorpusManifest]:
    """The real, concrete ``CorpusManifest`` PRODUCER (AC3) that ties AC6/AC7/
    AC8 together end to end: for each candidate base, resolve+validate its
    quote_mode against the sealed map, dispatch the quote-mode-aware corpus
    build, and assemble every symbol's ``SymbolCorpusManifest`` into ONE
    ``CorpusManifest`` in canonical lexicographic (final-symbol) order.

    Previously no code path built a ``CorpusManifest`` outside its own unit
    tests, so any manifest ``content_hash`` reported anywhere was
    unreproducible. Re-running this SAME function over the SAME
    already-collected shards (no new network collection) reproduces the
    identical ``content_hash()``.
    """
    per_symbol: list[SymbolCorpusManifest] = []
    rows_by_symbol: dict[str, list[ks.NormalizedKline]] = {}
    for spec in candidates:
        rows, manifest = build_quote_mode_aware_corpus(
            base=spec.base,
            computed_quote_mode=spec.computed_quote_mode,
            computed_usdc_first_1m=spec.computed_usdc_first_1m,
            computed_usdt_first_1m=spec.computed_usdt_first_1m,
            sealed=sealed,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            archive_opener=archive_opener,
            rest_opener=rest_opener,
        )
        rows_by_symbol[manifest.symbol] = rows
        per_symbol.append(manifest)

    per_symbol_sorted = tuple(sorted(per_symbol, key=lambda m: m.symbol))
    symbols = tuple(m.symbol for m in per_symbol_sorted)
    corpus_manifest = CorpusManifest(
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        symbols=symbols,
        per_symbol=per_symbol_sorted,
    )
    return rows_by_symbol, corpus_manifest
