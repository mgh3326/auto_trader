"""ROB-944 (H4, ROB-940) — thin --run preflight/registration controller.

Wires the frozen H4 campaign envelope (research/nautilus_scalping.
rob944_frozen_campaign — never imported here, kept research-side pure) to
the already-merged ROB-846/946 registry bridge
(app.services.research_campaign_bridge) and DB write guard
(app.services.research_db_write_guard). This module owns NO independent
authorization decision: opt-in/policy checks are entirely
``research_campaign_bridge``'s own (``assert_research_write_authorized`` is
evaluated FIRST, inside ``register_campaign_experiments``/``record_attempt``,
before any spec/shape inspection — see that module).

``run_full_campaign`` is the full gated orchestration (captain blocking
correction, 2026-07-17). It NEVER leaves a partial "registered but no
attempts" write:

  1. identity/hash-drift check BEFORE any DB call;
  2. register all 24 experiments (H6 bridge's own opt-in/policy checks fire
     first, inside that call);
  3. PREDECLARE the resulting 24 ``(strategy_key, config_id) -> experiment_id``
     mapping and its expected ``experiment_id`` SET -- this is the trusted
     ground truth every later step is validated against;
  4. ONLY THEN invoke the caller-injected ``build_attempt_evidence`` (child
     execution) with that predeclared mapping;
  5. validate the FULL returned batch against the predeclared expected set
     BEFORE recording a single attempt: exact count/set match (no missing/
     extra/duplicate experiment_id), every attempt's
     ``campaign_run_id``/``retry_index=0`` matches, and every attempt carries
     exactly 3 non-null scenario artifact hashes/counts;
  6. record each attempt keyed by the TRUSTED expected ``experiment_id``
     (never the evidence's own self-reported ``attempt_key.experiment_id`` --
     using the evidence's own value there would make
     ``record_primary_attempt``'s cross-check tautological, since it would
     be comparing a value against itself);
  7. call the SAME ``campaign_completeness_report`` H6 already provides; if
     its verdict is anything other than ``"complete"``, raise BEFORE the
     caller can commit -- an accounting-incomplete campaign must never be
     persisted as if it were.

Accounting completeness is NOT the same as empirical success: a campaign
where H6 accounting is fully ``"complete"`` but every primary attempt's
``status`` is ``crashed``/``rejected``/``timeout`` is legitimate, COMMITTABLE
terminal evidence (a real, complete record of a failed run) — this module
returns normally for that case. Distinguishing "did the campaign actually
pass" from "is the record of it complete" is the CALLER's (CLI's)
responsibility, by inspecting ``report.status_counts``.

Boundary: no broker/order/fill/execution-ledger/scheduler/MCP import (see
tests/services/research/test_no_broker_import_guard.py-style AST coverage,
extended to this module in ROB-944's guard test).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.schemas.research_campaign_bridge import (
    AttemptEvidence,
    CampaignCompletenessReport,
)
from app.services.research_campaign_bridge import (
    campaign_completeness_report,
    record_attempt,
    register_campaign_experiments,
)
from app.services.research_db_write_guard import ResearchDbPolicy

__all__ = [
    "AttemptKeyExperimentMismatchError",
    "CampaignAccountingIncompleteError",
    "CampaignBatchValidationError",
    "CampaignHashDriftError",
    "CampaignRunIdDerivationError",
    "RunIdentityMismatchError",
    "run_full_campaign",
]
# Captain trust-boundary hole #1 (2026-07-17): the OLD public
# ``run_preflight_and_register``/``record_primary_attempt`` let a caller
# persist directly WITHOUT going through ``run_full_campaign``'s full
# validated-batch path (malformed hashes, forged run_identity, cross-status
# reason pairs, non-canonical scenario order/hashes all bypassed). They are
# now module-PRIVATE (``_run_preflight_and_register``/
# ``_record_primary_attempt``) -- ``run_full_campaign`` is the SOLE
# supported/exported DB-write entry point. They also each independently
# enforce the same format/contract checks now, as defense in depth for the
# (test-only) direct-call path that remains.

_EXPECTED_SCENARIO_COUNT = 3
# Captain BLOCKING controller audit (item 5): the exact canonical
# scenario ORDER, mirroring rob940_cost_model.COST_SCENARIOS -- this
# module never imports the research-side module (see module docstring),
# so this stable, frozen ROB-942 contract is a literal here, exactly like
# ``_EXPECTED_SCENARIO_COUNT`` already is.
_EXPECTED_SCENARIO_ORDER: tuple[str, ...] = ("base", "primary_stress", "upward_stress")
_HEX64_RE = re.compile(
    r"\A[0-9a-f]{64}\Z"
)  # \Z (not $) -- $ tolerates one trailing "\n" in non-MULTILINE mode

# Captain persistence-boundary correction (2026-07-17): a caller-injected
# ``allowed_reason_codes`` set would let ANY caller pass e.g.
# ``frozenset({"SECRET"})`` and have it persisted -- this controller must
# own its OWN closed allowlist, exactly like ``_EXPECTED_SCENARIO_ORDER``
# above, with no expansion hook. Literal duplication of
# ``rob944_walkforward``'s fixed reason codes (this module never imports
# that research-side module -- see module docstring boundary); scoped by
# ``status`` since a reason_code legal for "crashed" is never legal for
# "rejected" and vice versa.
_REASON_CHILD_EXECUTION_CRASHED = "child_execution_crashed"
_REASON_CHILD_EXECUTION_TIMEOUT = "child_execution_timeout"
_REASON_GLOBAL_CORPUS_LOAD_FAILED = "global_corpus_load_failed"
_REASON_DATA_GAP_IN_POSITION = "rejected:data_gap_in_position"
_REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS = "insufficient_train_evidence_all_folds"

_ALLOWED_REASON_CODES_BY_STATUS: dict[str, frozenset[str]] = {
    "completed": frozenset(),  # must be None -- checked separately, never a code
    "crashed": frozenset(
        {_REASON_CHILD_EXECUTION_CRASHED, _REASON_GLOBAL_CORPUS_LOAD_FAILED}
    ),
    "timeout": frozenset({_REASON_CHILD_EXECUTION_TIMEOUT}),
    "rejected": frozenset(
        {_REASON_DATA_GAP_IN_POSITION, _REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS}
    ),
}


class CampaignHashDriftError(ValueError):
    """The caller's campaign specs do not derive the pinned expected
    full-campaign hash -- refused BEFORE any registration/DB call."""


class AttemptKeyExperimentMismatchError(ValueError):
    """``AttemptEvidence.attempt_key.experiment_id``/``campaign_run_id``
    does not match the identity this call is explicitly recording under --
    refused BEFORE delegating to the registry (captain audit item 4)."""


class CampaignBatchValidationError(ValueError):
    """The returned attempt-evidence batch does not exactly match the
    PREDECLARED expected experiment_id set (missing/extra/duplicate), does
    not name the expected ``campaign_run_id``, is not ``retry_index=0``, or
    is missing a required scenario artifact hash -- refused BEFORE any
    attempt is recorded."""


class CampaignRunIdDerivationError(ValueError):
    """``campaign_run_id`` is not the value canonically DERIVED from
    ``actual_full_campaign_hash`` (an arbitrary UUID/timestamp/operator typo),
    or either hash is not a well-formed lowercase 64-hex digest -- refused
    BEFORE any registration/child-execution/DB call (captain direct-controller
    identity gate, 2026-07-17): this app-side controller is the actual DB
    persistence boundary and must not rely solely on the CLI's own (separate)
    copy of this same check."""


class RunIdentityMismatchError(ValueError):
    """``AttemptEvidence.run_identity`` does not equal the value canonically
    RE-DERIVED at this DB persistence boundary from trusted lineage facts
    (full_campaign_hash, campaign_run_id, predeclared strategy_key/config_id,
    experiment_id, retry_index, this evidence's own status, and its
    format-checked fold_evidence_hash) -- refused BEFORE any
    ``record_attempt`` call (independent controller audit correction,
    2026-07-17). A forged/tampered/arbitrary 64-hex ``run_identity`` was
    previously accepted as long as its FORMAT looked like a hash; this
    closes that gap for every field this module can independently
    reconstruct. ``fold_evidence_hash``/scenario ``artifact_hash`` remain
    format-checked only -- this module has no access to the raw scenario
    evidence they were derived from, so it cannot re-derive THEM, only
    verify ``run_identity`` was built correctly ON TOP of whichever
    fold_evidence_hash this evidence carries."""


class CampaignAccountingIncompleteError(ValueError):
    """``campaign_completeness_report``'s verdict is not ``"complete"``
    after recording every predeclared attempt -- something is structurally
    wrong (missing/extra/mismatch/duplicate-or-gap). Raised so the caller
    never commits an accounting-incomplete campaign."""


async def _run_preflight_and_register(
    session: AsyncSession,
    *,
    specs: list[StrategyExperimentIdentity],
    actual_full_campaign_hash: str,
    expected_full_campaign_hash: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
) -> list[ResearchStrategyExperiment]:
    """Fail closed BEFORE any DB call if the identity hash has drifted, then
    delegate registration to the existing, already-merged bridge (which
    itself checks write-authorization FIRST, then exactly-24/unique specs).

    Module-private (captain trust-boundary hole #1, 2026-07-17): this used
    to be a PUBLIC export a caller could use to bypass ``run_full_campaign``'s
    full validated-batch path entirely. ``run_full_campaign`` is the sole
    supported/exported entry point; this remains only as an internal
    building block (and a direct-call target for narrowly-scoped tests).
    """
    if not _HEX64_RE.match(actual_full_campaign_hash) or not _HEX64_RE.match(
        expected_full_campaign_hash
    ):
        raise CampaignHashDriftError(
            "actual_full_campaign_hash/expected_full_campaign_hash must both be well-formed "
            "lowercase 64-hex digests -- refusing registration/write"
        )
    if actual_full_campaign_hash != expected_full_campaign_hash:
        # Sanitization addendum (2026-07-17): never echo either hash value
        # -- both are caller-controlled at this trust boundary.
        raise CampaignHashDriftError(
            "full_campaign_hash mismatch -- refusing registration/write"
        )
    return await register_campaign_experiments(
        session,
        specs=specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )


async def _record_primary_attempt(
    session: AsyncSession,
    *,
    experiment_id: str,
    campaign_run_id: str,
    evidence: AttemptEvidence,
    strategy_name: str,
    timeframe: str,
    runner: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
) -> ResearchBacktestRun:
    """Record one attempt (typically the primary, ``retry_index=0``) after
    asserting ``evidence.attempt_key`` genuinely names the identity this call
    is recording under -- checked BEFORE any delegation to ``record_attempt``.

    ``experiment_id`` MUST come from a source the caller trusts independently
    of ``evidence`` (e.g. a registration result or a predeclared expected
    set) -- passing ``evidence.attempt_key.experiment_id`` back as
    ``experiment_id`` makes this check compare a value against itself and
    catches nothing; ``run_full_campaign`` never does that.

    Module-private (captain trust-boundary hole #1, 2026-07-17) -- also now
    independently re-checks the closed status/reason contract and hash
    formats as defense in depth for the direct-call path, since it is no
    longer guaranteed every caller went through ``_validate_attempt_batch``
    first.
    """
    # Captain persistence-boundary correction + delta sanitization pass
    # (2026-07-17): never echo EITHER side's ID/hash value -- not
    # evidence.attempt_key's own (untrusted, evidence-self-reported) value,
    # and not the "expected" experiment_id/campaign_run_id this call itself
    # received, for consistency with every other validation message in this
    # module (field/count-only, never interpolated raw identifiers).
    if evidence.attempt_key.experiment_id != experiment_id:
        raise AttemptKeyExperimentMismatchError(
            "AttemptKey.experiment_id does not match the expected experiment_id this call "
            "is recording under"
        )
    if evidence.attempt_key.campaign_run_id != campaign_run_id:
        raise AttemptKeyExperimentMismatchError(
            "AttemptKey.campaign_run_id does not match the expected campaign_run_id this "
            "call is recording under"
        )
    _assert_single_attempt_format_and_contract(evidence, experiment_id=experiment_id)
    # TOCTOU correction (2026-07-17): persist a DEEP, decoupled snapshot --
    # never the caller's own (potentially still-mutable, still-referenced)
    # object -- so a caller cannot mutate ``evidence`` after this point and
    # have the mutation reach ``record_attempt``.
    snapshot = evidence.model_copy(deep=True)
    return await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=snapshot,
        strategy_name=strategy_name,
        timeframe=timeframe,
        runner=runner,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )


def _assert_hex64(value: str | None, *, field: str) -> None:
    """Captain BLOCKING controller audit (item 5): every persisted hash
    field must be a lowercase 64-hex SHA-256 digest before ``record_attempt``
    is ever called -- catches a malformed/truncated/uppercase/non-hash value
    (e.g. a caller accidentally passing a raw label or a 32-char digest)
    fail-closed at this trust boundary.

    Captain P1-C sanitization precision (2026-07-17): never interpolate
    ``experiment_id`` (or any other caller-controlled identifier) into this
    message -- field name only, even though ``experiment_id`` at most call
    sites here is itself a TRUSTED value; the recovered contract at this
    persistence trust boundary is field/count-only, uniformly, regardless
    of per-call trust reasoning."""
    if value is None or not _HEX64_RE.match(value):
        raise CampaignBatchValidationError(
            f"{field} must be a lowercase 64-hex SHA-256 digest"
        )


def _assert_single_attempt_format_and_contract(
    evidence: AttemptEvidence, *, experiment_id: str
) -> None:
    """The per-entry checks that do NOT require extra caller context
    (scenario count/order, hex64 hash formats, exact status/reason
    contract, nonnegative trade counts) -- factored out so BOTH
    ``_validate_attempt_batch`` (the batch path) and ``_record_primary_attempt``
    (the direct-call path, now private but still independently defended)
    apply the IDENTICAL rules. Does NOT include ``run_identity``
    re-derivation, which needs ``full_campaign_hash``/``strategy_key``/
    ``config_id`` this function does not have."""
    # Captain P1-C sanitization precision (2026-07-17): none of these
    # messages interpolate ``experiment_id`` (dropped from every branch
    # below) -- field/count-only at this persistence trust boundary,
    # uniformly, even for the calls where ``experiment_id`` happens to be a
    # trusted value. ``evidence.status``/``scenario.scenario_name`` remain
    # safe to report: both are Literal-typed (closed enum) at the pydantic
    # layer, not raw free-form caller text.
    if len(evidence.scenario_evidence) != _EXPECTED_SCENARIO_COUNT:
        raise CampaignBatchValidationError(
            f"attempt does not have exactly {_EXPECTED_SCENARIO_COUNT} scenario_evidence rows"
        )
    scenario_names = tuple(s.scenario_name for s in evidence.scenario_evidence)
    if scenario_names != _EXPECTED_SCENARIO_ORDER:
        raise CampaignBatchValidationError(
            "attempt scenario_evidence must be in the exact canonical order"
        )
    if evidence.status == "completed":
        if evidence.reason_code is not None:
            raise CampaignBatchValidationError(
                "attempt has status='completed' but a non-null reason_code -- refusing to persist"
            )
    elif evidence.reason_code not in _ALLOWED_REASON_CODES_BY_STATUS[evidence.status]:
        raise CampaignBatchValidationError(
            f"attempt has a reason_code not permitted for status={evidence.status!r} under "
            "the closed allowlist -- refusing to persist"
        )
    _assert_hex64(evidence.fold_evidence_hash, field="fold_evidence_hash")
    _assert_hex64(evidence.run_identity, field="run_identity")
    for scenario in evidence.scenario_evidence:
        if scenario.trade_count < 0:
            raise CampaignBatchValidationError(
                f"attempt scenario {scenario.scenario_name!r} has a negative trade_count"
            )
        _assert_hex64(
            scenario.artifact_hash,
            field=f"scenario[{scenario.scenario_name}].artifact_hash",
        )


def _validate_attempt_batch(
    attempts: list[AttemptEvidence],
    *,
    expected_experiment_ids: frozenset[str],
    campaign_run_id: str,
    full_campaign_hash: str,
    key_by_experiment_id: dict[str, tuple[str, str]],
) -> dict[str, AttemptEvidence]:
    """Validate the FULL returned batch against the PREDECLARED expected
    experiment_id set, BEFORE any attempt is recorded. Returns an
    ``experiment_id -> AttemptEvidence`` map built from the validated batch,
    keyed by the id the evidence itself claims -- which, having just been
    proven to exactly match ``expected_experiment_ids``, is safe for the
    caller to iterate by the TRUSTED expected id instead.

    Captain BLOCKING controller audit (item 5) + persistence-boundary
    correction: this is the PERSISTENCE TRUST BOUNDARY -- ``status``/
    ``scenario_name`` are already Literal-typed at the pydantic layer
    (closed by construction), but ``reason_code`` is a free string, scenario
    ORDER is not pydantic-enforced (only membership is), and hash fields
    carry no format constraint. All four are checked here, fail-closed,
    before any ``record_attempt`` call, against this module's OWN closed
    ``_ALLOWED_REASON_CODES_BY_STATUS`` -- never a caller-injected allowlist
    (a direct caller could otherwise pass ``frozenset({"SECRET"})`` and have
    it persisted).

    Every raised message reports counts/field names/TRUSTED expected
    identifiers only -- never a raw value taken from the untrusted
    ``attempts`` batch itself (that batch comes from a caller-injected
    ``build_attempt_evidence`` callback and must never be treated as safe
    to echo back into an exception message, which could be logged/printed/
    surfaced to an operator terminal).
    """
    seen_ids = [a.attempt_key.experiment_id for a in attempts]
    if len(seen_ids) != len(expected_experiment_ids):
        raise CampaignBatchValidationError(
            f"expected exactly {len(expected_experiment_ids)} attempt evidence entries, "
            f"got {len(seen_ids)}"
        )
    if len(set(seen_ids)) != len(seen_ids):
        duplicate_count = len(seen_ids) - len(set(seen_ids))
        raise CampaignBatchValidationError(
            f"the returned attempt batch contains {duplicate_count} duplicate experiment_id "
            "entry/entries"
        )
    actual_set = set(seen_ids)
    if actual_set != set(expected_experiment_ids):
        # Captain P1-C sanitization precision (2026-07-17): count-only, even
        # for the "missing" side (a subset of OUR OWN predeclared set) --
        # never echo the actual id list in either direction.
        missing_count = len(set(expected_experiment_ids) - actual_set)
        extra_count = len(actual_set - set(expected_experiment_ids))
        raise CampaignBatchValidationError(
            "returned attempt batch does not match the predeclared expected experiment_id "
            f"set ({missing_count} missing, {extra_count} unexpected id(s) present)"
        )

    by_id: dict[str, AttemptEvidence] = {}
    for evidence in attempts:
        # Safe to report from here on: actual_set == expected_experiment_ids
        # was just proven above, so every evidence.attempt_key.experiment_id
        # in this loop is confirmed to be one of OUR OWN predeclared ids.
        experiment_id = evidence.attempt_key.experiment_id
        # Captain P1-C sanitization precision (2026-07-17): field/count-only
        # here too -- never echo experiment_id/campaign_run_id, even though
        # both are trusted-derived at this point in the batch loop.
        if evidence.attempt_key.campaign_run_id != campaign_run_id:
            raise CampaignBatchValidationError(
                "attempt does not have the expected campaign_run_id"
            )
        if evidence.attempt_key.retry_index != 0:
            raise CampaignBatchValidationError(
                "attempt does not have retry_index=0 (only primary attempts here)"
            )
        if len(evidence.scenario_evidence) != _EXPECTED_SCENARIO_COUNT:
            raise CampaignBatchValidationError(
                f"attempt does not have exactly {_EXPECTED_SCENARIO_COUNT} scenario_evidence rows"
            )
        # scenario_name is Literal-typed (closed enum) -- safe to report as-is.
        _assert_single_attempt_format_and_contract(
            evidence, experiment_id=experiment_id
        )
        strategy_key, config_id = key_by_experiment_id[experiment_id]
        expected_run_identity = _derive_expected_run_identity(
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
            strategy_key=strategy_key,
            experiment_id=experiment_id,
            retry_index=evidence.attempt_key.retry_index,
            config_id=config_id,
            status=evidence.status,
            fold_evidence_hash=evidence.fold_evidence_hash,
        )
        if evidence.run_identity != expected_run_identity:
            # Captain P1-C sanitization precision (2026-07-17): the last
            # missed raw-ID seam -- never interpolate experiment_id here
            # either, even though it is trusted-derived at this point in the
            # loop (actual_set == expected_experiment_ids already proven).
            raise RunIdentityMismatchError(
                "attempt has a run_identity that does not match the value canonically "
                "re-derived from trusted lineage facts -- refusing to persist"
            )
        # TOCTOU correction (2026-07-17): store a DEEP, decoupled snapshot,
        # taken immediately after this entry passed every check -- never
        # the caller's own (still-mutable, still-referenced) object. A
        # caller mutating its original ``evidence`` after this call returns
        # can never affect what actually gets recorded downstream.
        by_id[experiment_id] = evidence.model_copy(deep=True)
    return by_id


def _derive_expected_campaign_run_id(full_campaign_hash: str) -> str:
    """The SAME deterministic derivation the CLI uses (``research_contracts.
    canonical_hash`` directly -- the pure, generic authority; never the
    ``app.services.research_canonical_hash`` wrapper, and never
    ``rob944_walkforward``/``rob944_frozen_campaign``, both of which stay
    research-side-only). Duplicated here deliberately: this controller is
    the actual DB persistence boundary and must not rely solely on the
    CLI's own (separate) copy of this same check.

    Fable Q1 FINAL (orch-fable-answer-rob944b-20260717.md, 2026-07-17) --
    exact recipe (precision correction, 2026-07-17: this does NOT base64-
    encode ``full_campaign_hash``'s own bytes directly): canonical SHA-256
    of the payload ``{full_campaign_hash, kind: "primary_run"}`` produces a
    NEW 32-byte digest; THAT digest's full 32 raw bytes (not the input
    hash's bytes, and not that digest's 64-hex string form) are re-encoded
    as UNPADDED URL-safe base64 (43 chars) -- NOT hex, and NOT a truncation
    of anything (full 256-bit entropy of the new digest is preserved).
    Required so that
    ``AttemptKey.idempotency_key()`` (``campaign_run_id:experiment_id:retry_index``)
    fits ``research.backtest_runs.trial_idempotency_key VARCHAR(128)``
    WITHOUT any H6 schema/source mutation: ``"rob944-primary-"`` (15) + 43 =
    58-char campaign_run_id; 58 + 1 + 64 (experiment_id, H6's own unchanged
    full-hex format) + 1 + 1 ("0") = 125 <= 128. This EXACT derivation
    (identical stdlib base64 recipe) must match
    ``run_rob944_campaign._derive_primary_campaign_run_id`` bit-for-bit.
    """
    import base64

    from research_contracts.canonical_hash import canonical_sha256

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    digest_bytes = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(digest_bytes).rstrip(b"=").decode("ascii")
    return f"rob944-primary-{suffix}"


def _derive_expected_run_identity(
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    strategy_key: str,
    experiment_id: str,
    retry_index: int,
    config_id: str,
    status: str,
    fold_evidence_hash: str,
) -> str:
    """Independent controller audit correction (2026-07-17): re-derive
    ``run_identity`` at the DB boundary from ONLY trusted values -- must
    match ``run_rob944_campaign._summary_to_attempt_evidence``'s payload
    shape EXACTLY (the redundant short-slug ``"strategy"`` field was
    dropped from that payload specifically so it is fully reconstructible
    here from ``strategy_key``/``config_id`` alone, both already known via
    the predeclared ``experiment_id_by_key`` mapping)."""
    from research_contracts.canonical_hash import canonical_sha256

    return canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "strategy_key": strategy_key,
            "experiment_id": experiment_id,
            "retry_index": retry_index,
            "config_id": config_id,
            "status": status,
            "fold_evidence_hash": fold_evidence_hash,
        }
    )


async def run_full_campaign(
    session: AsyncSession,
    *,
    specs: list[StrategyExperimentIdentity],
    actual_full_campaign_hash: str,
    expected_full_campaign_hash: str,
    campaign_run_id: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
    build_attempt_evidence: Callable[
        [dict[tuple[str, str], str]], list[AttemptEvidence]
    ],
    strategy_name: str,
    timeframe: str,
    runner: str,
) -> CampaignCompletenessReport:
    """The full gated orchestration -- see module docstring for the exact
    7-step order. Raises (never commits) on hash drift, campaign_run_id
    derivation mismatch, batch-validation failure, or accounting
    incompleteness; the caller (CLI) is responsible for calling
    ``session.commit()`` only after this returns successfully, and for
    rolling back on any exception.

    Captain direct-controller identity gate (2026-07-17): both hashes must
    be well-formed lowercase 64-hex digests, and ``campaign_run_id`` must be
    the value canonically DERIVED from ``actual_full_campaign_hash`` -- all
    checked here, BEFORE ``register_campaign_experiments``/any child
    execution/any DB write, independent of whatever the CLI already checked
    (defense in depth at the actual persistence boundary).
    """
    if not _HEX64_RE.match(actual_full_campaign_hash) or not _HEX64_RE.match(
        expected_full_campaign_hash
    ):
        raise CampaignHashDriftError(
            "actual_full_campaign_hash/expected_full_campaign_hash must both be well-formed "
            "lowercase 64-hex digests -- refusing registration/write"
        )
    if actual_full_campaign_hash != expected_full_campaign_hash:
        # Sanitization addendum (2026-07-17): never echo either hash value
        # -- both are caller-controlled at this trust boundary.
        raise CampaignHashDriftError(
            "full_campaign_hash mismatch -- refusing registration/write"
        )
    expected_campaign_run_id = _derive_expected_campaign_run_id(
        actual_full_campaign_hash
    )
    if campaign_run_id != expected_campaign_run_id:
        # Captain P1-C sanitization precision (2026-07-17): never echo
        # either campaign_run_id value in this message, even the OUR-OWN
        # derived "expected" one -- field/count-only uniformly at this
        # persistence trust boundary.
        raise CampaignRunIdDerivationError(
            "campaign_run_id must be the value canonically derived from the frozen "
            "full-campaign hash -- an arbitrary UUID/timestamp/operator typo is refused"
        )

    registered = await register_campaign_experiments(
        session,
        specs=specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    await session.flush()

    # Step 3: PREDECLARE the expected (strategy_key, config_id) ->
    # experiment_id mapping and its id SET -- the trusted ground truth for
    # everything that follows. Child execution (step 4) happens AFTER this.
    experiment_id_by_key: dict[tuple[str, str], str] = {}
    for spec, row in zip(specs, registered, strict=True):
        config_id = (
            spec.params.get("config_id") if isinstance(spec.params, dict) else None
        )
        experiment_id_by_key[(spec.strategy_key, config_id)] = row.experiment_id
    expected_experiment_ids = frozenset(experiment_id_by_key.values())
    if len(expected_experiment_ids) != len(registered):
        raise CampaignBatchValidationError(
            "registered experiment_ids are not unique -- cannot predeclare attempt keys"
        )
    # Reverse mapping -- lets _validate_attempt_batch re-derive/verify each
    # attempt's run_identity from ONLY trusted (strategy_key, config_id)
    # facts, never the evidence's own self-reported values.
    key_by_experiment_id = {v: k for k, v in experiment_id_by_key.items()}

    # Step 4: child execution -- ONLY NOW, against the predeclared mapping.
    attempts = build_attempt_evidence(experiment_id_by_key)

    # Step 5: validate the FULL batch against the predeclared expected set
    # BEFORE recording a single attempt.
    evidence_by_id = _validate_attempt_batch(
        attempts,
        expected_experiment_ids=expected_experiment_ids,
        campaign_run_id=campaign_run_id,
        full_campaign_hash=actual_full_campaign_hash,
        key_by_experiment_id=key_by_experiment_id,
    )

    # Step 6: record each attempt keyed by the TRUSTED expected id, in a
    # deterministic (sorted) order -- never by the evidence's own
    # self-reported id, which would make the cross-check tautological.
    for experiment_id in sorted(expected_experiment_ids):
        evidence = evidence_by_id[experiment_id]
        await _record_primary_attempt(
            session,
            experiment_id=experiment_id,
            campaign_run_id=campaign_run_id,
            evidence=evidence,
            strategy_name=strategy_name,
            timeframe=timeframe,
            runner=runner,
            guard_opt_in_enabled=guard_opt_in_enabled,
            guard_policy=guard_policy,
        )
    await session.flush()

    # Step 7: accounting completeness is the final gate before the caller
    # may commit. It is NOT the same as empirical success -- see docstring.
    report = await campaign_completeness_report(
        session, campaign_run_id=campaign_run_id, expected_specs=specs
    )
    if report.verdict != "complete":
        # Captain P1-C sanitization precision (2026-07-17): never interpolate
        # the actual (non-"complete") verdict text into this message -- H6's
        # own injected/derived verdict category is still not safe to echo
        # verbatim at this trust boundary.
        raise CampaignAccountingIncompleteError(
            "campaign_completeness_report verdict is not 'complete' after recording every "
            "predeclared attempt -- refusing to let the caller commit an accounting-incomplete "
            "campaign"
        )
    return report
