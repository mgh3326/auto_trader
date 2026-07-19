#!/usr/bin/env python3
"""ROB-944 (H4, ROB-940) — walk-forward campaign CLI: --plan / --run boundary.

``--plan`` is PURE: no network, DB connection/query/write, file-artifact
write, environment mutation, or child run. It validates the full frozen
ROB-940 campaign envelope and emits stable, machine-readable JSON --
byte-identical across repeated invocations and across ``PYTHONHASHSEED``.
The 24 canonical experiment IDs are derived via the PURE
``research_contracts.canonical_hash`` authority (the same algorithm H6's
registry uses) -- ``--plan`` never imports ``app.*``. The plan also emits
``expected_campaign_run_id``: the ONE deterministic ``campaign_run_id`` a
``--run`` invocation for this exact frozen campaign must supply (see
``_derive_primary_campaign_run_id`` -- never a UUID/wall-clock value).

``--run`` is the empirical execution path, gated IN THIS ORDER, fail-closed
on the first unmet condition, before any later step:

  1. a FRESH recomputation of the full-campaign hash + 24 identities,
     compared against an operator-pinned ``--expected-full-campaign-hash``
     (never two values both freshly derived from the same call -- that
     would be vacuous), AND a check that ``--campaign-run-id`` exactly
     equals the value canonically DERIVED from that hash -- an arbitrary
     caller-supplied UUID/timestamp is rejected, not merely accepted as-is;
  2. the H6 registration bridge (``app.services.rob944_campaign_controller``)
     is importable;
  3. an explicit, default-off write opt-in
     (``ROB944_RESEARCH_WRITE_OPT_IN=true``);
  4. an authorized non-production research DB target
     (``app.services.research_db_write_guard``'s policy, positively matched
     against the actual resolved session bind);
  5. all 24 experiment registrations, with the resulting mapping PREDECLARED
     before any child execution;
  6. ONLY THEN: corpus loading (network-0 offline loader) + per-strategy
     walk-forward execution + recording all 24 deterministic
     ``retry_index=0`` attempts (each ``completed``/``rejected``/``crashed``/
     ``timeout``, with 3 required scenario artifact hashes/counts) +
     ``campaign_completeness_report``.

Accounting completeness (H6's verdict) is NOT the same as empirical success:
a campaign can be fully, correctly RECORDED (every attempt accounted for)
while every attempt's own status is ``crashed``/``rejected``/``timeout``.
``main`` distinguishes the two -- exit 0 requires BOTH accounting-complete
AND every primary attempt ``status="completed"``; anything else exits
nonzero, even though the (legitimate, accurate) evidence was still
committed.

This worker (ROB-944 H4) NEVER invokes ``--run`` -- every empirical run is a
deliberate, later, operator action. This file is nonetheless the COMPLETE,
correct implementation (not a stub) so that a later operator invocation
needs no further wiring; it is unit-tested throughout with fixtures/local
disposable-DB only (see the ``tests/`` alongside this file and
``tests/services/research/test_rob944_campaign_controller.py``).

Security (captain corrections, 2026-07-17): no raw exception/log text ever
reaches a persisted ``reason_code`` OR any persisted artifact hash's INPUT
-- ``rob944_walkforward`` maps every crash/timeout/gap-rejection to a FIXED
code and hashes only stable identity + that code, never ``str(exc)``; this
module additionally re-validates every ``reason_code`` against the SAME
closed allowlist before building any ``AttemptEvidence`` (defense in depth
against a caller who bypasses ``summarize_config_attempts_for_h6``).
``campaign_run_id``/``run_identity`` are always deterministic canonical
hashes of lineage facts -- never a wall-clock timestamp or UUID.

``--help``/``--version`` are handled entirely by argparse before any of this
module's own gate logic runs -- they never touch DB/network/runtime
surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Captain config/plan audit (2026-07-18, item C): this module previously
# mutated process-global ``sys.path`` at IMPORT TIME (inserting its own
# directory + the repo root) so that a bare ``import rob944_frozen_campaign``
# and ``from app.services...`` would resolve. That mutation is redundant in
# BOTH real invocation modes and has been removed rather than restructured:
# (1) direct script execution (``python .../run_rob944_campaign.py``) --
# CPython itself already prepends the executed script's own directory to
# ``sys.path[0]`` before running it, so ``rob944_frozen_campaign`` (a
# sibling module) resolves with no help from this file; (2) test/module
# import (``import run_rob944_campaign``) -- this package's ``conftest.py``
# already inserts both paths before any test module is collected. ``app.*``
# resolves in both modes purely from the project's own editable install
# (verified: ``uv run python3 -c "import app"`` succeeds regardless of cwd,
# with no path manipulation anywhere). Removing this satisfies the pure
# ``--plan`` command contract (no process-global state mutation as a
# side effect of merely importing this module) without weakening direct
# CLI usability -- both invocation paths were empirically re-verified
# after removal (this file's own test suite, and a bare
# ``uv run python3 research/nautilus_scalping/run_rob944_campaign.py --plan``).
from rob944_frozen_campaign import (
    H1_MANIFEST_PATH,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_frozen_campaign_envelope,
)

__version__ = "rob944-campaign-cli.v1"

_ENV_WRITE_OPT_IN = "ROB944_RESEARCH_WRITE_OPT_IN"
_ENV_ARTIFACT_ROOT = "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"


class RunPreflightError(RuntimeError):
    """A ``--run`` fail-closed gate was not satisfied."""


def _derive_experiment_ids(rows: list[dict]) -> list[str]:
    """The 24 canonical experiment IDs, derived via the PURE
    ``research_contracts.canonical_hash`` authority (identical algorithm to
    H6's registry) -- no ``app.*`` import, no DB, no network. ``rows`` MUST
    be plain dicts (e.g. from ``FrozenCampaignEnvelope.to_dict()["rows"]``,
    never the envelope's own sealed ``MappingProxyType`` fields directly --
    ``encode_canonical`` does not accept ``mappingproxy``).
    """
    from research_contracts.canonical_hash import (
        compute_identity_hashes,
        derive_experiment_id,
    )

    ids = []
    for row in rows:
        components = row["components"]
        hashes = compute_identity_hashes(components)
        ids.append(
            derive_experiment_id(row["strategy_key"], row["strategy_version"], hashes)
        )
    return ids


def _derive_primary_campaign_run_id(full_campaign_hash: str) -> str:
    """The ONE deterministic ``campaign_run_id`` for the primary run of a
    given frozen campaign -- a canonical hash of the campaign's own identity
    and a fixed run-kind label, never a UUID/wall-clock value. A ``--run``
    invocation MUST supply exactly this value; any other string (a UUID, a
    timestamp, an operator typo) is rejected.

    Fable Q1 FINAL (orch-fable-answer-rob944b-20260717.md, 2026-07-17):
    derivation payload is the canonical ``{full_campaign_hash, kind:
    "primary_run"}`` dict -> SHA-256 -> the full 32-byte digest re-encoded
    as UNPADDED URL-safe base64 (43 chars) -- NOT the 64-hex string, and NOT
    a truncation of it. Full 256-bit entropy is preserved (a re-encoding,
    not a shortening) while fitting H6's existing
    ``trial_idempotency_key VARCHAR(128)`` composite
    (``campaign_run_id:experiment_id:retry_index``) without any H6
    schema/source mutation: ``"rob944-primary-"`` (15) + 43 = 58-char
    campaign_run_id; 58 + 1 + 64 (experiment_id, unchanged full-hex) + 1 +
    1 ("0") = 125 <= 128. ``app.services.rob944_campaign_controller``
    duplicates this EXACT derivation (see its own
    ``_derive_expected_campaign_run_id``) as an independent defense-in-depth
    check at the actual DB persistence boundary -- both must always agree
    bit-for-bit.
    """
    import base64

    from research_contracts.canonical_hash import canonical_sha256

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    digest_bytes = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(digest_bytes).rstrip(b"=").decode("ascii")
    return f"rob944-primary-{suffix}"


def build_plan() -> dict:
    """Build the pure, stable --plan payload. Raises on any frozen-pin
    mismatch or malformed campaign shape -- never silently degrades.

    Uses ``envelope.to_dict()`` (a plain, JSON-serializable structure)
    throughout, never the envelope's own sealed ``MappingProxyType``
    fields directly -- those exist to make the envelope immutable, not to
    be handed to ``json.dumps``/``encode_canonical`` (neither accepts
    ``mappingproxy``).
    """
    envelope = build_production_frozen_campaign_envelope()
    plain = envelope.to_dict()
    # Q4 addendum (captain, 2026-07-17): the envelope itself now carries the
    # ordered 24 experiment IDs as an explicit, hashed component (step 3 of
    # the acyclic chain) -- ``plain["experiment_ids"]`` IS the authoritative
    # value the just-computed full_campaign_hash below commits to. A fresh,
    # independent recomputation off the same rows must agree exactly; any
    # divergence would mean the envelope's own internal derivation and this
    # CLI's derivation authority have drifted apart.
    experiment_ids = plain["experiment_ids"]
    if len(experiment_ids) != 24 or len(set(experiment_ids)) != 24:
        raise RuntimeError(
            f"expected exactly 24 unique experiment IDs, got {len(experiment_ids)} "
            f"total ({len(set(experiment_ids))} unique)"
        )
    if list(experiment_ids) != _derive_experiment_ids(plain["rows"]):
        raise RuntimeError(
            "envelope-embedded experiment_ids diverged from an independent "
            "recomputation off the same rows -- refusing to proceed"
        )
    full_campaign_hash = envelope.full_campaign_hash()
    # Captain config/plan audit (2026-07-18, item A): --plan must emit a
    # LOSSLESS, machine-resolvable payload an external auditor can feed
    # straight back into canonical_sha256 to independently reproduce
    # full_campaign_hash -- never merely assert an opaque digest. ``plain``
    # (envelope.to_dict()) IS the exact structure full_campaign_hash() was
    # computed from; self-verify that here (cheap, and catches any future
    # drift between to_dict()'s shape and full_campaign_hash()'s own
    # internal computation) before ever emitting it as authoritative.
    from research_contracts.canonical_hash import canonical_sha256

    if canonical_sha256(plain) != full_campaign_hash:
        raise RuntimeError(
            "full_campaign_payload does not independently reproduce "
            "full_campaign_hash -- refusing to emit an unauditable plan"
        )
    # Captain config/plan audit (item A): explicit, top-level, actual
    # byte-derived S1/S2 source SHA-256s -- extracted from the SAME rows
    # already bound into full_campaign_hash (components["code"] carries
    # each row's StrategySourceProvenance.verified_source_sha256()), never
    # re-read/re-derived independently (which could silently diverge from
    # what was actually hashed).
    s1_source_sha256 = next(
        row["components"]["code"]["source_sha256"]
        for row in plain["rows"]
        if row["strategy_key"] == PRODUCTION_S1_STRATEGY_KEY
    )
    s2_source_sha256 = next(
        row["components"]["code"]["source_sha256"]
        for row in plain["rows"]
        if row["strategy_key"] == PRODUCTION_S2_STRATEGY_KEY
    )
    cost = plain["rows"][0]["components"]["cost"]
    return {
        "schema_version": plain["schema_version"],
        "full_campaign_hash": full_campaign_hash,
        # Captain config/plan audit (item A): the exact, complete payload
        # full_campaign_hash was computed from -- canonical_sha256(this
        # field) MUST equal full_campaign_hash above; any external auditor
        # can verify this without trusting the CLI's own self-check.
        "full_campaign_payload": plain,
        "s1_source_sha256": s1_source_sha256,
        "s2_source_sha256": s2_source_sha256,
        "expected_campaign_run_id": _derive_primary_campaign_run_id(full_campaign_hash),
        "window_start_iso": plain["window_start_iso"],
        "window_end_iso": plain["window_end_iso"],
        "universe": plain["universe"],
        "dataset_manifest_hash": plain["dataset_manifest_hash"],
        "signal_manifest_hash": plain["signal_manifest_hash"],
        "fold_schedule": plain["fold_schedule"],
        "fold_count": len(plain["fold_schedule"]),
        "scenario_execution": plain["scenario_execution"],
        "cost_scenarios": cost["scenarios"],
        "primary_scenario": cost["primary_scenario"],
        "min_tp_distance_bps": cost["min_tp_distance_bps"],
        "funding_pit_policy": plain["funding_pit_policy"],
        "data_gap_policy": plain["data_gap_policy"],
        "posture": plain["posture"],
        "execution_code_provenance": plain["execution_code_provenance"],
        "h3_fixed_constants": plain["h3_fixed_constants"],
        "h4_reason_contract": plain["h4_reason_contract"],
        "experiment_ids": experiment_ids,
        "expected_logical_attempts": len(experiment_ids),
        # Fable report/register closure (2026-07-17): H5 is out of scope --
        # H4 only hands off a READABLE contract. Top-level (not nested
        # inside h3_fixed_constants) for operator discoverability; the
        # verbatim Korean S2 spec-deviation sentence (Fable condition 2,
        # do not re-word) must survive unchanged into the eventual freeze
        # echo/completion report/H5 handoff.
        "spec_deviations": plain["h3_fixed_constants"]["s2_spec_deviations"],
        "campaign_run_id_derivation": {
            "payload": {
                "full_campaign_hash": "<the full_campaign_hash above>",
                "kind": "primary_run",
            },
            "recipe": "SHA-256 -> full 32 raw bytes -> unpadded URL-safe base64 (43 chars)",
            "note": "NOT the 64-hex digest string, and NOT a truncation of it -- full 256-bit entropy preserved",
            "prefix": "rob944-primary-",
            "campaign_run_id_length": 58,
            "primary_idempotency_key_length": 125,
            "idempotency_key_max": 128,
        },
    }


def _import_campaign_controller():
    """Isolated so tests can force an ImportError without faking sys.modules."""
    import app.services.rob944_campaign_controller as controller

    return controller


# ---------------------------------------------------------------------------
# --run: lineage-bound evidence construction (pure conversion logic, unit-
# testable with fake summaries -- no corpus loading required for THIS part).
# ---------------------------------------------------------------------------


def _attempt_allowed_reasons_by_status() -> dict[str, frozenset]:
    """Captain trust-boundary addendum (2026-07-17): a bare membership
    check against the GLOBAL ``KNOWN_REASON_CODES`` allowlist lets a
    cross-status pair through (e.g. ``status="completed"`` with
    ``reason_code="child_execution_crashed"``) -- H6's own DTO has no
    status/reason-pair validation of its own and would silently accept it,
    so this exact closed status-scoped mapping is the only place this gets
    caught. ATTEMPT-level statuses are exactly ``completed``/``rejected``/
    ``crashed``/``timeout`` (``AttemptStatus`` -- never ``never_selected``,
    which is scenario-only)."""
    from rob944_walkforward import (
        REASON_CHILD_EXECUTION_CRASHED,
        REASON_CHILD_EXECUTION_TIMEOUT,
        REASON_DATA_GAP_IN_POSITION,
        REASON_GLOBAL_CORPUS_LOAD_FAILED,
        REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    )

    return {
        "completed": frozenset(),  # must be None -- checked separately
        "rejected": frozenset(
            {REASON_DATA_GAP_IN_POSITION, REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS}
        ),
        "crashed": frozenset(
            {REASON_CHILD_EXECUTION_CRASHED, REASON_GLOBAL_CORPUS_LOAD_FAILED}
        ),
        "timeout": frozenset({REASON_CHILD_EXECUTION_TIMEOUT}),
    }


def _scenario_allowed_reasons_by_status() -> dict[str, frozenset]:
    """SCENARIO-level statuses differ from ATTEMPT-level in two ways
    (captain precision correction, 2026-07-17): (1) they additionally
    include ``never_selected`` (a config that never won a single fold),
    whose reason is always the fixed ``REASON_NEVER_SELECTED_IN_ANY_FOLD``
    sentinel; (2) ``rejected`` is narrower -- a SCENARIO can only ever be
    gap-rejected (``REASON_DATA_GAP_IN_POSITION``); ``insufficient_train_evidence_all_folds``
    is an ATTEMPT-only reason (the whole config was never train-eligible in
    any fold) and must never appear on a per-scenario row. ``crashed`` stays
    the SAME two-code set as attempt-level -- the deterministic 24-row
    global-corpus-load-failure fallback intentionally emits 3 crashed
    scenario sentinels with ``REASON_GLOBAL_CORPUS_LOAD_FAILED`` per config,
    and that must remain valid."""
    from rob944_walkforward import (
        REASON_DATA_GAP_IN_POSITION,
        REASON_NEVER_SELECTED_IN_ANY_FOLD,
    )

    allowed = dict(_attempt_allowed_reasons_by_status())
    allowed["rejected"] = frozenset({REASON_DATA_GAP_IN_POSITION})
    allowed["never_selected"] = frozenset({REASON_NEVER_SELECTED_IN_ANY_FOLD})
    return allowed


def _assert_status_reason_contract(
    status: str, reason_code: str | None, *, allowed_by_status, context: str
) -> None:
    """Fail closed unless ``reason_code`` is EXACTLY the set permitted for
    ``status`` under the closed mapping -- ``completed`` requires ``None``;
    every other status requires ``reason_code`` to be one of its own
    specific allowed codes, never merely "some known code from anywhere".
    Never echoes the offending status/reason_code (could be a forged/
    secret-bearing value)."""
    allowed = allowed_by_status.get(status)
    if allowed is None:
        raise ValueError(f"refusing to persist an unknown status for {context}")
    if status == "completed":
        if reason_code is not None:
            raise ValueError(
                f"attempt/scenario for {context} has status='completed' but a non-null "
                "reason_code -- refusing to persist"
            )
        return
    if reason_code not in allowed:
        raise ValueError(
            f"attempt/scenario for {context} has a reason_code not permitted for its status "
            "under the closed status-scoped allowlist -- refusing to persist"
        )


_KNOWN_SCENARIO_NAMES = frozenset({"base", "primary_stress", "upward_stress"})
_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _assert_hex64(value, *, context: str) -> None:
    # type(...) is not str (never isinstance) -- captain normalization
    # correction (2026-07-17): consistent exact-type discipline with every
    # other persisted/hashed identifier field, even though a regex match
    # here already operates on the real underlying character buffer.
    if type(value) is not str or not _HEX64_RE.match(value):
        raise ValueError(
            f"{context} has a malformed hash -- must be a lowercase 64-hex digest"
        )


_CANONICAL_FOLD_IDS = frozenset(f"fold-{i:02d}" for i in range(8))
_CANONICAL_CONFIG_IDS_BY_STRATEGY = {
    "S1": frozenset(f"S1-{i:02d}" for i in range(12)),
    "S2": frozenset(f"S2-{i:02d}" for i in range(12)),
}


def _assert_valid_attempt_identity(
    strategy: str, config_id: str, *, context: str
) -> None:
    """Captain live-validation correction (2026-07-17): ``summary.strategy``
    must be exactly ``"S1"``/``"S2"``, and ``summary.config_id`` must be an
    EXACT member of that strategy's own frozen 12-config set (``S{1,2}-00``
    through ``-11``) -- not merely regex-shaped (which would let ``S1-99``
    or ``S3-00`` through)."""
    if strategy not in _CANONICAL_CONFIG_IDS_BY_STRATEGY:
        raise ValueError(
            f"{context} has a strategy outside the closed {{S1, S2}} set -- refusing to persist"
        )
    if config_id not in _CANONICAL_CONFIG_IDS_BY_STRATEGY[strategy]:
        raise ValueError(
            f"{context} has a config_id outside its strategy's exact frozen 12-config set -- "
            "refusing to persist"
        )


def _assert_valid_fold_selection_row(row, *, strategy: str, context: str) -> None:
    """Captain final-audit item A (2026-07-17) + follow-up: ``fold_selection_trace``
    rows were previously hashed with NO validation of ``train_input_hash``
    (format), ``rejection_reason``/``excluded_symbols`` reasons (closed
    set), their consistency with ``rejected``, ``fold_id``/``fold_selected_config_id``
    (closed/canonical formats), or ``eligible_symbols``/``excluded_symbols``
    (must be unique, subsets of the frozen 4-symbol universe, disjoint from
    each other, and together cover it EXACTLY) -- all untrusted input from a
    caller-injected callback. Never echoes any offending value itself."""
    from rob941_frozen_scope import UNIVERSE
    from rob944_selection import (
        INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
    )

    if row.fold_id not in _CANONICAL_FOLD_IDS:
        raise ValueError(
            f"{context} has a fold_id outside the closed canonical set -- refusing to persist"
        )
    if row.fold_selected_config_id is not None:
        # Exact membership in the SAME strategy's frozen 12-config set --
        # not merely regex-shaped (would let S1-99/S3-00 through).
        if row.fold_selected_config_id not in _CANONICAL_CONFIG_IDS_BY_STRATEGY.get(
            strategy, frozenset()
        ):
            raise ValueError(
                f"{context} has a fold_selected_config_id outside this attempt's exact "
                "frozen 12-config set -- refusing to persist"
            )
    _assert_hex64(row.train_input_hash, context=f"{context} train_input_hash")
    # Captain final-audit semantic correction (2026-07-17): a pure auditor
    # probe fed rejected="SECRET-CONTROL\n" (a truthy non-bool) straight
    # into the branch below -- FoldSelectionEvidenceSummary's type hint
    # (bool) is NOT runtime-enforced (a plain dataclass, no validator), so
    # this is a genuine runtime trust boundary. Require EXACT bool
    # (``type(...) is bool``) BEFORE any branch/hash/output touches this
    # field -- expressing the declared bool-only contract directly, the
    # same exact-type discipline this validator already applies elsewhere
    # (e.g. trade_count). Never interpolate the offending value itself.
    if type(row.rejected) is not bool:
        raise ValueError(f"{context} has a non-bool rejected -- refusing to persist")
    if row.rejected:
        if row.rejection_reason != INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON:
            raise ValueError(
                f"{context} is rejected but its rejection_reason is not the expected "
                "closed sentinel -- refusing to persist"
            )
    elif row.rejection_reason is not None:
        raise ValueError(
            f"{context} is not rejected but carries a non-null rejection_reason -- "
            "refusing to persist"
        )
    # Captain adjacent trust-boundary correction (2026-07-17, item B): type
    # hints are not runtime validators -- pure probes fed
    # equal_weight_expectancy_bps=True, pooled_expectancy_bps=Decimal(...),
    # profit_factor=None straight through. Decimal in particular can
    # survive canonical_sha256 but then fail json.dumps AFTER a DB commit
    # already happened -- must be caught here, before hashing. Exact
    # ``type(...) is float`` (never isinstance, which would accept the
    # bool subtype) rejects bool/int/Decimal/None uniformly; NaN/+Inf/-Inf
    # remain valid float values here (accepted) since
    # _json_safe_float_or_sentinel's sentinel normalization is the
    # contractual way those are represented downstream, not a reason to
    # reject them at this layer.
    if row.rejected:
        if row.equal_weight_expectancy_bps is not None:
            raise ValueError(
                f"{context} is rejected but equal_weight_expectancy_bps is not None -- "
                "refusing to persist"
            )
        if row.pooled_expectancy_bps is not None:
            raise ValueError(
                f"{context} is rejected but pooled_expectancy_bps is not None -- "
                "refusing to persist"
            )
    else:
        if type(row.equal_weight_expectancy_bps) is not float:
            raise ValueError(
                f"{context} equal_weight_expectancy_bps must be an exact float for a "
                "non-rejected row -- refusing to persist"
            )
        if type(row.pooled_expectancy_bps) is not float:
            raise ValueError(
                f"{context} pooled_expectancy_bps must be an exact float for a "
                "non-rejected row -- refusing to persist"
            )
    if type(row.profit_factor) is not float:
        raise ValueError(
            f"{context} profit_factor must be an exact float (finite/nan/+inf/-inf all "
            "permitted; sentinel normalization is contractual) -- refusing to persist"
        )
    # Captain adjacent trust-boundary correction (2026-07-17, item A):
    # eligible_symbols/excluded_symbols must be the EXACT container types
    # the dataclass/generator contract declares (tuple, and tuple-of-
    # 2-tuples) -- a set/list-valued probe would otherwise pass every
    # membership/uniqueness/coverage check below while its ORDER varies
    # across PYTHONHASHSEED once list(...)/iteration silently accepted it,
    # corrupting canonical_sha256/operator JSON determinism. Checked BEFORE
    # any list(...)/set(...) conversion or tuple-unpacking (a malformed
    # excluded_symbols entry would otherwise raise a raw, unsanitized
    # unpacking error here instead of this fixed message).
    if type(row.eligible_symbols) is not tuple:
        raise ValueError(
            f"{context} eligible_symbols must be an exact tuple -- refusing to persist"
        )
    if type(row.excluded_symbols) is not tuple:
        raise ValueError(
            f"{context} excluded_symbols must be an exact tuple -- refusing to persist"
        )
    for entry in row.excluded_symbols:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError(
                f"{context} has an excluded_symbols entry that is not an exact 2-tuple -- "
                "refusing to persist"
            )
    eligible_list = list(row.eligible_symbols)
    eligible_set = set(eligible_list)
    if len(eligible_set) != len(eligible_list):
        raise ValueError(
            f"{context} has a duplicate eligible_symbols entry -- refusing to persist"
        )
    excluded_list = [symbol for symbol, _reason in row.excluded_symbols]
    excluded_set = set(excluded_list)
    if len(excluded_set) != len(excluded_list):
        raise ValueError(
            f"{context} has a duplicate excluded_symbols entry -- refusing to persist"
        )
    for _symbol, reason in row.excluded_symbols:
        if reason != INSUFFICIENT_SYMBOL_EVIDENCE_REASON:
            raise ValueError(
                f"{context} has an excluded_symbols entry with a reason outside the "
                "closed allowlist -- refusing to persist"
            )
    universe_set = set(UNIVERSE)
    if not eligible_set.issubset(universe_set) or not excluded_set.issubset(
        universe_set
    ):
        raise ValueError(
            f"{context} has a symbol outside the frozen 4-symbol universe -- refusing to persist"
        )
    if eligible_set & excluded_set:
        raise ValueError(
            f"{context} has a symbol in BOTH eligible_symbols and excluded_symbols -- "
            "refusing to persist"
        )
    if eligible_set | excluded_set != universe_set:
        raise ValueError(
            f"{context} eligible_symbols/excluded_symbols do not exactly cover the frozen "
            "4-symbol universe -- refusing to persist"
        )
    # Item A (continued): membership/uniqueness/coverage alone do not prove
    # ORDER -- both partitions must preserve the FROZEN UNIVERSE's own
    # order (expected = UNIVERSE filtered by membership), checked only
    # AFTER exact-cover is already proven above. Never silently sort/
    # reorder attacker input to "fix" it -- a wrong order is refused, not
    # normalized.
    expected_eligible_order = tuple(
        symbol for symbol in UNIVERSE if symbol in eligible_set
    )
    if row.eligible_symbols != expected_eligible_order:
        raise ValueError(
            f"{context} eligible_symbols does not preserve the frozen universe order -- "
            "refusing to persist"
        )
    expected_excluded_symbol_order = tuple(
        symbol for symbol in UNIVERSE if symbol in excluded_set
    )
    actual_excluded_symbol_order = tuple(
        symbol for symbol, _reason in row.excluded_symbols
    )
    if actual_excluded_symbol_order != expected_excluded_symbol_order:
        raise ValueError(
            f"{context} excluded_symbols does not preserve the frozen universe order -- "
            "refusing to persist"
        )


def _assert_valid_fold_selection_trace(
    trace, *, strategy: str, status: str, reason_code: str | None, context: str
) -> None:
    """Captain precision addendum + live-validation correction (2026-07-17):
    every NON-global attempt must have EXACTLY the 8 canonical unique fold
    IDs. An EMPTY trace is exempted ONLY for the exact deterministic
    global-corpus-load-failure signature (``status="crashed"`` AND
    ``reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED``) -- any OTHER
    status/reason with an empty trace is refused fail-closed, never
    silently accepted just because the trace happens to be empty."""
    if not trace:
        from rob944_walkforward import REASON_GLOBAL_CORPUS_LOAD_FAILED

        if status == "crashed" and reason_code == REASON_GLOBAL_CORPUS_LOAD_FAILED:
            return
        raise ValueError(
            f"{context} has an empty fold_selection_trace but is not the exact "
            "global-corpus-load-failure fallback signature -- refusing to persist"
        )
    fold_ids = [row.fold_id for row in trace]
    if len(set(fold_ids)) != len(fold_ids) or set(fold_ids) != _CANONICAL_FOLD_IDS:
        raise ValueError(
            f"{context} fold_selection_trace does not have exactly the 8 canonical unique "
            "fold IDs -- refusing to persist"
        )
    for idx, row in enumerate(trace):
        _assert_valid_fold_selection_row(
            row, strategy=strategy, context=f"{context}/fold#{idx}"
        )


def _s2_rejections_to_no_trade_records(rejections):
    """Fable condition 1 (2026-07-17): convert H3 S2's own
    ``RejectedCandidate``s (target_direction_invalid/tp_above_max/
    tp_below_r_min_sl/tp_below_abs_floor/confirmation_failed/
    next_bar_unavailable) into canonical ``NoTradeRecord``s -- a pure,
    directly testable field-mapping, extracted from ``_s2_gen_factory`` so
    the conversion itself (not just the wiring) has an isolated test."""
    from rob940_engine import NoTradeRecord

    return tuple(
        NoTradeRecord(
            strategy=r.strategy,
            config_id=r.config_id,
            symbol=r.symbol,
            side=r.side,
            signal_ts=r.signal_ts,
            reason=r.reason,
            fold_id=r.fold_id,
        )
        for r in rejections
    )


def _known_no_trade_reasons() -> frozenset[str]:
    """Captain trust-boundary addendum (2026-07-17) + allowlist correction:
    the CLOSED set of no_trade_reason_counts KEYS this system can ever
    legitimately produce -- H2's own engine-level no-fill reasons
    (rob940_engine.py, bare string literals -- that module exports no named
    constants for them, so these are duplicated here deliberately, same
    pattern as ``app.services.rob944_campaign_controller._EXPECTED_SCENARIO_ORDER``),
    H4's funding-gate reasons (importable from ``rob944_gap_funding``), and
    H3 S2's own FULL six-code rejection set (rob940_signal_s2.py:142-150,
    213, 227 -- likewise bare literals, no exported named authority to
    import instead; this list must stay in sync with that source if it ever
    changes). A ``no_trade_reason_counts`` dict is untrusted input flowing
    from a caller-injected ``build_attempt_evidence``/generator callback --
    an arbitrary caller-injected key must never be hashed or printed.
    """
    from rob944_gap_funding import (
        REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
        REASON_FUNDING_EVIDENCE_UNAVAILABLE,
    )

    return frozenset(
        {
            # rob940_engine.py (H2)
            "next_bar_unavailable",
            "daily_stop_active",
            "daily_entry_cap",
            "cooldown_active",
            "tp_below_min_distance",
            # rob944_gap_funding.py (H4 funding PIT gate)
            REASON_FUNDING_EVIDENCE_UNAVAILABLE,
            REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
            # rob940_signal_s2.py (H3 S2's full 6-code rejection set --
            # next_bar_unavailable already listed above, shared with H2)
            "confirmation_failed",
            "target_direction_invalid",
            "tp_above_max",
            "tp_below_r_min_sl",
            "tp_below_abs_floor",
        }
    )


def _assert_known_no_trade_reason_counts(counts, *, context: str) -> None:
    """Fail closed on an unknown key, a non-int count, a bool masquerading
    as an int (``bool`` is an ``int`` subclass in Python), or a negative
    count -- BEFORE any such dict is hashed or printed. Never echoes the
    offending key/value itself (could be an arbitrary/secret string)."""
    known = _known_no_trade_reasons()
    for key, value in counts.items():
        if key not in known:
            raise ValueError(
                f"refusing to persist an unknown no_trade_reason_counts key for {context} -- "
                "only the closed known-reasons allowlist may be persisted"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"no_trade_reason_counts for {context} has a non-nonnegative-int count -- "
                "refusing to persist"
            )


# ---------------------------------------------------------------------------
# Captain normalization-boundary correction (2026-07-17): a pure auditor
# probe demonstrated a caller-owned mapping (no_trade_reason_counts) can be
# a dict SUBCLASS returning benign content on a first .items() pass (during
# validation) and secret-bearing content on a LATER pass (during hashing/
# operator-JSON output) -- validating one object and then re-reading it a
# second time for hashing/output is a TOCTOU hole. The bounded audit then
# widened this to the WHOLE summary boundary (scenario_summaries/
# fold_selection_trace themselves, nested eligible/excluded containers,
# and every persisted/hashed identifier string) -- a stateful iterable/
# proxy/subclass can misbehave anywhere a caller-owned object is read more
# than once. And a str SUBCLASS ("AliasStr") can override __eq__/__hash__
# to pass an allowlist/membership check while its actual buffer content
# (what canonical_sha256/output actually uses) is something else entirely.
#
# The fix is ONE architectural boundary: require EXACT canonical runtime
# types everywhere (``type(x) is T``, never ``isinstance`` -- isinstance
# accepts subclasses, which is exactly what enables every attack above),
# reading every field/container/dict EXACTLY ONCE to build a BRAND-NEW
# snapshot (fresh tuples, fresh plain dicts, values copied out of
# caller-owned rows into freshly-constructed dataclass instances) --
# thereafter, hashing/evidence-building/operator-capture consume ONLY the
# normalized snapshot, never touching the caller's original objects again.
# A real (non-subclassed) tuple/dict/dataclass instance cannot lie between
# reads -- only a subclass overriding dunder methods can, and exact-type
# checks reject every subclass outright, closing this for good.
# ---------------------------------------------------------------------------


def _assert_exact_str(value, *, context: str) -> str:
    """``type(value) is str`` -- never ``isinstance`` -- rejects an
    AliasStr-style subclass whose overridden ``__eq__``/``__hash__`` could
    otherwise pass an allowlist/membership check while its actual buffer
    content (what canonical_sha256/output actually reads) differs. Checked
    BEFORE any membership/equality comparison, never after."""
    if type(value) is not str:
        raise ValueError(f"{context} must be an exact str -- refusing to persist")
    return value


def _assert_exact_str_or_none(value, *, context: str):
    if value is None:
        return None
    return _assert_exact_str(value, context=context)


def _assert_exact_bool(value, *, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be an exact bool -- refusing to persist")
    return value


def _assert_exact_int(value, *, context: str) -> int:
    """``type(value) is int`` rejects bool (``type(True) is bool``, never
    ``int``) AND any int subclass uniformly -- ``isinstance`` would accept
    both."""
    if type(value) is not int:
        raise ValueError(f"{context} must be an exact int -- refusing to persist")
    return value


def _assert_exact_float(value, *, context: str) -> float:
    """``type(value) is float`` rejects bool/int/``Decimal``/``None``
    uniformly. NaN/+Inf/-Inf remain valid float VALUES here (accepted) --
    ``_json_safe_float_or_sentinel``'s sentinel normalization is the
    contractual way those are represented downstream, not a reason to
    reject them at this type-only layer."""
    if type(value) is not float:
        raise ValueError(f"{context} must be an exact float -- refusing to persist")
    return value


def _normalize_no_trade_reason_counts(value, *, context: str) -> dict[str, int]:
    """A dict SUBCLASS can override ``.items()``/``__iter__`` to return
    DIFFERENT content on each call -- a pure deterministic probe returning
    ``{}`` on a first (validation) pass and ``{"SECRET-...": 1}`` on a
    later (hash/output) pass. A genuine builtin ``dict`` cannot do this
    (its methods are fixed C-level implementations that always reflect
    current storage) -- requiring ``type(value) is dict`` (never
    ``isinstance``) BEFORE the single ``.items()`` pass below closes this
    fully: every key/value is read HERE, ONCE, into a brand-new plain
    dict; every caller downstream operates ONLY on that copy, never the
    original mapping again."""
    if type(value) is not dict:
        raise ValueError(
            f"{context} no_trade_reason_counts must be an exact dict -- refusing to persist"
        )
    normalized: dict[str, int] = {}
    for idx, (key, val) in enumerate(value.items()):
        key = _assert_exact_str(
            key, context=f"{context} no_trade_reason_counts key#{idx}"
        )
        val = _assert_exact_int(
            val, context=f"{context} no_trade_reason_counts value#{idx}"
        )
        normalized[key] = val
    return normalized


def _normalize_scenario_evidence_summary(row, *, context: str):
    """One caller-owned ``ScenarioEvidenceSummary`` -> a brand-new one
    built from freshly type-checked/copied fields. Rejects any subclass/
    proxy of ``ScenarioEvidenceSummary`` itself (``type(row) is ...``,
    never ``isinstance``) -- a frozen dataclass only blocks external
    ``__setattr__``, not a subclass overriding ``__getattribute__`` to
    return different values on repeated reads. ``trade_count`` is snapshot
    via ``_assert_exact_int`` HERE (captain live-review correction,
    2026-07-17: the prior version passed it through unchecked, so an int
    SUBCLASS would survive into the normalized DTO and only be caught by
    the downstream nonnegative check's ``isinstance`` -- which accepts
    subclasses) -- the nonnegative semantic check remains downstream,
    applied to this now-guaranteed-exact int."""
    from rob944_walkforward import ScenarioEvidenceSummary

    if type(row) is not ScenarioEvidenceSummary:
        raise ValueError(
            f"{context} must be an exact ScenarioEvidenceSummary -- refusing to persist"
        )
    scenario_name = _assert_exact_str(
        row.scenario_name, context=f"{context} scenario_name"
    )
    status = _assert_exact_str(row.status, context=f"{context} status")
    reason_code = _assert_exact_str_or_none(
        row.reason_code, context=f"{context} reason_code"
    )
    trade_count = _assert_exact_int(row.trade_count, context=f"{context} trade_count")
    artifact_hash = _assert_exact_str(
        row.artifact_hash, context=f"{context} artifact_hash"
    )
    no_trade_reason_counts = _normalize_no_trade_reason_counts(
        row.no_trade_reason_counts, context=context
    )
    return ScenarioEvidenceSummary(
        scenario_name=scenario_name,
        status=status,
        reason_code=reason_code,
        trade_count=trade_count,
        artifact_hash=artifact_hash,
        no_trade_reason_counts=no_trade_reason_counts,
    )


def _normalize_fold_selection_evidence_summary(row, *, context: str):
    """One caller-owned ``FoldSelectionEvidenceSummary`` -> a brand-new
    one. Exact-type container/entry checks here mirror (and precede, in
    the normal path) ``_assert_valid_fold_selection_row``'s own item-A/B
    checks -- kept in BOTH places deliberately: this is the primary
    boundary in the normal (normalize-first) path, while
    ``_assert_valid_fold_selection_row`` remains a defense-in-depth net
    for any caller that reaches it without going through normalization
    first."""
    from rob944_walkforward import FoldSelectionEvidenceSummary

    if type(row) is not FoldSelectionEvidenceSummary:
        raise ValueError(
            f"{context} must be an exact FoldSelectionEvidenceSummary -- refusing to persist"
        )
    fold_id = _assert_exact_str(row.fold_id, context=f"{context} fold_id")
    fold_selected_config_id = _assert_exact_str_or_none(
        row.fold_selected_config_id, context=f"{context} fold_selected_config_id"
    )
    # Captain design precision (2026-07-17): bind every raw field to a
    # local exactly once, BEFORE type-checking/iterating it -- type-
    # checking ``row.eligible_symbols`` and then separately re-reading
    # ``row.eligible_symbols`` for iteration is exactly the double-read
    # TOCTOU seam this whole boundary exists to close (applies uniformly
    # here to excluded_symbols/expectancy/profit_factor too).
    raw_eligible_symbols = row.eligible_symbols
    if type(raw_eligible_symbols) is not tuple:
        raise ValueError(
            f"{context} eligible_symbols must be an exact tuple -- refusing to persist"
        )
    eligible_symbols = tuple(
        _assert_exact_str(s, context=f"{context} eligible_symbols#{idx}")
        for idx, s in enumerate(raw_eligible_symbols)
    )
    raw_excluded_symbols = row.excluded_symbols
    if type(raw_excluded_symbols) is not tuple:
        raise ValueError(
            f"{context} excluded_symbols must be an exact tuple -- refusing to persist"
        )
    excluded_symbols = []
    for idx, entry in enumerate(raw_excluded_symbols):
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError(
                f"{context} excluded_symbols#{idx} must be an exact 2-tuple -- "
                "refusing to persist"
            )
        raw_symbol, raw_reason = entry
        symbol = _assert_exact_str(
            raw_symbol, context=f"{context} excluded_symbols#{idx} symbol"
        )
        reason = _assert_exact_str(
            raw_reason, context=f"{context} excluded_symbols#{idx} reason"
        )
        excluded_symbols.append((symbol, reason))
    rejected = _assert_exact_bool(row.rejected, context=f"{context} rejected")
    rejection_reason = _assert_exact_str_or_none(
        row.rejection_reason, context=f"{context} rejection_reason"
    )
    train_input_hash = _assert_exact_str(
        row.train_input_hash, context=f"{context} train_input_hash"
    )
    no_trade_reason_counts = _normalize_no_trade_reason_counts(
        row.no_trade_reason_counts, context=context
    )
    raw_equal_weight_expectancy_bps = row.equal_weight_expectancy_bps
    raw_pooled_expectancy_bps = row.pooled_expectancy_bps
    if rejected:
        if raw_equal_weight_expectancy_bps is not None:
            raise ValueError(
                f"{context} is rejected but equal_weight_expectancy_bps is not None -- "
                "refusing to persist"
            )
        if raw_pooled_expectancy_bps is not None:
            raise ValueError(
                f"{context} is rejected but pooled_expectancy_bps is not None -- "
                "refusing to persist"
            )
        equal_weight_expectancy_bps = None
        pooled_expectancy_bps = None
    else:
        equal_weight_expectancy_bps = _assert_exact_float(
            raw_equal_weight_expectancy_bps,
            context=f"{context} equal_weight_expectancy_bps",
        )
        pooled_expectancy_bps = _assert_exact_float(
            raw_pooled_expectancy_bps, context=f"{context} pooled_expectancy_bps"
        )
    profit_factor = _assert_exact_float(
        row.profit_factor, context=f"{context} profit_factor"
    )
    return FoldSelectionEvidenceSummary(
        fold_id=fold_id,
        fold_selected_config_id=fold_selected_config_id,
        eligible_symbols=eligible_symbols,
        excluded_symbols=tuple(excluded_symbols),
        equal_weight_expectancy_bps=equal_weight_expectancy_bps,
        pooled_expectancy_bps=pooled_expectancy_bps,
        profit_factor=profit_factor,
        rejected=rejected,
        rejection_reason=rejection_reason,
        train_input_hash=train_input_hash,
        no_trade_reason_counts=no_trade_reason_counts,
    )


_KNOWN_DIAGNOSTIC_TRANSPORTS = frozenset({"in_process"})
_KNOWN_DIAGNOSTIC_STAGES = frozenset({"generator", "funding_gate", "engine"})


def _normalize_child_failure_evidence_row(row, *, context: str):
    """ROB-970 (Q2, Fable-approved): one caller-owned ``ChildFailureEvidence``
    -> exact-type/field-checked, unchanged. Additive, persistence-only --
    never touches any hash/identity payload downstream."""
    from rob944_diagnostic_evidence import ChildFailureEvidence

    if type(row) is not ChildFailureEvidence:
        raise ValueError(
            f"{context} must be an exact ChildFailureEvidence -- refusing to persist"
        )
    transport = _assert_exact_str(row.transport, context=f"{context} transport")
    if transport not in _KNOWN_DIAGNOSTIC_TRANSPORTS:
        raise ValueError(
            f"{context} transport is outside the closed known-transport set"
        )
    stage = _assert_exact_str(row.stage, context=f"{context} stage")
    if stage not in _KNOWN_DIAGNOSTIC_STAGES:
        raise ValueError(f"{context} stage is outside the closed known-stage set")
    _assert_exact_str(row.exception_type, context=f"{context} exception_type")
    _assert_exact_str(row.message, context=f"{context} message")
    _assert_exact_str(row.traceback_text, context=f"{context} traceback_text")
    stderr = _assert_exact_str_or_none(row.stderr, context=f"{context} stderr")
    if transport == "in_process" and stderr is not None:
        raise ValueError(
            f"{context} in_process transport must never fabricate a stderr value"
        )
    _assert_exact_str(row.strategy, context=f"{context} strategy")
    _assert_exact_str(row.config_id, context=f"{context} config_id")
    _assert_exact_str_or_none(row.symbol, context=f"{context} symbol")
    _assert_exact_str_or_none(row.fold_id, context=f"{context} fold_id")
    _assert_exact_str_or_none(row.scenario_name, context=f"{context} scenario_name")
    _assert_exact_str(row.signature, context=f"{context} signature")
    occurrence_count = _assert_exact_int(
        row.occurrence_count, context=f"{context} occurrence_count"
    )
    if occurrence_count < 1:
        raise ValueError(f"{context} occurrence_count must be >= 1")
    _assert_exact_bool(row.truncated, context=f"{context} truncated")
    return row


def _normalize_diagnostic_evidence_tuple(raw_value, *, context: str):
    if type(raw_value) is not tuple:
        raise ValueError(f"{context} must be an exact tuple -- refusing to persist")
    return tuple(
        _normalize_child_failure_evidence_row(row, context=f"{context}#{idx}")
        for idx, row in enumerate(raw_value)
    )


def _normalize_diagnostic_overflow(raw_value, *, context: str):
    """ROB-970 R1 (Q1=A, cap=32): honest overflow accounting -- exact-type/
    field-checked, unchanged. Additive, persistence-only."""
    from rob944_diagnostic_evidence import DiagnosticOverflowMetadata

    if type(raw_value) is not DiagnosticOverflowMetadata:
        raise ValueError(
            f"{context} must be an exact DiagnosticOverflowMetadata -- refusing to persist"
        )
    _assert_exact_bool(raw_value.truncated, context=f"{context} truncated")
    omitted_distinct_signatures = _assert_exact_int(
        raw_value.omitted_distinct_signatures,
        context=f"{context} omitted_distinct_signatures",
    )
    if omitted_distinct_signatures < 0:
        raise ValueError(f"{context} omitted_distinct_signatures must be >= 0")
    omitted_occurrences = _assert_exact_int(
        raw_value.omitted_occurrences, context=f"{context} omitted_occurrences"
    )
    if omitted_occurrences < 0:
        raise ValueError(f"{context} omitted_occurrences must be >= 0")
    if omitted_distinct_signatures > omitted_occurrences:
        raise ValueError(
            f"{context} omitted_distinct_signatures cannot exceed omitted_occurrences"
        )
    return raw_value


def _normalize_config_attempt_evidence_summary(summary, *, context: str):
    """THE single normalization entry point (captain normalization-scope
    clarification, 2026-07-17): every container is required to be its
    exact canonical runtime type, every nested row its exact canonical
    dataclass type, every persisted/hashed string its exact ``str``, every
    count its exact ``int``, every no_trade_reason_counts mapping its
    exact ``dict`` -- rejecting subclasses/proxies BEFORE any allowlist
    membership/equality check runs on them anywhere downstream. Returns a
    BRAND-NEW ``ConfigAttemptEvidenceSummary`` built entirely from this one
    pass; nothing downstream (validation, hashing, operator capture)
    touches the caller's original object again."""
    from rob944_walkforward import ConfigAttemptEvidenceSummary

    if type(summary) is not ConfigAttemptEvidenceSummary:
        raise ValueError(
            f"{context} must be an exact ConfigAttemptEvidenceSummary -- refusing to persist"
        )
    strategy = _assert_exact_str(summary.strategy, context=f"{context} strategy")
    config_id = _assert_exact_str(summary.config_id, context=f"{context} config_id")
    status = _assert_exact_str(summary.status, context=f"{context} status")
    reason_code = _assert_exact_str_or_none(
        summary.reason_code, context=f"{context} reason_code"
    )
    # Captain design precision (2026-07-17): bind every raw field to a
    # local exactly once, BEFORE type-checking/iterating it -- never
    # type-check an attribute then re-read the attribute a second time
    # for iteration (that second read is exactly the TOCTOU seam this
    # whole boundary exists to close). ``summary`` is already confirmed
    # ``type(...) is ConfigAttemptEvidenceSummary`` above, so
    # ``fold_selection_trace`` is read directly (the exact DTO always
    # carries this field) -- never ``getattr`` with a fallback default.
    raw_scenario_summaries = summary.scenario_summaries
    if type(raw_scenario_summaries) is not tuple:
        raise ValueError(
            f"{context} scenario_summaries must be an exact tuple -- refusing to persist"
        )
    normalized_scenario_rows = tuple(
        _normalize_scenario_evidence_summary(row, context=f"{context}/scenario#{idx}")
        for idx, row in enumerate(raw_scenario_summaries)
    )
    raw_fold_trace = summary.fold_selection_trace
    if type(raw_fold_trace) is not tuple:
        raise ValueError(
            f"{context} fold_selection_trace must be an exact tuple -- refusing to persist"
        )
    normalized_fold_rows = tuple(
        _normalize_fold_selection_evidence_summary(row, context=f"{context}/fold#{idx}")
        for idx, row in enumerate(raw_fold_trace)
    )
    # Captain design review (2026-07-17, item 3): canonically order BOTH
    # traces HERE, in the snapshot itself, right after exact-primitive
    # normalization -- so operator capture/output and H6/hash-building
    # consume the IDENTICAL order from a single source, rather than each
    # independently re-sorting (or worse, one of them forgetting to).
    # Safe to sort now: every row's scenario_name/fold_id is already
    # proven an exact str by the per-row normalizer above.
    scenario_summaries = tuple(
        sorted(normalized_scenario_rows, key=lambda row: row.scenario_name)
    )
    fold_selection_trace = tuple(
        sorted(normalized_fold_rows, key=lambda row: row.fold_id)
    )
    diagnostic_evidence = _normalize_diagnostic_evidence_tuple(
        summary.diagnostic_evidence, context=f"{context} diagnostic_evidence"
    )
    diagnostic_overflow = _normalize_diagnostic_overflow(
        summary.diagnostic_overflow, context=f"{context} diagnostic_overflow"
    )
    return ConfigAttemptEvidenceSummary(
        strategy=strategy,
        config_id=config_id,
        status=status,
        reason_code=reason_code,
        scenario_summaries=scenario_summaries,
        fold_selection_trace=fold_selection_trace,
        diagnostic_evidence=diagnostic_evidence,
        diagnostic_overflow=diagnostic_overflow,
    )


