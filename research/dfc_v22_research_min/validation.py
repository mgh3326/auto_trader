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
from decimal import Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from . import contract as c
from .schema import (
    CANONICAL_TABLES,
    GAP_EXCLUSION_REQUIRED_KEYS,
    MANIFEST_GAP_REQUIRED_KEYS,
    MANIFEST_LIFECYCLE_REQUIRED_KEYS,
    MANIFEST_RANKING_INPUT_DEFICIT_REQUIRED_KEYS,
    MANIFEST_REQUIRED_KEYS,
    MANIFEST_SOURCE_REQUIRED_KEYS,
    MANIFEST_TABLE_REQUIRED_KEYS,
    RANKING_INPUT_DEFICIT_ROW_REQUIRED_KEYS,
    SAMPLE_REPORT_REQUIRED_KEYS,
    SAMPLE_ROW_REQUIRED_KEYS,
)

__all__ = [
    "ARM_LABELS",
    "ContractViolation",
    "RUN_INVALID_AUTHENTICITY_EVIDENCE",
    "RUN_INVALID_CORPUS_LITERALS",
    "RUN_INVALID_FORBIDDEN_SOURCE",
    "RUN_INVALID_INPUT_EVIDENCE",
    "RUN_INVALID_MANIFEST_SCHEMA",
    "RUN_INVALID_OUTCOME_EVIDENCE",
    "RUN_INVALID_SCOPE_SEPARATION",
    "RUN_INVALID_TABLE_SCHEMA",
    "validate_corpus",
    "validate_input_evidence",
    "validate_manifest",
    "validate_outcomes",
    "validate_premium_index_gap",
    "validate_ranking_input_deficit",
    "validate_sample_readiness",
    "validate_table",
]

RUN_INVALID_MANIFEST_SCHEMA = "RUN_INVALID_MANIFEST_SCHEMA"
RUN_INVALID_CORPUS_LITERALS = "RUN_INVALID_CORPUS_LITERALS"
RUN_INVALID_FORBIDDEN_SOURCE = "RUN_INVALID_FORBIDDEN_SOURCE"
RUN_INVALID_AUTHENTICITY_EVIDENCE = "RUN_INVALID_AUTHENTICITY_EVIDENCE"
RUN_INVALID_SCOPE_SEPARATION = "RUN_INVALID_SCOPE_SEPARATION"
RUN_INVALID_TABLE_SCHEMA = "RUN_INVALID_TABLE_SCHEMA"
RUN_INVALID_OUTCOME_EVIDENCE = c.RUN_INVALID_OUTCOME_EVIDENCE
RUN_INVALID_INPUT_EVIDENCE = c.RUN_INVALID_INPUT_EVIDENCE

#: Re-exported so a reader of the validator sees the closed arm-label domain
#: it enforces without following an import (A2-C4 / NW-F4).
ARM_LABELS = c.ARM_LABELS


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


