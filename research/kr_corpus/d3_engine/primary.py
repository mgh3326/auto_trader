"""Deterministic 16-physical D3 locked-clock correction replay harness.

This module adds only the corpus-to-E1-engine execution and artifact surface. It
does not select a winner, compute Pareto dominance, run sensitivity variants, or
touch broker/database/scheduler surfaces.
"""

from __future__ import annotations

import csv
import gc
import hashlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.cash import CashLedger, Settlement
from research.kr_corpus.d3_engine.constants import FEE_RATE, ArtifactPaths
from research.kr_corpus.d3_engine.costs import cash_required
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.guards import (
    SealedAccessGuard,
    SealedAccessSpy,
    measure_sealed_fail_closed_probes,
)
from research.kr_corpus.d3_engine.metrics import nearest_rank
from research.kr_corpus.d3_engine.models import (
    Arm,
    CashflowView,
    CorporateAction,
    DataView,
    EngineResult,
    Fill,
    OrderClass,
    OrderSide,
    PortfolioRunInput,
    RunState,
)
from research.kr_corpus.d3_engine.primary_corpus import (
    CORPUS_BINDINGS,
    LoadedCorpusView,
    PrimaryCorpusLoader,
    PrimaryCorpusPaths,
    market_for,
)
from research.kr_corpus.d3_engine.sources import (
    FrozenKospiIndex,
    sha256_file,
    verify_start_gate,
)
from research.kr_corpus.d3_engine.tick import TickTable, load_tick_table

ENGINE_BASE_COMMIT = "10166c850bd9b1579619244bdbec35418091bb35"
PRIMARY_SCHEMA_VERSION = "d3.primary_run.v1"
ORDER_CHANGING_SCHEMA_VERSION = "d3.order_changing_clamped_rows.v1"
FIDELITY_METHOD_CHECKSUMS_SHA256 = (
    "16f86b089a163192588c41c637ce7b1d9ab57b6a1b9cd28b2ac6fb3bff249293"
)
DELIST_CHECKSUMS_SHA256 = (
    "aa542c9ce90f303191b9812bc696d7d6a6594da6c7f5e9209085850726fb609c"
)
DELIST_AUDIT_SHA256 = "b6e4d508997aa37d7659f487657da5dd4b133011d5f2bea95e95a87152b2bcf9"
STATE_PRIORITY_A1 = (
    "RUN_INVALID",
    "INCONCLUSIVE_DATA_BIAS",
    "INCONCLUSIVE_UNRESOLVED_TERMINAL",
    "CALIBRATION_DATA_BIAS",
    "CALIBRATION_MISMATCH",
    "verdict",
)
TRIAL_COUNT = 0
CORRECTION_REPLAY_COUNT = 1
SUPERSEDED_MANIFEST_SHA256 = (
    "30ae19f9d3892e76b848796f45df260741a120d23a8676d42bf914b78fc8f5d3"
)
CLAMPED_ROWS_SCHEMA_SHA256 = (
    "57a55bfb41a0bbb70a36c098a9f561ca90e28098eca8f7845dee0a1d4ec7b5b2"
)
CLAMPED_ROWS_SHA256 = "a01cb786f7c7d6743473d42ee524de948995084e5af6fd93e372386d6abde28a"
CLAMPED_ROWS_COUNT = 817_576
CLAMPED_UNIT_IDS_SHA256 = (
    "248fd8f3b0c03d76eaa803aff8e1b1d5fb971b5b5949c14327fa1dd957dcaf91"
)


class PrimaryRunInvalid(RuntimeError):
    code = "RUN_INVALID_PRIMARY_HARNESS"


class ClampedRowsChanged(PrimaryRunInvalid):
    code = "NEEDS_UPSTREAM(clamped_rows_changed_by_correction)"


@dataclass(frozen=True, slots=True)
class PrimaryHarnessPaths:
    artifacts: ArtifactPaths
    corpus: PrimaryCorpusPaths
    delist_root: Path
    fidelity_root: Path
    superseded_root: Path
    output_root: Path
    progress_report: Path

    @classmethod
    def defaults(cls) -> PrimaryHarnessPaths:
        work = Path.home() / "work"
        return cls(
            artifacts=ArtifactPaths.defaults(),
            corpus=PrimaryCorpusPaths.defaults(),
            delist_root=work / "herdr-artifacts" / "kr-delist-events-v1",
            fidelity_root=work / "herdr-artifacts" / "d3-fidelity-method-v1",
            superseded_root=work / "herdr-artifacts" / "d3-primary-run-v1",
            output_root=work / "herdr-artifacts" / "d3-r1c-correction-replay-v1",
            progress_report=(
                work
                / "herdr-inbox"
                / "jobs"
                / "d3-r1c-locked-clock-20260807-1700"
                / "events"
                / "impl.md"
            ),
        )


@dataclass(slots=True)
class _CapitalDays:
    symbol: str
    cycle_start_session: date
    sessions_open: int = 0
    invested_capital_days_krw: Decimal = Decimal(0)
    underwater_capital_days_krw: Decimal = Decimal(0)
    locked_capital_days_krw: Decimal = Decimal(0)
    max_age_sessions: int = 0
    last_session: date | None = None
    terminal_quantity: int = 0
    terminal_average_price: Decimal = Decimal(0)
    terminal_cost_basis: Decimal = Decimal(0)


@dataclass(slots=True)
class PrimaryTrace:
    signals: list[dict[str, object]] = field(default_factory=list)
    daily: list[dict[str, object]] = field(default_factory=list)
    capital_days: dict[tuple[str, int], _CapitalDays] = field(default_factory=dict)
    engine_invocations: int = 0