def _summary_to_attempt_evidence(
    summary,
    *,
    strategy_key: str,
    experiment_id: str,
    full_campaign_hash: str,
    campaign_run_id: str,
):
    """Thin direct-call wrapper (captain design cross-check correction,
    2026-07-17): normalizes the RAW ``summary`` exactly ONCE, then
    delegates every remaining validation/hash-building step to
    ``_normalized_summary_to_attempt_evidence``, which assumes an
    already-normalized snapshot and never re-normalizes. This is the
    entry point for any caller handing over a raw (not yet normalized)
    ``ConfigAttemptEvidenceSummary`` -- including every direct test call --
    while the batch/capture path (``_normalize_and_capture_summaries``)
    normalizes once itself and calls the CORE directly, never this
    wrapper, so a summary is never normalized twice."""
    # Captain design review (2026-07-17, item 2): the lineage args
    # themselves need exact-str gating BEFORE they are used to build a
    # context string or enter any hash payload -- static, non-
    # interpolating contexts here, since the values are not yet proven
    # safe to reference in a message.
    strategy_key = _assert_exact_str(
        strategy_key, context="lineage argument strategy_key"
    )
    experiment_id = _assert_exact_str(
        experiment_id, context="lineage argument experiment_id"
    )
    full_campaign_hash = _assert_exact_str(
        full_campaign_hash, context="lineage argument full_campaign_hash"
    )
    campaign_run_id = _assert_exact_str(
        campaign_run_id, context="lineage argument campaign_run_id"
    )
    context = f"experiment {experiment_id}"
    normalized = _normalize_config_attempt_evidence_summary(summary, context=context)
    return _normalized_summary_to_attempt_evidence(
        normalized,
        strategy_key=strategy_key,
        experiment_id=experiment_id,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
    )


