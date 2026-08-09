"""Fail-closed validators for ``DFC_V22_RESEARCH_MIN`` (A2).

Every validator raises :class:`ContractViolation` and returns ``None`` on
success.  There is deliberately no "warn" mode and no repair mode: a corpus that
does not satisfy the contract is not a corpus that may be measured, and the one
failure this design is built against is a builder quietly *fixing* its input
(dropping a row, imputing a bar, defaulting a missing hash) so that the run looks
clean.

These validators read in-memory objects only.  Nothing in this module opens a
socket, and nothing decides *what* to collect — collection is a separate relay
unit that runs after this contract is signed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pyarrow as pa

from . import contract as c
from .schema import (
    CANONICAL_TABLES,
    MANIFEST_REQUIRED_KEYS,
    MANIFEST_SOURCE_REQUIRED_KEYS,
    MANIFEST_TABLE_REQUIRED_KEYS,
)

__all__ = [
    "ContractViolation",
    "RUN_INVALID_AUTHENTICITY_EVIDENCE",
    "RUN_INVALID_CORPUS_LITERALS",
    "RUN_INVALID_FORBIDDEN_SOURCE",
    "RUN_INVALID_MANIFEST_SCHEMA",
    "RUN_INVALID_OUTCOME_EVIDENCE",
    "RUN_INVALID_SCOPE_SEPARATION",
    "RUN_INVALID_TABLE_SCHEMA",
    "validate_manifest",
    "validate_outcomes",
    "validate_table",
]

RUN_INVALID_MANIFEST_SCHEMA = "RUN_INVALID_MANIFEST_SCHEMA"
RUN_INVALID_CORPUS_LITERALS = "RUN_INVALID_CORPUS_LITERALS"
RUN_INVALID_FORBIDDEN_SOURCE = "RUN_INVALID_FORBIDDEN_SOURCE"
RUN_INVALID_AUTHENTICITY_EVIDENCE = "RUN_INVALID_AUTHENTICITY_EVIDENCE"
RUN_INVALID_SCOPE_SEPARATION = "RUN_INVALID_SCOPE_SEPARATION"
RUN_INVALID_TABLE_SCHEMA = "RUN_INVALID_TABLE_SCHEMA"
RUN_INVALID_OUTCOME_EVIDENCE = c.RUN_INVALID_OUTCOME_EVIDENCE


class ContractViolation(Exception):
    """A corpus artifact that the A2 contract refuses.

    ``code`` is the terminal run status the caller must report; it is never
    downgraded to a warning and never repaired in place.
    """

    def __init__(self, code: str, detail: str, *, clause: str) -> None:
        super().__init__(f"[{code}] {clause}: {detail}")
        self.code = code
        self.detail = detail
        self.clause = clause


def _fail(code: str, clause: str, detail: str) -> None:
    raise ContractViolation(code, detail, clause=clause)


def _iso_utc(value: Any) -> str:
    return str(value)


# --- A2-C3: forbidden source material -------------------------------------


def _reject_forbidden_tokens(code: str, clause: str, names: Iterable[str]) -> None:
    for name in names:
        lowered = str(name).lower()
        for token in c.FORBIDDEN_COLUMN_TOKENS:
            if token in lowered:
                _fail(
                    code,
                    clause,
                    f"forbidden source material referenced by {name!r} "
                    f"(matched {token!r}); funding/OI/mark/index are excluded "
                    "from this corpus",
                )


# --- table validation ------------------------------------------------------


def validate_table(name: str, table: pa.Table) -> None:
    """Validate one canonical parquet table against its frozen schema.

    Args:
        name: Canonical table name, e.g. ``"klines_4h"``.
        table: The table as loaded from parquet.

    Raises:
        ContractViolation: With ``RUN_INVALID_TABLE_SCHEMA`` for an unknown
            table, a missing/extra column or a type mismatch; with
            ``RUN_INVALID_FORBIDDEN_SOURCE`` when a column name carries
            funding/open-interest/mark/index material (A2-C3).
    """
    expected = CANONICAL_TABLES.get(name)
    if expected is None:
        _fail(
            RUN_INVALID_TABLE_SCHEMA,
            "A2-C3",
            f"unknown canonical table {name!r}; allowed: {sorted(CANONICAL_TABLES)}",
        )
        return

    actual_names = list(table.schema.names)
    _reject_forbidden_tokens(RUN_INVALID_FORBIDDEN_SOURCE, "A2-C3", actual_names)

    expected_names = list(expected.names)
    missing = [n for n in expected_names if n not in actual_names]
    extra = [n for n in actual_names if n not in expected_names]
    if missing or extra:
        _fail(
            RUN_INVALID_TABLE_SCHEMA,
            "A2-C3",
            f"table {name!r} column set mismatch; missing={missing} extra={extra}",
        )

    if actual_names != expected_names:
        _fail(
            RUN_INVALID_TABLE_SCHEMA,
            "A2-C3",
            f"table {name!r} column order mismatch; "
            f"expected={expected_names} actual={actual_names}",
        )

    for field in expected:
        actual = table.schema.field(field.name)
        if not actual.type.equals(field.type):
            _fail(
                RUN_INVALID_TABLE_SCHEMA,
                "A2-C3",
                f"table {name!r} column {field.name!r} type mismatch; "
                f"expected={field.type} actual={actual.type}",
            )

    for field in expected:
        if field.nullable:
            continue
        if table.column(field.name).null_count:
            _fail(
                RUN_INVALID_TABLE_SCHEMA,
                "A2-C3",
                f"table {name!r} column {field.name!r} is non-nullable but "
                "carries nulls; imputation and placeholders are forbidden",
            )


# --- A2-C4: outcome semantics ---------------------------------------------


def _outcome_column_guard(table: pa.Table) -> None:
    """Reject free booleans and free prices before anything else (A2-C4)."""
    allowed = set(CANONICAL_TABLES["outcomes"].names)
    for field in table.schema:
        if field.name in allowed:
            continue
        if pa.types.is_boolean(field.type):
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"free boolean column {field.name!r} in outcomes; outcome rows "
                "are generated from raw evidence only",
            )
        lowered = field.name.lower()
        for token in c.OUTCOME_FREE_PRICE_TOKENS:
            if token in lowered:
                _fail(
                    RUN_INVALID_OUTCOME_EVIDENCE,
                    "A2-C4",
                    f"free price/PnL column {field.name!r} in outcomes; the "
                    "only permitted number is the derived "
                    f"{c.OUTCOME_UNIT}",
                )
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"unknown outcomes column {field.name!r}; the outcome schema is "
            "closed so that no hand-supplied input can enter",
        )


def _kline_index(klines: pa.Table) -> dict[tuple[str, int], dict[str, Any]]:
    rows = klines.to_pylist()
    return {(row["symbol"], row["open_time"]): row for row in rows}


def validate_outcomes(
    outcomes: pa.Table,
    klines: pa.Table,
    decisions: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the outcome table against the decisions and the raw bars.

    ``decisions`` is the independent record of which signal epochs exist — one
    entry per ``BasketDecision``, carrying ``signal_epoch_open_time``,
    ``candidate_any`` and ``winner_symbol``.  Passing it in is what makes a
    silently deleted outcome row detectable: the contract says a missing or
    incomplete next bar is reported as ``RUN_INVALID_OUTCOME_EVIDENCE``, not
    dropped, so an outcome table that is simply *shorter* than the decision set
    is the exact failure mode being guarded.

    Raises:
        ContractViolation: Always with ``RUN_INVALID_OUTCOME_EVIDENCE``.
    """
    _outcome_column_guard(outcomes)
    validate_table("outcomes", outcomes)

    bars = _kline_index(klines)
    rows = outcomes.to_pylist()

    by_epoch: dict[int, dict[str, Any]] = {}
    for row in rows:
        epoch = row["signal_epoch_open_time"]
        if epoch in by_epoch:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"duplicate outcome row for signal epoch {epoch}",
            )
        by_epoch[epoch] = row

    expected_epochs = [d["signal_epoch_open_time"] for d in decisions]
    missing = [e for e in expected_epochs if e not in by_epoch]
    if missing:
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"{len(missing)} decision epoch(s) have no outcome row "
            f"(first={missing[0]}); rows may not be deleted — an unavailable "
            "or incomplete next bar is reported, not removed",
        )
    unexpected = [e for e in by_epoch if e not in set(expected_epochs)]
    if unexpected:
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"outcome rows without a decision: {sorted(unexpected)}",
        )

    for decision in decisions:
        epoch = decision["signal_epoch_open_time"]
        row = by_epoch[epoch]

        if row["candidate_any"] != decision[c.OUTCOME_ARM_LABEL_FIELD]:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: arm label {row['candidate_any']!r} does not "
                f"match the decision's {c.OUTCOME_ARM_LABEL_FIELD} "
                f"{decision[c.OUTCOME_ARM_LABEL_FIELD]!r}",
            )
        if row["winner_symbol"] != decision[c.OUTCOME_SYMBOL_FIELD]:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: winner {row['winner_symbol']!r} does not match "
                f"the decision's winner {decision[c.OUTCOME_SYMBOL_FIELD]!r}",
            )
        if row["evidence_status"] != c.OUTCOME_EVIDENCE_STATUS:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: evidence_status {row['evidence_status']!r} is "
                f"not {c.OUTCOME_EVIDENCE_STATUS!r}",
            )

        symbol = row["winner_symbol"]
        t_bar = bars.get((symbol, row["t_kline_open_time"]))
        if t_bar is None:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: no raw kline for {symbol} at "
                f"{row['t_kline_open_time']}",
            )
            return

        expected_next_open = t_bar["open_time"] + c.BAR_INTERVAL_MS
        if row["next_kline_open_time"] != expected_next_open:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: next bar must be the immediately next 4h bar "
                f"({expected_next_open}), got {row['next_kline_open_time']}",
            )

        next_bar = bars.get((symbol, expected_next_open))
        if next_bar is None:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: the next 4h bar for {symbol} at "
                f"{expected_next_open} is absent from the corpus",
            )
            return
        if next_bar["close_time"] != row["next_kline_close_time"]:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: next bar close_time "
                f"{row['next_kline_close_time']} does not match the raw bar "
                f"({next_bar['close_time']})",
            )

        if t_bar["payload_sha256"] != row["t_close_payload_sha256"]:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: t close payload hash does not match the raw "
                "bar it claims to come from",
            )
        if next_bar["payload_sha256"] != row["next_close_payload_sha256"]:
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: next close payload hash does not match the raw "
                "bar it claims to come from",
            )

        recomputed = _abs_log_return_bps(t_bar["close"], next_bar["close"])
        if abs(recomputed - row["outcome_abs_log_return_bps"]) > (
            c.OUTCOME_BPS_TOLERANCE
        ):
            _fail(
                RUN_INVALID_OUTCOME_EVIDENCE,
                "A2-C4",
                f"epoch {epoch}: recorded outcome "
                f"{row['outcome_abs_log_return_bps']} bps is not the value "
                f"derived from the referenced raw closes ({recomputed} bps)",
            )