class PrimaryPortfolioEngine(PortfolioEngine):
    """E1 execution core with a precomputed, exact-equivalent signal tape."""

    def __init__(
        self,
        tick_table: TickTable,
        *,
        view: LoadedCorpusView,
        all_clamp_rows: dict[tuple[date, str], object],
        market_sessions: tuple[date, ...],
    ) -> None:
        super().__init__(tick_table, access_guard=SealedAccessGuard(SealedAccessSpy()))
        self._view = view
        self._all_clamp_rows = all_clamp_rows
        self._session_positions = {
            session: index for index, session in enumerate(market_sessions)
        }
        self._trace = PrimaryTrace()
        self._capture = False
        self._trace_units = Decimal(0)
        self._trace_last_unit_price = Decimal(1)
        self._trace_previous_contribution = Decimal(0)

    def execute(
        self,
        run_input: PortfolioRunInput,
        *,
        b0_demand_pairs: frozenset[tuple[date, str]] | None = None,
    ) -> tuple[EngineResult, PrimaryTrace]:
        self._trace = PrimaryTrace()
        self._capture = True
        self._trace_units = run_input.config.initial_cash
        self._trace_last_unit_price = Decimal(1)
        self._trace_previous_contribution = Decimal(0)
        try:
            self._trace.engine_invocations += 1
            gc_was_enabled = gc.isenabled()
            if gc_was_enabled:
                gc.disable()
            try:
                result = super()._run(
                    run_input,
                    b0_demand_pairs=b0_demand_pairs,
                )
            finally:
                if gc_was_enabled:
                    gc.enable()
        finally:
            self._capture = False
        return result, self._trace

    @staticmethod
    def _signal_history(
        symbol_bars: list[Any], *, index: int, segment_start: int
    ) -> list[Any]:
        """The exact signal tape only needs t and its prior 120-bar association."""

        return symbol_bars[max(segment_start, index - 120) : index + 1]

    def _signal_for_session(
        self, history: list[Any], decision_index: int
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        current = history[decision_index]
        snapshot = self._view.signals.get((current.session, current.symbol))
        if snapshot is None:
            return None
        if self._capture:
            prior = history[max(0, decision_index - 120) : decision_index]
            clamped_keys = [
                (item.session, item.symbol)
                for item in prior
                if (item.session, item.symbol) in self._all_clamp_rows
            ]
            current_clamp = self._all_clamp_rows.get((current.session, current.symbol))
            self._trace.signals.append(
                {
                    "session": current.session,
                    "symbol": current.symbol,
                    "market": current.market,
                    "rsi": snapshot.rsi,
                    "l2_price": snapshot.l2_price,
                    "fib_high": snapshot.fib_high,
                    "fib_low": snapshot.fib_low,
                    "previous_close": snapshot.previous_close,
                    "indicator_window_start": prior[0].session,
                    "indicator_window_end": prior[-1].session,
                    "indicator_window_clamped_rows": len(clamped_keys),
                    "indicator_window_clamped_key_sha256": _key_list_sha(clamped_keys),
                    "current_bar_clamped": current_clamp is not None,
                }
            )
        return (
            snapshot.rsi,
            snapshot.l2_price,
            snapshot.fib_high,
            snapshot.fib_low,
        )

    def _finish_day(
        self,
        *,
        state: RunState,
        cash: Any,
        by_session: dict[tuple[date, str], Any],
        session: date,
        cumulative_contribution: Decimal,
        cumulative_contribution_series: list[Decimal],
        daily_invested: list[Decimal],
        daily_locked_ratios: list[Decimal],
        nav_series: list[Decimal],
        last_closes: dict[str, Decimal],
        fee_rate: Decimal,
        terminal: bool,
    ) -> None:
        PortfolioEngine._finish_day(
            state=state,
            cash=cash,
            by_session=by_session,
            session=session,
            cumulative_contribution=cumulative_contribution,
            cumulative_contribution_series=cumulative_contribution_series,
            daily_invested=daily_invested,
            daily_locked_ratios=daily_locked_ratios,
            nav_series=nav_series,
            last_closes=last_closes,
            fee_rate=fee_rate,
            terminal=terminal,
        )
        if not self._capture:
            return
        contribution = cumulative_contribution - self._trace_previous_contribution
        if contribution:
            self._trace_units += contribution / self._trace_last_unit_price
        nav = nav_series[-1]
        unit_price = nav / self._trace_units
        reserved = sum(cash.reserved_orders.values(), Decimal(0))
        payables = sum((item.amount for item in cash.payables), Decimal(0))
        receivables = sum((item.amount for item in cash.receivables), Decimal(0))
        position_market_value = Decimal(0)
        open_positions = 0
        session_index = self._session_positions[session]
        for symbol, position in sorted(state.positions.items()):
            if position.quantity < 1:
                continue
            open_positions += 1
            close = (
                by_session[(session, symbol)].close
                if (session, symbol) in by_session
                else last_closes[symbol]
            )
            position_market_value += close * position.quantity
            first_index = position.cycle_first_fill_index
            if first_index is None:
                raise AssertionError("open position lacks cycle first fill")
            key = (symbol, first_index)
            capital = self._trace.capital_days.get(key)
            if capital is None:
                capital = _CapitalDays(
                    symbol=symbol,
                    cycle_start_session=next(
                        day
                        for day, index in self._session_positions.items()
                        if index == first_index
                    ),
                )
                self._trace.capital_days[key] = capital
            age = session_index - first_index + 1
            cost = position.invested_cost_basis
            capital.sessions_open += 1
            capital.invested_capital_days_krw += cost
            if close < position.average_price:
                capital.underwater_capital_days_krw += cost
            if position.underwater_streak >= 180:
                capital.locked_capital_days_krw += cost
            capital.max_age_sessions = max(capital.max_age_sessions, age)
            capital.last_session = session
            capital.terminal_quantity = position.quantity if terminal else 0
            capital.terminal_average_price = (
                position.average_price if terminal else Decimal(0)
            )
            capital.terminal_cost_basis = cost if terminal else Decimal(0)
        self._trace.daily.append(
            {
                "session": session,
                "nav": nav,
                "unit_price": unit_price,
                "units": self._trace_units,
                "contribution": contribution,
                "cumulative_contribution": cumulative_contribution,
                "invested_cost_basis": daily_invested[-1],
                "locked_share": daily_locked_ratios[-1],
                "position_market_value": position_market_value,
                "open_positions": open_positions,
                "settled_orderable_cash": cash.orderable_cash,
                "settled_reserved_order_cash": reserved,
                "settled_cash_total": cash.orderable_cash + reserved,
                "unsettled_buy_payables": payables,
                "unsettled_sell_receivables": receivables,
                "session_index": session_index,
                "reserved_orders": [
                    {"order_id": order_id, "amount": amount}
                    for order_id, amount in sorted(cash.reserved_orders.items())
                ],
                "buy_payables": [
                    {
                        "trade_session_index": item.trade_session_index,
                        "settle_session_index": item.settle_session_index,
                        "amount": item.amount,
                        "kind": item.kind,
                    }
                    for item in cash.payables
                ],
                "sell_receivables": [
                    {
                        "trade_session_index": item.trade_session_index,
                        "settle_session_index": item.settle_session_index,
                        "amount": item.amount,
                        "kind": item.kind,
                    }
                    for item in cash.receivables
                ],
            }
        )
        self._trace_last_unit_price = unit_price
        self._trace_previous_contribution = cumulative_contribution


@dataclass(frozen=True, slots=True)
class PhysicalRun:
    arm: Arm
    cashflow_view: CashflowView
    data_view: DataView

    @property
    def run_id(self) -> str:
        return "__".join(
            (self.arm.value, self.cashflow_view.value, self.data_view.value)
        )


def primary_matrix() -> tuple[PhysicalRun, ...]:
    return tuple(
        PhysicalRun(arm, cashflow, view)
        for view in (DataView.ORIGINAL_VALID_BAR, DataView.CLAMP_ADMIT_V1)
        for cashflow in (
            CashflowView.WITH_CONTRIBUTION,
            CashflowView.NO_CONTRIBUTION,
        )
        for arm in Arm
    )


class PrimaryRunHarness:
    def __init__(
        self,
        *,
        harness_commit: str,
        paths: PrimaryHarnessPaths | None = None,
    ) -> None:
        if len(harness_commit) != 40:
            raise ValueError("harness_commit must be a full Git SHA")
        self.harness_commit = harness_commit
        self.paths = paths or PrimaryHarnessPaths.defaults()
        self._sha_gate: tuple[dict[str, str], ...] = ()
        self._stamps: dict[str, object] = {}

    def run_all(self) -> dict[str, object]:
        self._preflight()
        tick_table = load_tick_table(self.paths.artifacts.tick_yaml)
        index = FrozenKospiIndex.load(self.paths.artifacts.index_csv)
        market_sessions = tuple(row.session for row in index.rows)
        index_closes = tuple((row.session, row.close) for row in index.rows)
        corporate_actions, delist_evidence = _load_delist_actions(
            self.paths.delist_root,
            market_sessions=market_sessions,
        )

        loader_guard = SealedAccessGuard(SealedAccessSpy())
        loader = PrimaryCorpusLoader(paths=self.paths.corpus, guard=loader_guard)
        clamp = loader.load(
            DataView.CLAMP_ADMIT_V1,
            market_sessions=market_sessions,
        )
        original = loader.load(
            DataView.ORIGINAL_VALID_BAR,
            market_sessions=market_sessions,
        )
        views = {
            DataView.ORIGINAL_VALID_BAR: original,
            DataView.CLAMP_ADMIT_V1: clamp,
        }
        access_evidence = loader_guard.spy.evidence()
        if access_evidence["sealed_access_spy"] != 0:
            raise PrimaryRunInvalid("sealed corpus read measured during primary load")
        if access_evidence["sealed_access_blocked_attempts"] != 0:
            raise PrimaryRunInvalid("primary loader attempted a sealed access")

        self._stamps = self._build_stamps(
            original=original,
            clamp=clamp,
            delist_evidence=delist_evidence,
            access_evidence=access_evidence,
        )
        self.paths.output_root.mkdir(parents=True, exist_ok=True)
        (self.paths.output_root / "runs").mkdir(exist_ok=True)
        self._append_progress(
            "\nLOADER = PASS · "
            f"original rows={original.row_count} files={original.parquet_files} · "
            f"clamp rows={clamp.row_count} files={clamp.parquet_files} · "
            f"SEALED_ACCESS_MEASURED={access_evidence['sealed_access_spy']}\n"
        )
        gc.collect()
        gc.freeze()
        self._append_progress(
            f"\nEXECUTION_GC_HEAP_FROZEN = {gc.get_freeze_count()} objects · "
            "semantics-neutral performance isolation\n"
        )

        completed: list[dict[str, object]] = []
        b0_demand: dict[
            tuple[CashflowView, DataView, int], frozenset[tuple[date, str]]
        ] = {}
        for physical in primary_matrix():
            target = self.paths.output_root / "runs" / physical.run_id
            if target.exists():
                existing = _read_completed_run(target, physical)
                completed.append(existing)
                if physical.arm is Arm.B0:
                    for attempt in (1, 2):
                        b0_demand[
                            (physical.cashflow_view, physical.data_view, attempt)
                        ] = _demand_from_orders_file(target / "evidence.json")
                continue
            first, first_demand = self._execute_attempt(
                physical,
                view=views[physical.data_view],
                all_clamp_rows=clamp.clamp_rows,
                tick_table=tick_table,
                market_sessions=market_sessions,
                index_closes=index_closes,
                corporate_actions=corporate_actions,
                b0_demand_pairs=(
                    None
                    if physical.arm is Arm.B0
                    else b0_demand[(physical.cashflow_view, physical.data_view, 1)]
                ),
            )
            second, second_demand = self._execute_attempt(
                physical,
                view=views[physical.data_view],
                all_clamp_rows=clamp.clamp_rows,
                tick_table=tick_table,
                market_sessions=market_sessions,
                index_closes=index_closes,
                corporate_actions=corporate_actions,
                b0_demand_pairs=(
                    None
                    if physical.arm is Arm.B0
                    else b0_demand[(physical.cashflow_view, physical.data_view, 2)]
                ),
            )
            if first != second:
                raise PrimaryRunInvalid(
                    f"non-deterministic physical run:{physical.run_id}"
                )
            if first_demand != second_demand:
                raise PrimaryRunInvalid(
                    f"non-deterministic B0 demand:{physical.run_id}"
                )
            finalized = _seal_determinism(
                physical=physical,
                first=first,
                second=second,
                stamps=self._stamps,
            )
            _publish_bundle(target, finalized)
            run_json_sha = hashlib.sha256(finalized["run.json"]).hexdigest()
            row = {
                "run_id": physical.run_id,
                "arm": physical.arm.value,
                "cashflow_view": physical.cashflow_view.value,
                "data_view": physical.data_view.value,
                "deterministic_2runs": True,
                "run_json_sha256": run_json_sha,
                "bundle_sha256": _bundle_sha(finalized),
            }
            completed.append(row)
            if physical.arm is Arm.B0:
                b0_demand[(physical.cashflow_view, physical.data_view, 1)] = (
                    first_demand
                )
                b0_demand[(physical.cashflow_view, physical.data_view, 2)] = (
                    second_demand
                )
            self._append_progress(
                f"\nRUN_COMPLETE {len(completed):02d}/16 = {physical.run_id} · "
                f"2RUN_BYTE_IDENTICAL=PASS · bundle_sha256={row['bundle_sha256']}\n"
            )
            gc.collect()

        completed.sort(key=lambda row: str(row["run_id"]))
        comparison = _compare_correction_replay(
            correction_root=self.paths.output_root,
            superseded_root=self.paths.superseded_root,
        )
        _write_bytes(
            self.paths.output_root / "bitexact-comparison.json",
            canonical_bytes(
                {
                    "schema_version": PRIMARY_SCHEMA_VERSION,
                    "artifact_kind": "locked-clock-bitexact-comparison",
                    "stamps": self._stamps,
                    "payload": comparison,
                }
            ),
        )
        if comparison["status"] != "PASS":
            self._append_progress(
                "\nRUN_INVALID = correction bit-exact mismatch · "
                f"differences={comparison['unexpected_differences']}\n"
            )
            raise PrimaryRunInvalid("RUN_INVALID_CORRECTION_BITEXACT")

        clamped_pair_exposure = _build_clamped_pair_exposure(
            output_root=self.paths.output_root,
            completed=completed,
            stamps=self._stamps,
        )
        superseded_manifest = json.loads(
            (self.paths.superseded_root / "manifest.json").read_bytes()
        )
        pairing = _build_order_changing_rows(
            output_root=self.paths.output_root,
            completed=completed,
            stamps=self._stamps,
            frozen_source_shas={
                str(row["run_id"]): str(row["run_json_sha256"])
                for row in superseded_manifest["matrix"]["runs"]
            },
            binding_stamps=superseded_manifest["stamps"],
        )
        clamped_invariance = _measure_clamped_rows_invariance(
            root=self.paths.output_root,
            row_count=int(pairing["summary"]["row_count"]),
            unit_ids_sha256=str(pairing["summary"]["unit_ids_sha256"]),
        )
        comparison["root_artifacts"] = _compare_root_artifacts(
            correction_root=self.paths.output_root,
            superseded_root=self.paths.superseded_root,
        )
        comparison["clamped_rows"] = clamped_invariance
        if not all(
            bool(row["identical"]) for row in comparison["root_artifacts"].values()
        ):
            comparison["status"] = "FAIL"
            comparison["unexpected_differences"].append("root_artifact_payload_changed")
        _write_bytes(
            self.paths.output_root / "bitexact-comparison.json",
            canonical_bytes(
                {
                    "schema_version": PRIMARY_SCHEMA_VERSION,
                    "artifact_kind": "locked-clock-bitexact-comparison",
                    "stamps": self._stamps,
                    "payload": comparison,
                }
            ),
        )
        if not clamped_invariance["unchanged"]:
            self._append_progress(
                "\nNEEDS_UPSTREAM(clamped_rows_changed_by_correction) = "
                f"{clamped_invariance}\n"
            )
            raise ClampedRowsChanged(ClampedRowsChanged.code)
        if comparison["status"] != "PASS":
            raise PrimaryRunInvalid("RUN_INVALID_CORRECTION_ROOT_ARTIFACT")

        fail_closed = measure_sealed_fail_closed_probes()
        if fail_closed["status"] != "PASS":
            raise PrimaryRunInvalid("sealed three-route fail-closed probe failed")
        sealed_payload = {
            "schema_version": PRIMARY_SCHEMA_VERSION,
            "stamps": self._stamps,
            "measurement": {
                **access_evidence,
                "measurement_scope": "PrimaryCorpusLoader actual file/parquet/bar/key reads",
                "sealed_access_measured": access_evidence["sealed_access_spy"],
                "blocked_attempts_during_primary": access_evidence[
                    "sealed_access_blocked_attempts"
                ],
            },
            "fail_closed_probes": fail_closed,
        }
        _write_bytes(
            self.paths.output_root / "sealed-access.json",
            canonical_bytes(sealed_payload),
        )
        manifest = {
            "schema_version": PRIMARY_SCHEMA_VERSION,
            "stamps": self._stamps,
            "matrix": {
                "physical_runs": len(completed),
                "expected_physical_runs": 16,
                "deterministic_2runs": sum(
                    bool(row["deterministic_2runs"]) for row in completed
                ),
                "runs": completed,
            },
            "dual_view_pairing": pairing["pairing"],
            "order_changing_clamped_rows": pairing["summary"],
            "clamped_pair_exposure": clamped_pair_exposure,
            "bitexact_comparison_path": "bitexact-comparison.json",
            "bitexact_invariants_passed": comparison["status"] == "PASS",
            "clamped_rows_unchanged": clamped_invariance,
            "sealed_access_path": "sealed-access.json",
            "supersedes": self._stamps["supersedes"],
            "trial_count": TRIAL_COUNT,
            "correction_replay_count": CORRECTION_REPLAY_COUNT,
            "winner_selected": False,
            "pareto_computed": False,
            "sensitivity_runs": 0,
        }
        _write_bytes(
            self.paths.output_root / "manifest.json", canonical_bytes(manifest)
        )
        measured = measure_primary_run_executed(self.paths.output_root)
        if not measured["primary_run_executed"]:
            raise PrimaryRunInvalid(f"primary measurement failed:{measured}")
        _write_superseded_link(
            superseded_root=self.paths.superseded_root,
            correction_root=self.paths.output_root,
        )
        self._append_progress(
            "\nRUNS = 16/16 physical · DETERMINISTIC_2RUNS = 16/16 PASS · "
            "DUAL_VIEW_PAIRED = 8/8 · WINNER_SELECTED = NO\n"
            "\nBITEXACT_TABLE = PASS · C3_FULL_EXACT = YES · "
            "B0_C1_C2_TRIM_FIRES = 0\n"
            "\nCLAMPED_ROWS_UNCHANGED = YES · "
            f"schema_sha={clamped_invariance['schema_after_sha256']} · "
            f"rows_sha={clamped_invariance['rows_after_sha256']} · "
            f"row_count={clamped_invariance['row_count_after']} · "
            "unit_id_identical=YES\n"
            "\nSEALED_ACCESS_MEASURED = 0 · "
            "holdout/D3_CALIBRATION_2025/prospective fail-closed PASS\n"
            "\ntrial_count = 0 · correction_replay_count = 1\n"
        )
        gc.unfreeze()
        return manifest

    def _preflight(self) -> None:
        self._sha_gate = verify_start_gate(self.paths.artifacts)
        if len(self._sha_gate) != 10:
            raise PrimaryRunInvalid(f"SHA gate count drift:{len(self._sha_gate)}")
        if sha256_file(self.paths.fidelity_root / "checksums.sha256") != (
            FIDELITY_METHOD_CHECKSUMS_SHA256
        ):
            raise PrimaryRunInvalid("fidelity method checksum drift")
        if self.paths.output_root.resolve() == self.paths.superseded_root.resolve():
            raise PrimaryRunInvalid(
                "correction output cannot overwrite superseded root"
            )
        if sha256_file(self.paths.superseded_root / "manifest.json") != (
            SUPERSEDED_MANIFEST_SHA256
        ):
            raise PrimaryRunInvalid("superseded primary manifest drift")
        if (
            sha256_file(
                self.paths.superseded_root / "order-changing-clamped-rows-schema.json"
            )
            != CLAMPED_ROWS_SCHEMA_SHA256
        ):
            raise PrimaryRunInvalid("superseded clamped schema drift")
        if (
            sha256_file(self.paths.superseded_root / "order-changing-clamped-rows.json")
            != CLAMPED_ROWS_SHA256
        ):
            raise PrimaryRunInvalid("superseded clamped rows drift")
        self.paths.progress_report.parent.mkdir(parents=True, exist_ok=True)

    def _build_stamps(
        self,
        *,
        original: LoadedCorpusView,
        clamp: LoadedCorpusView,
        delist_evidence: dict[str, object],
        access_evidence: dict[str, int],
    ) -> dict[str, object]:
        return {
            "input_sha256": {str(row["file"]): row["sha256"] for row in self._sha_gate},
            "engine_base_commit": ENGINE_BASE_COMMIT,
            "engine_commit": self.harness_commit,
            "harness_commit": self.harness_commit,
            "trial_count": TRIAL_COUNT,
            "correction_replay_count": CORRECTION_REPLAY_COUNT,
            "supersedes": {
                "artifact_root": str(self.paths.superseded_root.resolve()),
                "manifest_sha256": SUPERSEDED_MANIFEST_SHA256,
                "status": "RUN_INVALID_PENDING_LOCKED_CLOCK_CORRECTION",
            },
            "superseded_by_link": str(
                (self.paths.superseded_root / "superseded-by.json").resolve()
            ),
            "corpus": {
                **CORPUS_BINDINGS,
                "original_rows": original.row_count,
                "derived_rows": clamp.row_count,
                "original_signal_tape_sha256": original.signal_tape_sha256,
                "derived_signal_tape_sha256": clamp.signal_tape_sha256,
            },
            "delist_sidecar": delist_evidence,
            "fidelity_method_checksums_sha256": (FIDELITY_METHOD_CHECKSUMS_SHA256),
            "state_priority_a1": STATE_PRIORITY_A1,
            "sealed_access_at_load_complete": access_evidence,
            "execution_conventions": {
                "fee": "43bp neutral: 21.5bp/side",
                "cashflow": "3500000 KRW or none",
                "settlement": "T+2",
                "same_bar": "buy-first same symbol",
                "sensitivity": False,
                "gc_isolation": (
                    "long-lived corpus heap frozen; cyclic GC suspended per attempt; "
                    "reference-count semantics unchanged"
                ),
            },
        }

    def _execute_attempt(
        self,
        physical: PhysicalRun,
        *,
        view: LoadedCorpusView,
        all_clamp_rows: dict[tuple[date, str], object],
        tick_table: TickTable,
        market_sessions: tuple[date, ...],
        index_closes: tuple[tuple[date, Decimal], ...],
        corporate_actions: tuple[CorporateAction, ...],
        b0_demand_pairs: frozenset[tuple[date, str]] | None,
    ) -> tuple[dict[str, bytes], frozenset[tuple[date, str]]]:
        engine = PrimaryPortfolioEngine(
            tick_table,
            view=view,
            all_clamp_rows=all_clamp_rows,
            market_sessions=market_sessions,
        )
        result, trace = engine.execute(
            PortfolioRunInput(
                arm=physical.arm,
                cashflow_view=physical.cashflow_view,
                bars=view.bars,  # type: ignore[arg-type]
                data_view=physical.data_view,
                market_sessions=market_sessions,
                index_closes=index_closes,
                corporate_actions=corporate_actions,
                decision_start=date(2015, 1, 1),
            ),
            b0_demand_pairs=b0_demand_pairs,
        )
        bundle = _artifact_bundle(
            physical=physical,
            result=result,
            trace=trace,
            view=view,
            all_clamp_rows=all_clamp_rows,
            stamps=self._stamps,
        )
        demand = frozenset(
            (row["session"], str(row["symbol"]))
            for row in result.evidence["counterfactual_demand_pairs"]
        )
        return bundle, demand

    def _append_progress(self, text: str) -> None:
        with self.paths.progress_report.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()


def _artifact_bundle(
    *,
    physical: PhysicalRun,
    result: EngineResult,
    trace: PrimaryTrace,
    view: LoadedCorpusView,
    all_clamp_rows: dict[tuple[date, str], object],
    stamps: dict[str, object],
) -> dict[str, bytes]:
    signal_rows = sorted(trace.signals, key=lambda row: (row["session"], row["symbol"]))
    signal_index = {(row["session"], row["symbol"]): row for row in signal_rows}
    events = list(result.events)
    submitted = [row for row in events if row["event"] == "order_submitted"]
    policy_rejects = [row for row in events if row["event"] == "policy_rejected"]
    cash_rejects = [row for row in events if row["event"] == "cash_rejected"]
    skip_rows = [
        row
        for row in events
        if row["event"]
        in {"order_expired", "policy_rejected", "cash_rejected", "c3_time_trim_skipped"}
    ]
    fills = _fill_rows(result.fills, submitted)
    orders = _order_rows(
        submitted=submitted,
        fills=fills,
        skips=skip_rows,
        signal_index=signal_index,
        view=view,
        all_clamp_rows=all_clamp_rows,
    )
    order_by_id = {
        str(order["order_id"]): order
        for order in orders
        if order["order_id"] is not None
    }
    fill_exposure = []
    for fill in fills:
        order = order_by_id.get(str(fill["order_id"]))
        if order is None:
            raise PrimaryRunInvalid(f"fill lacks order projection:{fill['order_id']}")
        if not (
            order["current_bar_clamped"]
            or int(order["indicator_window_clamped_rows"]) > 0
        ):
            continue
        fill_exposure.append(
            {
                "exposure_type": "fill",
                **fill,
                "market": order["market"],
                "current_bar_clamped": order["current_bar_clamped"],
                "indicator_window_clamped_rows": order["indicator_window_clamped_rows"],
                "indicator_window_clamped_key_sha256": order[
                    "indicator_window_clamped_key_sha256"
                ],
            }
        )
    clamped_exposure = (
        [
            {"exposure_type": "signal", **signal}
            for signal in signal_rows
            if signal["current_bar_clamped"]
            or int(signal["indicator_window_clamped_rows"]) > 0
        ]
        + [
            {"exposure_type": "order", **order}
            for order in orders
            if order["current_bar_clamped"]
            or int(order["indicator_window_clamped_rows"]) > 0
        ]
        + fill_exposure
    )
    capital_days = [
        {
            "symbol": value.symbol,
            "cycle_start_session": value.cycle_start_session,
            "last_session": value.last_session,
            "sessions_open": value.sessions_open,
            "max_age_sessions": value.max_age_sessions,
            "invested_capital_days_krw": value.invested_capital_days_krw,
            "underwater_capital_days_krw": (value.underwater_capital_days_krw),
            "locked_capital_days_krw": value.locked_capital_days_krw,
            "terminal_quantity": value.terminal_quantity,
            "terminal_average_price": value.terminal_average_price,
            "terminal_cost_basis": value.terminal_cost_basis,
        }
        for _, value in sorted(trace.capital_days.items())
    ]
    terminal_daily = trace.daily[-1]
    open_lots = [
        {
            **row,
            "terminal_session": terminal_daily["session"],
            "market": market_for(
                view,
                symbol=str(row["symbol"]),
                session=terminal_daily["session"],
            ),
        }
        for row in result.terminal_positions
    ]
    funding = _funding_p05(trace.daily, events)
    policy_sessions = sorted({row["session"] for row in policy_rejects})
    cash_sessions = sorted({row["session"] for row in cash_rejects})
    metrics = {
        **result.metrics,
        "funding_p05_days": funding["p05_days"],
        "funding_complete_anchor_count": funding["complete_anchor_count"],
        "funding_right_censored_anchor_count": funding["right_censored_anchor_count"],
        "policy_rejected_sessions": len(policy_sessions),
        "cash_rejected_sessions": len(cash_sessions),
        "unserved_policy_or_cash_sessions": len(
            set(policy_sessions) | set(cash_sessions)
        ),
        "clamped_signal_rows": sum(
            row["exposure_type"] == "signal" for row in clamped_exposure
        ),
        "clamped_order_rows": sum(
            row["exposure_type"] == "order" for row in clamped_exposure
        ),
        "clamped_fill_rows": len(fill_exposure),
        "clamped_fill_gross_notional": sum(
            (Decimal(str(row["gross"])) for row in fill_exposure), Decimal(0)
        ),
        "clamped_realized_pnl": sum(
            (
                Decimal(str(row["realized_pnl"]))
                for row in fill_exposure
                if row["realized_pnl"] is not None
            ),
            Decimal(0),
        ),
        "clamped_view_terminal_nav": result.metrics["terminal_nav"],
    }
    evidence = {
        **result.evidence,
        "engine_execution": {
            "class": "PrimaryPortfolioEngine",
            "base_core": "PortfolioEngine._run",
            "invocations": trace.engine_invocations,
            "signal_tape_sha256": view.signal_tape_sha256,
            "signal_tape_equivalence_test": (
                "tests/research/kr_corpus/d3_engine/test_d3_primary.py"
            ),
        },
        "physical_run_completed": True,
        "physical_run_id": physical.run_id,
        "funding_method": funding,
        "winner_selected": False,
        "pareto_computed": False,
    }
    cash_daily = [
        {
            key: row[key]
            for key in (
                "session",
                "contribution",
                "cumulative_contribution",
                "settled_orderable_cash",
                "settled_reserved_order_cash",
                "settled_cash_total",
                "unsettled_buy_payables",
                "unsettled_sell_receivables",
                "session_index",
                "reserved_orders",
                "buy_payables",
                "sell_receivables",
            )
        }
        for row in trace.daily
    ]
    nav_daily = [
        {
            key: row[key]
            for key in (
                "session",
                "nav",
                "unit_price",
                "units",
                "invested_cost_basis",
                "locked_share",
                "position_market_value",
                "open_positions",
            )
        }
        for row in trace.daily
    ]
    payloads: dict[str, object] = {
        "signals.json": signal_rows,
        "submitted.json": submitted,
        "fills.json": fills,
        "skips.json": skip_rows,
        "policy-rejects.json": policy_rejects,
        "cash-rejects.json": cash_rejects,
        "clamped-exposure.json": clamped_exposure,
        "open-lots.json": open_lots,
        "capital-days.json": capital_days,
        "cash-daily.json": cash_daily,
        "nav-unit-price-daily.json": nav_daily,
        "events.json": events,
        "orders.json": orders,
        "metrics.json": metrics,
        "evidence.json": evidence,
    }
    bundle = {
        name: canonical_bytes(
            {
                "schema_version": PRIMARY_SCHEMA_VERSION,
                "artifact_kind": name.removesuffix(".json"),
                "run_id": physical.run_id,
                "stamps": stamps,
                "rows" if isinstance(payload, list) else "payload": payload,
            }
        )
        for name, payload in payloads.items()
    }
    content_checksums = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(bundle.items())
    }
    bundle["run.json"] = canonical_bytes(
        {
            "schema_version": PRIMARY_SCHEMA_VERSION,
            "artifact_kind": "physical_run",
            "run_id": physical.run_id,
            "stamps": stamps,
            "parameters": {
                "arm": physical.arm,
                "cashflow_view": physical.cashflow_view,
                "data_view": physical.data_view,
                "fee_rate": "0.00215",
                "monthly_contribution": "3500000",
                "settlement": "T+2",
                "same_bar": "buy-first same symbol",
            },
            "result_status": result.status,
            "content_checksums": content_checksums,
            "artifact_count_excluding_run_json": len(content_checksums),
            "engine_invocations": trace.engine_invocations,
            "physical_run_completed": True,
            "winner_selected": False,
        }
    )
    return bundle