def _normalized_summary_to_attempt_evidence(
    summary,
    *,
    strategy_key: str,
    experiment_id: str,
    full_campaign_hash: str,
    campaign_run_id: str,
):
    """The normalized CORE (captain design cross-check correction,
    2026-07-17): ASSUMES ``summary`` already came from
    ``_normalize_config_attempt_evidence_summary`` (exact types
    throughout, scenario/fold rows already canonically ordered) -- does
    NOT re-normalize or re-sort. Callers that hold a raw summary must go
    through ``_summary_to_attempt_evidence`` (the thin wrapper) instead;
    calling this directly on an unnormalized object is the caller's own
    contract violation, not something this function re-defends against
    (that would be exactly the double-normalization/double-copy this
    split exists to avoid).

    One ``rob944_walkforward.ConfigAttemptEvidenceSummary`` -> one
    ``AttemptEvidence``.

    ``fold_evidence_hash`` is a hash of JUST the per-scenario fold-derived
    evidence (status/reason/count/artifact/no-trade-reason-counts per
    scenario) -- semantically distinct from ``run_identity``, which NESTS
    ``fold_evidence_hash`` inside a payload that additionally binds the full
    lineage: full-campaign hash, campaign_run_id, canonical experiment_id,
    retry_index, strategy/config_id/status. Both are deterministic, no
    wall-clock/UUID. Every reason_code (attempt-level and per-scenario) is
    re-validated against the closed allowlist before anything is built.
    """
    from rob944_walkforward import _json_safe_float_or_sentinel

    from app.schemas.research_campaign_bridge import (
        AttemptEvidence,
        AttemptKey,
        ChildFailureDiagnostic,
        ChildFailureDiagnosticOverflow,
        ScenarioEvidence,
    )
    from research_contracts.canonical_hash import canonical_sha256

    # Captain final-audit item A (2026-07-17): context strings must NEVER
    # be built from unvalidated summary.strategy/config_id/scenario_name/
    # fold_id (an injected secret/control string would otherwise be
    # echoed) -- only the CALLER-TRUSTED experiment_id (already validated/
    # looked-up by the caller before this function is ever invoked) is
    # safe to interpolate. Untrusted per-row identifiers are replaced with
    # a bare index.
    context = f"experiment {experiment_id}"
    # Captain live-validation correction (2026-07-17): summary.strategy must
    # be exactly S1/S2, and summary.config_id must be an EXACT member of
    # that strategy's own frozen 12-config set -- checked BEFORE anything
    # else touches either value.
    _assert_valid_attempt_identity(summary.strategy, summary.config_id, context=context)
    # Captain trust-boundary addendum (2026-07-17): a bare KNOWN_REASON_CODES
    # membership check lets a cross-status pair through (e.g.
    # status="completed" + reason_code="child_execution_crashed") -- H6's
    # own DTO has no status/reason-pair validation, so this exact
    # status-scoped contract is the only place that gets caught, BEFORE
    # anything is hashed/persisted.
    _assert_status_reason_contract(
        summary.status,
        summary.reason_code,
        allowed_by_status=_attempt_allowed_reasons_by_status(),
        context=context,
    )
    scenario_allowed = _scenario_allowed_reasons_by_status()
    # Captain pre-hash trust check (2026-07-17): exact 3 UNIQUE canonical
    # scenarios -- checked BEFORE anything below is hashed, not merely
    # relied on downstream (the controller rejects a malformed batch LATER,
    # but by then an unsafe artifact_hash/trade_count may already have
    # entered fold_evidence_hash).
    if (
        len(summary.scenario_summaries) != 3
        or {row.scenario_name for row in summary.scenario_summaries}
        != _KNOWN_SCENARIO_NAMES
    ):
        raise ValueError(
            f"{context} does not have exactly the 3 unique canonical scenarios -- refusing to persist"
        )
    for idx, row in enumerate(summary.scenario_summaries):
        scenario_context = f"{context}/scenario#{idx}"
        _assert_status_reason_contract(
            row.status,
            row.reason_code,
            allowed_by_status=scenario_allowed,
            context=scenario_context,
        )
        # Captain trust-boundary addendum (2026-07-17): validate
        # no_trade_reason_counts KEYS/VALUES too -- untrusted input from a
        # caller-injected callback must never be hashed/printed as-is.
        _assert_known_no_trade_reason_counts(
            row.no_trade_reason_counts, context=scenario_context
        )
        # Captain pre-hash trust check: artifact_hash must be lowercase
        # hex64, and trade_count a strict (non-bool) nonnegative int --
        # BEFORE either is hashed.
        _assert_hex64(row.artifact_hash, context=f"{scenario_context} artifact_hash")
        # type(...) is not int (never isinstance) also rejects an int
        # SUBCLASS uniformly, not merely bool -- captain normalization
        # correction (2026-07-17). Message text unchanged.
        if type(row.trade_count) is not int or row.trade_count < 0:
            raise ValueError(
                f"{scenario_context} has a non-nonnegative-int trade_count -- refusing to persist"
            )
    # ``summary`` is the NORMALIZED core's input -- an exact
    # ConfigAttemptEvidenceSummary always carries this field; read it
    # directly, never via getattr with a fallback default.
    fold_trace = summary.fold_selection_trace
    for idx, fold_row in enumerate(fold_trace):
        _assert_known_no_trade_reason_counts(
            fold_row.no_trade_reason_counts, context=f"{context}/fold#{idx}"
        )
    _assert_valid_fold_selection_trace(
        fold_trace,
        strategy=summary.strategy,
        status=summary.status,
        reason_code=summary.reason_code,
        context=context,
    )

    # Captain design review (2026-07-17, item 3): canonical ordering (by
    # scenario_name / fold_id) now happens ONCE, inside
    # ``_normalize_config_attempt_evidence_summary`` itself -- ``summary``
    # here is ALREADY sorted; re-sorting a second time would be a harmless
    # no-op, but the whole point of the split is that this core trusts its
    # normalized input rather than re-deriving guarantees the normalizer
    # already established. Named identically to the old locals (now a
    # direct alias) so the hash-payload/evidence-building code below is
    # unchanged.
    ordered_summaries = summary.scenario_summaries
    ordered_fold_trace = summary.fold_selection_trace

    retry_index = 0
    fold_evidence_hash = canonical_sha256(
        {
            "strategy": summary.strategy,
            "config_id": summary.config_id,
            # Captain P1 (independent audit): the ATTEMPT's own terminal
            # status/reason_code must be bound here too -- two rejected
            # summaries with identical scenario rows but DIFFERENT
            # rejection reasons (e.g. data_gap vs insufficient_train_evidence)
            # previously collided in this hash.
            "status": summary.status,
            "reason_code": summary.reason_code,
            "scenario_summaries": [
                {
                    "scenario_name": row.scenario_name,
                    "status": row.status,
                    "reason_code": row.reason_code,
                    "trade_count": row.trade_count,
                    "artifact_hash": row.artifact_hash,
                    "no_trade_reason_counts": row.no_trade_reason_counts,
                }
                for row in ordered_summaries
            ],
            # Captain P1 (end-to-end provenance gap): the per-fold TRAIN
            # selection trace -- previously entirely absent, so a TRAIN-only
            # mutation (e.g. a price change altering train_input_hash
            # without changing which config won OOS) was invisible here.
            "fold_selection_trace": [
                {
                    "fold_id": row.fold_id,
                    "fold_selected_config_id": row.fold_selected_config_id,
                    "eligible_symbols": list(row.eligible_symbols),
                    "excluded_symbols": [list(pair) for pair in row.excluded_symbols],
                    "equal_weight_expectancy_bps": _json_safe_float_or_sentinel(
                        row.equal_weight_expectancy_bps
                    ),
                    "pooled_expectancy_bps": _json_safe_float_or_sentinel(
                        row.pooled_expectancy_bps
                    ),
                    "profit_factor": _json_safe_float_or_sentinel(row.profit_factor),
                    "rejected": row.rejected,
                    "rejection_reason": row.rejection_reason,
                    "train_input_hash": row.train_input_hash,
                    "no_trade_reason_counts": row.no_trade_reason_counts,
                }
                for row in ordered_fold_trace
            ],
        }
    )
    # Independent controller audit correction (2026-07-17): drop the
    # redundant short-slug "strategy" ("S1"/"S2") field -- strategy_key (the
    # full production key) already identifies the strategy, and dropping it
    # makes this payload fully reconstructible by
    # app.services.rob944_campaign_controller from ONLY values it already
    # trusts (predeclared strategy_key/config_id per experiment_id, the
    # validated campaign_run_id/full_campaign_hash, and this evidence's own
    # status/fold_evidence_hash) -- see that module's
    # ``_derive_expected_run_identity``, which MUST match this shape exactly.
    run_identity = canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "strategy_key": strategy_key,
            "experiment_id": experiment_id,
            "retry_index": retry_index,
            "config_id": summary.config_id,
            "status": summary.status,  # explicit for direct queryability; reason_code already nested via fold_evidence_hash
            "fold_evidence_hash": fold_evidence_hash,
        }
    )
    return AttemptEvidence(
        attempt_key=AttemptKey(
            campaign_run_id=campaign_run_id,
            experiment_id=experiment_id,
            retry_index=retry_index,
        ),
        status=summary.status,
        reason_code=summary.reason_code,
        fold_evidence_hash=fold_evidence_hash,
        run_identity=run_identity,
        scenario_evidence=tuple(
            ScenarioEvidence(
                scenario_name=row.scenario_name,
                trade_count=row.trade_count,
                artifact_hash=row.artifact_hash,
            )
            for row in ordered_summaries
        ),
        # ROB-970 (Q2, Fable-approved): additive, persistence-only -- carried
        # through UNCHANGED from the already-normalized summary; deliberately
        # never referenced by fold_evidence_hash/run_identity above.
        diagnostic_evidence=tuple(
            ChildFailureDiagnostic(
                transport=d.transport,
                stage=d.stage,
                exception_type=d.exception_type,
                message=d.message,
                traceback_text=d.traceback_text,
                stderr=d.stderr,
                strategy=d.strategy,
                config_id=d.config_id,
                symbol=d.symbol,
                fold_id=d.fold_id,
                scenario_name=d.scenario_name,
                signature=d.signature,
                occurrence_count=d.occurrence_count,
                truncated=d.truncated,
            )
            for d in summary.diagnostic_evidence
        ),
        # ROB-970 R1 (Q1=A, cap=32): same additive treatment as
        # diagnostic_evidence -- carried through UNCHANGED, never referenced
        # by fold_evidence_hash/run_identity above.
        diagnostic_overflow=ChildFailureDiagnosticOverflow(
            truncated=summary.diagnostic_overflow.truncated,
            omitted_distinct_signatures=summary.diagnostic_overflow.omitted_distinct_signatures,
            omitted_occurrences=summary.diagnostic_overflow.omitted_occurrences,
        ),
    )