def _require_arm_label(value: Any, *, origin: str, epoch: Any) -> str:
    """Return ``value`` iff it is one of the two admissible arm labels (A2-C4).

    There is no coercion path.  ``bool`` is checked first and by type, because
    ``bool`` is what a silent ``bool(...)`` conversion produces and what NW-F4
    names as forbidden free input; an arbitrary string is rejected right after,
    because an arm label the contract never defined is not an arm label at all.
    Either way the caller gets ``RUN_INVALID_OUTCOME_EVIDENCE`` — never a
    repaired value.
    """
    if isinstance(value, bool):
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"epoch {epoch}: {origin} arm label is a free bool ({value!r}); "
            f"candidate_any is an arm label, one of {list(c.ARM_LABELS)}, and "
            "is never coerced from a truth value",
        )
    if not isinstance(value, str) or value not in c.ARM_LABELS:
        _fail(
            RUN_INVALID_OUTCOME_EVIDENCE,
            "A2-C4",
            f"epoch {epoch}: {origin} arm label {value!r} is not one of the "
            f"admissible arm labels {list(c.ARM_LABELS)}; the arm-label domain "
            "is closed by contract",
        )
    return str(value)


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

        decision_arm = _require_arm_label(
            decision[c.OUTCOME_ARM_LABEL_FIELD], origin="decision", epoch=epoch
        )
        row_arm = _require_arm_label(
            row["candidate_any"], origin="outcome row", epoch=epoch
        )
        if row_arm != decision_arm:
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
            self-consistency admissibility claim (A2-C5), and
            ``RUN_INVALID_INPUT_EVIDENCE`` for a lifecycle-authority or
            archive-gap declaration the contract refuses (A2-C6 / A2-C7).
    """
    validate_input_evidence(manifest)

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


# --- A2-C6: lifecycle authority absence and substitute evidence -----------


def _validate_lifecycle_eligibility(manifest: Mapping[str, Any]) -> None:
    """The corpus must state that no authority exists, and use the substitute.

    The failure this guards is not a builder inventing a Binance endpoint out
    of nothing — it is a builder quietly promoting one of the two known proxies
    to "the lifecycle source", which would make an inference from data presence
    read as a record Binance stands behind.
    """
    declared = manifest.get("lifecycle_eligibility")
    if not isinstance(declared, Mapping):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            "manifest does not declare lifecycle_eligibility; the absence of "
            "an authoritative public lifecycle source is stated by this "
            "corpus, never left to be assumed either way",
        )
        return

    missing = [k for k in MANIFEST_LIFECYCLE_REQUIRED_KEYS if k not in declared]
    if missing:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            f"lifecycle_eligibility is missing {missing}",
        )

    authoritative = declared["authoritative_public_source"]
    if authoritative is not c.LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            f"authoritative_public_source must be "
            f"{c.LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE!r}: A2-MEASURE "
            f"established that none exists, so naming {authoritative!r} "
            "asserts an authority that was looked for and not found",
        )

    kind = declared["evidence_kind"]
    if kind in c.LIFECYCLE_PROXY_KINDS:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            f"evidence_kind {kind!r} is a proxy, not the eligibility evidence; "
            f"{c.LIFECYCLE_PROXY_LIMITS[kind]}",
        )
    if kind != c.ELIGIBILITY_EVIDENCE_KIND:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            f"evidence_kind must be {c.ELIGIBILITY_EVIDENCE_KIND!r} — a "
            "completed 4h kline with non-zero traded volume across the "
            f"ranking window — got {kind!r}",
        )

    limits = declared["proxy_limits"]
    if not isinstance(limits, Mapping) or set(limits) != set(c.LIFECYCLE_PROXY_LIMITS):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            "proxy_limits must record exactly the known proxies "
            f"{sorted(c.LIFECYCLE_PROXY_LIMITS)}; dropping one loses the "
            "record of what it cannot answer",
        )
    for name, text in c.LIFECYCLE_PROXY_LIMITS.items():
        if limits.get(name) != text:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C6",
                f"proxy_limits[{name!r}] does not carry the frozen limitation "
                "text; a proxy's limits may not be softened",
            )


# --- A2-C7: premium-index archive gap -------------------------------------


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"{label}: {value!r} is not a parsable quote volume",
        )
        raise


def _validate_gap_section(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the manifest's gap declaration; return the in-window subset."""
    gap = manifest.get("premium_index_gap")
    if not isinstance(gap, Mapping):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            "manifest does not declare premium_index_gap; the klines vs "
            "premiumIndexKlines archive diff is an input to this corpus and "
            "is recorded, not assumed empty",
        )
        return ()

    missing = [k for k in MANIFEST_GAP_REQUIRED_KEYS if k not in gap]
    if missing:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"premium_index_gap is missing {missing}",
        )

    symbols = tuple(gap["symbols"])
    in_window = tuple(gap["in_window_symbols"])
    stray = [s for s in in_window if s not in symbols]
    if stray:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"in_window_symbols are not part of the measured diff: {stray}",
        )

    exclusions = gap["exclusions"]
    by_symbol: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(exclusions):
        label = f"exclusions[{index}]"
        absent = [k for k in GAP_EXCLUSION_REQUIRED_KEYS if k not in entry]
        if absent:
            _fail(RUN_INVALID_INPUT_EVIDENCE, "A2-C7", f"{label}: missing {absent}")
        reason = entry["reason"]
        if reason not in c.GAP_EXCLUSION_REASONS:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"{label}: reason {reason!r} is not one of "
                f"{list(c.GAP_EXCLUSION_REASONS)}; the reason set is closed so "
                "that no gap symbol is dropped on an unstated ground",
            )
        if not entry["evidence_sha256"]:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"{label}: excluding a gap symbol requires evidence",
            )
        if reason == c.GAP_EXCLUSION_BASE_SYMBOL_REASON:
            base = entry.get("base_symbol")
            if not base:
                _fail(
                    RUN_INVALID_INPUT_EVIDENCE,
                    "A2-C7",
                    f"{label}: reason {reason!r} must name the base_symbol it "
                    "claims the rows are identical to",
                )
            if base in symbols:
                _fail(
                    RUN_INVALID_INPUT_EVIDENCE,
                    "A2-C7",
                    f"{label}: base_symbol {base!r} is itself in the gap set, "
                    "so it carries no premium-index material either",
                )
        by_symbol[str(entry["symbol"])] = entry

    unaccounted = [s for s in symbols if s not in in_window and s not in by_symbol]
    if unaccounted:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"{len(unaccounted)} gap symbol(s) are neither audited per epoch "
            f"nor excluded with a reason (first={unaccounted[0]!r}); every "
            "symbol in the diff is accounted for",
        )
    both = [s for s in in_window if s in by_symbol]
    if both:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"gap symbol(s) both audited and excluded: {both}",
        )
    return in_window