def _fill_rows(
    fills: tuple[Fill, ...], submitted: list[dict[str, object]]
) -> list[dict[str, object]]:
    orders = {str(row["order_id"]): row for row in submitted}
    rows: list[dict[str, object]] = []
    positions: dict[str, tuple[int, Decimal]] = {}
    for fill in fills:
        order = orders.get(fill.order_id)
        rung = order.get("rung") if order else _time_trim_rung(fill.order_id)
        old_quantity, old_cost_basis = positions.get(fill.symbol, (0, Decimal(0)))
        cost_basis_added: Decimal | None = None
        cost_basis_released: Decimal | None = None
        realized_pnl: Decimal | None = None
        if fill.side is OrderSide.BUY:
            cost_basis_added = fill.gross + fill.fee
            positions[fill.symbol] = (
                old_quantity + fill.quantity,
                old_cost_basis + cost_basis_added,
            )
        else:
            if old_quantity < fill.quantity:
                raise PrimaryRunInvalid(
                    f"sell fill exceeds reconstructed position:{fill.order_id}"
                )
            cost_basis_released = (
                old_cost_basis * Decimal(fill.quantity) / Decimal(old_quantity)
            )
            realized_pnl = fill.gross - fill.fee - cost_basis_released
            remaining_quantity = old_quantity - fill.quantity
            positions[fill.symbol] = (
                remaining_quantity,
                (
                    Decimal(0)
                    if remaining_quantity == 0
                    else old_cost_basis - cost_basis_released
                ),
            )
        rows.append(
            {
                "order_id": fill.order_id,
                "session": fill.session,
                "symbol": fill.symbol,
                "side": fill.side,
                "class": fill.order_class,
                "rung": rung,
                "quantity": fill.quantity,
                "price": fill.price,
                "gross": fill.gross,
                "fee": fill.fee,
                "cost_basis_added": cost_basis_added,
                "cost_basis_released": cost_basis_released,
                "realized_pnl": realized_pnl,
            }
        )
    return rows