def _summaries_to_attempt_evidence(
    summaries,
    *,
    strategy_key: str,
    experiment_id_by_key: dict,
    full_campaign_hash: str,
    campaign_run_id: str,
):
    """Legacy convenience wrapper -- routed through the SAME normalized-
    batch boundary as ``_normalize_and_capture_summaries`` (captain
    precision follow-up, 2026-07-17, item 2), so it can never silently
    regress to the old per-item raw-wrapper pattern (which re-normalized
    each item independently and had no exact-tuple discipline on the
    outer batch). No capture wiring -- this helper never touches
    operator-visible output. ``summaries`` must now be an exact tuple,
    matching the boundary's own requirement."""
    return _normalize_and_capture_summaries(
        summaries,
        strategy_key=strategy_key,
        experiment_id_by_key=experiment_id_by_key,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        capture_summaries_into=None,
    )


def _normalize_experiment_id_by_key(value, *, context: str) -> dict:
    """``experiment_id_by_key``: ``{(strategy_key, config_id):
    experiment_id}`` -- captain design precision (2026-07-17, item 2):
    snapshot and exact-validate this mapping too, exactly once, BEFORE any
    fallback artifact/evidence construction reads it. Exact plain ``dict``
    (never a subclass), every key an exact 2-tuple of exact ``str``
    (strategy_key, config_id), every value an exact ``str`` experiment_id.
    Every downstream consumer must use ONLY this normalized snapshot,
    never the caller's original mapping."""
    if type(value) is not dict:
        raise ValueError(f"{context} must be an exact dict -- refusing to persist")
    normalized: dict[tuple[str, str], str] = {}
    for idx, (key, val) in enumerate(value.items()):
        if type(key) is not tuple or len(key) != 2:
            raise ValueError(
                f"{context} key#{idx} must be an exact 2-tuple -- refusing to persist"
            )
        raw_strategy_key, raw_config_id = key
        strategy_key = _assert_exact_str(
            raw_strategy_key, context=f"{context} key#{idx} strategy_key"
        )
        config_id = _assert_exact_str(
            raw_config_id, context=f"{context} key#{idx} config_id"
        )
        experiment_id = _assert_exact_str(
            val, context=f"{context} key#{idx} experiment_id"
        )
        normalized[(strategy_key, config_id)] = experiment_id
    return normalized


