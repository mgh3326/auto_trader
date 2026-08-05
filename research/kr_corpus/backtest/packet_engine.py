"""Exact, offline KR-A0 packet engine.

This module is additive to the legacy Stage-B path.  It implements only the
three hash-bound packet candidates and has no broker, account, database, or
scheduler dependency.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, Protocol

from research.kr_corpus.registry.exact_binding import (
    CandidateBinding,
    CandidateRegistry,
)

Z90: Final = 1.2815515655446004
COST_BASE: Final = 0.0043
COST_SENSITIVITY: Final = 0.0083
POPULATION_FLOOR: Final = 20
MARKETS: Final[tuple[str, str]] = ("KOSPI", "KOSDAQ")

Identity = tuple[str, str]
MembershipInterval = tuple[int, int]
MembershipIntervals = tuple[MembershipInterval, ...]


class RunInvalid(RuntimeError):
    """Fail-closed result for an invalid packet run."""

    def __init__(
        self,
        label: str,
        detail: str = "",
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.label = label
        self.detail = detail
        self.evidence = dict(evidence or {})
        message = label if not detail else f"{label} {detail}"
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Bar:
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


@dataclass
class AccessSpy:
    """Records attempted reads that would cross the sealed exploration boundary."""

    bar_oob_reads: int = 0
    membership_oob_reads: int = 0
    source_oob_records: int = 0
    source_query_maxima: list[int] = field(default_factory=list)

    @property
    def total_oob_reads(self) -> int:
        return self.bar_oob_reads + self.membership_oob_reads

    def as_dict(self) -> dict[str, Any]:
        return {
            "bar_oob_reads": self.bar_oob_reads,
            "membership_oob_reads": self.membership_oob_reads,
            "source_oob_records": self.source_oob_records,
            "source_query_maxima": list(self.source_query_maxima),
            "total_oob_reads": self.total_oob_reads,
        }


@dataclass
class PacketWorld:
    """Boundary-clamped materialized input for one packet run."""

    bars: Mapping[tuple[Identity, int], Bar]
    symbols: tuple[Identity, ...]
    # A15-1: intervals are a mechanical projection of presence-only rows.  They
    # are observation intervals, never an implicit listing-status assertion.
    membership: Mapping[Identity, MembershipIntervals]
    delist_events: Mapping[Identity, int]
    dates: Mapping[int, date]
    month_last_sessions: frozenset[int]
    exploration_end_session_idx: int
    spy: AccessSpy = field(default_factory=AccessSpy)

    def bar(self, key: Identity, session_idx: int) -> Bar | None:
        if session_idx > self.exploration_end_session_idx:
            self.spy.bar_oob_reads += 1
            return None
        if session_idx < 0:
            return None
        return self.bars.get((key, session_idx))

    def is_member(self, key: Identity, session_idx: int) -> bool:
        if session_idx > self.exploration_end_session_idx:
            self.spy.membership_oob_reads += 1
            return False
        return any(
            start <= session_idx <= end for start, end in self.membership.get(key, ())
        )

    def membership_interval_at(
        self, key: Identity, session_idx: int
    ) -> MembershipInterval | None:
        """Return the observed presence interval containing ``session_idx``."""

        for interval in self.membership.get(key, ()):
            if interval[0] <= session_idx <= interval[1]:
                return interval
        return None

    def assert_boundary_clean(self) -> None:
        if self.spy.total_oob_reads != 0:
            raise RunInvalid(
                "RUN_INVALID_BOUNDARY_ACCESS",
                evidence=self.spy.as_dict(),
            )


class BoundedPacketSource(Protocol):
    """Explicit-query source contract used by the production materializer.

    ``fetch_membership`` returns presence-only rows keyed by ``session_idx``;
    it never supplies a listing status or precomputed interval.
    """

    def fetch_sessions(
        self, *, max_session_idx: int
    ) -> Iterable[Mapping[str, Any]]: ...

    def fetch_bars(self, *, max_session_idx: int) -> Iterable[Mapping[str, Any]]: ...

    def fetch_membership(
        self, *, max_session_idx: int
    ) -> Iterable[Mapping[str, Any]]: ...


class PacketWorldAdapter:
    """Loads a self-contained fixture directory with boundary-first materialization."""

    @classmethod
    def from_fixture_directory(cls, directory: Path | str) -> PacketWorld:
        root = Path(directory)
        config = _read_config(root / "fixture_config.csv")
        end = config.get("exploration_end_session_idx")
        if not isinstance(end, int) or end < 0:
            raise RunInvalid("RUN_INVALID_CONFIG", "exploration_end_session_idx")
        spy = AccessSpy()
        sessions = _read_csv_rows(root / "fixture_sessions.csv")
        bars = _read_csv_rows(root / "fixture_bars.csv")
        membership = _read_csv_rows(root / "fixture_membership.csv")
        # A15-1/2: a DART sidecar is optional at startup.  Its absence means
        # no C8 evidence exists; it must never be inferred from membership
        # absence or an observation interval ending.
        delist_path = root / "fixture_delist_events.csv"
        delist = _read_csv_rows(delist_path) if delist_path.is_file() else []
        return cls._materialize(
            sessions=sessions,
            bars=bars,
            membership=membership,
            delist_events=delist,
            exploration_end_session_idx=end,
            spy=spy,
            require_presence_membership=False,
        )

    @classmethod
    def from_source(
        cls,
        source: BoundedPacketSource,
        *,
        exploration_end_session_idx: int,
    ) -> PacketWorld:
        """Materialize boundary-filtered bars and presence-only membership rows."""

        if exploration_end_session_idx < 0:
            raise RunInvalid("RUN_INVALID_CONFIG", "negative exploration end")
        spy = AccessSpy()
        methods = (
            (source.fetch_sessions, _record_session_index),
            (source.fetch_bars, _record_session_index),
            (source.fetch_membership, _membership_record_index),
        )
        records: list[list[Mapping[str, Any]]] = []
        for method, indexer in methods:
            spy.source_query_maxima.append(exploration_end_session_idx)
            fetched = list(method(max_session_idx=exploration_end_session_idx))
            for record in fetched:
                idx = indexer(record)
                if idx > exploration_end_session_idx:
                    spy.source_oob_records += 1
                    raise RunInvalid(
                        "RUN_INVALID_SOURCE_BOUNDARY",
                        f"source returned s{idx} above exploration end",
                        evidence=spy.as_dict(),
                    )
            records.append(fetched)
        # The DART sidecar arrives independently.  A source without that
        # capability is valid and produces an empty evidence map (no C8).
        fetch_delist_events = getattr(source, "fetch_delist_events", None)
        if callable(fetch_delist_events):
            spy.source_query_maxima.append(exploration_end_session_idx)
            fetched_delist = list(
                fetch_delist_events(max_session_idx=exploration_end_session_idx)
            )
            for record in fetched_delist:
                # A normalized fixture can carry a session index.  The live
                # DART sidecar instead carries delist_date and is resolved
                # against the materialized session map below.
                idx = _delist_source_session_index(record)
                if idx is not None and idx > exploration_end_session_idx:
                    spy.source_oob_records += 1
                    raise RunInvalid(
                        "RUN_INVALID_SOURCE_BOUNDARY",
                        f"source returned s{idx} above exploration end",
                        evidence=spy.as_dict(),
                    )
            records.append(fetched_delist)
        else:
            records.append([])
        return cls._materialize(
            sessions=records[0],
            bars=records[1],
            membership=records[2],
            delist_events=records[3],
            exploration_end_session_idx=exploration_end_session_idx,
            spy=spy,
            require_presence_membership=True,
        )

    @classmethod
    def _materialize(
        cls,
        *,
        sessions: Iterable[Mapping[str, Any]],
        bars: Iterable[Mapping[str, Any]],
        membership: Iterable[Mapping[str, Any]],
        delist_events: Iterable[Mapping[str, Any]],
        exploration_end_session_idx: int,
        spy: AccessSpy,
        require_presence_membership: bool,
    ) -> PacketWorld:
        dates: dict[int, date] = {}
        month_last: set[int] = set()
        for row in sessions:
            idx = _record_session_index(row)
            if idx > exploration_end_session_idx:
                continue
            if idx in dates:
                raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", f"session {idx}")
            raw_date = row.get("date_informational")
            if not isinstance(raw_date, str):
                raise RunInvalid("RUN_INVALID_SESSION", f"session {idx} date")
            try:
                dates[idx] = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise RunInvalid("RUN_INVALID_SESSION", f"session {idx} date") from exc
            if str(row.get("month_last")) == "1":
                month_last.add(idx)
        expected_indices = set(range(exploration_end_session_idx + 1))
        if set(dates) != expected_indices:
            raise RunInvalid("RUN_INVALID_DATA_GAP", "session calendar")

        parsed_bars: dict[tuple[Identity, int], Bar] = {}
        for row in bars:
            idx = _record_session_index(row)
            if idx > exploration_end_session_idx:
                continue
            key = _identity_from_row(row)
            compound = (key, idx)
            if compound in parsed_bars:
                raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", str(compound))
            parsed_bars[compound] = Bar(
                open=_optional_float(row.get("open")),
                high=_optional_float(row.get("high")),
                low=_optional_float(row.get("low")),
                close=_optional_float(row.get("close")),
                volume=_optional_int(row.get("volume")),
            )

        parsed_membership = _membership_intervals_from_rows(
            membership,
            exploration_end_session_idx=exploration_end_session_idx,
            require_presence_rows=require_presence_membership,
        )
        symbols = list(parsed_membership)

        parsed_delist: dict[Identity, int] = {}
        for row in delist_events:
            key = _identity_from_row(row)
            if key in parsed_delist:
                raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", f"delist {key}")
            event_session = _delist_event_session_index(row, dates)
            if (
                event_session is not None
                and event_session <= exploration_end_session_idx
            ):
                parsed_delist[key] = event_session

        return PacketWorld(
            bars=parsed_bars,
            symbols=tuple(symbols),
            membership=parsed_membership,
            delist_events=parsed_delist,
            dates=dates,
            month_last_sessions=frozenset(month_last),
            exploration_end_session_idx=exploration_end_session_idx,
            spy=spy,
        )


@dataclass(frozen=True)
class CandidateRun:
    """Raw engine artifacts plus a stamped serialized output surface."""

    binding: CandidateBinding
    input_shas: Mapping[str, Any]
    log: Mapping[str, Any]
    trades: tuple[Mapping[str, Any], ...]
    baselines: Mapping[str, Mapping[str, Any]]
    verdict: Mapping[str, Any]

    def golden_payload(self) -> dict[str, Any]:
        """Unstamped payload used only for comparison to upstream golden vectors."""

        return {
            "log": dict(self.log),
            "trades": [dict(trade) for trade in self.trades],
            "baselines": {key: dict(value) for key, value in self.baselines.items()},
            "verdict": dict(self.verdict),
        }

    def to_dict(self) -> dict[str, Any]:
        stamp = {
            "strategy_id": self.binding.strategy_id,
            "contract_hash": self.binding.contract_hash,
        }
        return {
            **stamp,
            "input_shas": dict(self.input_shas),
            "log": {**stamp, **dict(self.log)},
            "trades": [{**stamp, **dict(trade)} for trade in self.trades],
            "baselines": {
                key: {**stamp, **dict(value)} for key, value in self.baselines.items()
            },
            "verdict": {**stamp, **dict(self.verdict)},
        }


class PacketEngine:
    """The three exact packet formulae; no shared signal fallback exists."""

    def __init__(self, registry: CandidateRegistry) -> None:
        self._registry = registry

    def run(self, world: PacketWorld, strategy_id: str) -> CandidateRun:
        binding = self._registry.binding_for(strategy_id)
        self._check_identity(world)
        self._check_data_gaps(world)
        log, trades = self._execute_candidate(world, binding)
        baselines = self._build_baselines(world, binding, trades)
        verdict = self._compose_verdict(
            world.dates,
            trades,
            baselines,
            missing_exit_count=len(log["missing_exit"]),
        )
        world.assert_boundary_clean()
        return CandidateRun(
            binding=binding,
            input_shas=self._registry.inputs.as_dict(),
            log=log,
            trades=tuple(trades),
            baselines=baselines,
            verdict=verdict,
        )

    def signals(
        self, world: PacketWorld, strategy_id: str, session_idx: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Expose an exact signal snapshot for golden-level assertions."""

        binding = self._registry.binding_for(strategy_id)
        return self._signals_for(binding, world, session_idx)

    def _signals_for(
        self, binding: CandidateBinding, world: PacketWorld, session_idx: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if binding.strategy_id == "rev3_reclaim":
            return self._signals_rev3(binding, world, session_idx)
        if binding.strategy_id == "brk20_confirm":
            return self._signals_brk20(binding, world, session_idx)
        if binding.strategy_id == "lowvol_up_month":
            return self._signals_lowvol(binding, world, session_idx)
        raise RunInvalid(
            "RUN_INVALID_UNBOUND_STRATEGY",
            f"shared fallback forbidden for {binding.strategy_id}",
        )

    def _check_identity(self, world: PacketWorld) -> None:
        by_symbol: dict[str, list[tuple[str, int, int]]] = {}
        for (market, symbol), intervals in world.membership.items():
            by_symbol.setdefault(symbol, []).extend(
                (market, start, end) for start, end in intervals
            )
        conflicts: list[str] = []
        for symbol, entries in by_symbol.items():
            for left_idx, left in enumerate(entries):
                for right in entries[left_idx + 1 :]:
                    if left[0] == right[0]:
                        continue
                    if max(left[1], right[1]) <= min(left[2], right[2]):
                        conflicts.append(symbol)
        if conflicts:
            raise RunInvalid(
                "RUN_INVALID_IDENTITY_CONFLICT",
                ",".join(sorted(conflicts)),
                evidence={"symbols": sorted(conflicts)},
            )

    def _check_data_gaps(self, world: PacketWorld) -> None:
        for session_idx in range(world.exploration_end_session_idx + 1):
            for market in MARKETS:
                if not any(
                    world.bar(key, session_idx) is not None
                    for key in world.symbols
                    if key[0] == market
                ):
                    raise RunInvalid("RUN_INVALID_DATA_GAP", f"{market}@s{session_idx}")

    def _a8_base_set(self, world: PacketWorld, session_idx: int) -> list[Identity]:
        if session_idx < 20:
            return []
        return [
            key
            for key in world.symbols
            if all(
                world.is_member(key, session_idx - offset)
                and valid_bar(world.bar(key, session_idx - offset))
                for offset in range(21)
            )
        ]

    def _liquidity_percentiles(
        self, world: PacketWorld, session_idx: int, base: Iterable[Identity]
    ) -> dict[Identity, float]:
        base_values = tuple(base)
        result: dict[Identity, float] = {}
        for market in MARKETS:
            keys = [key for key in base_values if key[0] == market]
            values = [
                _require_bar(world.bar(key, session_idx), key, session_idx).close
                * _require_bar(world.bar(key, session_idx), key, session_idx).volume
                for key in keys
            ]
            for key, value in zip(keys, values, strict=True):
                result[key] = pct_asc(values, value)
        return result

    def _per_market_percentiles(
        self, values_by_market: Mapping[str, Mapping[Identity, float]]
    ) -> dict[Identity, float]:
        """A1: each market has an independent, self-inclusive population."""

        result: dict[Identity, float] = {}
        for market in MARKETS:
            values_for_market = values_by_market.get(market, {})
            values = list(values_for_market.values())
            for key, value in values_for_market.items():
                result[key] = pct_asc(values, value)
        return result

    def _populations(
        self, binding: CandidateBinding, world: PacketWorld, session_idx: int
    ) -> tuple[dict[str, list[Identity]], dict[Identity, float], list[str]]:
        if session_idx < 20:
            return {market: [] for market in MARKETS}, {}, ["insufficient_history"]
        base = self._a8_base_set(world, session_idx)
        liquidity = self._liquidity_percentiles(world, session_idx, base)
        minimum = float(binding.parameters["liquidity_percentile_min"])
        populations: dict[str, list[Identity]] = {}
        notes: list[str] = []
        for market in MARKETS:
            population = [
                key
                for key in base
                if key[0] == market and self._at_least(liquidity[key], minimum)
            ]
            if len(population) < POPULATION_FLOOR:
                notes.append(f"POPULATION_BELOW_FLOOR:{market}:{len(population)}")
                population = []
            populations[market] = population
        return populations, liquidity, notes

    def _signals_rev3(
        self, binding: CandidateBinding, world: PacketWorld, session_idx: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        populations, liquidity, notes = self._populations(binding, world, session_idx)
        signals: list[dict[str, Any]] = []
        lookback = int(binding.parameters["reversal_lookback_sessions"])
        percentile_max = float(binding.parameters["loser_percentile_max"])
        clv_min = float(binding.parameters["close_location_min"])
        volume_min = float(binding.parameters["volume_ratio_min"])
        r3_by_market: dict[str, dict[Identity, float]] = {}
        for market in MARKETS:
            population = populations[market]
            r3_by_market[market] = {
                key: _require_bar(world.bar(key, session_idx), key, session_idx).close
                / _require_bar(
                    world.bar(key, session_idx - lookback), key, session_idx
                ).close
                - 1
                for key in population
            }
        r3_percentiles = self._per_market_percentiles(r3_by_market)
        for market in MARKETS:
            population = populations[market]
            r3 = r3_by_market[market]
            for key in population:
                bar = _require_bar(world.bar(key, session_idx), key, session_idx)
                upper, lower = self._high_c(bar), self._low_c(bar)
                if upper <= lower:
                    continue
                clv = (bar.close - lower) / (upper - lower)
                median = statistics.median(
                    _require_bar(
                        world.bar(key, session_idx - offset), key, session_idx
                    ).volume
                    for offset in range(1, 21)
                )
                ratio = bar.volume / median
                percentile = r3_percentiles[key]
                if (
                    self._at_most(percentile, percentile_max)
                    and self._at_least(clv, clv_min)
                    and self._at_least(ratio, volume_min)
                ):
                    signals.append(
                        {
                            "market": key[0],
                            "symbol": key[1],
                            "r3": r3[key],
                            "r3_pct": percentile,
                            "clv": clv,
                            "volume_ratio": ratio,
                            "liq_pct": liquidity[key],
                        }
                    )
        signals.sort(key=rev3_rank_key)
        return signals, notes

    def _signals_brk20(
        self, binding: CandidateBinding, world: PacketWorld, session_idx: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        populations, liquidity, notes = self._populations(binding, world, session_idx)
        signals: list[dict[str, Any]] = []
        lookback = int(binding.parameters["breakout_lookback_sessions"])
        clv_min = float(binding.parameters["close_location_min"])
        volume_min = float(binding.parameters["volume_ratio_min"])
        for market in MARKETS:
            for key in populations[market]:
                bar = _require_bar(world.bar(key, session_idx), key, session_idx)
                upper, lower = self._high_c(bar), self._low_c(bar)
                if upper <= lower:
                    continue
                prior_high = max(
                    self._high_c(
                        _require_bar(
                            world.bar(key, session_idx - offset), key, session_idx
                        )
                    )
                    for offset in range(1, lookback + 1)
                )
                margin = bar.close / prior_high - 1
                clv = (bar.close - lower) / (upper - lower)
                median = statistics.median(
                    _require_bar(
                        world.bar(key, session_idx - offset), key, session_idx
                    ).volume
                    for offset in range(1, 21)
                )
                ratio = bar.volume / median
                if (
                    margin > 0
                    and self._at_least(clv, clv_min)
                    and self._at_least(ratio, volume_min)
                ):
                    signals.append(
                        {
                            "market": key[0],
                            "symbol": key[1],
                            "margin": margin,
                            "clv": clv,
                            "volume_ratio": ratio,
                            "liq_pct": liquidity[key],
                        }
                    )
        signals.sort(key=brk20_rank_key)
        return signals, notes

    def _signals_lowvol(
        self, binding: CandidateBinding, world: PacketWorld, session_idx: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        populations, liquidity, notes = self._populations(binding, world, session_idx)
        signals: list[dict[str, Any]] = []
        percentile_max = float(binding.parameters["low_volatility_percentile_max"])
        trend_floor = float(binding.parameters["trend_return_floor"])
        volatility_by_market: dict[str, dict[Identity, float]] = {}
        returns_by_market: dict[str, dict[Identity, float]] = {}
        for market in MARKETS:
            population = populations[market]
            volatility: dict[Identity, float] = {}
            returns: dict[Identity, float] = {}
            for key in population:
                closes = [
                    _require_bar(
                        world.bar(key, session_idx - offset), key, session_idx
                    ).close
                    for offset in range(20, -1, -1)
                ]
                daily_returns = [closes[idx + 1] / closes[idx] - 1 for idx in range(20)]
                volatility[key] = canonical_sample_stdev(daily_returns)
                returns[key] = closes[-1] / closes[0] - 1
            volatility_by_market[market] = volatility
            returns_by_market[market] = returns
        vol_percentiles = self._per_market_percentiles(volatility_by_market)
        for market in MARKETS:
            population = populations[market]
            volatility = volatility_by_market[market]
            returns = returns_by_market[market]
            for key in population:
                percentile = vol_percentiles[key]
                if (
                    self._at_most(percentile, percentile_max)
                    and returns[key] > trend_floor
                ):
                    signals.append(
                        {
                            "market": key[0],
                            "symbol": key[1],
                            "vol20": volatility[key],
                            "vol20_pct": percentile,
                            "r20": returns[key],
                            "liq_pct": liquidity[key],
                        }
                    )
        signals.sort(key=lowvol_rank_key)
        return signals, notes

    def _execute_candidate(
        self, world: PacketWorld, binding: CandidateBinding
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        positions: list[dict[str, Any]] = []
        log: dict[str, Any] = {
            "entries": [],
            "ignored_held": [],
            "skipped_max10": [],
            "no_fill": [],
            "censored": [],
            # A15-3: evidence-less invalid/missing maturity exits are visible
            # and excluded at trade granularity, rather than invalidating a run.
            "missing_exit": [],
            "notes": {},
        }
        for signal_session in range(world.exploration_end_session_idx + 1):
            if (
                binding.strategy_id == "lowvol_up_month"
                and signal_session not in world.month_last_sessions
            ):
                continue
            signals, notes = self._signals_for(binding, world, signal_session)
            if notes:
                log["notes"][signal_session] = notes
            entry_session = signal_session + 1
            live: list[dict[str, Any]] = []
            for signal in signals:
                if (
                    entry_session > world.exploration_end_session_idx
                    or entry_session + binding.holding_sessions
                    > world.exploration_end_session_idx
                ):
                    log["censored"].append(
                        {
                            "session": signal_session,
                            "symbol": signal["symbol"],
                            "market": signal["market"],
                            "status": "RIGHT_CENSORED_NOT_TRADEABLE",
                        }
                    )
                else:
                    live.append(signal)

            occupied = sum(
                1
                for position in positions
                if position["entry_idx"] <= entry_session <= position["occupy_until"]
            )
            slots = binding.max_positions - occupied
            selected, skipped_for_capacity = self._select_two_stage(
                positions=positions,
                live=live,
                entry_session=entry_session,
                slots=slots,
                signal_session=signal_session,
                log=log,
            )
            self._attempt_entries(
                world=world,
                binding=binding,
                positions=positions,
                selected=selected,
                skipped_for_capacity=skipped_for_capacity,
                signal_session=signal_session,
                entry_session=entry_session,
                log=log,
            )

        trades = self._resolve_positions(world, positions, log)
        trades.sort(
            key=lambda trade: (trade["entry_idx"], trade["market"], trade["symbol"])
        )
        return log, trades

    def _select_two_stage(
        self,
        *,
        positions: list[dict[str, Any]],
        live: list[dict[str, Any]],
        entry_session: int,
        slots: int,
        signal_session: int,
        log: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selected: list[dict[str, Any]] = []
        skipped_for_capacity: list[dict[str, Any]] = []
        for signal in live:
            key = (signal["market"], signal["symbol"])
            if any(
                position["key"] == key
                and position["entry_idx"] <= entry_session <= position["occupy_until"]
                for position in positions
            ):
                log["ignored_held"].append(
                    {
                        "session": signal_session,
                        "market": signal["market"],
                        "symbol": signal["symbol"],
                    }
                )
                continue
            if len(selected) >= slots:
                log["skipped_max10"].append(
                    {
                        "session": signal_session,
                        "market": signal["market"],
                        "symbol": signal["symbol"],
                    }
                )
                skipped_for_capacity.append(signal)
                continue
            selected.append(signal)
        return selected, skipped_for_capacity

    def _attempt_entries(
        self,
        *,
        world: PacketWorld,
        binding: CandidateBinding,
        positions: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        skipped_for_capacity: list[dict[str, Any]],
        signal_session: int,
        entry_session: int,
        log: dict[str, Any],
    ) -> None:
        fallback = iter(skipped_for_capacity)
        attempts = iter(selected)
        for signal in attempts:
            entry_bar = world.bar((signal["market"], signal["symbol"]), entry_session)
            if not self._entry_ok(entry_bar):
                log["no_fill"].append(
                    {
                        "session": signal_session,
                        "market": signal["market"],
                        "symbol": signal["symbol"],
                        "entry_session": entry_session,
                    }
                )
                if self._substitute_after_no_fill():
                    self._attempt_mutant_fallback(
                        world=world,
                        binding=binding,
                        positions=positions,
                        fallback=fallback,
                        signal_session=signal_session,
                        entry_session=entry_session,
                        log=log,
                    )
                continue
            self._record_position(
                world=world,
                binding=binding,
                positions=positions,
                signal=signal,
                signal_session=signal_session,
                entry_session=entry_session,
                entry_bar=entry_bar,
                log=log,
            )

    def _attempt_mutant_fallback(
        self,
        *,
        world: PacketWorld,
        binding: CandidateBinding,
        positions: list[dict[str, Any]],
        fallback: Iterable[dict[str, Any]],
        signal_session: int,
        entry_session: int,
        log: dict[str, Any],
    ) -> None:
        """Test-only hook: subclasses can demonstrate why substitution is forbidden."""

        for candidate in fallback:
            entry_bar = world.bar(
                (candidate["market"], candidate["symbol"]), entry_session
            )
            if self._entry_ok(entry_bar):
                self._record_position(
                    world=world,
                    binding=binding,
                    positions=positions,
                    signal=candidate,
                    signal_session=signal_session,
                    entry_session=entry_session,
                    entry_bar=entry_bar,
                    log=log,
                )
                return

    def _record_position(
        self,
        *,
        world: PacketWorld,
        binding: CandidateBinding,
        positions: list[dict[str, Any]],
        signal: Mapping[str, Any],
        signal_session: int,
        entry_session: int,
        entry_bar: Bar,
        log: dict[str, Any],
    ) -> None:
        key = (str(signal["market"]), str(signal["symbol"]))
        exit_due = entry_session + binding.holding_sessions
        # §12: decision-disclosure evidence classifies an invalid scheduled
        # maturity bar; it is not a trading terminal or a capacity-release date.
        occupy_until = exit_due
        positions.append(
            {
                "key": key,
                "entry_idx": entry_session,
                "exit_due": exit_due,
                "occupy_until": occupy_until,
                "signal_session": signal_session,
                "entry_open": entry_bar.open,
            }
        )
        log["entries"].append(
            {
                "session": signal_session,
                "symbol": signal["symbol"],
                "market": signal["market"],
                "entry_session": entry_session,
            }
        )

    def _resolve_positions(
        self,
        world: PacketWorld,
        positions: Iterable[Mapping[str, Any]],
        log: dict[str, Any],
    ) -> list[dict[str, Any]]:
        regular_positions: list[Mapping[str, Any]] = []
        transfer_occurrences: list[dict[str, Any]] = []
        for position in positions:
            if self._transfer_precedes_maturity() and self._transfer_gate(
                world, position
            ):
                transfer_occurrences.append(self._transfer_evidence(world, position))
                continue
            regular_positions.append(position)

        if transfer_occurrences:
            raise RunInvalid(
                "RUN_INVALID_MARKET_TRANSFER",
                evidence={
                    "occurrence_count": len(transfer_occurrences),
                    "affected_identities": transfer_occurrences,
                },
            )

        trades: list[dict[str, Any]] = []
        deferred_transfers: list[dict[str, Any]] = []
        for position in regular_positions:
            key = position["key"]
            exit_due = position["exit_due"]
            exit_bar = world.bar(key, exit_due)
            if not self._maturity_ok(exit_bar):
                # §12: after the transfer gate, decision-disclosure evidence
                # classifies only a missing/invalid scheduled maturity bar.
                # The C8 search and reported exit remain scheduled at exit_due.
                event_session = world.delist_events.get(key)
                if event_session is not None and event_session <= exit_due:
                    trades.append(self._terminal_trade(world, position))
                    continue
                log["missing_exit"].append(
                    {
                        "market": key[0],
                        "symbol": key[1],
                        "entry_session": position["entry_idx"],
                        "exit_due": exit_due,
                    }
                )
                continue
            if not self._transfer_precedes_maturity() and self._transfer_gate(
                world, position
            ):
                deferred_transfers.append(self._transfer_evidence(world, position))
                continue
            gross = exit_bar.close / position["entry_open"] - 1
            trades.append(
                {
                    **_trade_position_fields(position),
                    "market": key[0],
                    "symbol": key[1],
                    "exit_session": exit_due,
                    "exit_close": exit_bar.close,
                    "gross": gross,
                    "delisted_exit": False,
                    "net_43bp": gross - COST_BASE,
                    "net_83bp": gross - COST_SENSITIVITY,
                }
            )
        if deferred_transfers:
            raise RunInvalid(
                "RUN_INVALID_MARKET_TRANSFER",
                evidence={
                    "occurrence_count": len(deferred_transfers),
                    "affected_identities": deferred_transfers,
                },
            )
        return trades

    def _terminal_trade(
        self, world: PacketWorld, position: Mapping[str, Any]
    ) -> dict[str, Any]:
        key = position["key"]
        last_close: float | None = None
        for session_idx in range(position["exit_due"], position["entry_idx"] - 1, -1):
            bar = world.bar(key, session_idx)
            if self._maturity_ok(bar):
                last_close = bar.close
                break
        gross = (
            last_close / position["entry_open"] - 1 if last_close is not None else -1.0
        )
        return {
            **_trade_position_fields(position),
            "market": key[0],
            "symbol": key[1],
            "exit_session": position["exit_due"],
            "exit_close": last_close,
            "gross": gross,
            "delisted_exit": True,
            "net_43bp": gross - COST_BASE,
            "net_83bp": gross - COST_SENSITIVITY,
        }

    def _transfer_gate(self, world: PacketWorld, position: Mapping[str, Any]) -> bool:
        key = position["key"]
        interval = world.membership_interval_at(key, int(position["signal_session"]))
        if interval is None:
            raise RunInvalid("RUN_INVALID_MEMBERSHIP", f"no observed leg for {key}")
        membership_end = interval[1]
        return membership_end < position["exit_due"] and self._other_market_transfer(
            world, key, membership_end=membership_end
        )

    def _transfer_evidence(
        self, world: PacketWorld, position: Mapping[str, Any]
    ) -> dict[str, Any]:
        market, symbol = position["key"]
        interval = world.membership_interval_at(
            (market, symbol), int(position["signal_session"])
        )
        if interval is None:
            raise RunInvalid(
                "RUN_INVALID_MEMBERSHIP", f"no observed leg for {(market, symbol)}"
            )
        return {
            "market": market,
            "symbol": symbol,
            "identity": [market, symbol],
            "membership_end": interval[1],
            "exit_due": position["exit_due"],
        }

    def _other_market_transfer(
        self, world: PacketWorld, key: Identity, *, membership_end: int
    ) -> bool:
        market, symbol = key
        return any(
            other_symbol == symbol and other_market != market and start > membership_end
            for (other_market, other_symbol), intervals in world.membership.items()
            for start, _ in intervals
        )

    def _build_baselines(
        self,
        world: PacketWorld,
        binding: CandidateBinding,
        trades: Iterable[Mapping[str, Any]],
    ) -> dict[str, Mapping[str, Any]]:
        baselines: dict[str, Mapping[str, Any]] = {}
        for trade in trades:
            signal_session = int(trade["signal_session"])
            base = self._a8_base_set(world, signal_session)
            liquidity = self._liquidity_percentiles(world, signal_session, base)
            key = (str(trade["market"]), str(trade["symbol"]))
            target_pct = liquidity[key]
            baseline = self._baseline(
                world,
                signal_session=signal_session,
                holding_sessions=binding.holding_sessions,
                target_pct=target_pct,
            )
            baselines[_baseline_key(key, int(trade["entry_idx"]))] = baseline
        return baselines

    def _baseline(
        self,
        world: PacketWorld,
        *,
        signal_session: int,
        holding_sessions: int,
        target_pct: float,
    ) -> dict[str, Any]:
        base = self._a8_base_set(world, signal_session)
        liquidity = self._liquidity_percentiles(world, signal_session, base)
        decile = min(9, int(target_pct * 10))
        returns: list[float] = []
        excluded_entry = 0
        excluded_maturity = 0
        terminal_included = 0
        excluded_transfer = 0
        entry_session = signal_session + 1
        exit_due = entry_session + holding_sessions
        for key in base:
            if min(9, int(liquidity[key] * 10)) != decile:
                continue
            entry_bar = world.bar(key, entry_session)
            if not self._entry_ok(entry_bar):
                excluded_entry += 1
                continue
            # A15 §12 mirror: a held-through-market-transfer cohort member is
            # excluded before interpreting its scheduled maturity price.  This
            # is a cohort exclusion, not a strategy-level RunInvalid verdict.
            position = {
                "key": key,
                "signal_session": signal_session,
                "exit_due": exit_due,
            }
            if self._transfer_gate(world, position):
                excluded_transfer += 1
                continue
            exit_bar = world.bar(key, exit_due)
            if self._maturity_ok(exit_bar):
                returns.append(exit_bar.close / entry_bar.open - 1)
                continue
            event_session = world.delist_events.get(key)
            if event_session is not None and event_session <= exit_due:
                last_close: float | None = None
                for session_idx in range(exit_due, signal_session, -1):
                    bar = world.bar(key, session_idx)
                    if self._maturity_ok(bar):
                        last_close = bar.close
                        break
                returns.append(
                    last_close / entry_bar.open - 1 if last_close is not None else -1.0
                )
                terminal_included += 1
                continue
            excluded_maturity += 1
        return {
            "decile": decile,
            "n": len(returns),
            "gross_mean": statistics.mean(returns) if returns else None,
            "cohort_excluded_entry": excluded_entry,
            "cohort_excluded_maturity": excluded_maturity,
            "cohort_terminal_included": terminal_included,
            "cohort_excluded_transfer": excluded_transfer,
        }

    def _at_least(self, actual: float, threshold: float) -> bool:
        return actual >= threshold

    def _at_most(self, actual: float, threshold: float) -> bool:
        return actual <= threshold

    def _high_c(self, bar: Bar) -> float:
        return high_c(bar)

    def _low_c(self, bar: Bar) -> float:
        return low_c(bar)

    def _entry_ok(self, bar: Bar | None) -> bool:
        return entry_ok(bar)

    def _maturity_ok(self, bar: Bar | None) -> bool:
        return maturity_ok(bar)

    def _substitute_after_no_fill(self) -> bool:
        return False

    def _transfer_precedes_maturity(self) -> bool:
        return True

    def _compose_verdict(
        self,
        dates: Mapping[int, date],
        trades: Iterable[Mapping[str, Any]],
        baselines: Mapping[str, Mapping[str, Any]],
        *,
        missing_exit_count: int,
    ) -> dict[str, Any]:
        """Hook retained so CI can prove that dropping missing-exit counts fails."""

        return compose_verdict(
            dates,
            trades,
            baselines,
            missing_exit_count=missing_exit_count,
        )


def pct_asc(values: Iterable[float], value: float) -> float:
    """A1: self-inclusive, tie-inclusive ascending percentile."""

    materialized = tuple(values)
    if not materialized:
        raise RunInvalid("RUN_INVALID_EMPTY_POPULATION")
    return sum(1 for candidate in materialized if candidate <= value) / len(
        materialized
    )


def valid_bar(bar: Bar | None) -> bool:
    return (
        bar is not None
        and all(
            _finite_positive(getattr(bar, field))
            for field in ("open", "high", "low", "close")
        )
        and _finite_positive(bar.volume)
    )


def entry_ok(bar: Bar | None) -> bool:
    return (
        bar is not None and _finite_positive(bar.open) and _finite_positive(bar.volume)
    )


def maturity_ok(bar: Bar | None) -> bool:
    return (
        bar is not None and _finite_positive(bar.close) and _finite_positive(bar.volume)
    )


def high_c(bar: Bar) -> float:
    return max(
        _required_float(bar.high), _required_float(bar.open), _required_float(bar.close)
    )


def low_c(bar: Bar) -> float:
    return min(
        _required_float(bar.low), _required_float(bar.open), _required_float(bar.close)
    )


def rev3_rank_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row["r3_pct"],
        row["r3"],
        -row["clv"],
        -row["volume_ratio"],
        -row["liq_pct"],
        0 if row["market"] == "KOSPI" else 1,
        row["symbol"],
    )


def brk20_rank_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -row["margin"],
        -row["volume_ratio"],
        -row["clv"],
        -row["liq_pct"],
        0 if row["market"] == "KOSPI" else 1,
        row["symbol"],
    )


def lowvol_rank_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row["vol20_pct"],
        row["vol20"],
        -row["r20"],
        -row["liq_pct"],
        0 if row["market"] == "KOSPI" else 1,
        row["symbol"],
    )


def compose_verdict(
    dates: Mapping[int, date],
    trades: Iterable[Mapping[str, Any]],
    baselines: Mapping[str, Mapping[str, Any]],
    *,
    missing_exit_count: int = 0,
) -> dict[str, Any]:
    """A11/A14/A15 verdict composition using entry-session equal weighting.

    Run-invalid conditions are raised by the caller before this point.  Of the
    completed-run labels, A15's strictly-greater-than-five-percent
    ``INCONCLUSIVE_MISSING_EXITS`` override takes precedence over cluster,
    falsification, and pass labels.
    """

    materialized = tuple(trades)
    if not isinstance(missing_exit_count, int) or missing_exit_count < 0:
        raise RunInvalid("RUN_INVALID_MISSING_EXIT_COUNT")
    profiles: dict[str, dict[str, Any]] = {}
    for tag, cost in (("43bp", COST_BASE), ("83bp", COST_SENSITIVITY)):
        excesses: list[float] = []
        by_session: dict[int, list[float]] = {}
        for trade in materialized:
            key = _baseline_key(
                (str(trade["market"]), str(trade["symbol"])), int(trade["entry_idx"])
            )
            baseline = baselines.get(key)
            if baseline is None or baseline.get("gross_mean") is None:
                raise RunInvalid(
                    "RUN_INVALID_EMPTY_BASELINE",
                    f"{trade['symbol']}@e{trade['entry_idx']}",
                )
            excess = (float(trade["gross"]) - cost) - float(baseline["gross_mean"])
            excesses.append(excess)
            by_session.setdefault(int(trade["entry_idx"]), []).append(excess)
        session_means = {
            session: statistics.mean(values)
            for session, values in sorted(by_session.items())
        }
        by_year: dict[int, list[float]] = {}
        for session, average in session_means.items():
            try:
                year = dates[session].year
            except KeyError as exc:
                raise RunInvalid(
                    "RUN_INVALID_SESSION", f"entry session {session}"
                ) from exc
            by_year.setdefault(year, []).append(average)
        positive_years = sum(
            1 for values in by_year.values() if statistics.mean(values) > 0
        )
        clusters = len(session_means)
        profile: dict[str, Any] = {
            "filled": len(materialized),
            "gate_filled_ge_300": len(materialized) >= 300,
            "trade_mean_excess": statistics.mean(excesses) if excesses else None,
            "gate_mean_excess_gt_0": bool(excesses) and statistics.mean(excesses) > 0,
            "n_entry_sessions": clusters,
            "session_means": session_means,
            "positive_years": positive_years,
            "n_years": len(by_year),
            "year_means": {
                str(year): statistics.mean(values)
                for year, values in sorted(by_year.items())
            },
            "gate_positive_years_ge_6": positive_years >= 6,
            "z": Z90,
        }
        if clusters < 30:
            profile["verdict"] = "UNIDENTIFIABLE_CLUSTERS"
            if clusters >= 2:
                values = list(session_means.values())
                profile["lcb_info_only"] = clustered_lcb(values)
        else:
            values = list(session_means.values())
            lcb = clustered_lcb(values)
            profile["lcb"] = lcb
            profile["gate_clustered_lcb_gt_0"] = lcb > 0
            failures = [
                gate
                for gate in (
                    "gate_filled_ge_300",
                    "gate_mean_excess_gt_0",
                    "gate_clustered_lcb_gt_0",
                    "gate_positive_years_ge_6",
                )
                if not profile[gate]
            ]
            profile["verdict"] = "FALSIFIED" if failures else "PASS"
            profile["failed_gates"] = failures
        profiles[tag] = profile
    base_mean = profiles["43bp"]["trade_mean_excess"]
    sensitivity_mean = profiles["83bp"]["trade_mean_excess"]
    total_attempted_exits = len(materialized) + missing_exit_count
    missing_exit_ratio = (
        missing_exit_count / total_attempted_exits if total_attempted_exits else 0.0
    )
    verdict = profiles["43bp"]["verdict"]
    if missing_exit_ratio > 0.05:
        verdict = "INCONCLUSIVE_MISSING_EXITS"
    return {
        "verdict": verdict,
        "cost_sensitive": base_mean is not None
        and base_mean > 0
        and (sensitivity_mean or 0) <= 0,
        "missing_exit_count": missing_exit_count,
        "missing_exit_ratio": missing_exit_ratio,
        "profiles": profiles,
    }


def clustered_lcb(session_means: Iterable[float]) -> float:
    values = tuple(session_means)
    if len(values) < 2:
        raise RunInvalid("UNIDENTIFIABLE_CLUSTERS")
    return statistics.mean(values) - Z90 * canonical_sample_stdev(values) / math.sqrt(
        len(values)
    )


def canonical_sample_stdev(values: Iterable[float]) -> float:
    """A15's canonical CPython 3.13 ``statistics.stdev`` calculation.

    Golden v6 deliberately binds byte identity to the repository's plain
    Python 3.13 runtime.  The v5 compatibility kernel is therefore no longer
    part of this engine.
    """

    materialized = tuple(values)
    if len(materialized) < 2:
        raise RunInvalid("UNIDENTIFIABLE_CLUSTERS")
    return statistics.stdev(materialized)


def membership_intervals_from_presence_table(
    table: Any,
    *,
    exploration_end_session_idx: int,
) -> dict[Identity, MembershipIntervals]:
    """Mechanically derive continuous observed intervals from presence rows.

    The caller may hand this function a PyArrow table read from the
    presence-only membership parquet.  Only ``market``, ``symbol`` and
    ``session_idx`` participate: a membership ``status`` field, if an
    upstream table happens to carry one, is deliberately ignored.  Delisting
    is represented solely by separately supplied DART evidence.
    """

    to_pylist = getattr(table, "to_pylist", None)
    if not callable(to_pylist):
        raise RunInvalid("RUN_INVALID_MEMBERSHIP", "presence table has no to_pylist")
    rows = to_pylist()
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise RunInvalid("RUN_INVALID_MEMBERSHIP", "presence table rows")
    return _membership_intervals_from_rows(
        rows,
        exploration_end_session_idx=exploration_end_session_idx,
        require_presence_rows=True,
    )


def membership_intervals_from_presence_parquet(
    path: Path | str,
    *,
    exploration_end_session_idx: int,
) -> dict[Identity, MembershipIntervals]:
    """Read the A15 presence-only parquet and derive observation intervals.

    This adapter intentionally delegates delisting to a separate evidence
    sidecar; there is no status-to-delisted conversion on this path.
    """

    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(Path(path))
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise RunInvalid(
            "RUN_INVALID_MEMBERSHIP", "presence parquet unreadable"
        ) from exc
    return membership_intervals_from_presence_table(
        table,
        exploration_end_session_idx=exploration_end_session_idx,
    )


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise RunInvalid("RUN_INVALID_FIXTURE", f"missing {path.name}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_config(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in _read_csv_rows(path):
        key = row.get("key")
        if not isinstance(key, str) or not key:
            raise RunInvalid("RUN_INVALID_CONFIG", "empty key")
        if key in result:
            raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", f"config {key}")
        result[key] = _as_int(row.get("value"), f"config {key}")
    return result


def _record_session_index(row: Mapping[str, Any]) -> int:
    raw = row.get("session_idx", row.get("delist_session_idx"))
    return _as_int(raw, "session_idx")


def _delist_source_session_index(row: Mapping[str, Any]) -> int | None:
    raw = row.get("delist_session_idx")
    if raw in (None, ""):
        return None
    return _as_int(raw, "delist session")


def _delist_event_session_index(
    row: Mapping[str, Any], dates: Mapping[int, date]
) -> int | None:
    """Resolve only explicit sidecar evidence to a known market session.

    Golden fixtures use the already-normalized ``delist_session_idx``.  The
    A15 sidecar contract supplies ``delist_date`` and ``reason``; an event date
    must match a materialized XKRX session exactly.  If a future sidecar event
    lies beyond this exploration window it is irrelevant and ignored.  Weekend
    or otherwise ambiguous alignment is fail-closed rather than guessed.
    """

    # The normalized fixture records an evidence class explicitly.  Unknown
    # classes are not interchangeable with a decision disclosure and must
    # fail closed; an absent class remains valid for the dated live-sidecar
    # shape (``delist_date`` + ``reason``) described by A15-1.
    evidence_type = row.get("evidence_type")
    if evidence_type not in (None, "") and evidence_type != "decision_disclosure":
        raise RunInvalid("RUN_INVALID_UNKNOWN_EVIDENCE_TYPE", str(evidence_type))

    raw_session = row.get("delist_session_idx")
    raw_date = row.get("delist_date")
    if raw_session not in (None, ""):
        event_session = _as_int(raw_session, "delist session")
        if raw_date in (None, ""):
            return event_session
        parsed_date = _parse_delist_date(raw_date)
        if dates.get(event_session) != parsed_date:
            raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "session/date mismatch")
        return event_session
    if raw_date in (None, ""):
        raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "missing delist date")
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "missing delist reason")
    parsed_date = _parse_delist_date(raw_date)
    if parsed_date > max(dates.values()):
        return None
    matches = [
        session_idx
        for session_idx, session_date in dates.items()
        if session_date == parsed_date
    ]
    if len(matches) != 1:
        raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "delist date is not a session")
    return matches[0]