def _order_rows(
    *,
    submitted: list[dict[str, object]],
    fills: list[dict[str, object]],
    skips: list[dict[str, object]],
    signal_index: dict[tuple[date, str], dict[str, object]],
    view: LoadedCorpusView,
    all_clamp_rows: dict[tuple[date, str], object],
) -> list[dict[str, object]]:
    fill_by_id = {str(row["order_id"]): row for row in fills}
    expired = {
        str(row["order_id"]): row for row in skips if row["event"] == "order_expired"
    }
    rows: list[dict[str, object]] = []
    for order in submitted:
        order_id = str(order["order_id"])
        fill = fill_by_id.get(order_id)
        key = (order["session"], str(order["symbol"]))
        association = _clamp_association(key, signal_index, all_clamp_rows)
        rows.append(
            {
                "order_id": order_id,
                "market": market_for(
                    view,
                    symbol=key[1],
                    session=key[0],  # type: ignore[arg-type]
                ),
                "symbol": key[1],
                "session_date": key[0],
                "side": _enum_value(order["side"]),
                "order_class": _fidelity_order_class(order["class"]),
                "engine_order_class": _enum_value(order["class"]),
                "rung_id": str(order["rung"]),
                "limit_price": order["limit"],
                "quantity": order["quantity"],
                "sim_outcome": "filled" if fill else "submitted_unfilled",
                "sim_fill_price": fill["price"] if fill else None,
                "skip_reason": "expired" if order_id in expired else None,
                **association,
            }
        )
    submitted_ids = {str(row["order_id"]) for row in submitted}
    for fill in fills:
        if str(fill["order_id"]) in submitted_ids:
            continue
        key = (fill["session"], str(fill["symbol"]))
        rows.append(
            {
                "order_id": fill["order_id"],
                "market": market_for(
                    view,
                    symbol=key[1],
                    session=key[0],  # type: ignore[arg-type]
                ),
                "symbol": key[1],
                "session_date": key[0],
                "side": _enum_value(fill["side"]),
                "order_class": _fidelity_order_class(fill["class"]),
                "engine_order_class": _enum_value(fill["class"]),
                "rung_id": str(fill["rung"]),
                "limit_price": fill["price"],
                "quantity": fill["quantity"],
                "sim_outcome": "filled",
                "sim_fill_price": fill["price"],
                "skip_reason": None,
                **_clamp_association(key, signal_index, all_clamp_rows),
            }
        )
    for skip in skips:
        if skip["event"] not in {"policy_rejected", "cash_rejected"}:
            continue
        key = (skip["session"], str(skip["symbol"]))
        raw_rungs = skip.get("rungs")
        rungs = raw_rungs if isinstance(raw_rungs, list) else [skip]
        for rung in rungs:
            if not isinstance(rung, dict):
                raise PrimaryRunInvalid("malformed rejected rung evidence")
            rows.append(
                {
                    "order_id": None,
                    "market": market_for(
                        view,
                        symbol=key[1],
                        session=key[0],  # type: ignore[arg-type]
                    ),
                    "symbol": key[1],
                    "session_date": key[0],
                    "side": _enum_value(skip.get("side", OrderSide.BUY)),
                    "order_class": _fidelity_order_class(skip.get("class", "other")),
                    "engine_order_class": _enum_value(skip.get("class", "other")),
                    "rung_id": str(rung.get("rung", "POLICY")),
                    "limit_price": rung.get("limit", skip.get("limit")),
                    "quantity": skip.get("quantity"),
                    "sim_outcome": "suppressed",
                    "sim_fill_price": None,
                    "skip_reason": str(skip.get("reason", skip["event"])),
                    **_clamp_association(key, signal_index, all_clamp_rows),
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            row["session_date"],
            row["symbol"],
            row["side"],
            row["order_class"],
            row["rung_id"],
            row["sim_outcome"],
        ),
    )