def _normalize_and_capture_summaries(
    summaries,
    *,
    strategy_key: str,
    experiment_id_by_key: dict,
    full_campaign_hash: str,
    campaign_run_id: str,
    capture_summaries_into: list | None = None,
) -> list:
    """Captain design cross-check + design-review corrections (2026-07-17):
    normalize EVERY summary exactly once, HERE, into an exact TUPLE batch
    -- before ``experiment_id_by_key``'s lookup (which uses ``config_id``
    as part of its dict key) and before operator-visible capture. Builds
    ALL H6 evidence by calling the NORMALIZED CORE
    (``_normalized_summary_to_attempt_evidence``) directly on those exact
    snapshot objects -- never the raw wrapper (``_summary_to_attempt_evidence``
    would re-normalize, contradicting exactly-once/same-snapshot and
    copying every nested dict twice). Only AFTER every evidence entry has
    been successfully built does operator capture receive the SAME
    normalized objects -- never before, and never a second independent
    traversal/copy of the caller's original summaries.

    Captain precision follow-up (2026-07-17, item 1): every lineage arg
    AND ``experiment_id_by_key`` itself are gated/normalized FIRST, before
    any f-string context, dict lookup, or normalized-core call -- calling
    the core directly (as this function does) bypasses the thin wrapper's
    OWN lineage gating, so this is the only place left that can do it for
    this call path."""
    strategy_key = _assert_exact_str(
        strategy_key, context="lineage argument strategy_key"
    )
    full_campaign_hash = _assert_exact_str(
        full_campaign_hash, context="lineage argument full_campaign_hash"
    )
    campaign_run_id = _assert_exact_str(
        campaign_run_id, context="lineage argument campaign_run_id"
    )
    experiment_id_by_key = _normalize_experiment_id_by_key(
        experiment_id_by_key, context="experiment_id_by_key"
    )
    if type(summaries) is not tuple:
        raise ValueError(
            "summaries batch must be an exact tuple -- refusing to persist"
        )
    normalized = tuple(
        _normalize_config_attempt_evidence_summary(
            s, context=f"{strategy_key}/summary#{idx}"
        )
        for idx, s in enumerate(summaries)
    )
    evidence = [
        _normalized_summary_to_attempt_evidence(
            s,
            strategy_key=strategy_key,
            experiment_id=experiment_id_by_key[(strategy_key, s.config_id)],
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
        )
        for s in normalized
    ]
    if capture_summaries_into is not None:
        capture_summaries_into.extend(normalized)
    return evidence