def validate_premium_index_gap(
    gap_audit: pa.Table,
    pit_universe: pa.Table,
    in_window_symbols: Sequence[str],
) -> None:
    """Decide, per epoch, whether the archive gap touches the candidate pool.

    The verdict is recorded by the builder and *recomputed* here, exactly as
    outcome numbers are (A2-C4).  Nothing in this function returns a pool, edits
    a pool, or names a replacement symbol: when a gap symbol outranks the
    declared top 3, the epoch is reported ``RUN_INVALID_INPUT_EVIDENCE`` and
    that is the whole of the response.  Promoting the next-ranked symbol into
    the vacancy would turn an input-evidence failure into a clean-looking
    ranking, which is the failure OD-26 names first.

    Args:
        gap_audit: The ``premium_index_gap_audit`` table.
        pit_universe: The ``pit_universe`` table, carrying the declared ranks.
        in_window_symbols: Gap symbols that require a per-epoch audit row.

    Raises:
        ContractViolation: Always with ``RUN_INVALID_INPUT_EVIDENCE``.
    """
    validate_table("premium_index_gap_audit", gap_audit)
    validate_table("pit_universe", pit_universe)

    pools: dict[int, list[dict[str, Any]]] = {}
    for row in pit_universe.to_pylist():
        pools.setdefault(row["epoch_open_time"], []).append(row)

    audit: dict[tuple[int, str], dict[str, Any]] = {}
    for row in gap_audit.to_pylist():
        key = (row["epoch_open_time"], row["symbol"])
        if key in audit:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"duplicate gap audit row for {key}",
            )
        audit[key] = row

    expected = frozenset(in_window_symbols)
    for epoch, symbol in audit:
        if symbol not in expected:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"gap audit row for {symbol!r} at epoch {epoch}, which is not "
                "declared as an in-window gap symbol",
            )
        if epoch not in pools:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"gap audit row at epoch {epoch}, which has no pit_universe pool",
            )

    for epoch in sorted(pools):
        pool = _ranked_pool(epoch, pools[epoch])
        cut_symbol = pool[-1]["symbol"]
        cut_volume = _decimal(
            pool[-1]["quote_volume_lookback"], label=f"epoch {epoch} rank cut"
        )
        pool_symbols = {row["symbol"] for row in pool}

        for symbol in in_window_symbols:
            if symbol in pool_symbols:
                _fail(
                    RUN_INVALID_INPUT_EVIDENCE,
                    "A2-C7",
                    f"epoch {epoch}: {symbol!r} is in the candidate pool but "
                    "the archive carries no premium index for it; the epoch is "
                    "invalid, and the pool is not re-ranked around it",
                )
            row = audit.get((epoch, symbol))
            if row is None:
                _fail(
                    RUN_INVALID_INPUT_EVIDENCE,
                    "A2-C7",
                    f"epoch {epoch}: in-window gap symbol {symbol!r} has no "
                    "audit row; a gap symbol is accounted for at every epoch, "
                    "never omitted",
                )
                continue
            _check_gap_row(epoch, row, cut_symbol=cut_symbol, cut_volume=cut_volume)