def _clamp_association(
    key: tuple[object, str],
    signal_index: dict[tuple[date, str], dict[str, object]],
    all_clamp_rows: dict[tuple[date, str], object],
) -> dict[str, object]:
    typed_key = (key[0], key[1])
    signal = signal_index.get(typed_key)  # type: ignore[arg-type]
    return {
        "current_bar_clamped": typed_key in all_clamp_rows,
        "indicator_window_clamped_rows": (
            int(signal["indicator_window_clamped_rows"]) if signal else 0
        ),
        "indicator_window_clamped_key_sha256": (
            signal["indicator_window_clamped_key_sha256"] if signal else None
        ),
    }


def _funding_p05(
    daily: list[dict[str, object]],
    events: list[dict[str, object]],
) -> dict[str, object]:
    month_ends: list[date] = []
    for index, row in enumerate(daily):
        session = row["session"]
        if not isinstance(session, date):
            raise AssertionError("daily session is not a date")
        if index == len(daily) - 1:
            month_ends.append(session)
            continue
        next_session = daily[index + 1]["session"]
        if not isinstance(next_session, date):
            raise AssertionError("daily next session is not a date")
        if (session.year, session.month) != (next_session.year, next_session.month):
            month_ends.append(session)
    terminal = daily[-1]["session"]
    assert isinstance(terminal, date)
    events_by_session: dict[date, list[dict[str, object]]] = {}
    for event in events:
        session = event.get("session")
        if isinstance(session, date):
            events_by_session.setdefault(session, []).append(event)
    complete: list[dict[str, object]] = []
    censored = 0
    for anchor in month_ends:
        horizon = anchor + timedelta(days=90)
        if horizon > terminal:
            censored += 1
            continue
        anchor_row = next(row for row in daily if row["session"] == anchor)
        ledger = _cash_ledger_from_daily(anchor_row)
        first_reject: date | None = None
        missed_notional = Decimal(0)
        missed_signals = 0
        for row in daily:
            session = row["session"]
            if not isinstance(session, date) or not (anchor < session <= horizon):
                continue
            session_index = int(row["session_index"])
            ledger.settle_pre_open(session_index)
            for event in events_by_session.get(session, []):
                event_name = event["event"]
                if event_name in {
                    "monthly_contribution_pre_open",
                    "t2_settle_pre_open",
                }:
                    continue
                if event_name == "order_expired":
                    if _enum_value(event["side"]) == OrderSide.BUY.value:
                        ledger.expire_order(str(event["order_id"]))
                    continue
                if event_name == "order_submitted":
                    if _enum_value(event["side"]) != OrderSide.BUY.value:
                        continue
                    gross_limit = Decimal(str(event["limit"])) * int(event["quantity"])
                    required = cash_required(gross_limit, FEE_RATE)
                    if not ledger.reserve_order(str(event["order_id"]), required):
                        first_reject = session
                        missed_notional = gross_limit
                        missed_signals = 1
                        break
                    continue
                if event_name == "cash_rejected":
                    first_reject = session
                    missed_notional = Decimal(str(event["limit"])) * int(
                        event["quantity"]
                    )
                    missed_signals = 1
                    break
                if event_name != "fill":
                    continue
                gross = Decimal(str(event["gross"]))
                fee = Decimal(str(event["fee"]))
                if _enum_value(event["side"]) == OrderSide.BUY.value:
                    ledger.fill_buy(
                        order_id=str(event["order_id"]),
                        actual_amount=gross + fee,
                        trade_session_index=session_index,
                    )
                else:
                    ledger.fill_sell(
                        net_amount=gross - fee,
                        trade_session_index=session_index,
                    )
            if first_reject is not None:
                break
        days = 90 if first_reject is None else (first_reject - anchor).days
        complete.append(
            {
                "anchor": anchor,
                "horizon": horizon,
                "first_cash_reject": first_reject,
                "funded_days": days,
                "missed_notional_at_first_reject": missed_notional,
                "missed_signals_at_first_reject": missed_signals,
            }
        )
    values = [int(row["funded_days"]) for row in complete]
    return {
        "method": (
            "month_end_rolling_90_calendar_day_zero_contribution_replay_to_first_"
            "otherwise_eligible_cash_reject"
        ),
        "replay_invariant": (
            "base event stream is exact until first stress cash rejection; future "
            "contributions are omitted and settled/reserved/unsettled cash is replayed"
        ),
        "nearest_rank_percentile": "0.05",
        "p05_days": nearest_rank(values, Decimal("0.05")) if values else None,
        "complete_anchor_count": len(complete),
        "right_censored_anchor_count": censored,
        "anchors": complete,
    }


def _cash_ledger_from_daily(row: dict[str, object]) -> CashLedger:
    ledger = CashLedger(Decimal(str(row["settled_orderable_cash"])))
    reserved = row["reserved_orders"]
    payables = row["buy_payables"]
    receivables = row["sell_receivables"]
    if not all(isinstance(items, list) for items in (reserved, payables, receivables)):
        raise PrimaryRunInvalid("daily cash snapshot lists are malformed")
    ledger.reserved_orders = {
        str(item["order_id"]): Decimal(str(item["amount"]))
        for item in reserved
        if isinstance(item, dict)
    }
    ledger.payables = [
        Settlement(
            int(item["trade_session_index"]),
            int(item["settle_session_index"]),
            Decimal(str(item["amount"])),
            str(item["kind"]),
        )
        for item in payables
        if isinstance(item, dict)
    ]
    ledger.receivables = [
        Settlement(
            int(item["trade_session_index"]),
            int(item["settle_session_index"]),
            Decimal(str(item["amount"])),
            str(item["kind"]),
        )
        for item in receivables
        if isinstance(item, dict)
    ]
    if len(ledger.reserved_orders) != len(reserved):
        raise PrimaryRunInvalid("daily reserved cash snapshot malformed")
    if len(ledger.payables) != len(payables) or len(ledger.receivables) != len(
        receivables
    ):
        raise PrimaryRunInvalid("daily settlement cash snapshot malformed")
    return ledger