def _global_failure_summaries(experiment_id_by_key: dict) -> tuple:
    """The 24 deterministic ``ConfigAttemptEvidenceSummary`` sentinel rows
    for a GLOBAL (pre-per-config) failure -- corpus loading, manifest
    validation, or any other precondition needed before ANY per-strategy
    walk-forward can run. Every entry is ``status="crashed"`` with the FIXED
    ``REASON_GLOBAL_CORPUS_LOAD_FAILED`` code and a deterministic 3-scenario
    sentinel (no raw exception text anywhere; every hash is stable identity
    + status + reason only). Kept separate from evidence conversion so the
    SAME summaries can back both the persisted DB evidence
    (``_global_failure_evidence_batch``) and the operator-visible CLI
    ``capture_summaries_into`` view, through one single source.
    """
    from rob940_cost_model import COST_SCENARIOS
    from rob944_walkforward import (
        REASON_GLOBAL_CORPUS_LOAD_FAILED,
        ConfigAttemptEvidenceSummary,
        ScenarioEvidenceSummary,
    )

    from research_contracts.canonical_hash import canonical_sha256

    summaries = []
    for (strategy_key, config_id), _experiment_id in experiment_id_by_key.items():
        # Captain global-fallback identity-consistency correction
        # (2026-07-17): the OPERATOR-VISIBLE summary.strategy must be
        # consistently the short "S1"/"S2" slug (matching every real,
        # non-fallback summary) -- the FULL production strategy_key is
        # still bound into the artifact hash (identity provenance) and
        # passed explicitly to _summary_to_attempt_evidence's own
        # strategy_key param by the caller, never conflated with this field.
        slug = config_id.split("-", 1)[0]
        scenario_summaries = tuple(
            ScenarioEvidenceSummary(
                scenario_name=scenario.name,
                status="crashed",
                reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
                trade_count=0,
                artifact_hash=canonical_sha256(
                    {
                        "strategy_key": strategy_key,
                        "config_id": config_id,
                        "scenario_name": scenario.name,
                        "status": "crashed",
                        "reason_code": REASON_GLOBAL_CORPUS_LOAD_FAILED,
                    }
                ),
                no_trade_reason_counts={},
            )
            for scenario in COST_SCENARIOS
        )
        summaries.append(
            ConfigAttemptEvidenceSummary(
                strategy=slug,
                config_id=config_id,
                status="crashed",
                reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
                scenario_summaries=scenario_summaries,
            )
        )
    return tuple(summaries)


def _global_failure_evidence_batch(
    experiment_id_by_key: dict, *, full_campaign_hash: str, campaign_run_id: str
) -> list:
    """A GLOBAL (pre-per-config) failure must still yield a full 24-entry
    terminal batch, never zero attempts, once the 24 experiment keys are
    already predeclared (post-registration).

    Captain consistency correction (2026-07-17): builds the SAME
    ``rob944_walkforward.ConfigAttemptEvidenceSummary``/``ScenarioEvidenceSummary``
    shape the real (non-failure) path produces (via ``_global_failure_summaries``),
    then converts via the IDENTICAL normalized-core boundary used for real
    evidence -- so ``fold_evidence_hash``/``run_identity`` bind the actual
    3 canonical scenario names/status/count/artifact-hashes here too, not
    merely the bare reason code, and the operator-visible CLI view and
    persisted DB evidence are built through one single conversion path,
    never two independently hand-rolled ones that could silently drift
    apart.

    Captain precision follow-up (2026-07-17, item 2): routed through
    ``_build_fallback_evidence_and_capture`` (``capture_summaries_into=None``
    -- this legacy helper never touched operator-visible output) so it
    cannot silently regress to the old per-item raw-wrapper pattern this
    module has moved away from everywhere else.
    """
    return _build_fallback_evidence_and_capture(
        experiment_id_by_key,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        capture_summaries_into=None,
    )