def _parse_delist_date(raw: object) -> date:
    if not isinstance(raw, str):
        raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "invalid delist date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise RunInvalid("RUN_INVALID_DELIST_EVIDENCE", "invalid delist date") from exc


def _membership_record_index(row: Mapping[str, Any]) -> int:
    """Index a presence row, with interval rows retained for golden fixtures."""

    raw_presence = row.get("session_idx")
    if raw_presence not in (None, ""):
        return _as_int(raw_presence, "membership session_idx")
    return _as_int(row.get("start_idx"), "membership start_idx")


def _membership_intervals_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    exploration_end_session_idx: int,
    require_presence_rows: bool = False,
) -> dict[Identity, MembershipIntervals]:
    """Normalize either presence rows or fixture-only interval rows.

    Real A15 inputs are presence rows.  The interval branch is kept only for
    the hash-bound upstream golden CSV fixture, whose bytes predate the parquet
    adapter.  Both paths end in the same plural interval representation.
    """

    materialized = list(rows)
    if not materialized:
        return {}
    modes: set[str] = set()
    for row in materialized:
        has_presence = row.get("session_idx") not in (None, "")
        has_start = row.get("start_idx") not in (None, "")
        has_end = row.get("end_idx") not in (None, "")
        if has_presence and not (has_start or has_end):
            modes.add("presence")
        elif not has_presence and has_start and has_end:
            modes.add("interval")
        else:
            raise RunInvalid("RUN_INVALID_MEMBERSHIP", "ambiguous membership row")
    if len(modes) != 1:
        raise RunInvalid("RUN_INVALID_MEMBERSHIP", "mixed membership row formats")
    mode = next(iter(modes))
    if require_presence_rows and mode != "presence":
        raise RunInvalid("RUN_INVALID_MEMBERSHIP", "presence-only rows required")

    intervals_by_key: dict[Identity, list[MembershipInterval]] = {}
    if mode == "presence":
        seen_presence: set[tuple[Identity, int]] = set()
        for row in materialized:
            key = _identity_from_row(row)
            session_idx = _as_int(row.get("session_idx"), "membership session_idx")
            if session_idx < 0:
                raise RunInvalid("RUN_INVALID_MEMBERSHIP", str(key))
            compound = (key, session_idx)
            if compound in seen_presence:
                raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", f"membership {compound}")
            seen_presence.add(compound)
            if session_idx <= exploration_end_session_idx:
                intervals_by_key.setdefault(key, []).append((session_idx, session_idx))
    else:
        seen_intervals: set[tuple[Identity, int, int]] = set()
        for row in materialized:
            key = _identity_from_row(row)
            start = _as_int(row.get("start_idx"), "membership start")
            end = _as_int(row.get("end_idx"), "membership end")
            if start < 0 or start > end:
                raise RunInvalid("RUN_INVALID_MEMBERSHIP", str(key))
            compound = (key, start, end)
            if compound in seen_intervals:
                raise RunInvalid("RUN_INVALID_DUPLICATE_ROW", f"membership {compound}")
            seen_intervals.add(compound)
            if start <= exploration_end_session_idx:
                intervals_by_key.setdefault(key, []).append(
                    (start, min(end, exploration_end_session_idx))
                )

    return {
        key: _canonicalize_membership_intervals(key, intervals)
        for key, intervals in intervals_by_key.items()
    }