def _ranked_pool(epoch: int, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the pool in declared rank order, having checked it *is* the ranking."""
    pool = sorted((dict(r) for r in rows), key=lambda r: r["rank"])
    ranks = [r["rank"] for r in pool]
    if ranks != list(range(1, c.UNIVERSE_TOP_N + 1)):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: pit_universe ranks are {ranks}, expected exactly "
            f"1..{c.UNIVERSE_TOP_N}",
        )
    symbols = [r["symbol"] for r in pool]
    if len(set(symbols)) != len(symbols):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: duplicate symbol in the candidate pool {symbols}",
        )
    reordered = sorted(
        pool,
        key=lambda r: (
            -_decimal(r["quote_volume_lookback"], label=f"epoch {epoch} {r['symbol']}"),
            r["symbol"],
        ),
    )
    if [r["symbol"] for r in reordered] != symbols:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: the declared pool order {symbols} is not the "
            f"{c.UNIVERSE_RANKING_METRIC} ranking it claims to be "
            f"({[r['symbol'] for r in reordered]}, ties broken "
            f"{c.UNIVERSE_TIE_BREAK})",
        )
    return pool


def _check_gap_row(
    epoch: int,
    row: Mapping[str, Any],
    *,
    cut_symbol: str,
    cut_volume: Decimal,
) -> None:
    status = row["eligibility_status"]
    if status not in c.ELIGIBILITY_STATUSES:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: eligibility_status {status!r} is not one of "
            f"{list(c.ELIGIBILITY_STATUSES)}",
        )
    if not row["eligibility_evidence_sha256"]:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C6",
            f"epoch {epoch}: {row['symbol']!r} carries no eligibility evidence; "
            f"eligibility is decided by {c.ELIGIBILITY_EVIDENCE_KIND}, and the "
            "evidence it was decided from is recorded",
        )

    volume = row["quote_volume_lookback"]
    if status == c.NOT_ELIGIBLE:
        if volume is not None:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"epoch {epoch}: {row['symbol']!r} is recorded not_eligible yet "
                f"carries a lookback volume ({volume!r})",
            )
        recomputed = c.NO_IMPACT
    else:
        if volume is None:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C7",
                f"epoch {epoch}: {row['symbol']!r} is recorded eligible with no "
                "lookback volume, so it cannot be ranked against the pool",
            )
        value = _decimal(volume, label=f"epoch {epoch} {row['symbol']}")
        outranks = value > cut_volume or (
            value == cut_volume and str(row["symbol"]) < cut_symbol
        )
        recomputed = c.RUN_INVALID_INPUT_EVIDENCE if outranks else c.NO_IMPACT

    verdict = row["verdict"]
    if verdict not in c.GAP_EPOCH_VERDICTS:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: verdict {verdict!r} is not one of "
            f"{list(c.GAP_EPOCH_VERDICTS)}",
        )
    if verdict != recomputed:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: {row['symbol']!r} is recorded {verdict} but the "
            f"evidence supports {recomputed} (lookback volume {volume!r} "
            f"against the rank-{c.UNIVERSE_TOP_N} cut {cut_symbol} "
            f"{cut_volume}); the verdict is recomputed, not accepted",
        )
    if recomputed == c.RUN_INVALID_INPUT_EVIDENCE:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C7",
            f"epoch {epoch}: gap symbol {row['symbol']!r} outranks the "
            f"rank-{c.UNIVERSE_TOP_N} candidate {cut_symbol!r}, so the archive "
            "gap intersects this epoch's pool. The epoch is invalid; it is not "
            "repaired by promoting the next-ranked symbol",
        )


# --- A2-C9 (OD-31): ranking-input deficit epochs ---------------------------


def _validate_ranking_input_deficit_section(
    manifest: Mapping[str, Any],
) -> tuple[int, ...]:
    """Check the manifest's A2-C9 declaration against the frozen enumeration.

    The enumeration is *restated* here and compared, not referenced: the frozen
    list was fixed by a measurement that has already been read, so the only
    thing standing between it and a quiet edit is that both the source digest
    and the epochs themselves have to match ``contract`` exactly.
    """
    section = manifest.get("ranking_input_deficit")
    if not isinstance(section, Mapping):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "manifest does not declare ranking_input_deficit; the enumerated "
            "epochs whose top-3 a ranking-input deficit changed are an input to "
            "this corpus and are recorded, not assumed empty",
        )
        return ()

    missing = [
        k for k in MANIFEST_RANKING_INPUT_DEFICIT_REQUIRED_KEYS if k not in section
    ]
    if missing:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"ranking_input_deficit is missing {missing}",
        )
    unknown = [
        k for k in section if k not in MANIFEST_RANKING_INPUT_DEFICIT_REQUIRED_KEYS
    ]
    if unknown:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"ranking_input_deficit carries unknown key(s): {sorted(unknown)}",
        )

    if section["enumeration_path"] != c.RANKING_INPUT_DEFICIT_ENUMERATION_PATH:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "ranking_input_deficit.enumeration_path must be "
            f"{c.RANKING_INPUT_DEFICIT_ENUMERATION_PATH!r}, got "
            f"{section['enumeration_path']!r}",
        )
    if section["enumeration_sha256"] != c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "the enumeration list changed: declared enumeration_sha256 "
            f"{section['enumeration_sha256']!r} is not the frozen "
            f"{c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256!r}. The list was fixed "
            "by a measurement that has already been read, so a list that moves "
            "afterwards is a pre-registration that was rewritten, not a "
            "correction",
        )
    if section["scan_path"] != c.RANKING_INPUT_DEFICIT_SCAN_PATH:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "ranking_input_deficit.scan_path must be "
            f"{c.RANKING_INPUT_DEFICIT_SCAN_PATH!r}, got {section['scan_path']!r}",
        )
    if section["scan_sha256"] != c.RANKING_INPUT_DEFICIT_SCAN_SHA256:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "the gap scan the enumeration was measured over changed: declared "
            f"scan_sha256 {section['scan_sha256']!r} is not the frozen "
            f"{c.RANKING_INPUT_DEFICIT_SCAN_SHA256!r}. The epoch list can be "
            "restated unchanged while the scan beneath it moves, and then "
            "'every affected epoch' is a claim about a scope that no longer "
            "exists — so the scope is pinned too, not just the list",
        )
    if section["scan_record_count"] != c.RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            "ranking_input_deficit.scan_record_count must be "
            f"{c.RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT}, got "
            f"{section['scan_record_count']!r}",
        )
    if section["verdict"] != c.RANKING_INPUT_DEFICIT_VERDICT:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"ranking_input_deficit.verdict must be "
            f"{c.RANKING_INPUT_DEFICIT_VERDICT!r}, got {section['verdict']!r}; a "
            "deficit epoch carries the same terminal code as the other "
            "direction, never a softer one",
        )

    epochs = tuple(section["epochs"])
    if epochs != c.RANKING_INPUT_DEFICIT_EPOCHS:
        extra = sorted(set(epochs) - set(c.RANKING_INPUT_DEFICIT_EPOCHS))
        absent = sorted(set(c.RANKING_INPUT_DEFICIT_EPOCHS) - set(epochs))
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"declared deficit epochs are not the frozen enumeration of "
            f"{len(c.RANKING_INPUT_DEFICIT_EPOCHS)} (extra={extra}, "
            f"absent={absent}); the enumeration is fixed, not recomputed",
        )

    rows = list(section["rows"])
    if len(rows) != len(c.RANKING_INPUT_DEFICIT_ROWS):
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"ranking_input_deficit.rows carries {len(rows)} row(s), the frozen "
            f"enumeration has {len(c.RANKING_INPUT_DEFICIT_ROWS)}",
        )
    for index, (row, frozen) in enumerate(
        zip(rows, c.RANKING_INPUT_DEFICIT_ROWS, strict=True)
    ):
        label = f"ranking_input_deficit.rows[{index}]"
        absent_keys = [
            k for k in RANKING_INPUT_DEFICIT_ROW_REQUIRED_KEYS if k not in row
        ]
        if absent_keys:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE, "A2-C9", f"{label}: missing {absent_keys}"
            )
        declared = (
            row["epoch_open_time"],
            row["as_archived_rank3"],
            row["would_have_been_rank3"],
        )
        if declared != frozen:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C9",
                f"{label}: declared {declared} is not the frozen enumeration row "
                f"{frozen}",
            )
    return epochs


def validate_ranking_input_deficit(
    manifest: Mapping[str, Any],
    pit_universe: pa.Table,
    outcomes: pa.Table | None = None,
    decisions: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Enforce A2-C9 on the frozen artifacts.

    Three things have to hold at every enumerated epoch, and each is one of the
    ways the clause could be evaded:

    1. The epoch is still **there**.  Deleting it is how a deficit disappears
       without leaving a trace, so an absent pool is a violation rather than a
       tidy corpus.
    2. The pool is the one the archive's own (deficient) input ranks.  Writing
       ``would_have_been_rank3`` into it is the silent re-ranking §31차 names —
       and it is the *more* tempting direction here, because the corrected
       ranking looks more accurate.  It is still a ranking nobody can reproduce
       from the recorded input.
    3. The epoch is not **scored**.  ``RUN_INVALID_INPUT_EVIDENCE`` is a
       terminal code, so an enumerated epoch that carries a decision or an
       outcome row was processed as if it were clean.

    Nothing here returns a pool or names a replacement, for the same reason
    :func:`validate_premium_index_gap` does not.

    Raises:
        ContractViolation: Always with ``RUN_INVALID_INPUT_EVIDENCE``.
    """
    epochs = _validate_ranking_input_deficit_section(manifest)
    if not epochs:
        return

    validate_table("pit_universe", pit_universe)

    pools: dict[int, list[dict[str, Any]]] = {}
    for row in pit_universe.to_pylist():
        pools.setdefault(row["epoch_open_time"], []).append(row)

    frozen_by_epoch = {row[0]: row for row in c.RANKING_INPUT_DEFICIT_ROWS}
    head = c.RANKING_INPUT_DEFICIT_UNCHANGED_HEAD

    #: Presence is required *within the table's own covered range* only.  An
    #: enumerated epoch outside ``[min, max]`` was never in this table's scope
    #: and nothing is claimed about it; one inside the range and missing is a
    #: hole, which is exactly how a deficit epoch would be made to disappear.
    #: Interior deletion cannot shrink the range, so the bound cannot be gamed
    #: by deleting the rows the check is looking for.
    covered_from, covered_to = (min(pools), max(pools)) if pools else (1, 0)

    for epoch in epochs:
        _, as_archived_rank3, would_have_been_rank3 = frozen_by_epoch[epoch]
        rows = pools.get(epoch)
        if not rows:
            if covered_from <= epoch <= covered_to:
                _fail(
                    RUN_INVALID_INPUT_EVIDENCE,
                    "A2-C9",
                    f"epoch {epoch} is enumerated as a ranking-input deficit and "
                    f"falls inside the pit_universe range "
                    f"[{covered_from}, {covered_to}] but has no pool; the epoch "
                    "is recorded with its reason, not removed",
                )
            continue
        declared = tuple(row["symbol"] for row in sorted(rows, key=lambda r: r["rank"]))
        if would_have_been_rank3 in declared:
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C9",
                f"epoch {epoch}: {would_have_been_rank3!r} is in the declared "
                f"pool {list(declared)}, which is the ranking complete input "
                "would have produced, not the one the recorded input produces "
                f"({[*head, as_archived_rank3]}). The epoch is invalid; it "
                "is not repaired by ranking it as if the deficit were not there",
            )
        if declared != (*head, as_archived_rank3):
            _fail(
                RUN_INVALID_INPUT_EVIDENCE,
                "A2-C9",
                f"epoch {epoch}: declared pool {list(declared)} is not the "
                f"as-archived ranking {[*head, as_archived_rank3]} the "
                "enumeration recorded for it",
            )

    scored: set[int] = {d["signal_epoch_open_time"] for d in decisions}
    if outcomes is not None:
        scored |= {row["signal_epoch_open_time"] for row in outcomes.to_pylist()}
    processed = sorted(scored & set(epochs))
    if processed:
        _fail(
            RUN_INVALID_INPUT_EVIDENCE,
            "A2-C9",
            f"{len(processed)} enumerated deficit epoch(s) carry a decision or "
            f"an outcome row (first={processed[0]}); "
            f"{c.RANKING_INPUT_DEFICIT_VERDICT} is terminal, so a deficit epoch "
            "is not scored as if its ranking input were complete",
        )