def _build_fallback_evidence_and_capture(
    experiment_id_by_key: dict,
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    capture_summaries_into: list | None = None,
) -> list:
    """Captain design precision (2026-07-17, items 2-3): the ONE fallback
    helper. Snapshots/exact-validates ``experiment_id_by_key`` first (never
    the caller's original mapping again after this point), generates the
    deterministic 24-entry global-fallback summaries as an exact tuple,
    normalizes each entry exactly once, builds/validates ALL 24 via the
    normalized core, and ONLY THEN -- after every entry has succeeded --
    replaces operator capture atomically with those same normalized
    objects. Never a partial or raw (pre-normalization) capture.

    Captain precision follow-up (2026-07-17, item 1): ``full_campaign_hash``/
    ``campaign_run_id`` are ALSO gated here, first -- this function calls
    the normalized core directly (bypassing the thin wrapper's own
    lineage gating), so it must do that gating itself."""
    full_campaign_hash = _assert_exact_str(
        full_campaign_hash, context="lineage argument full_campaign_hash"
    )
    campaign_run_id = _assert_exact_str(
        campaign_run_id, context="lineage argument campaign_run_id"
    )
    normalized_ids = _normalize_experiment_id_by_key(
        experiment_id_by_key, context="global-fallback experiment_id_by_key"
    )
    fallback_summaries = _global_failure_summaries(normalized_ids)
    # summaries only carry the short "S1"/"S2" slug in .strategy --
    # recover the FULL production strategy_key/experiment_id per
    # config_id (unique across the whole campaign), from the SAME
    # normalized snapshot.
    strategy_key_by_config_id = {
        config_id: strategy_key for (strategy_key, config_id) in normalized_ids
    }
    experiment_id_by_config_id = {
        config_id: exp_id
        for (_strategy_key, config_id), exp_id in normalized_ids.items()
    }
    normalized_fallback = tuple(
        _normalize_config_attempt_evidence_summary(
            s, context=f"global-fallback/summary#{idx}"
        )
        for idx, s in enumerate(fallback_summaries)
    )
    fallback_evidence = [
        _normalized_summary_to_attempt_evidence(
            s,
            strategy_key=strategy_key_by_config_id[s.config_id],
            experiment_id=experiment_id_by_config_id[s.config_id],
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
        )
        for s in normalized_fallback
    ]
    if capture_summaries_into is not None:
        capture_summaries_into.clear()
        capture_summaries_into.extend(normalized_fallback)
    return fallback_evidence


def _build_real_attempt_evidence(
    experiment_id_by_key: dict,
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    capture_summaries_into: list | None = None,
) -> list:
    """The REAL corpus-loading + walk-forward + H6-evidence pipeline.

    Fully implemented and correct, but ROB-944 (H4) NEVER invokes this --
    empirical --run remains a deliberate, later, operator action. ANY
    exception here (missing/tampered corpus, manifest validation failure,
    etc.) is caught and converted into a full 24-entry crashed terminal
    batch (see ``_build_fallback_evidence_and_capture``) -- never zero
    attempts, since the 24 keys are already predeclared by the time this
    runs.

    Captain global-fallback consistency correction (2026-07-17): if S1
    already succeeded and populated ``capture_summaries_into`` with 12 real
    summaries before S2 (or any later step) fails, the operator-visible CLI
    view MUST match the persisted DB evidence exactly -- so on this path the
    partial capture is CLEARED and refilled with the full 24-entry global
    failure sentinel, never left showing 12 real + nothing.

    Captain precision follow-up (2026-07-17, "one remaining boundary"):
    ``experiment_id_by_key``/``full_campaign_hash``/``campaign_run_id`` are
    snapshotted/exact-gated HERE, at the very entry point, BEFORE any
    corpus loading or walk-forward work -- previously the first gate on
    this mapping fired only AFTER a full per-strategy walk-forward run had
    already completed (inside ``_normalize_and_capture_summaries``), and
    the fallback branch re-read the caller's ORIGINAL mapping again after
    an exception. The SAME normalized snapshot is now passed to BOTH the
    inner (real pipeline) and fallback branches -- no downstream path
    sees the caller's original mapping/strings again. Leaf helpers still
    re-gate their own inputs too (harmless/idempotent on an already-
    normalized snapshot, and keeps them safe for any other direct caller).
    """
    full_campaign_hash = _assert_exact_str(
        full_campaign_hash, context="lineage argument full_campaign_hash"
    )
    campaign_run_id = _assert_exact_str(
        campaign_run_id, context="lineage argument campaign_run_id"
    )
    experiment_id_by_key = _normalize_experiment_id_by_key(
        experiment_id_by_key, context="experiment_id_by_key"
    )
    try:
        return _build_real_attempt_evidence_inner(
            experiment_id_by_key,
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
            capture_summaries_into=capture_summaries_into,
        )
    except Exception:  # noqa: BLE001 -- deliberate: any global pre-execution failure still yields a full terminal batch, never zero attempts, and never re-raises raw exception text
        return _build_fallback_evidence_and_capture(
            experiment_id_by_key,
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
            capture_summaries_into=capture_summaries_into,
        )


def _build_real_attempt_evidence_inner(
    experiment_id_by_key: dict,
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    capture_summaries_into: list | None = None,
) -> list:
    """Requires ``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` to locate the
    offline-persisted H1 corpus shards (network-0 load, see
    ``rob941_offline_loader``). ``capture_summaries_into``, if given, is
    appended with every ``ConfigAttemptEvidenceSummary`` produced -- the
    full per-scenario status/reason/count/artifact/no-trade-reason detail,
    for OPERATOR-VISIBLE output alongside the (necessarily coarser)
    H6-shaped DB write.
    """
    import rob941_offline_loader as offline_loader
    from rob940_bars_agg import Bar1m, aggregate_complete
    from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS
    from rob940_signal_s1 import generate_s1_signals
    from rob940_signal_s2 import generate_s2_signals
    from rob941_frozen_scope import WINDOW_END_MS, WINDOW_START_MS
    from rob941_funding_sidecar import FundingSidecar
    from rob941_manifest import CorpusManifest
    from rob944_folds import generate_frozen_fold_schedule
    from rob944_walkforward import (
        ConfigSpec,
        GeneratedSignalBatch,
        run_walkforward,
        summarize_config_attempts_for_h6,
    )

    artifact_root = os.environ.get(_ENV_ARTIFACT_ROOT)
    if not artifact_root:
        raise RunPreflightError(
            f"{_ENV_ARTIFACT_ROOT} is required for --run corpus loading"
        )

    manifest = CorpusManifest.load(H1_MANIFEST_PATH)
    corpus = offline_loader.load_corpus(manifest, Path(artifact_root))

    bars_1m = {
        symbol: tuple(
            Bar1m(
                ts=r.open_time_ms,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.base_volume,
            )
            for r in rows
        )
        for symbol, rows in corpus["klines"].items()
    }
    funding_sidecars = {
        symbol: FundingSidecar.from_rows(symbol, rows)
        for symbol, rows in corpus["funding"].items()
    }
    gap_ranges = {k.symbol: k.gap_ranges for k in manifest.klines}
    fold_schedule = generate_frozen_fold_schedule(WINDOW_START_MS, WINDOW_END_MS)

    def _s1_gen_factory(config):
        def _gen(symbol, bars_slice, fold_id):
            bars_15m = aggregate_complete(bars_slice, bucket_minutes=15)
            return generate_s1_signals(bars_15m, config, symbol=symbol, fold_id=fold_id)

        return _gen

    def _s2_gen_factory(config):
        def _gen(symbol, bars_slice, fold_id):
            bars_5m = aggregate_complete(bars_slice, bucket_minutes=5)
            gen_result = generate_s2_signals(
                bars_5m, bars_slice, config, symbol=symbol, fold_id=fold_id
            )
            return GeneratedSignalBatch(
                signals=gen_result.signals,
                rejections=_s2_rejections_to_no_trade_records(gen_result.rejections),
            )

        return _gen

    evidence: list = []
    for strategy, configs, gen_factory, strategy_key in (
        ("S1", FROZEN_S1_CONFIGS, _s1_gen_factory, PRODUCTION_S1_STRATEGY_KEY),
        ("S2", FROZEN_S2_CONFIGS, _s2_gen_factory, PRODUCTION_S2_STRATEGY_KEY),
    ):
        specs = tuple(
            ConfigSpec(config_id=c.config_id, generate_signals=gen_factory(c))
            for c in configs
        )
        result = run_walkforward(
            strategy=strategy,
            configs=specs,
            bars_1m=bars_1m,
            funding_sidecars=funding_sidecars,
            gap_ranges=gap_ranges,
            fold_schedule=fold_schedule,
        )
        summaries = summarize_config_attempts_for_h6(result)
        # Captain normalization-scope clarification (2026-07-17): capture
        # and evidence-building must consume the SAME normalized snapshot
        # -- never capture the raw ``summaries`` here while a separate,
        # later traversal (inside evidence-building) normalizes its own
        # copy independently.
        evidence.extend(
            _normalize_and_capture_summaries(
                summaries,
                strategy_key=strategy_key,
                experiment_id_by_key=experiment_id_by_key,
                full_campaign_hash=full_campaign_hash,
                campaign_run_id=campaign_run_id,
                capture_summaries_into=capture_summaries_into,
            )
        )
    return evidence


def _run_precheck_bridge_and_opt_in() -> None:
    """The pre-DB ``run`` gates, IN ORDER: H6 bridge importability FIRST,
    then explicit write opt-in. (Captain correction: the prior revision
    checked opt-in before the bridge; the required order is hash+24 IDs ->
    bridge importability -> opt-in -> DB policy -> registrations -> loader.)
    """
    try:
        _import_campaign_controller()
    except ImportError as exc:
        # The import error's own message is never echoed -- it can contain
        # local filesystem paths or other environment details.
        raise RunPreflightError("H6 registration bridge unavailable") from exc

    from app.services.research_db_write_guard import research_write_opt_in_enabled

    if not research_write_opt_in_enabled(os.environ.get(_ENV_WRITE_OPT_IN)):
        raise RunPreflightError(
            f"{_ENV_WRITE_OPT_IN} is not explicitly true -- refusing to run "
            "(default-off research-write opt-in)"
        )


async def _safe_rollback(session) -> None:
    """Independent controller audit correction (2026-07-17): a rollback
    attempted in response to an ALREADY-failed orchestration/commit must
    never itself raw-trace (which would mask the original failure's
    sanitized message with a second, unsanitized one, and could leak
    connection/query details). Any rollback failure is reported via a
    FIXED, sanitized message only."""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 -- deliberate: never let a rollback failure raw-trace or mask the original error
        print(
            "rollback failed after an orchestration/commit error -- see server-side logs "
            "for diagnostics",
            file=sys.stderr,
        )


def _is_empirical_success(report) -> bool:
    """H6 "accounting complete" is a DIFFERENT claim from "empirical
    success": a campaign can be fully recorded with every attempt
    crashed/rejected/timeout. Empirical success additionally requires every
    PRIMARY attempt to be ``status="completed"`` -- and must be retry-safe:
    ``report.status_counts`` aggregates ALL attempts including any future
    retries, so counting ``status_counts["completed"] == expected_total``
    alone is not enough (e.g. 23 completed primaries + 1 crashed primary + 1
    completed RETRY of that same experiment would satisfy that count without
    every primary having succeeded). Requiring ``total_attempts ==
    expected_total`` (no extra retry rows at all) AND ``retry_attempts == 0``
    closes that gap; only then does the completed-count check mean what it
    claims to mean.
    """
    return (
        report.verdict == "complete"
        and report.total_attempts == report.expected_total
        and report.retry_attempts == 0
        and report.status_counts.get("completed", 0) == report.expected_total
    )


def _run_empirical(*, expected_full_campaign_hash: str, campaign_run_id: str) -> int:
    """Thin sanitized wrapper around ``_run_empirical_impl`` (P1-C outer-
    boundary hardening, 2026-07-17): EVERY documented exit code (2/4/5/6/7/0)
    is produced by the impl's own explicit control flow -- this wrapper only
    exists to catch whatever the impl's own internal (narrower) sanitized
    try/excepts do NOT already cover: envelope construction/hash/derive/
    to_dict/experiment-id recomputation, and any non-``RunPreflightError``
    escaping ``_run_precheck_bridge_and_opt_in`` -- none of which sit inside
    any try today. A raw traceback anywhere in that surface could leak
    secrets/paths; this is the final backstop, never the primary mechanism
    (the impl's own narrower catches still produce more specific sanitized
    messages first).
    """
    try:
        return _run_empirical_impl(
            expected_full_campaign_hash=expected_full_campaign_hash,
            campaign_run_id=campaign_run_id,
        )
    except Exception:  # noqa: BLE001 -- deliberate: final sanitized backstop for the whole --run boundary, never re-raise
        print(
            "run failed with an unexpected error before a documented exit code could be "
            "produced -- see server-side logs for diagnostics",
            file=sys.stderr,
        )
        return 6