def _build_order_changing_rows(
    *,
    output_root: Path,
    completed: list[dict[str, object]],
    stamps: dict[str, object],
    frozen_source_shas: dict[str, str] | None = None,
    binding_stamps: dict[str, object] | None = None,
) -> dict[str, object]:
    by_combo = {
        (
            str(row["arm"]),
            str(row["cashflow_view"]),
            str(row["data_view"]),
        ): row
        for row in completed
    }
    rows: list[dict[str, object]] = []
    pairs: list[dict[str, object]] = []
    for arm in Arm:
        for cashflow in CashflowView:
            original_meta = by_combo[
                (arm.value, cashflow.value, DataView.ORIGINAL_VALID_BAR.value)
            ]
            clamp_meta = by_combo[
                (arm.value, cashflow.value, DataView.CLAMP_ADMIT_V1.value)
            ]
            original_orders = _read_rows(
                output_root / "runs" / str(original_meta["run_id"]) / "orders.json"
            )
            clamp_orders = _read_rows(
                output_root / "runs" / str(clamp_meta["run_id"]) / "orders.json"
            )
            original_index = _unique_order_index(original_orders)
            clamp_index = _unique_order_index(clamp_orders)
            changed = 0
            for key in sorted(set(original_index) | set(clamp_index)):
                original = original_index.get(key)
                clamp = clamp_index.get(key)
                codes = _oc_codes(original, clamp)
                if not codes:
                    continue
                changed += 1
                pair_id = hashlib.sha256(
                    "|".join(
                        (arm.value, cashflow.value, *(str(item) for item in key))
                    ).encode()
                ).hexdigest()[:32]
                for role, record, source in (
                    ("orig_side", original, original_meta),
                    ("clamp_side", clamp, clamp_meta),
                ):
                    material = _paired_material(record, original or clamp)
                    fidelity = {
                        "pair_id": pair_id,
                        "arm": arm.value,
                        "cashflow_view": cashflow.value,
                        "data_view_role": role,
                        **material,
                        "oc_codes": codes,
                        "source_artifact_sha": (
                            frozen_source_shas[str(source["run_id"])]
                            if frozen_source_shas is not None
                            else source["run_json_sha256"]
                        ),
                    }
                    fidelity["unit_id"] = _fidelity_unit_id(fidelity)
                    rows.append(fidelity)
            pairs.append(
                {
                    "arm": arm.value,
                    "cashflow_view": cashflow.value,
                    "original_run_id": original_meta["run_id"],
                    "clamp_run_id": clamp_meta["run_id"],
                    "order_changing_keys": changed,
                    "paired": True,
                }
            )
    rows.sort(key=lambda row: (row["pair_id"], row["data_view_role"]))
    schema = _order_changing_schema()
    frozen_stamps = binding_stamps if binding_stamps is not None else stamps
    _write_bytes(
        output_root / "order-changing-clamped-rows.json",
        canonical_bytes(
            {
                "schema_version": ORDER_CHANGING_SCHEMA_VERSION,
                "artifact_kind": "order-changing-clamped-rows",
                "stamps": frozen_stamps,
                "rows": rows,
            }
        ),
    )
    _write_bytes(
        output_root / "order-changing-clamped-rows-schema.json",
        canonical_bytes(
            {
                "schema_version": ORDER_CHANGING_SCHEMA_VERSION,
                "artifact_kind": "order-changing-clamped-rows-schema",
                "stamps": frozen_stamps,
                "payload": schema,
            }
        ),
    )
    _write_bytes(
        output_root / "dual-view-pairing.json",
        canonical_bytes(
            {
                "schema_version": PRIMARY_SCHEMA_VERSION,
                "artifact_kind": "dual-view-pairing",
                "stamps": stamps,
                "rows": pairs,
            }
        ),
    )
    return {
        "pairing": {
            "pairs": len(pairs),
            "expected_pairs": 8,
            "all_paired": len(pairs) == 8 and all(row["paired"] for row in pairs),
            "rows": pairs,
        },
        "summary": {
            "schema_version": ORDER_CHANGING_SCHEMA_VERSION,
            "row_count": len(rows),
            "unit_ids_sha256": _line_sha256([str(row["unit_id"]) for row in rows]),
            "pair_count": len({row["pair_id"] for row in rows}),
            "artifact": "order-changing-clamped-rows.json",
            "schema": "order-changing-clamped-rows-schema.json",
            "fidelity_method_compatible": True,
        },
    }


def _build_clamped_pair_exposure(
    *,
    output_root: Path,
    completed: list[dict[str, object]],
    stamps: dict[str, object],
) -> dict[str, object]:
    by_combo = {
        (
            str(row["arm"]),
            str(row["cashflow_view"]),
            str(row["data_view"]),
        ): row
        for row in completed
    }
    fields = (
        "clamped_signal_rows",
        "clamped_order_rows",
        "clamped_fill_rows",
        "clamped_fill_gross_notional",
        "clamped_realized_pnl",
        "clamped_view_terminal_nav",
    )
    rows: list[dict[str, object]] = []
    for data_view in DataView:
        for cashflow in CashflowView:
            b0_meta = by_combo[(Arm.B0.value, cashflow.value, data_view.value)]
            b0_metrics = _read_payload(
                output_root / "runs" / str(b0_meta["run_id"]) / "metrics.json"
            )
            for arm in Arm:
                arm_meta = by_combo[(arm.value, cashflow.value, data_view.value)]
                arm_metrics = _read_payload(
                    output_root / "runs" / str(arm_meta["run_id"]) / "metrics.json"
                )
                rows.append(
                    {
                        "arm": arm.value,
                        "cashflow_view": cashflow.value,
                        "data_view": data_view.value,
                        "arm_run_id": arm_meta["run_id"],
                        "b0_run_id": b0_meta["run_id"],
                        "arm_exposure": {field: arm_metrics[field] for field in fields},
                        "b0_exposure": {field: b0_metrics[field] for field in fields},
                        "arm_minus_b0": {
                            field: Decimal(str(arm_metrics[field]))
                            - Decimal(str(b0_metrics[field]))
                            for field in fields
                        },
                    }
                )
    _write_bytes(
        output_root / "clamped-pair-exposure.json",
        canonical_bytes(
            {
                "schema_version": PRIMARY_SCHEMA_VERSION,
                "artifact_kind": "clamped-pair-exposure",
                "stamps": stamps,
                "rows": rows,
            }
        ),
    )
    return {
        "artifact": "clamped-pair-exposure.json",
        "row_count": len(rows),
        "expected_row_count": 16,
        "comparison": "arm-minus-B0 within cashflow and data view; diagnostic only",
    }


def _order_key(row: dict[str, object]) -> tuple[str, str, str, str, str]:
    return (
        str(row["symbol"]),
        str(row["session_date"]),
        str(row["side"]),
        str(row["order_class"]),
        str(row["rung_id"]),
    )


def _unique_order_index(
    rows: list[dict[str, object]],
) -> dict[tuple[str, str, str, str, str], dict[str, object]]:
    result: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    for row in rows:
        key = _order_key(row)
        if key in result:
            raise PrimaryRunInvalid(f"duplicate fidelity order key:{key}")
        result[key] = row
    return result


def _oc_codes(
    original: dict[str, object] | None,
    clamp: dict[str, object] | None,
) -> list[str]:
    codes: list[str] = []
    original_submitted = (
        original is not None and original["sim_outcome"] != "suppressed"
    )
    clamp_submitted = clamp is not None and clamp["sim_outcome"] != "suppressed"
    if original_submitted != clamp_submitted:
        codes.append("OC_SUBMIT_XOR")
    original_filled = original is not None and original["sim_outcome"] == "filled"
    clamp_filled = clamp is not None and clamp["sim_outcome"] == "filled"
    if original_filled != clamp_filled:
        codes.append("OC_FILL_XOR")
    if original and clamp:
        if original["limit_price"] != clamp["limit_price"]:
            codes.append("OC_LIMIT_NE")
        if original["quantity"] != clamp["quantity"]:
            codes.append("OC_QTY_NE")
        if original["sim_fill_price"] != clamp["sim_fill_price"]:
            codes.append("OC_FILL_PX_NE")
        if original["skip_reason"] != clamp["skip_reason"]:
            codes.append("OC_SKIP_REASON_NE")
    return codes


def _paired_material(
    row: dict[str, object] | None,
    fallback: dict[str, object] | None,
) -> dict[str, object]:
    if fallback is None:
        raise AssertionError("order-changing pair lacks both sides")
    source = row or fallback
    return {
        "market": source.get("market"),
        "symbol": source["symbol"],
        "session_date": source["session_date"],
        "side": source["side"],
        "order_class": source["order_class"],
        "rung_id": source["rung_id"],
        "limit_price": row.get("limit_price") if row else None,
        "quantity": row.get("quantity") if row else None,
        "sim_outcome": row.get("sim_outcome") if row else "suppressed",
        "sim_fill_price": row.get("sim_fill_price") if row else None,
        "skip_reason": row.get("skip_reason") if row else "absent_in_view",
        "current_bar_clamped": bool(row and row["current_bar_clamped"]),
        "indicator_window_clamped_rows": (
            int(row["indicator_window_clamped_rows"]) if row else 0
        ),
    }