def validate_input_evidence(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Run the A2-C6 / A2-C7 / A2-C9 manifest half; return the in-window subset."""
    _validate_lifecycle_eligibility(manifest)
    in_window = _validate_gap_section(manifest)
    _validate_ranking_input_deficit_section(manifest)
    return in_window


def validate_corpus(
    manifest: Mapping[str, Any],
    *,
    pit_universe: pa.Table,
    gap_audit: pa.Table,
    klines: pa.Table,
    premium_index: pa.Table,
    outcomes: pa.Table,
    decisions: Sequence[Mapping[str, Any]],
) -> None:
    """Validate a corpus in ``TERMINAL_CODE_PRIORITY`` order.

    Input evidence is adjudicated first and unconditionally.  A corpus built on
    material the contract refuses is the wrong artifact, so reporting some
    downstream code for it would name a symptom and hide the cause — and it
    would let a run whose ranking input is inadmissible be written up as an
    outcome-level problem instead.

    Every canonical table is checked here, so that this function is the whole
    entry point: a table left out would be a schema nobody validates.

    Raises:
        ContractViolation: The highest-priority code the artifacts violate.
    """
    in_window = validate_input_evidence(manifest)
    validate_premium_index_gap(gap_audit, pit_universe, in_window)
    validate_ranking_input_deficit(manifest, pit_universe, outcomes, decisions)
    validate_manifest(manifest)  # re-runs the pure input-evidence half; idempotent
    validate_table("klines_4h", klines)
    validate_table("premium_index_4h", premium_index)
    validate_outcomes(outcomes, klines, decisions)


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


# --- A2-C8 (OD-26 Job B): pre-registered sample readiness protocol --------


def validate_sample_readiness(report: Mapping[str, Any]) -> None:
    """Validate a Job-B sample-readiness report against the frozen A2-C8 rule.

    The sample is not merely schema-checked: ``sample_rule`` and ``quarters``
    are recomputed from :mod:`contract` and compared for equality, so a report
    cannot claim a different rule or a hand-picked epoch set and still pass.
    The verdict is likewise recomputed from the row-level completeness flags
    and the reported ``run_invalid_epochs``, and it must be one of the two
    closed outcomes — ``UNDETERMINED`` is not a re-judgeable third option here.

    Raises:
        ContractViolation: With ``RUN_INVALID_MANIFEST_SCHEMA`` for structural
            problems or a verdict outside the closed domain, and
            ``RUN_INVALID_CORPUS_LITERALS`` when the sample rule, the drawn
            epochs, or the recomputed verdict do not match what the frozen
            protocol requires.
    """
    missing = [k for k in SAMPLE_REPORT_REQUIRED_KEYS if k not in report]
    if missing:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C8",
            f"sample readiness report is missing required key(s): {missing}",
        )
    unknown = [k for k in report if k not in SAMPLE_REPORT_REQUIRED_KEYS]
    if unknown:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C8",
            f"sample readiness report carries unknown key(s): {sorted(unknown)}",
        )

    if report["contract_id"] != c.CONTRACT_ID:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C8",
            f"contract_id must be {c.CONTRACT_ID!r}, got {report['contract_id']!r}",
        )

    declared_rule = dict(report["sample_rule"])
    frozen_rule = dict(c.SAMPLE_RULE)
    if declared_rule != frozen_rule:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C8",
            f"sample_rule must be exactly {frozen_rule}, got {declared_rule}; "
            "the sample rule is pre-registered and may not be tuned after "
            "measurement results are read",
        )

    expected_plan = {k: list(v) for k, v in c.sample_plan().items()}
    declared_plan = {k: list(v) for k, v in report["quarters"].items()}
    if declared_plan != expected_plan:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C8",
            "declared sample epochs do not reproduce "
            "contract.sample_plan(); the extraction algorithm is a pure "
            "function of the frozen seed and quarter boundaries, so any "
            "mismatch means the sample was drawn, then edited, or drawn "
            "differently from the registered algorithm",
        )

    known_epochs: set[int] = set()
    for epochs in expected_plan.values():
        known_epochs.update(epochs)

    rows = report["rows"]
    if not rows:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C8",
            "sample readiness report carries no rows",
        )

    seen_keys: set[tuple[int, int]] = set()
    all_complete = True
    for index, row in enumerate(rows):
        label = f"rows[{index}]"
        row_missing = [k for k in SAMPLE_ROW_REQUIRED_KEYS if k not in row]
        if row_missing:
            _fail(
                RUN_INVALID_MANIFEST_SCHEMA,
                "A2-C8",
                f"{label}: missing key(s) {row_missing}",
            )
        epoch = row["epoch_open_time"]
        if epoch not in known_epochs:
            _fail(
                RUN_INVALID_CORPUS_LITERALS,
                "A2-C8",
                f"{label}: epoch_open_time {epoch} is not part of the "
                "pre-registered sample; rows may only cover drawn epochs",
            )
        rank = row["rank"]
        if rank not in (1, 2, 3):
            _fail(
                RUN_INVALID_MANIFEST_SCHEMA,
                "A2-C8",
                f"{label}: rank must be 1, 2 or 3 (top-{c.UNIVERSE_TOP_N}), "
                f"got {rank!r}",
            )
        key = (epoch, rank)
        if key in seen_keys:
            _fail(
                RUN_INVALID_MANIFEST_SCHEMA,
                "A2-C8",
                f"{label}: duplicate row for epoch {epoch} rank {rank}",
            )
        seen_keys.add(key)
        if not row["provenance_sha256"]:
            _fail(
                RUN_INVALID_AUTHENTICITY_EVIDENCE,
                "A2-C5",
                f"{label}: provenance_sha256 is empty; every completeness "
                "check must be traceable to the object it was decided from",
            )
        if not (row["kline_complete"] and row["premium_index_complete"]):
            all_complete = False

    invalid_epochs = tuple(report["run_invalid_epochs"])
    for epoch in invalid_epochs:
        if epoch not in known_epochs:
            _fail(
                RUN_INVALID_CORPUS_LITERALS,
                "A2-C8",
                f"run_invalid_epochs carries {epoch}, which is not part of "
                "the pre-registered sample",
            )

    recomputed_verdict = (
        c.READY if (all_complete and not invalid_epochs) else c.NOT_READY
    )

    verdict = report["verdict"]
    if verdict not in c.SAMPLE_VERDICTS:
        _fail(
            RUN_INVALID_MANIFEST_SCHEMA,
            "A2-C8",
            f"verdict must be one of {list(c.SAMPLE_VERDICTS)}, got "
            f"{verdict!r}; §26차 closed this to a two-way choice — "
            "UNDETERMINED is not a re-judgeable outcome of this protocol",
        )
    if verdict != recomputed_verdict:
        _fail(
            RUN_INVALID_CORPUS_LITERALS,
            "A2-C8",
            f"declared verdict {verdict!r} does not match the recomputed "
            f"verdict {recomputed_verdict!r} (all_rows_complete="
            f"{all_complete}, run_invalid_epochs={list(invalid_epochs)}); "
            "the verdict is derived from the row evidence, never accepted "
            "as declared",
        )
