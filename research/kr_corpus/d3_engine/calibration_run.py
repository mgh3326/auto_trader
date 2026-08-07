"""D3-C2G calibration harness — the separate runner Amendment A2 authorizes.

Two physical B0 replays (``with_contribution`` x ``{original, clamp}``) over
``D3_CALIBRATION_2025``, each executed twice and required to be byte-identical,
compared against the operator's real 2025 KR trading.

Boundary this file keeps:

* ``primary.py`` is untouched. The primary runner still hard-codes
  ``SealedAccessGuard`` and still cannot process a 2025 bar. That isolation is
  measured, not asserted — see
  ``calibration_acceptance.measure_primary_path_isolation``.
* The warm-up corpus (2015-2024) is loaded through an ordinary
  ``SealedAccessGuard``, so the exploration path proves it is unchanged: its
  spy must report zero sealed reads and zero blocked attempts.
* Only the calibration engine receives ``CalibrationAccessGuard``, and only
  after its allow-list has been bound from the sealed manifest.

Nothing here selects a winner, computes Pareto dominance, runs a sensitivity
variant, or releases ``INCONCLUSIVE_DATA_BIAS``.
"""

from __future__ import annotations

import gc
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.backtest.holdout_guard import HOLDOUT_DIR
from research.kr_corpus.d3_engine.calibration_acceptance import (
    measure_calibration_fail_closed_probes,
    measure_primary_path_isolation,
)
from research.kr_corpus.d3_engine.calibration_actual_side import build_actual_side
from research.kr_corpus.d3_engine.calibration_corpus import (
    CalibrationCorpus,
    group_by_symbol,
    load_calibration_corpus,
)
from research.kr_corpus.d3_engine.calibration_engine import CalibrationPortfolioEngine
from research.kr_corpus.d3_engine.calibration_guard import (
    AMENDMENT_A2_SHA256,
    CALIBRATION_INDEX_SHA256,
    CalibrationAccessGuard,
    CalibrationAccessSpy,
)
from research.kr_corpus.d3_engine.calibration_metrics import (
    CycleFill,
    SessionAxis,
    capital_share_observation,
    classify_gap03,
    compute_cycle_metrics,
    reconstruct_cycles,
)
from research.kr_corpus.d3_engine.calibration_result import build_result
from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.constants import ArtifactPaths
from research.kr_corpus.d3_engine.guards import SealedAccessGuard, SealedAccessSpy
from research.kr_corpus.d3_engine.models import (
    Arm,
    CashflowView,
    DataView,
    EngineResult,
    OrderSide,
    PortfolioRunInput,
)
from research.kr_corpus.d3_engine.primary_corpus import (
    CORPUS_BINDINGS,
    CorpusBar,
    PrimaryCorpusLoader,
    PrimaryCorpusPaths,
    _prepare_signal_tape,
)
from research.kr_corpus.d3_engine.sources import (
    FrozenKospiIndex,
    sha256_file,
    verify_start_gate,
)
from research.kr_corpus.d3_engine.tick import load_tick_table

CALIBRATION_SCHEMA_VERSION = "d3.calibration_run.v1"
ENGINE_BASE_COMMIT = "603cddc902af7a5c7a98a095efd25690c8b933df"
GAP_CLOSURE_SHA256 = "b285acdd3f4971eaa50dbf5ecb2cad8043afb5c8b4b2cde2f71c65bd2aaef03b"
FIDELITY_REFREEZE_SHA256 = (
    "94623599a0b4b92b8a1c47623a47be827cc7bbd920e3a9df8ac6ee560e341a30"
)
SUPERSEDED_C2P_RESULT_SHA256 = (
    "12cd2044ab13350046557d64d820554aacb0a0eb1d2fdf359f738eca4b4ede0f"
)
DECISION_START = date(2025, 1, 1)
SHA_GATE_COUNT = 14
VIEWS: tuple[tuple[str, DataView], ...] = (
    ("original", DataView.ORIGINAL_VALID_BAR),
    ("clamp", DataView.CLAMP_ADMIT_V1),
)


class CalibrationRunInvalid(RuntimeError):
    code = "RUN_INVALID_CALIBRATION_HARNESS"