def _abs_log_return_bps(t_close: str, next_close: str) -> float:
    try:
        start = float(t_close)
        end = float(next_close)
    except (TypeError, ValueError):
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"unparsable raw close token(s): {t_close!r} -> {next_close!r}",
        )
        raise
    if start <= 0.0 or end <= 0.0:
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"non-positive raw close(s): {t_close!r} -> {next_close!r}",
        )
    return abs(math.log(end / start)) * 10_000.0


# --- manifest validation ---------------------------------------------------


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the frozen corpus manifest.

    Raises:
        ContractViolation: With ``RUN_INVALID_MANIFEST_SCHEMA`` for structural
            problems, ``RUN_INVALID_CORPUS_LITERALS`` when a frozen literal was
            changed, ``RUN_INVALID_SCOPE_SEPARATION`` when FUT-DATA-A1 is
            declared a prerequisite (A2-C1), ``RUN_INVALID_FORBIDDEN_SOURCE``
            for funding/OI/mark/index material (A2-C3) and
            ``RUN_INVALID_AUTHENTICITY_EVIDENCE`` for missing provenance or a
            self-consistency admissibility claim (A2-C5).
    """
    missing = [k for k in MANIFEST_REQUIRED_KEYS if k not in manifest]
    if missing:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C2",
            f"manifest is missing required key(s): {missing}",
        )
    unknown = [k for k in manifest if k not in MANIFEST_REQUIRED_KEYS]
    if unknown:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C2",
            f"manifest carries unknown key(s): {sorted(unknown)}",
        )

    _validate_scope_separation(manifest)
    _validate_literals(manifest)
    _validate_sources(manifest["sources"])
    _validate_tables(manifest["tables"])
    _validate_admissibility(manifest["admissibility"])


def _validate_scope_separation(manifest: Mapping[str, Any]) -> None:
    prereq = manifest["prerequisite_readiness"]
    if not isinstance(prereq, Mapping):
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C1",
            "prerequisite_readiness must be an object",
        )
        return

    declared = str(prereq.get("contract_id", ""))
    if declared in c.A1_READINESS_IDS:
        _fail(
            RUN_INVALID_SCOPE_SEPARATION,
            "A2-C1",
            f"{declared} is preserved as its own readiness statement and is "
            "not a prerequisite of DFC-v2.x; the prerequisite is "
            f"{c.CONTRACT_ID}",
        )
    if declared != c.CONTRACT_ID:
        _fail(
            RUN_INVALID_SCOPE_SEPARATION,
            "A2-C1",
            f"prerequisite readiness contract must be {c.CONTRACT_ID!r}, "
            f"got {declared!r}",
        )

    dimensions = tuple(prereq.get("dimensions", ()))
    if dimensions != c.A2_JUDGED_DIMENSIONS:
        _fail(
            RUN_INVALID_SCOPE_SEPARATION,
            "A2-C1",
            f"A2 judges exactly {list(c.A2_JUDGED_DIMENSIONS)}, got {list(dimensions)}",
        )


def _validate_literals(manifest: Mapping[str, Any]) -> None:
    if manifest["contract_id"] != c.CONTRACT_ID:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"contract_id must be {c.CONTRACT_ID!r}, got {manifest['contract_id']!r}",
        )
    if manifest["corpus_id"] != c.CORPUS_ID:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"corpus_id must be {c.CORPUS_ID!r}, got {manifest['corpus_id']!r}",
        )
    if manifest["root"] != c.CORPUS_ROOT:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"root must be {c.CORPUS_ROOT!r}, got {manifest['root']!r}; a "
            "scratch directory is not the frozen corpus",
        )
    if manifest["frozen"] is not True:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C5",
            "manifest must declare frozen=true",
        )

    _expect_window(manifest["warmup"], c.WARMUP_START, c.WARMUP_END, "warmup")
    _expect_window(
        manifest["judgment_window"],
        c.JUDGMENT_START,
        c.JUDGMENT_END,
        "judgment_window",
    )

    if manifest["outcome_tail_bars"] != c.OUTCOME_TAIL_BARS:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"outcome_tail_bars must be {c.OUTCOME_TAIL_BARS}, "
            f"got {manifest['outcome_tail_bars']!r}",
        )

    universe = manifest["universe"]
    expected_universe = {
        "lookback_calendar_days": c.UNIVERSE_LOOKBACK_CALENDAR_DAYS,
        "ranking_metric": c.UNIVERSE_RANKING_METRIC,
        "top_n": c.UNIVERSE_TOP_N,
        "tie_break": c.UNIVERSE_TIE_BREAK,
        "instrument_class": c.UNIVERSE_INSTRUMENT_CLASS,
    }
    if dict(universe) != expected_universe:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"universe rule must be exactly {expected_universe}, got {dict(universe)}",
        )

    imputation = manifest["imputation"]
    if imputation.get("policy") != "forbidden":
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C3",
            "imputation policy must be 'forbidden'",
        )
    if imputation.get("imputed_row_count", -1) != c.IMPUTED_ROW_COUNT_MAX:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C3",
            "imputed_row_count must be "
            f"{c.IMPUTED_ROW_COUNT_MAX}, got "
            f"{imputation.get('imputed_row_count')!r}",
        )


def _expect_window(
    window: Mapping[str, Any],
    start: Any,
    end: Any,
    label: str,
) -> None:
    expected_start = start.isoformat().replace("+00:00", "Z")
    expected_end = end.isoformat().replace("+00:00", "Z")
    actual_start = _iso_utc(window.get("start"))
    actual_end = _iso_utc(window.get("end"))
    if actual_start != expected_start or actual_end != expected_end:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C2",
            f"{label} must be [{expected_start}, {expected_end}), got "
            f"[{actual_start}, {actual_end})",
        )


def _validate_sources(sources: Sequence[Mapping[str, Any]]) -> None:
    if not sources:
        _fail(
            RUN_INVALID_AUTHENTICITY_EVIDENCE,
            "A2-C5",
            "manifest declares no source objects",
        )

    kinds: set[str] = set()
    for index, source in enumerate(sources):
        label = f"sources[{index}]"
        kind = str(source.get("kind", ""))
        if kind.lower() in c.FORBIDDEN_SOURCE_KINDS:
            _fail(
                RUN_INVALID_FORBIDDEN_SOURCE,
                "A2-C3",
                f"{label}: source kind {kind!r} is excluded from this corpus; "
                "DFC-v2.2 signals read kline OFI and premium index only",
            )
        _reject_forbidden_tokens(
            RUN_INVALID_FORBIDDEN_SOURCE,
            "A2-C3",
            [source.get("name", ""), source.get("endpoint", "")],
        )

        missing = [k for k in MANIFEST_SOURCE_REQUIRED_KEYS if k not in source]
        if missing:
            _fail(
                RUN_INVALID_AUTHENTICITY_EVIDENCE,
                "A2-C5",
                f"{label}: missing provenance field(s) {missing}; every "
                "original object records endpoint, query, retrieved_at, "
                "schema, object SHA-256 and per-epoch payload SHA",
            )
        for field in c.REQUIRED_PROVENANCE_FIELDS:
            if not source.get(field):
                _fail(
                    RUN_INVALID_AUTHENTICITY_EVIDENCE,
                    "A2-C5",
                    f"{label}: provenance field {field!r} is empty",
                )
        payload_hashes = source["payload_sha256_by_epoch"]
        if not isinstance(payload_hashes, Mapping) or not payload_hashes:
            _fail(
                RUN_INVALID_AUTHENTICITY_EVIDENCE,
                "A2-C5",
                f"{label}: payload_sha256_by_epoch must be a non-empty mapping",
            )
        if kind not in c.REQUIRED_SOURCE_KINDS:
            _fail(
                RUN_INVALID_MANIFEST_SCHEMA,
                "A2-C3",
                f"{label}: unknown source kind {kind!r}; allowed: "
                f"{list(c.REQUIRED_SOURCE_KINDS)}",
            )
        kinds.add(kind)

    absent = [k for k in c.REQUIRED_SOURCE_KINDS if k not in kinds]
    if absent:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C3",
            f"required source material absent: {absent}",
        )


def _validate_tables(tables: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for index, entry in enumerate(tables):
        label = f"tables[{index}]"
        missing = [k for k in MANIFEST_TABLE_REQUIRED_KEYS if k not in entry]
        if missing:
            _fail(
                RUN_INVALID_AUTHENTICITY_EVIDENCE,
                "A2-C5",
                f"{label}: missing freeze evidence {missing}",
            )
        name = str(entry["name"])
        if name not in CANONICAL_TABLES:
            _fail(
                RUN_INVALID_MANIFEST_SCHEMA,
                "A2-C3",
                f"{label}: unknown canonical table {name!r}",
            )
        if not entry["sha256"]:
            _fail(
                RUN_INVALID_AUTHENTICITY_EVIDENCE,
                "A2-C5",
                f"{label}: canonical parquet must be frozen under a SHA-256",
            )
        seen.add(name)

    absent = [n for n in CANONICAL_TABLES if n not in seen]
    if absent:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C3",
            f"canonical table(s) absent from manifest: {absent}",
        )


def _validate_admissibility(admissibility: Mapping[str, Any]) -> None:
    basis = str(admissibility.get("basis", ""))
    if basis.lower() in c.ADMISSIBILITY_FORBIDDEN_BASES:
        _fail(
            RUN_INVALID_AUTHENTICITY_EVIDENCE,
            "A2-C5",
            f"admissibility basis {basis!r} is a self-consistency claim; "
            "Binance authenticity is asserted only by independent "
            "re-collection of the same public object",
        )
    if basis != c.ADMISSIBILITY_BASIS:
        _fail(
            RUN_INVALID_AUTHENTICITY_EVIDENCE,
            "A2-C5",
            f"admissibility basis must be {c.ADMISSIBILITY_BASIS!r}, got {basis!r}",
        )
    if admissibility.get("independent_recollection_required") is not True:
        _fail(
            RUN_INVALID_AUTHENTICITY_EVIDENCE,
            "A2-C5",
            "independent_recollection_required must be true",
        )
    keys = tuple(admissibility.get("comparison_keys", ()))
    if keys != c.ADMISSIBILITY_COMPARISON_KEYS:
        _fail(
            RUN_INVALID_AUTHENTICITY_EVIDENCE,
            "A2-C5",
            "the independent verifier compares exactly "
            f"{list(c.ADMISSIBILITY_COMPARISON_KEYS)}, got {list(keys)}",
        )