def _run_empirical_impl(
    *, expected_full_campaign_hash: str, campaign_run_id: str
) -> int:
    """Gate 1 (fresh hash + 24 IDs + campaign_run_id derivation check), then
    gates 2-3 (bridge, opt-in), then delegate gates 4-6 (policy,
    registration, attempt recording) to
    ``app.services.rob944_campaign_controller.run_full_campaign``. Exit 0
    requires BOTH H6 accounting-complete AND every primary attempt
    ``status="completed"`` -- accounting completeness alone is not
    empirical success.
    """
    envelope = build_production_frozen_campaign_envelope()
    actual_hash = envelope.full_campaign_hash()
    if actual_hash != expected_full_campaign_hash:
        # Sanitization pass (2026-07-17): never echo either hash value --
        # --expected-full-campaign-hash is caller-controlled at this trust
        # boundary; a fixed, field-only message only.
        print(
            "run preflight failed: full_campaign_hash mismatch -- refusing to run",
            file=sys.stderr,
        )
        return 4
    expected_campaign_run_id = _derive_primary_campaign_run_id(actual_hash)
    if campaign_run_id != expected_campaign_run_id:
        # Sanitization pass: never echo --campaign-run-id (caller-controlled)
        # or the derived expected value in the same message.
        print(
            "run preflight failed: --campaign-run-id does not match the value canonically "
            "derived from the frozen full-campaign hash -- an arbitrary UUID/timestamp is "
            "refused",
            file=sys.stderr,
        )
        return 4
    plain = envelope.to_dict()
    # Q4 addendum: the envelope-embedded experiment_ids (already part of what
    # actual_hash above committed to) are authoritative; cross-check against
    # an independent recomputation off the same rows before trusting them.
    experiment_ids = plain["experiment_ids"]
    if len(experiment_ids) != 24 or len(set(experiment_ids)) != 24:
        print(
            "run preflight failed: expected exactly 24 unique experiment IDs",
            file=sys.stderr,
        )
        return 4
    if list(experiment_ids) != _derive_experiment_ids(plain["rows"]):
        print(
            "run preflight failed: envelope-embedded experiment_ids diverged from an "
            "independent recomputation off the same rows -- refusing to run",
            file=sys.stderr,
        )
        return 4

    try:
        _run_precheck_bridge_and_opt_in()
    except RunPreflightError as exc:
        print(f"run preflight failed: {exc}", file=sys.stderr)
        return 2

    import asyncio

    # P1-C outer-boundary hardening (2026-07-17): setup (imports, controller/
    # policy resolution, spec construction) happens BEFORE any session
    # exists -- a failure here (e.g. a broken app.* import, a malformed
    # frozen row failing StrategyExperimentIdentity validation) must not
    # raw-trace either, even though nothing has touched the DB yet.
    try:
        from app.core.db import AsyncSessionLocal
        from app.schemas.research_backtest import StrategyExperimentIdentity
        from app.services.research_db_write_guard import default_research_db_policy
        from app.services.rob944_campaign_controller import (
            CampaignAccountingIncompleteError,
            CampaignBatchValidationError,
            CampaignHashDriftError,
            CampaignRunIdDerivationError,
            RunIdentityMismatchError,
        )

        controller = _import_campaign_controller()
        guard_policy = default_research_db_policy()
        specs = [
            StrategyExperimentIdentity(
                strategy_key=row["strategy_key"],
                strategy_version=row["strategy_version"],
                hypothesis=row["hypothesis"],
                **row["components"],
            )
            for row in plain["rows"]
        ]
    except Exception:  # noqa: BLE001 -- deliberate: pre-session setup failure must exit sanitized, never re-raise
        print(
            "run setup failed before any database session was opened -- see server-side logs "
            "for diagnostics",
            file=sys.stderr,
        )
        return 6

    captured_summaries: list = []

    def _build_attempt_evidence(experiment_id_by_key: dict) -> list:
        return _build_real_attempt_evidence(
            experiment_id_by_key,
            full_campaign_hash=actual_hash,
            campaign_run_id=campaign_run_id,
            capture_summaries_into=captured_summaries,
        )

    async def _do_run() -> int:
        # P1-C outer-boundary hardening: this outer try wraps the
        # ``async with`` statement ITSELF, not only the code inside it --
        # AsyncSessionLocal() construction and __aenter__/__aexit__ failures
        # are NOT covered by the inner try (which only starts once ``session``
        # is already bound) and must not raw-trace either. No rollback is
        # attempted here: if construction/__aenter__ failed there is no valid
        # session to roll back, and if __aexit__ failed the session is
        # already mid-close.
        try:
            return await _do_run_with_session()
        except Exception:  # noqa: BLE001 -- deliberate: session-boundary failure must exit sanitized, never re-raise
            print(
                "run orchestration failed to establish/close the database session -- see "
                "server-side logs for diagnostics",
                file=sys.stderr,
            )
            return 6

    async def _do_run_with_session() -> int:
        async with AsyncSessionLocal() as session:
            try:
                report = await controller.run_full_campaign(
                    session,
                    specs=specs,
                    actual_full_campaign_hash=actual_hash,
                    expected_full_campaign_hash=expected_full_campaign_hash,
                    campaign_run_id=campaign_run_id,
                    guard_opt_in_enabled=True,
                    guard_policy=guard_policy,
                    build_attempt_evidence=_build_attempt_evidence,
                    strategy_name="rob940_walkforward",
                    timeframe="mixed_5m_15m",
                    runner="rob944-cli",
                )
            except (
                CampaignHashDriftError,
                CampaignBatchValidationError,
                CampaignAccountingIncompleteError,
                CampaignRunIdDerivationError,
                RunIdentityMismatchError,
            ) as exc:
                # These are OUR OWN typed exceptions with fixed, safe-to-print
                # templates (hashes/experiment IDs only, never raw external
                # text) -- printing them is intentional. Captain controller
                # catch-classification correction (2026-07-17):
                # CampaignRunIdDerivationError/RunIdentityMismatchError are
                # controller validation failures too -- they must roll back
                # and return the documented code 4, not fall through to the
                # generic "unknown failure" code 6.
                # P1-C sanitization precision (2026-07-17): never print
                # str(exc) -- even though these 5 are OUR OWN typed
                # exceptions whose messages were just sanitized at the
                # controller layer, this boundary must not rely on that
                # invariant holding forever. type(exc).__name__ is a TRUSTED
                # value (one of exactly these 5 literal class names in this
                # except clause), safe to report; the exception's own
                # message text is not.
                await _safe_rollback(session)
                print(
                    f"run preflight/orchestration failed (rolled back): {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 4
            except Exception:  # noqa: BLE001 -- deliberate: an UNKNOWN failure must roll back and exit with a FIXED sanitized message, never re-raise (a raw traceback could contain secrets/paths)
                await _safe_rollback(session)
                print(
                    "run orchestration failed with an unexpected error (rolled back) -- "
                    "see server-side logs for diagnostics",
                    file=sys.stderr,
                )
                return 6

            # Independent controller audit correction (2026-07-17): commit
            # itself can fail (constraint violation, connection drop) -- it
            # must be inside a sanitized try too, never a bare call whose
            # exception (and any subsequent rollback failure) could raw-trace.
            try:
                await session.commit()
            except Exception:  # noqa: BLE001 -- deliberate: a commit failure must roll back and exit with a FIXED sanitized message, never re-raise
                await _safe_rollback(session)
                print(
                    "run commit failed after orchestration succeeded (rolled back) -- "
                    "see server-side logs for diagnostics",
                    file=sys.stderr,
                )
                return 7

            accounting_complete = report.verdict == "complete"
            empirical_success = _is_empirical_success(report)
            # Captain item D (2026-07-17): the operator-visible
            # train_selection_trace was missing the expectancy/profit-factor/
            # train_input_hash fields already bound into fold_evidence_hash
            # (see _summary_to_attempt_evidence) -- same NaN/Inf-safe sentinel
            # encoding for the float fields.
            from rob944_walkforward import _json_safe_float_or_sentinel

            print(
                json.dumps(
                    {
                        "verdict": report.verdict,
                        "accounting_complete": accounting_complete,
                        "empirical_success": empirical_success,
                        "actual_registrations": report.actual_registrations,
                        "primary_attempts": report.primary_attempts,
                        "status_counts": report.status_counts,
                        "full_campaign_hash": actual_hash,
                        "campaign_run_id": campaign_run_id,
                        # Fable report/register closure (2026-07-17): H5 is
                        # out of scope -- H4 hands off a readable contract
                        # here too (same top-level field as --plan).
                        "spec_deviations": plain["h3_fixed_constants"][
                            "s2_spec_deviations"
                        ],
                        "per_config_scenario_evidence": [
                            {
                                "strategy": s.strategy,
                                "config_id": s.config_id,
                                "status": s.status,
                                "reason_code": s.reason_code,
                                "scenarios": [
                                    {
                                        "scenario_name": row.scenario_name,
                                        "status": row.status,
                                        "reason_code": row.reason_code,
                                        "trade_count": row.trade_count,
                                        "artifact_hash": row.artifact_hash,
                                        "no_trade_reason_counts": row.no_trade_reason_counts,
                                    }
                                    for row in s.scenario_summaries
                                ],
                                # Captain item E: TRAIN selection rejection
                                # traces/counts are operator-visible too,
                                # not only OOS scenario counts.
                                "train_selection_trace": [
                                    {
                                        "fold_id": row.fold_id,
                                        "fold_selected_config_id": row.fold_selected_config_id,
                                        "eligible_symbols": list(row.eligible_symbols),
                                        "excluded_symbols": [
                                            list(pair) for pair in row.excluded_symbols
                                        ],
                                        "equal_weight_expectancy_bps": _json_safe_float_or_sentinel(
                                            row.equal_weight_expectancy_bps
                                        ),
                                        "pooled_expectancy_bps": _json_safe_float_or_sentinel(
                                            row.pooled_expectancy_bps
                                        ),
                                        "profit_factor": _json_safe_float_or_sentinel(
                                            row.profit_factor
                                        ),
                                        "train_input_hash": row.train_input_hash,
                                        "rejected": row.rejected,
                                        "rejection_reason": row.rejection_reason,
                                        "no_trade_reason_counts": row.no_trade_reason_counts,
                                    }
                                    for row in getattr(s, "fold_selection_trace", ())
                                ],
                            }
                            for s in captured_summaries
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if empirical_success else 5

    # P1-C outer-boundary hardening: asyncio.run itself (event loop
    # setup/teardown) is outside every try above -- must not raw-trace.
    try:
        return asyncio.run(_do_run())
    except Exception:  # noqa: BLE001 -- deliberate: event-loop failure must exit sanitized, never re-raise
        print(
            "run orchestration failed before any result could be produced -- see server-side "
            "logs for diagnostics",
            file=sys.stderr,
        )
        return 6


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_rob944_campaign",
        description="ROB-944 (H4, ROB-940) walk-forward campaign runner",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan",
        action="store_true",
        help="pure, no I/O -- prints the frozen campaign plan",
    )
    mode.add_argument(
        "--run", action="store_true", help="empirical, fail-closed gated execution"
    )
    parser.add_argument(
        "--expected-full-campaign-hash",
        default=None,
        help="[--run, required] operator-pinned expected full_campaign_hash",
    )
    parser.add_argument(
        "--campaign-run-id",
        default=None,
        help=(
            "[--run, required] MUST equal the value --plan reports as "
            "expected_campaign_run_id -- never an arbitrary UUID/timestamp"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(
        argv
    )  # --help/--version exit via SystemExit here, before any gate logic
    if args.plan:
        print(json.dumps(build_plan(), indent=2, sort_keys=True))
        return 0
    # args.run
    if not args.expected_full_campaign_hash:
        print(
            "run preflight failed: --expected-full-campaign-hash is required for --run",
            file=sys.stderr,
        )
        return 2
    if not args.campaign_run_id:
        print(
            "run preflight failed: --campaign-run-id is required for --run",
            file=sys.stderr,
        )
        return 2
    return _run_empirical(
        expected_full_campaign_hash=args.expected_full_campaign_hash,
        campaign_run_id=args.campaign_run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