def _fidelity_unit_id(row: dict[str, object]) -> str:
    limit = "" if row["limit_price"] is None else str(row["limit_price"])
    material = "|".join(
        (
            "d3-fidelity-unit-v1",
            str(row["arm"]),
            str(row["cashflow_view"]),
            str(row["symbol"]),
            str(row["session_date"]),
            str(row["side"]),
            str(row["order_class"]),
            str(row["rung_id"]),
            limit,
            str(row["sim_outcome"]),
            ",".join(sorted(str(code) for code in row["oc_codes"])),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _order_changing_schema() -> dict[str, object]:
    return {
        "record_key": ["unit_id", "data_view_role"],
        "sampling_unit_id": "unit_id (role-free frozen literals.yaml algorithm)",
        "pair_key": ["pair_id", "data_view_role"],
        "comparison_key": [
            "arm",
            "cashflow_view",
            "symbol",
            "session_date",
            "side",
            "order_class",
            "rung_id",
        ],
        "required_fields": [
            "unit_id",
            "pair_id",
            "arm",
            "cashflow_view",
            "data_view_role",
            "symbol",
            "session_date",
            "side",
            "order_class",
            "rung_id",
            "limit_price",
            "sim_outcome",
            "sim_fill_price",
            "oc_codes",
            "source_artifact_sha",
        ],
        "enums": {
            "data_view_role": ["orig_side", "clamp_side"],
            "side": ["buy", "sell"],
            "order_class": ["new", "add", "trim", "exit", "other"],
            "sim_outcome": [
                "submitted_unfilled",
                "filled",
                "expired",
                "suppressed",
            ],
            "oc_codes": [
                "OC_SUBMIT_XOR",
                "OC_FILL_XOR",
                "OC_LIMIT_NE",
                "OC_QTY_NE",
                "OC_FILL_PX_NE",
                "OC_SKIP_REASON_NE",
            ],
        },
        "fidelity_method_binding": {
            "checksums_sha256": FIDELITY_METHOD_CHECKSUMS_SHA256,
            "population": "METHOD.md §1.2 OC-1 AND OC-2",
            "population_projection": (
                "every OC-2 changed comparison emits orig_side and clamp_side; "
                "identical dual-view order rows are excluded"
            ),
            "fidelity_unit_fields": "METHOD.md §1.4",
            "unit_id_algorithm": "literals.yaml population.unit_id_algorithm",
            "comparison": (
                "required fidelity-unit fields are an exact field superset; compatible"
            ),
        },
    }


def measure_primary_run_executed(root: Path) -> dict[str, object]:
    """Derive execution state from the completed, checksummed artifact bundle."""

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {
            "primary_run_executed": False,
            "reason": "manifest_missing",
            "physical_runs": 0,
        }
    try:
        manifest = json.loads(manifest_path.read_bytes())
        matrix = manifest["matrix"]
        runs = matrix["runs"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "primary_run_executed": False,
            "reason": f"manifest_invalid:{type(exc).__name__}",
            "physical_runs": 0,
        }
    expected_ids = {run.run_id for run in primary_matrix()}
    actual_ids = {str(row.get("run_id")) for row in runs}
    verified = 0
    for row in runs:
        run_id = str(row.get("run_id"))
        path = root / "runs" / run_id / "run.json"
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row.get("run_json_sha256"):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            payload.get("physical_run_completed") is True
            and int(payload.get("engine_invocations_total", 0)) == 2
            and row.get("deterministic_2runs") is True
            and _verify_physical_run_bundle(path.parent, payload)
            and _directory_bundle_sha(path.parent) == row.get("bundle_sha256")
        ):
            verified += 1
    value = (
        expected_ids == actual_ids
        and verified == 16
        and matrix.get("physical_runs") == 16
        and matrix.get("deterministic_2runs") == 16
    )
    return {
        "primary_run_executed": value,
        "reason": "artifact_derived",
        "physical_runs": len(runs),
        "verified_engine_runs": verified,
        "expected_matrix_match": expected_ids == actual_ids,
    }


def _load_delist_actions(
    root: Path,
    *,
    market_sessions: tuple[date, ...],
) -> tuple[tuple[CorporateAction, ...], dict[str, object]]:
    checksums = root / "checksums.sha256"
    audit = root / "audit_presence_endings.csv"
    if sha256_file(checksums) != DELIST_CHECKSUMS_SHA256:
        raise PrimaryRunInvalid("delist bundle checksum drift")
    if sha256_file(audit) != DELIST_AUDIT_SHA256:
        raise PrimaryRunInvalid("delist audit checksum drift")
    reader = csv.DictReader(io.StringIO(audit.read_text(encoding="utf-8")))
    by_symbol: dict[str, date] = {}
    matched_rows = 0
    for row in reader:
        if row["audit_status"] != "MATCH_WITHIN_60_CALENDAR_DAYS":
            continue
        matched_rows += 1
        symbol = row["symbol"]
        last_presence = date.fromisoformat(row["last_presence_session"])
        by_symbol[symbol] = max(by_symbol.get(symbol, last_presence), last_presence)
    session_index = {session: index for index, session in enumerate(market_sessions)}
    actions: list[CorporateAction] = []
    for symbol, last_presence in sorted(by_symbol.items()):
        position = session_index.get(last_presence)
        if position is None or position + 1 >= len(market_sessions):
            continue
        actions.append(
            CorporateAction(
                session=market_sessions[position + 1],
                symbol=symbol,
                kind="delist_evidence_data_end",
                data_ends_before_exploration_end=True,
            )
        )
    return tuple(actions), {
        "checksums_sha256": DELIST_CHECKSUMS_SHA256,
        "audit_presence_endings_sha256": DELIST_AUDIT_SHA256,
        "matched_audit_rows": matched_rows,
        "unique_symbols_with_actions": len(actions),
        "coverage_limit": "KOSDAQ DART decision evidence; KOSPI terminal partial scope",
        "action_session": "first frozen XKRX session after last presence",
    }


def _read_completed_run(target: Path, physical: PhysicalRun) -> dict[str, object]:
    raw = (target / "run.json").read_bytes()
    payload = json.loads(raw)
    if payload.get("run_id") != physical.run_id:
        raise PrimaryRunInvalid(f"existing run id mismatch:{target}")
    if payload.get("physical_run_completed") is not True:
        raise PrimaryRunInvalid(f"existing run incomplete:{target}")
    if not _verify_physical_run_bundle(target, payload):
        raise PrimaryRunInvalid(f"existing run checksum/determinism invalid:{target}")
    return {
        "run_id": physical.run_id,
        "arm": physical.arm.value,
        "cashflow_view": physical.cashflow_view.value,
        "data_view": physical.data_view.value,
        "deterministic_2runs": True,
        "run_json_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_sha256": _directory_bundle_sha(target),
    }


_RESULT_ARTIFACTS = (
    "signals.json",
    "submitted.json",
    "fills.json",
    "skips.json",
    "policy-rejects.json",
    "cash-rejects.json",
    "clamped-exposure.json",
    "open-lots.json",
    "capital-days.json",
    "cash-daily.json",
    "nav-unit-price-daily.json",
    "events.json",
    "orders.json",
    "metrics.json",
    "evidence.json",
)
_NON_C3_EXACT_ARTIFACTS = (
    "signals.json",
    "submitted.json",
    "fills.json",
    "skips.json",
    "policy-rejects.json",
    "cash-rejects.json",
    "clamped-exposure.json",
    "cash-daily.json",
    "events.json",
    "orders.json",
    "evidence.json",
)
_LOCKED_METRIC_KEYS = frozenset(
    {"locked_share_tw_mean", "locked_share_p95", "locked_share_max"}
)


def _compare_correction_replay(
    *, correction_root: Path, superseded_root: Path
) -> dict[str, object]:
    unexpected: list[str] = []
    run_rows: list[dict[str, object]] = []
    for physical in primary_matrix():
        before_root = superseded_root / "runs" / physical.run_id
        after_root = correction_root / "runs" / physical.run_id
        exact: dict[str, dict[str, object]] = {}
        if physical.arm is Arm.C3:
            for name in _RESULT_ARTIFACTS:
                row = _value_hash_comparison(before_root / name, after_root / name)
                exact[name] = row
                if not row["identical"]:
                    unexpected.append(f"{physical.run_id}:{name}")
            run_rows.append(
                {
                    "run_id": physical.run_id,
                    "arm": physical.arm.value,
                    "c3_full_bitexact": not any(
                        not bool(row["identical"]) for row in exact.values()
                    ),
                    "exact_artifacts": exact,
                    "allowed_locked_axis": None,
                    "non_c3_time_trim_fills": None,
                }
            )
            continue

        for name in _NON_C3_EXACT_ARTIFACTS:
            row = _value_hash_comparison(before_root / name, after_root / name)
            exact[name] = row
            if not row["identical"]:
                unexpected.append(f"{physical.run_id}:{name}")

        metrics_before = _read_wrapped_value(before_root / "metrics.json")
        metrics_after = _read_wrapped_value(after_root / "metrics.json")
        if not isinstance(metrics_before, dict) or not isinstance(metrics_after, dict):
            raise PrimaryRunInvalid("metrics artifact payload malformed")
        metric_differences = sorted(
            key
            for key in set(metrics_before) | set(metrics_after)
            if metrics_before.get(key) != metrics_after.get(key)
        )
        unexpected_metrics = sorted(set(metric_differences) - _LOCKED_METRIC_KEYS)
        if unexpected_metrics:
            unexpected.extend(
                f"{physical.run_id}:metrics:{key}" for key in unexpected_metrics
            )

        nav = _allowed_rows_comparison(
            before_root / "nav-unit-price-daily.json",
            after_root / "nav-unit-price-daily.json",
            allowed_keys=frozenset({"locked_share"}),
        )
        lots = _allowed_rows_comparison(
            before_root / "open-lots.json",
            after_root / "open-lots.json",
            allowed_keys=frozenset({"underwater_streak"}),
        )
        capital = _allowed_rows_comparison(
            before_root / "capital-days.json",
            after_root / "capital-days.json",
            allowed_keys=frozenset({"locked_capital_days_krw"}),
        )
        for name, row in (
            ("nav-unit-price-daily.json", nav),
            ("open-lots.json", lots),
            ("capital-days.json", capital),
        ):
            if not row["non_allowed_fields_identical"]:
                unexpected.append(f"{physical.run_id}:{name}:non_allowed_fields")

        fills_after = _read_wrapped_value(after_root / "fills.json")
        if not isinstance(fills_after, list):
            raise PrimaryRunInvalid("fills artifact rows malformed")
        trim_fills = sum(
            row.get("class") == OrderClass.TIME_TRIM.value for row in fills_after
        )
        if trim_fills:
            unexpected.append(f"{physical.run_id}:time_trim_fills={trim_fills}")
        locked_before = {
            key: metrics_before[key] for key in sorted(_LOCKED_METRIC_KEYS)
        }
        locked_after = {key: metrics_after[key] for key in sorted(_LOCKED_METRIC_KEYS)}
        if Decimal(str(locked_after["locked_share_tw_mean"])) <= 0:
            unexpected.append(f"{physical.run_id}:locked_share_tw_mean_not_nonzero")
        if Decimal(str(locked_after["locked_share_max"])) <= 0:
            unexpected.append(f"{physical.run_id}:locked_share_max_not_nonzero")
        run_rows.append(
            {
                "run_id": physical.run_id,
                "arm": physical.arm.value,
                "c3_full_bitexact": None,
                "exact_artifacts": exact,
                "allowed_locked_axis": {
                    "metrics": {
                        "before": locked_before,
                        "after": locked_after,
                        "changed_keys": metric_differences,
                        "unexpected_keys": unexpected_metrics,
                    },
                    "nav_unit_price": nav,
                    "open_lots": lots,
                    "capital_days": capital,
                },
                "non_c3_time_trim_fills": trim_fills,
            }
        )

    return {
        "status": "PASS" if not unexpected else "FAIL",
        "unexpected_differences": unexpected,
        "runs": run_rows,
        "summary": {
            "B0_C1_C2_non_locked_fields_bitexact": not any(
                item for item in unexpected if not item.startswith("C3__")
            ),
            "C3_full_bitexact": not any(
                item for item in unexpected if item.startswith("C3__")
            ),
            "B0_C1_C2_time_trim_fills": sum(
                int(row["non_c3_time_trim_fills"] or 0)
                for row in run_rows
                if row["arm"] != Arm.C3.value
            ),
        },
    }


def _compare_root_artifacts(
    *, correction_root: Path, superseded_root: Path
) -> dict[str, dict[str, object]]:
    return {
        name: _value_hash_comparison(
            superseded_root / name,
            correction_root / name,
        )
        for name in ("clamped-pair-exposure.json", "dual-view-pairing.json")
    }


def _value_hash_comparison(before: Path, after: Path) -> dict[str, object]:
    before_sha = hashlib.sha256(_wrapped_value_bytes(before)).hexdigest()
    after_sha = hashlib.sha256(_wrapped_value_bytes(after)).hexdigest()
    return {
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "identical": before_sha == after_sha,
    }


def _wrapped_value_bytes(path: Path) -> bytes:
    raw = path.read_bytes().rstrip(b"\n")
    positions = [
        (raw.find(marker), marker)
        for marker in (b'"payload":', b'"rows":')
        if raw.find(marker) >= 0
    ]
    if not positions:
        raise PrimaryRunInvalid(f"artifact lacks payload/rows wrapper:{path}")
    marker_at, marker = min(positions, key=lambda item: item[0])
    start = marker_at + len(marker)
    end = raw.rfind(b',"run_id":')
    if end < start:
        end = raw.rfind(b',"schema_version":')
    if end < start:
        raise PrimaryRunInvalid(f"artifact wrapper boundary malformed:{path}")
    return raw[start:end]


def _read_wrapped_value(path: Path) -> object:
    payload = json.loads(path.read_bytes())
    if "payload" in payload:
        return payload["payload"]
    if "rows" in payload:
        return payload["rows"]
    raise PrimaryRunInvalid(f"artifact lacks payload/rows:{path}")


def _allowed_rows_comparison(
    before: Path, after: Path, *, allowed_keys: frozenset[str]
) -> dict[str, object]:
    before_rows = _read_wrapped_value(before)
    after_rows = _read_wrapped_value(after)
    if not isinstance(before_rows, list) or not isinstance(after_rows, list):
        raise PrimaryRunInvalid("allowed-axis artifact rows malformed")

    def projection(row: object) -> object:
        if not isinstance(row, dict):
            raise PrimaryRunInvalid("allowed-axis row malformed")
        return {key: value for key, value in row.items() if key not in allowed_keys}

    non_allowed_identical = len(before_rows) == len(after_rows) and [
        projection(row) for row in before_rows
    ] == [projection(row) for row in after_rows]
    changed_rows = 0
    if len(before_rows) == len(after_rows):
        for old, new in zip(before_rows, after_rows, strict=True):
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise PrimaryRunInvalid("allowed-axis row malformed")
            if any(old.get(key) != new.get(key) for key in allowed_keys):
                changed_rows += 1
    return {
        "row_count_before": len(before_rows),
        "row_count_after": len(after_rows),
        "allowed_keys": sorted(allowed_keys),
        "allowed_field_changed_rows": changed_rows,
        "non_allowed_fields_identical": non_allowed_identical,
    }


def _measure_clamped_rows_invariance(
    *, root: Path, row_count: int, unit_ids_sha256: str
) -> dict[str, object]:
    schema_after = sha256_file(root / "order-changing-clamped-rows-schema.json")
    rows_after = sha256_file(root / "order-changing-clamped-rows.json")
    unchanged = (
        schema_after == CLAMPED_ROWS_SCHEMA_SHA256
        and rows_after == CLAMPED_ROWS_SHA256
        and row_count == CLAMPED_ROWS_COUNT
        and unit_ids_sha256 == CLAMPED_UNIT_IDS_SHA256
    )
    return {
        "unchanged": unchanged,
        "schema_before_sha256": CLAMPED_ROWS_SCHEMA_SHA256,
        "schema_after_sha256": schema_after,
        "rows_before_sha256": CLAMPED_ROWS_SHA256,
        "rows_after_sha256": rows_after,
        "row_count_before": CLAMPED_ROWS_COUNT,
        "row_count_after": row_count,
        "unit_ids_before_sha256": CLAMPED_UNIT_IDS_SHA256,
        "unit_ids_after_sha256": unit_ids_sha256,
        "unit_id_identical": unit_ids_sha256 == CLAMPED_UNIT_IDS_SHA256,
    }


def _line_sha256(values: list[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _write_superseded_link(*, superseded_root: Path, correction_root: Path) -> None:
    correction_manifest = correction_root / "manifest.json"
    payload = canonical_bytes(
        {
            "schema_version": PRIMARY_SCHEMA_VERSION,
            "artifact_kind": "superseded-by",
            "superseded_manifest_sha256": SUPERSEDED_MANIFEST_SHA256,
            "superseded_by": str(correction_manifest.resolve()),
            "superseded_by_manifest_sha256": sha256_file(correction_manifest),
            "reason": "RUN_INVALID_PENDING_LOCKED_CLOCK_CORRECTION",
            "trial_count": TRIAL_COUNT,
            "correction_replay_count": CORRECTION_REPLAY_COUNT,
        }
    )
    target = superseded_root / "superseded-by.json"
    if target.exists() and target.read_bytes() != payload:
        raise PrimaryRunInvalid(f"conflicting superseded_by link:{target}")
    _write_bytes(target, payload)


def _demand_from_orders_file(path: Path) -> frozenset[tuple[date, str]]:
    payload = json.loads(path.read_bytes())
    pairs = payload["payload"]["counterfactual_demand_pairs"]
    return frozenset(
        (date.fromisoformat(row["session"]), str(row["symbol"])) for row in pairs
    )


def _publish_bundle(target: Path, bundle: dict[str, bytes]) -> None:
    if target.exists():
        raise PrimaryRunInvalid(f"refusing to overwrite existing run:{target}")
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.", dir=target.parent
    ) as raw_tmp:
        temporary = Path(raw_tmp)
        for name, raw in bundle.items():
            _write_bytes(temporary / name, raw)
        shutil.copytree(temporary, target)


def _seal_determinism(
    *,
    physical: PhysicalRun,
    first: dict[str, bytes],
    second: dict[str, bytes],
    stamps: dict[str, object],
) -> dict[str, bytes]:
    """Attach evidence only after two independently built core bundles match."""

    first_sha = _bundle_sha(first)
    second_sha = _bundle_sha(second)
    if first != second or first_sha != second_sha:
        raise PrimaryRunInvalid(f"non-deterministic bundle:{physical.run_id}")
    first_run = json.loads(first["run.json"])
    second_run = json.loads(second["run.json"])
    first_content_checksums = first_run.get("content_checksums")
    second_content_checksums = second_run.get("content_checksums")
    if (
        not isinstance(first_content_checksums, dict)
        or first_content_checksums != second_content_checksums
    ):
        raise PrimaryRunInvalid(f"non-deterministic content map:{physical.run_id}")
    finalized = dict(first)
    finalized["determinism.json"] = canonical_bytes(
        {
            "schema_version": PRIMARY_SCHEMA_VERSION,
            "artifact_kind": "determinism",
            "run_id": physical.run_id,
            "stamps": stamps,
            "payload": {
                "attempts_executed": 2,
                "attempt_1_core_bundle_sha256": first_sha,
                "attempt_2_core_bundle_sha256": second_sha,
                "attempt_1_content_checksums": first_content_checksums,
                "attempt_2_content_checksums": second_content_checksums,
                "byte_identical": True,
            },
        }
    )
    run_payload = json.loads(finalized["run.json"])
    content_checksums = {
        name: hashlib.sha256(raw).hexdigest()
        for name, raw in sorted(finalized.items())
        if name != "run.json"
    }
    run_payload.update(
        {
            "content_checksums": content_checksums,
            "artifact_count_excluding_run_json": len(content_checksums),
            "attempts_executed": 2,
            "engine_invocations_total": 2,
            "deterministic_2runs": True,
        }
    )
    finalized["run.json"] = canonical_bytes(run_payload)
    return finalized


def _verify_physical_run_bundle(root: Path, run_payload: dict[str, object]) -> bool:
    checksums = run_payload.get("content_checksums")
    if not isinstance(checksums, dict):
        return False
    actual_names = {
        path.name for path in root.glob("*.json") if path.name != "run.json"
    }
    if actual_names != set(checksums):
        return False
    for name, expected in checksums.items():
        path = root / str(name)
        if not path.is_file() or sha256_file(path) != expected:
            return False
    try:
        determinism = json.loads((root / "determinism.json").read_bytes())["payload"]
    except (KeyError, TypeError, json.JSONDecodeError, OSError):
        return False
    first_content = determinism.get("attempt_1_content_checksums")
    second_content = determinism.get("attempt_2_content_checksums")
    if not isinstance(first_content, dict) or first_content != second_content:
        return False
    current_core_names = actual_names - {"determinism.json"}
    if current_core_names != set(first_content):
        return False
    if any(
        sha256_file(root / str(name)) != expected
        for name, expected in first_content.items()
    ):
        return False
    return bool(
        run_payload.get("attempts_executed") == 2
        and run_payload.get("engine_invocations_total") == 2
        and run_payload.get("deterministic_2runs") is True
        and determinism.get("attempts_executed") == 2
        and determinism.get("byte_identical") is True
        and determinism.get("attempt_1_core_bundle_sha256")
        == determinism.get("attempt_2_core_bundle_sha256")
    )


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
    temporary.replace(path)


def _bundle_sha(bundle: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, raw in sorted(bundle.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _directory_bundle_sha(root: Path) -> str:
    return _bundle_sha(
        {path.name: path.read_bytes() for path in sorted(root.glob("*.json"))}
    )


def _read_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_bytes())
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise PrimaryRunInvalid(f"artifact rows missing:{path}")
    return rows


def _read_payload(path: Path) -> dict[str, object]:
    envelope = json.loads(path.read_bytes())
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise PrimaryRunInvalid(f"artifact payload missing:{path}")
    return payload


def _key_list_sha(keys: list[tuple[date, str]]) -> str | None:
    if not keys:
        return None
    return hashlib.sha256(canonical_bytes(sorted(keys))).hexdigest()


def _time_trim_rung(order_id: str) -> str:
    if "-TIME-" not in order_id:
        return "FILL_ONLY"
    return f"trim_{order_id.rsplit('-', 1)[-1]}"


def _fidelity_order_class(value: object) -> str:
    raw = _enum_value(value)
    return {
        "new": "new",
        "add": "add",
        "resistance_trim": "trim",
        "time_trim": "trim",
    }.get(raw, "other")


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))