@dataclass(frozen=True, slots=True)
class CalibrationHarnessPaths:
    artifacts: ArtifactPaths
    corpus: PrimaryCorpusPaths
    holdout_root: Path
    index_2025_csv: Path
    gap_closure: Path
    fidelity_refreeze: Path
    amendment_a2: Path
    superseded_root: Path
    output_root: Path
    progress_report: Path

    @classmethod
    def defaults(cls) -> CalibrationHarnessPaths:
        work = Path.home() / "work"
        inbox = work / "herdr-inbox"
        inputs = work / "herdr-artifacts" / "d3-contract-inputs-v1"
        return cls(
            artifacts=ArtifactPaths.defaults(),
            corpus=PrimaryCorpusPaths.defaults(),
            holdout_root=HOLDOUT_DIR,
            index_2025_csv=inputs / "kospi_index_daily_2025.csv",
            gap_closure=inbox / "d3-calibration-gap-closure-20260807.md",
            fidelity_refreeze=inbox / "d3-fidelity-refreeze-all-arm-20260807.md",
            amendment_a2=inbox / "d3-amendment-a2-20260808.md",
            superseded_root=work / "herdr-artifacts" / "d3-calibration-2p-v1",
            output_root=work / "herdr-artifacts" / "d3-calibration-2g-v1",
            progress_report=(
                work
                / "herdr-inbox"
                / "jobs"
                / "d3-c2g-guard-20260808-0630"
                / "events"
                / "impl.md"
            ),
        )


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _fill_tape(result: EngineResult) -> list[CycleFill]:
    """Engine fills reduced onto the shared cycle-metric record (gross prices)."""

    return [
        CycleFill(
            symbol=fill.symbol,
            side="BUY" if fill.side is OrderSide.BUY else "SELL",
            quantity=Decimal(fill.quantity),
            price=fill.price,
            day=fill.session,
            sequence=index,
        )
        for index, fill in enumerate(result.fills)
    ]