def _canonicalize_membership_intervals(
    key: Identity, intervals: Iterable[MembershipInterval]
) -> MembershipIntervals:
    ordered = sorted(intervals)
    normalized: list[MembershipInterval] = []
    for start, end in ordered:
        if not normalized:
            normalized.append((start, end))
            continue
        prior_start, prior_end = normalized[-1]
        if start <= prior_end:
            raise RunInvalid("RUN_INVALID_MEMBERSHIP", f"overlapping intervals {key}")
        if start == prior_end + 1:
            normalized[-1] = (prior_start, end)
        else:
            normalized.append((start, end))
    return tuple(normalized)


def _identity_from_row(row: Mapping[str, Any]) -> Identity:
    market, symbol = row.get("market"), row.get("symbol")
    if market not in MARKETS or not isinstance(symbol, str) or not symbol:
        raise RunInvalid("RUN_INVALID_IDENTITY", f"market={market!r} symbol={symbol!r}")
    return str(market), symbol


def _optional_float(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RunInvalid("RUN_INVALID_BAR", f"non-float {raw!r}") from exc


def _optional_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RunInvalid("RUN_INVALID_BAR", f"non-int {raw!r}") from exc


def _as_int(raw: object, label: str) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RunInvalid("RUN_INVALID_FIXTURE", label) from exc


def _finite_positive(value: object) -> bool:
    return (
        value is not None
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _required_float(value: float | None) -> float:
    if value is None:
        raise RunInvalid("RUN_INVALID_BAR", "required field absent")
    return value


def _require_bar(bar: Bar | None, key: Identity, session_idx: int) -> Bar:
    if bar is None:
        raise RunInvalid("RUN_INVALID_DATA_GAP", f"{key}@s{session_idx}")
    return bar


def _trade_position_fields(position: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: position[name]
        for name in ("entry_idx", "exit_due", "signal_session", "entry_open")
    }


def _baseline_key(key: Identity, entry_idx: int) -> str:
    return f"{key[0]}:{key[1]}@e{entry_idx}"