class CalibrationRunHarness:
    def __init__(
        self,
        *,
        harness_commit: str,
        paths: CalibrationHarnessPaths | None = None,
    ) -> None:
        if len(harness_commit) != 40:
            raise ValueError("harness_commit must be a full Git SHA")
        self.harness_commit = harness_commit
        self.paths = paths or CalibrationHarnessPaths.defaults()
        self._sha_gate: tuple[dict[str, str], ...] = ()
        self._stamps: dict[str, Any] = {}

    # -- preflight ----------------------------------------------------------

    def _preflight(self) -> None:
        base = verify_start_gate(self.paths.artifacts)
        if len(base) != 10:
            raise CalibrationRunInvalid(f"base SHA gate count drift:{len(base)}")
        extra: list[dict[str, str]] = []
        for path, expected in (
            (self.paths.gap_closure, GAP_CLOSURE_SHA256),
            (self.paths.fidelity_refreeze, FIDELITY_REFREEZE_SHA256),
            (self.paths.index_2025_csv, CALIBRATION_INDEX_SHA256),
            (self.paths.amendment_a2, AMENDMENT_A2_SHA256),
        ):
            if not path.is_file():
                raise CalibrationRunInvalid(f"missing gate input:{path}")
            actual = sha256_file(path)
            if actual != expected:
                raise CalibrationRunInvalid(
                    f"sha gate drift:{path.name}:{actual}!={expected}"
                )
            extra.append({"file": path.name, "sha256": actual, "status": "PASS"})
        self._sha_gate = (*base, *extra)
        if len(self._sha_gate) != SHA_GATE_COUNT:
            raise CalibrationRunInvalid(f"SHA gate count drift:{len(self._sha_gate)}")
        if self.paths.output_root.resolve() == self.paths.superseded_root.resolve():
            raise CalibrationRunInvalid(
                "the C2P artifact root is preserved and must not be overwritten"
            )
        superseded_result = self.paths.superseded_root / "calibration-result.json"
        if sha256_file(superseded_result) != SUPERSEDED_C2P_RESULT_SHA256:
            raise CalibrationRunInvalid("preserved C2P result drifted before the run")
        self.paths.progress_report.parent.mkdir(parents=True, exist_ok=True)

    def _append_progress(self, text: str) -> None:
        with self.paths.progress_report.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()

    # -- execution ----------------------------------------------------------

    def run_all(self) -> dict[str, Any]:
        self._preflight()
        self._append_progress(
            f"\nSHA_GATE = {len(self._sha_gate)}/{SHA_GATE_COUNT} PASS "
            "(recomputed at run start, A2 included)\n"
        )

        ticks = load_tick_table(self.paths.artifacts.tick_yaml)
        exploration_index = FrozenKospiIndex.load(self.paths.artifacts.index_csv)
        calibration_index = FrozenKospiIndex.load(
            self.paths.index_2025_csv, expected_sha256=CALIBRATION_INDEX_SHA256
        )
        market_sessions = tuple(
            row.session for row in (*exploration_index.rows, *calibration_index.rows)
        )
        if market_sessions != tuple(sorted(set(market_sessions))):
            raise CalibrationRunInvalid("combined XKRX axis is not strict ascending")
        index_closes = tuple(
            (row.session, row.close)
            for row in (*exploration_index.rows, *calibration_index.rows)
        )
        calibration_sessions = tuple(row.session for row in calibration_index.rows)
        axis = SessionAxis(calibration_sessions)

        run_guard = CalibrationAccessGuard(
            calibration_sessions=calibration_sessions,
            spy=CalibrationAccessSpy(),
            holdout_root=self.paths.holdout_root,
        )
        calibration = load_calibration_corpus(
            run_guard, holdout_root=self.paths.holdout_root
        )
        self._append_progress(
            "\nCALIBRATION_CORPUS = "
            f"original {len(calibration.bars_original)} rows · "
            f"clamp {len(calibration.bars_clamp)} rows · "
            f"authorized files {len(calibration.manifest.allowed_paths)} · "
            f"excluded out-of-scope {calibration.manifest.excluded_out_of_scope}\n"
        )

        fail_closed = measure_calibration_fail_closed_probes(
            template=run_guard,
            prospective_path=calibration.manifest.prospective_example,
            outside_manifest_path=(
                calibration.manifest.run_root
                / "dataset"
                / "market=KOSPI"
                / "year=2025"
                / "ticker=999999.parquet"
            ),
            authorized_path=calibration.manifest.allowed_paths[0],
        )
        if fail_closed["status"] != "PASS":
            raise CalibrationRunInvalid(f"fail-closed probes failed:{fail_closed}")
        isolation = measure_primary_path_isolation(tick_table=ticks)
        if isolation["status"] != "PASS":
            raise CalibrationRunInvalid(f"primary path isolation failed:{isolation}")
        self._append_progress(
            f"\nFAILCLOSED_NEW = {fail_closed['outcomes']}\n"
            f"PRIMARY_ISOLATION = {isolation['status']} "
            f"(guard={isolation['installed_guard_class']})\n"
        )

        self._stamps = self._build_stamps(calibration=calibration)
        self.paths.output_root.mkdir(parents=True, exist_ok=True)
        (self.paths.output_root / "runs").mkdir(exist_ok=True)

        completed: list[dict[str, Any]] = []
        simulated_metrics: dict[str, dict[str, Any]] = {}
        simulated_census: dict[str, Any] = {}
        engine_statuses: dict[str, str] = {}
        exploration_access: dict[str, dict[str, int]] = {}
        engine_access: dict[str, dict[str, Any]] = {}
        operator_closes: dict[str, dict[date, Decimal]] = {}
        operator_symbols = self._operator_symbols()

        for view_name, data_view in VIEWS:
            run_id = f"B0__with_contribution__{data_view.value}"
            loader_guard = SealedAccessGuard(SealedAccessSpy())
            loader = PrimaryCorpusLoader(paths=self.paths.corpus, guard=loader_guard)
            warmup = loader.load(data_view, market_sessions=market_sessions)
            evidence = loader_guard.spy.evidence()
            if evidence["sealed_access_spy"] != 0:
                raise CalibrationRunInvalid("warm-up loader measured a sealed read")
            if evidence["sealed_access_blocked_attempts"] != 0:
                raise CalibrationRunInvalid("warm-up loader attempted a sealed access")
            exploration_access[view_name] = evidence

            calibration_bars = (
                calibration.bars_original
                if data_view is DataView.ORIGINAL_VALID_BAR
                else calibration.bars_clamp
            )
            bars = tuple(
                sorted(
                    [*warmup.bars, *calibration_bars],
                    key=lambda bar: (bar.session, bar.symbol),
                )
            )
            if len({(bar.session, bar.symbol) for bar in bars}) != len(bars):
                raise CalibrationRunInvalid("warm-up and 2025 bars collide")
            if view_name == "original":
                operator_closes = _collect_closes(bars, operator_symbols)

            positions = {
                session: index for index, session in enumerate(market_sessions)
            }
            tape = _prepare_signal_tape(group_by_symbol(bars), positions)
            self._append_progress(
                f"\nVIEW {view_name}: warm-up rows={warmup.row_count} · "
                f"2025 rows={len(calibration_bars)} · combined={len(bars)} · "
                f"signal tape={len(tape)}\n"
            )
            del warmup
            gc.collect()

            run_input = PortfolioRunInput(
                arm=Arm.B0,
                cashflow_view=CashflowView.WITH_CONTRIBUTION,
                bars=bars,  # type: ignore[arg-type]
                data_view=data_view,
                market_sessions=market_sessions,
                index_closes=index_closes,
                corporate_actions=(),
                decision_start=DECISION_START,
            )
            first_bundle, first_result, first_trace = self._execute_attempt(
                ticks, run_guard, tape, run_input, run_id=run_id
            )
            second_bundle, _, _ = self._execute_attempt(
                ticks, run_guard, tape, run_input, run_id=run_id
            )
            if first_bundle != second_bundle:
                raise CalibrationRunInvalid(f"non-deterministic physical run:{run_id}")

            for name, payload in first_bundle.items():
                _write_bytes(self.paths.output_root / "runs" / run_id / name, payload)
            bundle_sha = hashlib.sha256(
                b"".join(first_bundle[name] for name in sorted(first_bundle))
            ).hexdigest()
            completed.append(
                {
                    "run_id": run_id,
                    "arm": Arm.B0.value,
                    "cashflow_view": CashflowView.WITH_CONTRIBUTION.value,
                    "data_view": data_view.value,
                    "deterministic_2runs": True,
                    "bundle_sha256": bundle_sha,
                    "fills": len(first_result.fills),
                    "status": first_result.status,
                }
            )
            engine_statuses[run_id] = first_result.status
            engine_access[view_name] = {
                key: value
                for key, value in first_result.evidence.items()
                if key.startswith(("sealed_", "measured_", "calibration_"))
            }

            recon = reconstruct_cycles(_fill_tape(first_result))
            gap03 = classify_gap03(recon)
            metrics = compute_cycle_metrics(recon, gap03, axis)
            simulated_census[view_name] = metrics.pop("_census")
            metrics["capital_share"] = capital_share_observation(
                [Decimal(str(row["locked_share"])) for row in first_trace.daily],
                definition_id="gap04_locked_share_daily_grain_time_weighted_mean",
                note=(
                    "GAP-04 locked share straight off the engine's own daily "
                    "clock; cost basis includes the uniform 21.5bp buy fee, "
                    "which cancels in the ratio"
                ),
            )
            self._cross_check_locked_share(
                metrics["capital_share"], first_result, run_id
            )
            simulated_metrics[view_name] = metrics
            self._append_progress(
                f"\nRUN_COMPLETE {run_id} · 2RUN_BYTE_IDENTICAL=PASS · "
                f"fills={len(first_result.fills)} · bundle_sha256={bundle_sha}\n"
            )
            del bars, tape, first_bundle, second_bundle
            gc.collect()

        actual = build_actual_side(
            axis=axis,
            clock_sessions=market_sessions,
            closes=operator_closes,
        )
        _write_bytes(
            self.paths.output_root / "actual-side-metrics.json",
            canonical_bytes({"stamps": self._stamps, "payload": actual}),
        )

        result = build_result(
            actual_metrics=actual["metrics"],
            simulated_metrics=simulated_metrics,
            engine_statuses=engine_statuses,
            stamps=self._stamps,
            census={
                "actual": actual["census"],
                "simulated": simulated_census,
            },
        )
        _write_bytes(
            self.paths.output_root / "calibration-result.json", canonical_bytes(result)
        )

        sealed_payload = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "stamps": self._stamps,
            "amendment": "d3-amendment-a2-20260808.md",
            "amendment_sha256": AMENDMENT_A2_SHA256,
            "authorized_scope": {
                "dates": (
                    "<=2024 warm-up, plus the 242 2025 sessions carried by "
                    "kospi_index_daily_2025.csv"
                ),
                "paths": (
                    "exploration corpus, plus the D3_CALIBRATION_2025 manifest "
                    "entries whose partition year is 2025"
                ),
                "cell": "B0 x with_contribution x {original, clamp}, one-shot",
            },
            "calibration_loader_measurement": run_guard.spy.evidence(),
            "calibration_engine_measurement_per_view": engine_access,
            "exploration_loader_measurement": exploration_access,
            "corpus_evidence": calibration.evidence(),
            "fail_closed_probes": fail_closed,
            "primary_path_isolation": isolation,
        }
        _write_bytes(
            self.paths.output_root / "sealed-access.json",
            canonical_bytes(sealed_payload),
        )

        manifest = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "artifact": self.paths.output_root.name,
            "job_id": "D3-C2G",
            "label": "CALIBRATION_DIAGNOSTIC_ONLY",
            "purpose": "D3.1_MODEL_INPUT_ONLY",
            "stamps": self._stamps,
            "matrix": {
                "physical_runs": len(completed),
                "expected_physical_runs": 2,
                "deterministic_2runs": sum(
                    bool(row["deterministic_2runs"]) for row in completed
                ),
                "runs": completed,
            },
            "top_level_status": result["top_level_status"],
            "terminal_state_basis": result["terminal_state_basis"],
            "dual_view": result["dual_view"],
            "sealed_access_path": "sealed-access.json",
            "calibration_result_path": "calibration-result.json",
            "actual_side_path": "actual-side-metrics.json",
            "supersedes": {
                "artifact_root": str(self.paths.superseded_root.resolve()),
                "calibration_result_sha256": SUPERSEDED_C2P_RESULT_SHA256,
                "status": "CALIBRATION_INCOMPLETE_PENDING_ENGINE_CARVE_OUT",
                "preserved_untouched": True,
            },
            "winner_selected": False,
            "pareto_computed": False,
            "sensitivity_runs": 0,
            "inconclusive_data_bias_released": False,
            "limitations": [
                "no 2025 corporate-action sidecar is a frozen input, so the "
                "replay runs with corporate_actions=(); the exploration delist "
                "sidecar covers 2015-2024 only and cannot reach 2025",
                "an operator cycle whose first fill predates 2025 has no "
                "session_seq on the sealed 2025 axis and is excluded from "
                "open_lot_age_sessions rather than clipped",
            ],
        }
        _write_bytes(
            self.paths.output_root / "manifest.json", canonical_bytes(manifest)
        )
        self._write_checksums()
        self._append_progress(
            f"\nPHYSICAL_RUNS = {len(completed)}/2 · "
            f"DETERMINISTIC_2RUNS = {len(completed)}/2 · "
            f"TERMINAL_STATE = {result['top_level_status']} · "
            "WINNER_TOUCHED = NO\n"
        )
        return manifest

    # -- helpers ------------------------------------------------------------

    def _execute_attempt(
        self,
        ticks: Any,
        guard: CalibrationAccessGuard,
        tape: dict[tuple[date, str], Any],
        run_input: PortfolioRunInput,
        *,
        run_id: str,
    ) -> tuple[dict[str, bytes], EngineResult, Any]:
        # Each attempt gets its own spy: EngineResult.evidence embeds the guard
        # counters, so a shared accumulating spy would break byte-identity.
        engine = CalibrationPortfolioEngine(
            ticks, access_guard=guard.fresh_clone(), signals=tape
        )
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            result, trace = engine.execute(run_input)
        finally:
            if gc_was_enabled:
                gc.enable()
        payloads: dict[str, Any] = {
            "fills.json": [
                {
                    "order_id": fill.order_id,
                    "session": fill.session,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "class": fill.order_class,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "gross": fill.gross,
                    "fee": fill.fee,
                }
                for fill in result.fills
            ],
            "events.json": list(result.events),
            "terminal-positions.json": list(result.terminal_positions),
            "capital-share-daily.json": list(trace.daily),
            "metrics.json": result.metrics,
            "evidence.json": result.evidence,
        }
        bundle = {
            name: canonical_bytes(
                {
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "artifact_kind": name.removesuffix(".json"),
                    "run_id": run_id,
                    "stamps": self._stamps,
                    "rows" if isinstance(payload, list) else "payload": payload,
                }
            )
            for name, payload in payloads.items()
        }
        content_checksums = {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in sorted(bundle.items())
        }
        bundle["run.json"] = canonical_bytes(
            {
                "schema_version": CALIBRATION_SCHEMA_VERSION,
                "artifact_kind": "physical_run",
                "run_id": run_id,
                "stamps": self._stamps,
                "parameters": {
                    "arm": run_input.arm,
                    "cashflow_view": run_input.cashflow_view,
                    "data_view": run_input.data_view,
                    "decision_start": run_input.decision_start,
                    "fee_rate": "0.00215",
                    "monthly_contribution": "3500000",
                    "settlement": "T+2",
                    "same_bar": "buy-first same symbol",
                    "corporate_actions": 0,
                },
                "result_status": result.status,
                "content_checksums": content_checksums,
                "engine_invocations": trace.engine_invocations,
                "physical_run_completed": True,
                "winner_selected": False,
            }
        )
        return bundle, result, trace

    def _cross_check_locked_share(
        self, observation: dict[str, Any], result: EngineResult, run_id: str
    ) -> None:
        """The daily series must reproduce the engine's own locked-share metrics."""

        engine_mean = Decimal(str(result.metrics["locked_share_tw_mean"]))
        traced_mean = Decimal(str(observation["aggregate_decimal"]))
        engine_max = Decimal(str(result.metrics["locked_share_max"]))
        traced_max = Decimal(str(observation["max_decimal"]))
        if engine_mean != traced_mean or engine_max != traced_max:
            raise CalibrationRunInvalid(
                f"locked-share trace disagrees with engine metrics:{run_id}"
            )

    def _operator_symbols(self) -> frozenset[str]:
        from research.kr_corpus.d3_engine.calibration_actual_side import (
            load_actual_fills,
        )

        return frozenset(fill.symbol for fill in load_actual_fills())

    def _build_stamps(self, *, calibration: CalibrationCorpus) -> dict[str, Any]:
        return {
            "input_sha256": {str(row["file"]): row["sha256"] for row in self._sha_gate},
            "input_sha_gate_count": len(self._sha_gate),
            "engine_base_commit": ENGINE_BASE_COMMIT,
            "harness_commit": self.harness_commit,
            "label": "CALIBRATION_DIAGNOSTIC_ONLY",
            "purpose": "D3.1_MODEL_INPUT_ONLY",
            "amendment": {
                "id": "A2",
                "file": "d3-amendment-a2-20260808.md",
                "sha256": AMENDMENT_A2_SHA256,
            },
            "corpus": {
                **CORPUS_BINDINGS,
                "calibration_manifest_sha256": calibration.manifest.manifest_sha256,
                "calibration_checksums_sha256": calibration.manifest.checksums_sha256,
            },
            "execution_conventions": {
                "fee": "43bp neutral: 21.5bp/side",
                "cashflow": "3500000 KRW monthly",
                "settlement": "T+2",
                "same_bar": "buy-first same symbol",
                "decision_start": DECISION_START.isoformat(),
                "warmup": "2015-2024 exploration corpus, contiguous into 2025",
                "sensitivity": False,
            },
        }

    def _write_checksums(self) -> None:
        lines: list[str] = []
        for path in sorted(self.paths.output_root.rglob("*")):
            if not path.is_file() or path.name == "checksums.sha256":
                continue
            digest = sha256_file(path)
            lines.append(f"{digest}  {path.relative_to(self.paths.output_root)}")
        _write_bytes(
            self.paths.output_root / "checksums.sha256",
            ("\n".join(lines) + "\n").encode("utf-8"),
        )


def _collect_closes(
    bars: tuple[CorpusBar, ...], symbols: frozenset[str]
) -> dict[str, dict[date, Decimal]]:
    closes: dict[str, dict[date, Decimal]] = {symbol: {} for symbol in symbols}
    for bar in bars:
        series = closes.get(bar.symbol)
        if series is not None:
            series[bar.session] = Decimal(bar.close_int)
    return {symbol: series for symbol, series in closes.items() if series}


__all__ = [
    "CalibrationHarnessPaths",
    "CalibrationRunHarness",
    "CalibrationRunInvalid",
]
