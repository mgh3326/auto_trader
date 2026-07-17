"""ROB-944 (H4, ROB-940) — CLI --plan / --run boundary tests.

``--plan`` must be pure (no network/DB/write/env-mutation/child-run), byte-
identical across repeated invocations and PYTHONHASHSEED, and must emit the
full-campaign hash, fold schedule, 24 experiment IDs, expected logical
attempts=24, cost scenarios, and source/data/signal hashes -- derived via the
PURE ``research_contracts`` authority (no ``app.*`` import on this path).
``--run``'s PRE-DB gate ordering (fresh-hash-vs-operator-pin, then write
opt-in, then H6 bridge availability) must fail closed before any DB/network/
child-execution touches occur; ``--plan``/``--run`` are mutually exclusive
flags, and ``--version`` never touches any gate logic.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_rob944_campaign as cli

_SCRIPT = Path(__file__).resolve().parents[1] / "run_rob944_campaign.py"


def test_build_plan_is_deterministic_across_repeated_calls():
    plan1 = cli.build_plan()
    plan2 = cli.build_plan()
    assert plan1 == plan2


def test_build_plan_contains_required_fields():
    plan = cli.build_plan()
    assert len(plan["full_campaign_hash"]) == 64
    assert plan["fold_count"] == 8
    assert len(plan["fold_schedule"]) == 8
    assert len(plan["experiment_ids"]) == 24
    assert len(set(plan["experiment_ids"])) == 24
    assert plan["expected_logical_attempts"] == 24
    assert set(plan["cost_scenarios"]) == {"base", "primary_stress", "upward_stress"}
    assert plan["primary_scenario"] == "primary_stress"
    assert plan["dataset_manifest_hash"]
    assert plan["signal_manifest_hash"]
    assert plan["scenario_execution"] == "independent_run_with_fresh_state"


# ---------------------------------------------------------------------------
# Captain config/plan audit (2026-07-18): four pre-freeze blockers --
# (A) explicit S1/S2 source SHA-256s + an independently-auditable lossless
# canonical payload; (B) byte-exact (not text-mode) strategy source
# provenance (covered in test_rob944_frozen_campaign.py); (C) pure --plan
# sentinel/snapshot proof (zero DB/network/file-write/os.environ/child
# execution, no process-global sys.path mutation); (D) these are the plan
# output-contract tests catching the above omissions.
# ---------------------------------------------------------------------------


def test_plan_full_campaign_payload_independently_reproduces_full_campaign_hash():
    """Item A: full_campaign_payload must be the EXACT structure
    full_campaign_hash was computed from -- canonical_sha256(payload) ==
    full_campaign_hash, independently verifiable by any external auditor
    without trusting the CLI's own self-check."""
    from research_contracts.canonical_hash import canonical_sha256

    plan = cli.build_plan()
    assert canonical_sha256(plan["full_campaign_payload"]) == plan["full_campaign_hash"]


def test_plan_emits_s1_and_s2_source_sha256_matching_actual_committed_files():
    """Item A: s1_source_sha256/s2_source_sha256 must be the ACTUAL
    byte-derived SHA-256 of the real committed S1/S2 source files -- not
    merely present, but independently reproducible from the raw file
    bytes."""
    import hashlib

    from rob944_frozen_campaign import _S1_SOURCE_PATH, _S2_SOURCE_PATH

    plan = cli.build_plan()
    assert (
        plan["s1_source_sha256"]
        == hashlib.sha256(_S1_SOURCE_PATH.read_bytes()).hexdigest()
    )
    assert (
        plan["s2_source_sha256"]
        == hashlib.sha256(_S2_SOURCE_PATH.read_bytes()).hexdigest()
    )
    assert plan["s1_source_sha256"] != plan["s2_source_sha256"]


def test_plan_full_campaign_payload_contains_h1_h3_hashes_24_rows_and_policy_components():
    """Item A: the emitted payload must actually carry H1's
    dataset_manifest_hash, H3's signal_manifest_hash, all 24 rows/
    experiment_ids, and the material policy components (funding_pit_policy/
    data_gap_policy/posture) -- not a curated subset that omits exactly
    what would be needed to audit the hash."""
    plan = cli.build_plan()
    payload = plan["full_campaign_payload"]
    assert payload["dataset_manifest_hash"] == plan["dataset_manifest_hash"]
    assert payload["signal_manifest_hash"] == plan["signal_manifest_hash"]
    assert len(payload["rows"]) == 24
    assert len(payload["experiment_ids"]) == 24
    assert len(set(payload["experiment_ids"])) == 24
    assert payload["funding_pit_policy"] == plan["funding_pit_policy"]
    assert payload["data_gap_policy"] == plan["data_gap_policy"]
    assert payload["posture"] == plan["posture"]


def test_plan_never_touches_the_network(monkeypatch):
    """Item C: --plan must never open a network socket.

    Every purity test in this section explicitly ``monkeypatch.undo()``s
    BEFORE returning (not merely relying on the fixture's own end-of-test
    teardown) -- pytest's own internal bookkeeping (e.g. writing
    ``PYTEST_CURRENT_TEST`` into ``os.environ`` at teardown) runs AFTER
    the test function body but can still observe an un-reverted patch,
    which would misattribute pytest's own activity to ``build_plan``."""
    import socket

    def _boom(*args, **kwargs):
        raise AssertionError("build_plan must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    try:
        cli.build_plan()  # must not raise
    finally:
        monkeypatch.undo()


def test_plan_never_mutates_os_environ(monkeypatch):
    """Item C: --plan must never mutate os.environ.

    Captain test precision (2026-07-18): a bare ``__setitem__`` sentinel
    alone would stay green if build_plan regressed to
    ``os.environ.pop``/``.update``/``.setdefault``/``.clear``/``del
    os.environ[...]`` instead -- a full ``dict(os.environ)`` before/after
    SNAPSHOT comparison catches any mutation regardless of which method
    performed it, non-vacuously."""
    import os

    def _boom(self, key, value):
        raise AssertionError(
            f"build_plan must never mutate os.environ (attempted key={key!r})"
        )

    monkeypatch.setattr(type(os.environ), "__setitem__", _boom)
    before = dict(os.environ)
    try:
        cli.build_plan()
    finally:
        monkeypatch.undo()
    after = dict(os.environ)
    assert before == after


def test_plan_never_spawns_a_child_process(monkeypatch):
    """Item C: --plan must never spawn a child process."""
    import os
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError("build_plan must never spawn a child process")

    monkeypatch.setattr(subprocess.Popen, "__init__", _boom)
    monkeypatch.setattr(os, "fork", _boom, raising=False)
    monkeypatch.setattr(os, "posix_spawn", _boom, raising=False)
    try:
        cli.build_plan()
    finally:
        monkeypatch.undo()


def test_plan_never_opens_a_file_for_writing(monkeypatch, tmp_path):
    """Item C: --plan legitimately READS pinned fixture/source files, but
    must never open anything in a write/append/create/update mode.

    Captain test precision (2026-07-18): ``io.open`` and ``builtins.open``
    are the SAME object initially (``io.open is builtins.open`` at
    interpreter startup), but they are two INDEPENDENT attribute bindings
    (on the ``io`` module and the ``builtins`` module respectively) --
    patching only ``builtins.open`` does NOT affect ``io.open``, and
    ``pathlib.Path.open()``/``write_text()``/``write_bytes()`` all call
    ``io.open(...)`` directly (an attribute lookup on the ``io`` module),
    never ``builtins.open``. A build_plan regression to
    ``Path.write_text(...)`` would have stayed GREEN under a
    ``builtins.open``-only guard -- verified empirically before this fix.
    Both are patched here; read-only modes remain allowed."""
    import builtins
    import io

    original_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in mode for ch in ("w", "a", "x", "+")):
            raise AssertionError(
                f"build_plan must never open a file for writing (mode={mode!r})"
            )
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(io, "open", _guarded_open)
    try:
        # Non-vacuous proof (2026-07-18): the guard itself must actually
        # fire for a real Path.write_text() call, under the SAME patch
        # build_plan() runs under -- proving this is a real guard, not a
        # vacuously-passing no-op.
        with pytest.raises(AssertionError, match="must never open a file for writing"):
            (tmp_path / "probe.txt").write_text("should never reach disk")
        cli.build_plan()
    finally:
        monkeypatch.undo()


def test_plan_never_mutates_process_global_sys_path():
    """Item C: --plan (and merely importing/using this module) must never
    mutate process-global sys.path -- the module-level bootstrap mutation
    this file used to have at import time has been removed entirely."""
    before = list(sys.path)
    cli.build_plan()
    after = list(sys.path)
    assert before == after


def test_plan_never_imports_db_module_in_a_clean_subprocess():
    """Item C: --plan must cause zero DB touch -- proven in a CLEAN
    subprocess (avoids cross-test sys.modules pollution from other tests
    in this same file that legitimately exercise the --run path and DO
    import app.core.db)."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SCRIPT.parent)!r})\n"
        "import run_rob944_campaign as cli\n"
        "cli.build_plan()\n"
        "assert 'app.core.db' not in sys.modules, "
        "'app.core.db was imported during --plan'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPT.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_main_plan_flag_prints_valid_stable_json_to_stdout(capsys):
    exit_code = cli.main(["--plan"])
    assert exit_code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert len(parsed["experiment_ids"]) == 24


def test_plan_and_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["--plan", "--run"])


def test_neither_plan_nor_run_fails_closed():
    with pytest.raises(SystemExit):
        cli.main([])


def test_plan_flag_is_byte_identical_across_pythonhashseed_subprocess():
    outputs = []
    for seed in ("0", "12345", "999999"):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--plan"],
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            cwd=str(_SCRIPT.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


def test_version_flag_never_touches_run_gates(monkeypatch):
    called = {"gate": False}

    def _boom(*a, **k):
        called["gate"] = True
        raise AssertionError("--version must never reach run-gate logic")

    monkeypatch.setattr(cli, "_run_precheck_bridge_and_opt_in", _boom)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert called["gate"] is False


def test_help_never_touches_run_gates(monkeypatch):
    called = {"gate": False}

    def _boom(*a, **k):
        called["gate"] = True
        raise AssertionError("--help must never reach run-gate logic")

    monkeypatch.setattr(cli, "_run_precheck_bridge_and_opt_in", _boom)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    assert called["gate"] is False


def test_run_without_expected_hash_fails_closed_before_any_gate(monkeypatch):
    called = {"gate": False}
    monkeypatch.setattr(
        cli, "_run_precheck_bridge_and_opt_in", lambda: called.__setitem__("gate", True)
    )
    exit_code = cli.main(["--run", "--campaign-run-id", "run-fixed-001"])
    assert exit_code != 0
    assert called["gate"] is False


def test_run_without_campaign_run_id_fails_closed_before_any_gate(monkeypatch):
    called = {"gate": False}
    monkeypatch.setattr(
        cli, "_run_precheck_bridge_and_opt_in", lambda: called.__setitem__("gate", True)
    )
    exit_code = cli.main(["--run", "--expected-full-campaign-hash", "a" * 64])
    assert exit_code != 0
    assert called["gate"] is False


def test_run_fails_closed_on_hash_mismatch_before_opt_in_or_bridge_check(monkeypatch):
    """A fresh recomputation vs an operator-pinned MISMATCHED value must fail
    BEFORE the opt-in/bridge gates are ever consulted -- proving the two
    hash values are genuinely independent, not both freshly recomputed from
    the same call (which would be vacuous)."""
    called = {"gate": False}
    monkeypatch.setattr(
        cli, "_run_precheck_bridge_and_opt_in", lambda: called.__setitem__("gate", True)
    )
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            "0" * 64,
            "--campaign-run-id",
            "run-fixed-001",
        ]
    )
    assert exit_code != 0
    assert called["gate"] is False


def test_run_hash_mismatch_message_never_echoes_either_hash_value(monkeypatch, capsys):
    """P1-B: on full_campaign_hash mismatch, _run_empirical must print a
    fixed, field-only message -- never the operator-supplied
    --expected-full-campaign-hash (caller-controlled, could be a secret-
    looking sentinel) nor the freshly recomputed actual hash."""
    monkeypatch.setattr(cli, "_run_precheck_bridge_and_opt_in", lambda: None)
    sentinel = "sk-live-SUPERSECRETTOKEN-should-never-appear-in-stderr-0000000"
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            sentinel,
            "--campaign-run-id",
            "run-fixed-001",
        ]
    )
    assert exit_code == 4
    stderr = capsys.readouterr().err
    assert sentinel not in stderr
    actual_hash = cli.build_production_frozen_campaign_envelope().full_campaign_hash()
    assert actual_hash not in stderr
    assert "full_campaign_hash mismatch" in stderr


def test_run_campaign_run_id_mismatch_message_never_echoes_either_id(
    monkeypatch, capsys
):
    """P1-B: on campaign_run_id mismatch (hash already matches), the printed
    message must never echo the operator-supplied --campaign-run-id
    (caller-controlled) nor the canonically-derived expected value."""
    monkeypatch.setattr(cli, "_run_precheck_bridge_and_opt_in", lambda: None)
    plan = cli.build_plan()
    sentinel = "sk-live-SUPERSECRETTOKEN-should-never-appear-in-stderr"
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            sentinel,
        ]
    )
    assert exit_code == 4
    stderr = capsys.readouterr().err
    assert sentinel not in stderr
    assert plan["expected_campaign_run_id"] not in stderr
    assert "--campaign-run-id" in stderr


def test_run_mode_fails_closed_when_write_opt_in_is_absent(monkeypatch):
    monkeypatch.delenv("ROB944_RESEARCH_WRITE_OPT_IN", raising=False)
    with pytest.raises(cli.RunPreflightError):
        cli._run_precheck_bridge_and_opt_in()


def test_run_mode_fails_closed_when_write_opt_in_is_false(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "false")
    with pytest.raises(cli.RunPreflightError):
        cli._run_precheck_bridge_and_opt_in()


def test_run_mode_passes_precheck_when_opt_in_true_and_bridge_importable(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    cli._run_precheck_bridge_and_opt_in()  # must not raise


def test_run_mode_fails_closed_when_bridge_unimportable(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")

    def _raise_import_error():
        raise ImportError(
            "simulated: app.services.rob944_campaign_controller unavailable"
        )

    monkeypatch.setattr(cli, "_import_campaign_controller", _raise_import_error)
    with pytest.raises(cli.RunPreflightError):
        cli._run_precheck_bridge_and_opt_in()


def test_main_run_mode_exits_nonzero_when_hash_matches_but_opt_in_absent(
    monkeypatch, capsys
):
    """Once the (matching) hash AND correctly-derived campaign_run_id gates
    pass, the NEXT gate (opt-in) must still fail closed. Independent
    controller audit correction (2026-07-17): the prior version of this test
    passed an ARBITRARY campaign_run_id ("run-fixed-001"), so it never
    actually reached the opt-in gate at all (it failed earlier, at the
    campaign_run_id-derivation gate) -- the assertion (bare ``!= 0``) never
    noticed. Passes the REAL derived ``expected_campaign_run_id`` so this
    test genuinely exercises the opt-in gate, and checks the printed
    message names opt-in specifically."""
    monkeypatch.delenv("ROB944_RESEARCH_WRITE_OPT_IN", raising=False)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code != 0
    assert "opt-in" in capsys.readouterr().err


def test_bridge_gate_is_checked_before_opt_in_gate(monkeypatch, capsys):
    """Independent controller audit correction: with a matching hash AND
    correctly-derived campaign_run_id, if BOTH the bridge import AND the
    opt-in check would independently fail, the reported failure must be the
    BRIDGE one -- proving bridge-importability is checked strictly before
    opt-in (per _run_precheck_bridge_and_opt_in's own documented order),
    not merely that "some" RunPreflightError was raised."""
    monkeypatch.delenv(
        "ROB944_RESEARCH_WRITE_OPT_IN", raising=False
    )  # opt-in would ALSO fail

    def _raise_import_error():
        raise ImportError("simulated: bridge unavailable")

    monkeypatch.setattr(cli, "_import_campaign_controller", _raise_import_error)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code != 0
    stderr = capsys.readouterr().err
    assert "bridge" in stderr
    assert "opt-in" not in stderr


# ---------------------------------------------------------------------------
# Independent controller audit correction (2026-07-17): a rollback attempted
# in response to an ALREADY-failed orchestration/commit must never itself
# raw-trace (masking the original sanitized message with an unsanitized
# one, potentially leaking connection/query details).
# ---------------------------------------------------------------------------


def test_safe_rollback_succeeds_silently_when_rollback_succeeds():
    import asyncio

    class _FakeSession:
        async def rollback(self):
            pass

    asyncio.run(cli._safe_rollback(_FakeSession()))  # must not raise


def test_safe_rollback_swallows_rollback_failure_and_prints_only_a_sanitized_message(
    capsys,
):
    import asyncio

    class _FakeSession:
        async def rollback(self):
            raise RuntimeError(
                "boom: raw connection string postgres://user:secret@host/db"
            )

    asyncio.run(cli._safe_rollback(_FakeSession()))  # must not raise
    stderr = capsys.readouterr().err
    assert "boom" not in stderr
    assert "secret" not in stderr
    assert "rollback failed" in stderr


class _FakeDbSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _FakeSessionLocal:
    def __call__(self):
        return _FakeDbSession()


def _run_main_with_fake_controller(monkeypatch, run_full_campaign_side_effect):
    """Exercises the real --run path (main -> _run_empirical -> _do_run) with
    a fake DB session (no real DB touched) and a fake controller module
    whose ``run_full_campaign`` raises/returns ``run_full_campaign_side_effect``
    -- used to prove the CLI's exit-code classification for each controller
    exception type without needing a live database."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _FakeSessionLocal())

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            result = run_full_campaign_side_effect()
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)

    plan = cli.build_plan()
    return cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )


def test_campaign_run_id_derivation_error_exits_with_documented_code_4_not_generic_6(
    monkeypatch,
):
    from app.services.rob944_campaign_controller import CampaignRunIdDerivationError

    exit_code = _run_main_with_fake_controller(
        monkeypatch, lambda: CampaignRunIdDerivationError("forged campaign_run_id")
    )
    assert exit_code == 4


def test_run_identity_mismatch_error_exits_with_documented_code_4_not_generic_6(
    monkeypatch,
):
    from app.services.rob944_campaign_controller import RunIdentityMismatchError

    exit_code = _run_main_with_fake_controller(
        monkeypatch, lambda: RunIdentityMismatchError("forged run_identity")
    )
    assert exit_code == 4


def test_typed_controller_exception_message_never_echoed_only_exception_type_name(
    monkeypatch, capsys
):
    """P1-C sanitization precision (2026-07-17): the code-4 catch for the 5
    typed controller exceptions must never print ``str(exc)`` -- only the
    trusted ``type(exc).__name__`` -- even though these are OUR OWN
    (already-sanitized-at-the-controller-layer) exceptions, this boundary
    must not depend on that invariant holding forever."""
    from app.services.rob944_campaign_controller import CampaignRunIdDerivationError

    sentinel = "RAW-TRACE-SECRET-typed-exception-message-should-never-leak"
    exit_code = _run_main_with_fake_controller(
        monkeypatch, lambda: CampaignRunIdDerivationError(sentinel)
    )
    assert exit_code == 4
    stderr = capsys.readouterr().err
    assert sentinel not in stderr
    assert "CampaignRunIdDerivationError" in stderr


def test_unknown_controller_failure_still_exits_with_sanitized_code_6(monkeypatch):
    exit_code = _run_main_with_fake_controller(
        monkeypatch, lambda: RuntimeError("unexpected")
    )
    assert exit_code == 6


def test_envelope_construction_failure_exits_sanitized_code_6_never_raw_traces(
    monkeypatch, capsys
):
    """P1-C outer-boundary hardening: build_production_frozen_campaign_hash/
    envelope construction, hash/derive/to_dict/experiment-id recomputation
    all sit BEFORE any try in the impl -- a failure there must still exit
    sanitized code 6 via the ``_run_empirical`` thin-wrapper backstop, never
    raw-trace a secret-looking message."""
    sentinel = "RAW-TRACE-SECRET-envelope-construction-boom"

    def _boom():
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "build_production_frozen_campaign_envelope", _boom)
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            "a" * 64,
            "--campaign-run-id",
            "run-x",
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_unexpected_precheck_failure_exits_sanitized_code_6_never_raw_traces(
    monkeypatch, capsys
):
    """P1-C outer-boundary hardening: ``_run_empirical_impl`` only catches
    ``RunPreflightError`` around ``_run_precheck_bridge_and_opt_in`` -- any
    OTHER exception type escaping it (a bug, not a documented preflight
    failure) must still exit sanitized code 6 via the outer backstop, never
    raw-trace."""
    sentinel = "RAW-TRACE-SECRET-unexpected-precheck-boom"

    def _boom():
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "_run_precheck_bridge_and_opt_in", _boom)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_session_factory_construction_failure_exits_sanitized_code_6(
    monkeypatch, capsys
):
    """P1-C outer-boundary hardening: ``AsyncSessionLocal()`` itself
    (session-factory construction, before ``__aenter__`` even runs) raising
    must exit sanitized code 6, never raw-trace -- no rollback is attempted
    since no valid session was ever constructed."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    sentinel = "RAW-TRACE-SECRET-session-factory-boom"

    def _boom():
        raise RuntimeError(sentinel)

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _boom)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_session_aenter_failure_exits_sanitized_code_6(monkeypatch, capsys):
    """P1-C outer-boundary hardening: ``__aenter__`` raising (session
    constructed but connection/setup failed) must exit sanitized code 6,
    never raw-trace -- this failure is NOT inside the inner try (which only
    starts once ``session`` is already bound), so only the outer boundary
    catches it."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    sentinel = "RAW-TRACE-SECRET-aenter-boom"

    class _AenterFailingSession:
        async def __aenter__(self):
            raise RuntimeError(sentinel)

        async def __aexit__(self, *_exc_info):
            return False

    monkeypatch.setattr(
        "app.core.db.AsyncSessionLocal", lambda: _AenterFailingSession()
    )
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_session_aexit_failure_after_successful_commit_exits_sanitized_code_6(
    monkeypatch, capsys
):
    """P1-C outer-boundary hardening: ``__aexit__`` raising during close --
    even AFTER a successful controller run + commit -- must exit sanitized
    code 6 (not raw-trace, and not silently report the would-be success
    exit code, since the session's own close genuinely failed)."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    sentinel = "RAW-TRACE-SECRET-aexit-boom"

    class _AexitFailingSession(_FakeDbSession):
        async def __aexit__(self, *_exc_info):
            raise RuntimeError(sentinel)

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: _AexitFailingSession())

    class _FakeReport:
        verdict = "complete"
        total_attempts = 24
        expected_total = 24
        retry_attempts = 0
        status_counts = {"completed": 24}
        actual_registrations = 24
        primary_attempts = 24

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            return _FakeReport()

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_asyncio_run_failure_exits_sanitized_code_6(monkeypatch, capsys):
    """P1-C outer-boundary hardening: ``asyncio.run`` itself (event loop
    setup/teardown) failing must exit sanitized code 6, never raw-trace."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    sentinel = "RAW-TRACE-SECRET-asyncio-run-boom"

    def _boom(coro):
        coro.close()  # avoid a "coroutine was never awaited" warning from this fake
        raise RuntimeError(sentinel)

    monkeypatch.setattr("asyncio.run", _boom)
    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 6
    stderr = capsys.readouterr().err
    assert sentinel not in stderr


def test_commit_failure_exits_with_documented_code_7(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")

    class _CommitFailingSession(_FakeDbSession):
        async def commit(self):
            raise RuntimeError("commit failed: constraint violation")

    monkeypatch.setattr(
        "app.core.db.AsyncSessionLocal", lambda: _CommitFailingSession()
    )

    class _FakeReport:
        verdict = "complete"
        total_attempts = 24
        expected_total = 24
        retry_attempts = 0
        status_counts = {"completed": 24}
        actual_registrations = 24
        primary_attempts = 24

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            return _FakeReport()

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)

    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 7


# ---------------------------------------------------------------------------
# Captain P1-C transaction-coverage correction (2026-07-17): the exit-code
# tests above only assert the returned int -- they do NOT prove the actual
# commit()/rollback() CALL COUNTS match the documented lifecycle contract.
# This shared instrumented fake session + factory, run through the REAL
# main -> _run_empirical -> _do_run path, gives an independent, exact
# commit/rollback count assertion per documented exit code, plus
# factory/__aenter__/__aexit__ call counts and rollback-failure-through-main
# sanitization -- none inferred from the exit code alone.
# ---------------------------------------------------------------------------


class _CountingSession:
    def __init__(self, *, commit_exc=None, rollback_exc=None):
        self.commit_calls = 0
        self.rollback_calls = 0
        self.aenter_calls = 0
        self.aexit_calls = 0
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc

    async def __aenter__(self):
        self.aenter_calls += 1
        return self

    async def __aexit__(self, *_exc_info):
        self.aexit_calls += 1
        return False

    async def commit(self):
        self.commit_calls += 1
        if self._commit_exc is not None:
            raise self._commit_exc

    async def rollback(self):
        self.rollback_calls += 1
        if self._rollback_exc is not None:
            raise self._rollback_exc


class _CountingSessionFactory:
    def __init__(self, session):
        self.session = session
        self.construct_calls = 0

    def __call__(self):
        self.construct_calls += 1
        return self.session


def _lifecycle_fake_report(
    *,
    verdict="complete",
    total_attempts=24,
    expected_total=24,
    retry_attempts=0,
    completed=24,
):
    return SimpleNamespace(
        verdict=verdict,
        total_attempts=total_attempts,
        expected_total=expected_total,
        retry_attempts=retry_attempts,
        status_counts={"completed": completed},
        actual_registrations=expected_total,
        primary_attempts=total_attempts,
    )


def _run_main_with_counting_session(
    monkeypatch, *, run_full_campaign_side_effect, commit_exc=None, rollback_exc=None
):
    """Runs the REAL --run path (main -> _run_empirical -> _do_run) against
    a session whose commit()/rollback()/__aenter__/__aexit__ calls are all
    counted, and a fake controller whose run_full_campaign either raises or
    returns ``run_full_campaign_side_effect()``. Returns
    ``(exit_code, session, factory)`` so the caller can assert exact call
    counts independent of the returned exit code."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    session = _CountingSession(commit_exc=commit_exc, rollback_exc=rollback_exc)
    factory = _CountingSessionFactory(session)
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", factory)

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            result = run_full_campaign_side_effect()
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)

    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    return exit_code, session, factory


def test_lifecycle_success_code_0_commits_once_never_rolls_back(monkeypatch):
    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch, run_full_campaign_side_effect=lambda: _lifecycle_fake_report()
    )
    assert exit_code == 0
    assert session.commit_calls == 1
    assert session.rollback_calls == 0
    assert factory.construct_calls == 1
    assert session.aenter_calls == 1
    assert session.aexit_calls == 1


def test_lifecycle_accounting_complete_empirical_failure_code_5_commits_once_never_rolls_back(
    monkeypatch,
):
    """Accounting-complete (verdict="complete") but not every primary
    completed (completed=0) -- exit 5, and the commit still happens exactly
    once (a legitimate, COMMITTABLE record of a failed run) with no
    rollback attempted."""
    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch,
        run_full_campaign_side_effect=lambda: _lifecycle_fake_report(completed=0),
    )
    assert exit_code == 5
    assert session.commit_calls == 1
    assert session.rollback_calls == 0
    assert factory.construct_calls == 1


def test_lifecycle_typed_validation_failure_code_4_never_commits_rolls_back_once(
    monkeypatch,
):
    from app.services.rob944_campaign_controller import CampaignRunIdDerivationError

    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch,
        run_full_campaign_side_effect=lambda: CampaignRunIdDerivationError("forged"),
    )
    assert exit_code == 4
    assert session.commit_calls == 0
    assert session.rollback_calls == 1
    assert factory.construct_calls == 1


def test_lifecycle_unknown_failure_code_6_never_commits_rolls_back_once(monkeypatch):
    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch, run_full_campaign_side_effect=lambda: RuntimeError("unexpected")
    )
    assert exit_code == 6
    assert session.commit_calls == 0
    assert session.rollback_calls == 1
    assert factory.construct_calls == 1


def test_lifecycle_commit_failure_code_7_attempts_commit_once_then_rolls_back_once(
    monkeypatch,
):
    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch,
        run_full_campaign_side_effect=lambda: _lifecycle_fake_report(),
        commit_exc=RuntimeError("commit failed: constraint violation"),
    )
    assert exit_code == 7
    assert session.commit_calls == 1  # the attempt itself, which then raised
    assert session.rollback_calls == 1
    assert factory.construct_calls == 1


def test_lifecycle_rollback_failure_through_main_stays_sanitized_and_still_counted(
    monkeypatch, capsys
):
    """A typed validation failure (code 4) whose OWN rollback attempt then
    ALSO fails: rollback is still attempted exactly once (counted, even
    though it raised internally), the exit code is unaffected (still 4, not
    masked into something else), and neither the rollback failure's raw
    text nor the original exception's message leaks into stderr -- only
    _safe_rollback's own fixed sanitized text."""
    from app.services.rob944_campaign_controller import CampaignRunIdDerivationError

    rollback_sentinel = "RAW-TRACE-SECRET-rollback-failure-boom"
    exit_code, session, factory = _run_main_with_counting_session(
        monkeypatch,
        run_full_campaign_side_effect=lambda: CampaignRunIdDerivationError("forged"),
        rollback_exc=RuntimeError(rollback_sentinel),
    )
    assert exit_code == 4
    assert session.commit_calls == 0
    assert session.rollback_calls == 1
    stderr = capsys.readouterr().err
    assert rollback_sentinel not in stderr
    assert "rollback failed" in stderr


# ---------------------------------------------------------------------------
# Captain global-fallback consistency (2026-07-17, item D): a GLOBAL
# (pre-per-config) failure -- whether it happens BEFORE any per-strategy
# work starts, or AFTER S1 already succeeded and populated captured
# summaries -- must always yield a full 24-entry deterministic crashed
# batch, never zero/partial attempts, with the operator-visible
# capture_summaries_into view IDENTICAL to what's returned for persistence.
# ---------------------------------------------------------------------------


def _fake_experiment_id_by_key():
    return {
        (f"ROB940-S{slug}-KEY", f"S{slug}-{i:02d}"): f"exp-{slug}-{i:02d}"
        for slug in (1, 2)
        for i in range(12)
    }


def test_global_failure_evidence_batch_produces_exactly_24_crashed_entries():
    from rob944_walkforward import REASON_GLOBAL_CORPUS_LOAD_FAILED

    experiment_id_by_key = _fake_experiment_id_by_key()
    evidence = cli._global_failure_evidence_batch(
        experiment_id_by_key, full_campaign_hash="a" * 64, campaign_run_id="run-x"
    )
    assert len(evidence) == 24
    assert {e.attempt_key.experiment_id for e in evidence} == set(
        experiment_id_by_key.values()
    )
    assert all(e.status == "crashed" for e in evidence)
    assert all(e.reason_code == REASON_GLOBAL_CORPUS_LOAD_FAILED for e in evidence)
    assert all(len(e.scenario_evidence) == 3 for e in evidence)


def test_build_real_attempt_evidence_rejects_malformed_mapping_before_inner_is_called(
    monkeypatch,
):
    """Captain precision follow-up ("one remaining boundary", 2026-07-17):
    a dict-SUBCLASS experiment_id_by_key must be rejected BEFORE
    _build_real_attempt_evidence_inner is ever called -- proving the gate
    fires at the very entry point, before any corpus-loading/walk-forward
    work, not merely somewhere downstream after expensive work already
    ran."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "_build_real_attempt_evidence_inner must never be called for a malformed mapping"
        )

    monkeypatch.setattr(cli, "_build_real_attempt_evidence_inner", _boom)

    class _DictSubclass(dict):
        pass

    bad_mapping = _DictSubclass({("K", "S1-00"): "exp-00"})
    with pytest.raises(ValueError, match="experiment_id_by_key"):
        cli._build_real_attempt_evidence(
            bad_mapping,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_build_real_attempt_evidence_rejects_lineage_str_subclass_before_inner_is_called(
    monkeypatch,
):
    """Same boundary, for an AliasStr-style full_campaign_hash/
    campaign_run_id -- also rejected before _build_real_attempt_evidence_inner
    is ever called."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "_build_real_attempt_evidence_inner must never be called for a malformed lineage arg"
        )

    monkeypatch.setattr(cli, "_build_real_attempt_evidence_inner", _boom)

    class _AliasStr(str):
        pass

    with pytest.raises(ValueError, match="full_campaign_hash"):
        cli._build_real_attempt_evidence(
            {("K", "S1-00"): "exp-00"},
            full_campaign_hash=_AliasStr("h" * 64),
            campaign_run_id="run-001",
        )


def test_failure_before_any_strategy_work_yields_24_crashed_captured_and_24_crashed_evidence(
    monkeypatch,
):
    """A GLOBAL failure that happens BEFORE S1 even starts (e.g. corpus
    loading itself fails) -- capture_summaries_into must end up with
    exactly 24 crashed summaries, matching the 24 crashed AttemptEvidence
    actually returned for persistence."""

    def _boom(
        _experiment_id_by_key,
        *,
        full_campaign_hash,
        campaign_run_id,
        capture_summaries_into=None,
    ):
        raise RuntimeError("simulated: corpus load failed before any strategy ran")

    monkeypatch.setattr(cli, "_build_real_attempt_evidence_inner", _boom)

    experiment_id_by_key = _fake_experiment_id_by_key()
    captured: list = []
    evidence = cli._build_real_attempt_evidence(
        experiment_id_by_key,
        full_campaign_hash="b" * 64,
        campaign_run_id="run-y",
        capture_summaries_into=captured,
    )
    assert len(evidence) == 24
    assert all(e.status == "crashed" for e in evidence)
    assert len(captured) == 24
    assert all(s.status == "crashed" for s in captured)
    # Captain global-fallback identity-consistency correction: operator
    # summary.strategy is consistently the short S1/S2 slug (exactly 12
    # each), and every experiment_id in the returned evidence matches the
    # predeclared 24-entry mapping unchanged.
    assert sorted(s.strategy for s in captured) == ["S1"] * 12 + ["S2"] * 12
    assert {e.attempt_key.experiment_id for e in evidence} == set(
        experiment_id_by_key.values()
    )


def test_partial_s1_success_then_global_bomb_clears_stale_capture_to_24_crashed(
    monkeypatch,
):
    """S1 already succeeded and populated capture_summaries_into with 12
    REAL summaries before S2 (or any later global precondition) fails --
    the partial capture must be CLEARED and refilled with the full 24-entry
    crashed sentinel, never left showing 12 real + nothing (which would
    make the operator-visible CLI view inconsistent with the 24 crashed
    rows actually persisted)."""
    from rob944_walkforward import ConfigAttemptEvidenceSummary, ScenarioEvidenceSummary

    def _fake_real_summary(config_id):
        return ConfigAttemptEvidenceSummary(
            strategy="S1",
            config_id=config_id,
            status="completed",
            reason_code=None,
            scenario_summaries=(
                ScenarioEvidenceSummary(
                    scenario_name="base",
                    status="completed",
                    reason_code=None,
                    trade_count=1,
                    artifact_hash="a" * 64,
                    no_trade_reason_counts={},
                ),
                ScenarioEvidenceSummary(
                    scenario_name="primary_stress",
                    status="completed",
                    reason_code=None,
                    trade_count=1,
                    artifact_hash="b" * 64,
                    no_trade_reason_counts={},
                ),
                ScenarioEvidenceSummary(
                    scenario_name="upward_stress",
                    status="completed",
                    reason_code=None,
                    trade_count=1,
                    artifact_hash="c" * 64,
                    no_trade_reason_counts={},
                ),
            ),
        )

    def _partial_then_boom(
        _experiment_id_by_key,
        *,
        full_campaign_hash,
        campaign_run_id,
        capture_summaries_into=None,
    ):
        if capture_summaries_into is not None:
            capture_summaries_into.extend(
                _fake_real_summary(f"S1-{i:02d}") for i in range(12)
            )
        raise RuntimeError(
            "simulated: S2/global precondition failed after S1 succeeded"
        )

    monkeypatch.setattr(cli, "_build_real_attempt_evidence_inner", _partial_then_boom)

    experiment_id_by_key = _fake_experiment_id_by_key()
    captured: list = []
    evidence = cli._build_real_attempt_evidence(
        experiment_id_by_key,
        full_campaign_hash="c" * 64,
        campaign_run_id="run-z",
        capture_summaries_into=captured,
    )
    assert len(evidence) == 24
    assert all(e.status == "crashed" for e in evidence)
    # The stale partial (12 real "completed") capture must be gone entirely.
    assert len(captured) == 24
    assert all(s.status == "crashed" for s in captured)
    assert not any(s.status == "completed" for s in captured)
    # Captain global-fallback identity-consistency correction: exact 12
    # S1 + 12 S2 operator labels, and the 24 experiment_ids are unchanged
    # from the predeclared mapping (never dropped/duplicated/renamed).
    assert sorted(s.strategy for s in captured) == ["S1"] * 12 + ["S2"] * 12
    assert {e.attempt_key.experiment_id for e in evidence} == set(
        experiment_id_by_key.values()
    )
    # Captain precision follow-up (2026-07-17, item 3): every captured
    # object must be the exact normalized DTO type, and correspond
    # ONE-TO-ONE (via config_id -> experiment_id) with the 24 returned
    # attempts -- never a dropped/duplicated/mismatched entry between
    # operator capture and persisted evidence. (ConfigAttemptEvidenceSummary
    # already imported above, in this same function.)
    assert all(type(s) is ConfigAttemptEvidenceSummary for s in captured)
    experiment_id_by_config_id = {
        config_id: exp_id for (_sk, config_id), exp_id in experiment_id_by_key.items()
    }
    captured_config_ids = {s.config_id for s in captured}
    assert len(captured_config_ids) == 24  # no duplicates
    captured_experiment_ids = {
        experiment_id_by_config_id[cid] for cid in captured_config_ids
    }
    evidence_experiment_ids = {e.attempt_key.experiment_id for e in evidence}
    assert (
        captured_experiment_ids
        == evidence_experiment_ids
        == set(experiment_id_by_key.values())
    )


# ---------------------------------------------------------------------------
# lineage-bound evidence construction (pure, no corpus loading required)
# ---------------------------------------------------------------------------


def _fake_summary(
    config_id="S1-00", status="completed", reason_code=None, fold_selection_trace=None
):
    from rob944_walkforward import ConfigAttemptEvidenceSummary, ScenarioEvidenceSummary

    # Captain live-validation correction (2026-07-17): an EMPTY
    # fold_selection_trace is only valid for the exact global-corpus-load-
    # failure signature (status="crashed" + REASON_GLOBAL_CORPUS_LOAD_FAILED)
    # -- default to a full valid 8-fold trace so existing fixtures that
    # don't care about fold-trace specifics still pass; callers testing
    # that signature explicitly pass fold_selection_trace=().
    if fold_selection_trace is None:
        fold_selection_trace = _fake_full_fold_trace()
    return ConfigAttemptEvidenceSummary(
        strategy="S1",
        config_id=config_id,
        status=status,
        reason_code=reason_code,
        fold_selection_trace=fold_selection_trace,
        scenario_summaries=(
            ScenarioEvidenceSummary(
                scenario_name="base",
                status="completed",
                reason_code=None,
                trade_count=3,
                artifact_hash="a" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="primary_stress",
                status="completed",
                reason_code=None,
                trade_count=3,
                artifact_hash="b" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="upward_stress",
                status="completed",
                reason_code=None,
                trade_count=2,
                artifact_hash="c" * 64,
                no_trade_reason_counts={},
            ),
        ),
    )


def test_summary_to_attempt_evidence_binds_full_lineage_deterministically():
    summary = _fake_summary()
    evidence1 = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="ROB940-S1-DONCHIAN-15M",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    evidence2 = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="ROB940-S1-DONCHIAN-15M",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert evidence1.run_identity == evidence2.run_identity  # deterministic
    assert evidence1.attempt_key.experiment_id == "exp-abc"
    assert evidence1.attempt_key.campaign_run_id == "run-001"
    assert evidence1.attempt_key.retry_index == 0
    assert len(evidence1.scenario_evidence) == 3


def test_summary_to_attempt_evidence_run_identity_changes_with_lineage_facts():
    summary = _fake_summary()
    base = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    diff_hash = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="i" * 64,
        campaign_run_id="run-001",
    )
    diff_run = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-002",
    )
    diff_exp = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="K",
        experiment_id="exp-xyz",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert base.run_identity != diff_hash.run_identity
    assert base.run_identity != diff_run.run_identity
    assert base.run_identity != diff_exp.run_identity


def test_summaries_to_attempt_evidence_maps_via_strategy_key_and_config_id():
    # Captain precision follow-up (2026-07-17, item 2): _summaries_to_attempt_evidence
    # now routes through the normalized-batch boundary, which requires an
    # exact tuple -- a list is no longer accepted.
    summaries = (_fake_summary(config_id="S1-00"), _fake_summary(config_id="S1-01"))
    experiment_id_by_key = {
        ("ROB940-S1-DONCHIAN-15M", "S1-00"): "exp-00",
        ("ROB940-S1-DONCHIAN-15M", "S1-01"): "exp-01",
    }
    evidence_list = cli._summaries_to_attempt_evidence(
        summaries,
        strategy_key="ROB940-S1-DONCHIAN-15M",
        experiment_id_by_key=experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert {e.attempt_key.experiment_id for e in evidence_list} == {"exp-00", "exp-01"}


def test_normalize_and_capture_summaries_rejects_non_tuple_batch():
    """Captain design review (2026-07-17, item 1): the OUTER summaries
    container itself must be an exact tuple -- a list (or any other
    iterable) is refused before anything inside it is even normalized."""
    with pytest.raises(ValueError, match="exact tuple"):
        cli._normalize_and_capture_summaries(
            [_fake_summary()],  # a list, not a tuple
            strategy_key="K",
            experiment_id_by_key={("K", "S1-00"): "exp-abc"},
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_normalize_and_capture_summaries_capture_uses_same_normalized_snapshot_as_evidence():
    """Captain design cross-check correction (2026-07-17): capture must
    consume the IDENTICAL normalized snapshot objects that evidence-
    building consumed -- never a second, independent normalization pass
    (which would copy every nested dict twice and could, in principle,
    diverge). Proven by re-deriving evidence from the CAPTURED object
    directly (via the normalized core) and checking it matches the
    evidence _normalize_and_capture_summaries itself returned."""
    from rob944_walkforward import ConfigAttemptEvidenceSummary

    summary = _fake_summary()
    captured: list = []
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}
    evidence = cli._normalize_and_capture_summaries(
        (summary,),
        strategy_key="K",
        experiment_id_by_key=experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
        capture_summaries_into=captured,
    )
    assert len(evidence) == 1
    assert len(captured) == 1
    assert type(captured[0]) is ConfigAttemptEvidenceSummary
    rederived = cli._normalized_summary_to_attempt_evidence(
        captured[0],
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert rederived.run_identity == evidence[0].run_identity
    assert rederived.fold_evidence_hash == evidence[0].fold_evidence_hash


def test_normalize_and_capture_summaries_captured_object_is_the_same_identity_consumed_by_core(
    monkeypatch,
):
    """Captain design precision (2026-07-17, item 4): literal object-
    IDENTITY proof (``is``, not merely hash-equivalence) that the object
    the normalized core actually consumed to build evidence is the SAME
    Python object later appended to operator capture -- never two
    independently-normalized copies."""
    summary = _fake_summary()
    captured: list = []
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}

    consumed_objects: list = []
    original_core = cli._normalized_summary_to_attempt_evidence

    def _spy_core(s, **kwargs):
        consumed_objects.append(s)
        return original_core(s, **kwargs)

    monkeypatch.setattr(cli, "_normalized_summary_to_attempt_evidence", _spy_core)

    cli._normalize_and_capture_summaries(
        (summary,),
        strategy_key="K",
        experiment_id_by_key=experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
        capture_summaries_into=captured,
    )
    assert len(consumed_objects) == 1
    assert len(captured) == 1
    assert consumed_objects[0] is captured[0]


def test_normalize_and_capture_summaries_no_capture_when_evidence_build_fails():
    """Captain design precision (2026-07-17, item 4): if evidence-building
    fails for ANY entry in the batch, capture must receive NOTHING --
    never a partial capture from entries that happened to succeed before
    the failing one."""
    good_summary = _fake_summary(config_id="S1-00")
    bad_summary = _fake_summary(
        config_id="S1-99"
    )  # outside the canonical 12-config set
    captured: list = []
    experiment_id_by_key = {("K", "S1-00"): "exp-00", ("K", "S1-99"): "exp-99"}
    with pytest.raises(ValueError):
        cli._normalize_and_capture_summaries(
            (good_summary, bad_summary),
            strategy_key="K",
            experiment_id_by_key=experiment_id_by_key,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
            capture_summaries_into=captured,
        )
    assert captured == []


def test_build_fallback_evidence_and_capture_yields_exact_24_normalized_entries():
    """Captain design precision (2026-07-17, item 4): the fallback helper
    must produce EXACTLY 24 normalized ``ConfigAttemptEvidenceSummary``
    entries (never a partial/raw capture), each an exact instance (not a
    subclass/proxy) of the canonical type."""
    from rob944_walkforward import ConfigAttemptEvidenceSummary

    experiment_id_by_key = {
        (f"ROB940-S{slug}-KEY", f"S{slug}-{i:02d}"): f"exp-{slug}-{i:02d}"
        for slug in (1, 2)
        for i in range(12)
    }
    captured: list = []
    evidence = cli._build_fallback_evidence_and_capture(
        experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
        capture_summaries_into=captured,
    )
    assert len(evidence) == 24
    assert len(captured) == 24
    assert all(type(s) is ConfigAttemptEvidenceSummary for s in captured)
    assert all(s.status == "crashed" for s in captured)


def test_build_fallback_evidence_and_capture_captured_objects_are_the_same_identity_consumed_by_core(
    monkeypatch,
):
    """Captain normalization reaudit (2026-07-18): analogous to
    ``test_normalize_and_capture_summaries_captured_object_is_the_same_identity_consumed_by_core``
    (the real-batch path) -- every object the normalized core actually
    consumed to build FALLBACK evidence must be the SAME Python object
    (``is``, not merely equal) later published in operator capture, for
    all 24 entries, in order. Never two independently-normalized copies."""
    consumed_objects: list = []
    original_core = cli._normalized_summary_to_attempt_evidence

    def _spy_core(s, **kwargs):
        consumed_objects.append(s)
        return original_core(s, **kwargs)

    monkeypatch.setattr(cli, "_normalized_summary_to_attempt_evidence", _spy_core)

    experiment_id_by_key = {
        (f"ROB940-S{slug}-KEY", f"S{slug}-{i:02d}"): f"exp-{slug}-{i:02d}"
        for slug in (1, 2)
        for i in range(12)
    }
    captured: list = []
    evidence = cli._build_fallback_evidence_and_capture(
        experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
        capture_summaries_into=captured,
    )
    assert len(evidence) == 24
    assert len(consumed_objects) == 24
    assert len(captured) == 24
    for consumed, cap in zip(consumed_objects, captured, strict=True):
        assert consumed is cap


def test_build_fallback_evidence_and_capture_leaves_capture_unchanged_when_a_later_entry_fails(
    monkeypatch,
):
    """Captain precision follow-up (2026-07-17, item 3): seed capture with
    STALE objects, force the normalized core to fail partway through the
    24-entry fallback batch, and prove capture remains ENTIRELY unchanged
    -- never partially cleared/overwritten before the whole batch
    succeeds."""
    stale_sentinel = object()
    captured = [stale_sentinel]

    call_count = {"n": 0}
    original_core = cli._normalized_summary_to_attempt_evidence

    def _flaky_core(s, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 5:
            raise RuntimeError("simulated failure partway through the fallback batch")
        return original_core(s, **kwargs)

    monkeypatch.setattr(cli, "_normalized_summary_to_attempt_evidence", _flaky_core)

    experiment_id_by_key = {
        (f"ROB940-S{slug}-KEY", f"S{slug}-{i:02d}"): f"exp-{slug}-{i:02d}"
        for slug in (1, 2)
        for i in range(12)
    }
    with pytest.raises(RuntimeError):
        cli._build_fallback_evidence_and_capture(
            experiment_id_by_key,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
            capture_summaries_into=captured,
        )
    assert captured == [stale_sentinel]
    assert call_count["n"] == 5  # confirms the failure actually fired partway through


def test_lineage_arg_str_subclass_rejected_before_canonical_sha256(monkeypatch):
    """Captain precision follow-up (2026-07-17, item 4): an AliasStr-style
    str SUBCLASS strategy_key must be rejected -- exact-str gated BEFORE
    any f-string context, dict lookup, or canonical_sha256 call -- proven
    via the spy pattern (zero hash calls)."""
    import research_contracts.canonical_hash as canonical_hash_module

    class _AliasStr(str):
        pass

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}
    with pytest.raises(ValueError, match="strategy_key"):
        cli._normalize_and_capture_summaries(
            (summary,),
            strategy_key=_AliasStr("K"),
            experiment_id_by_key=experiment_id_by_key,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_lineage_arg_full_campaign_hash_subclass_rejected_before_canonical_sha256(
    monkeypatch,
):
    import research_contracts.canonical_hash as canonical_hash_module

    class _AliasStr(str):
        pass

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}
    with pytest.raises(ValueError, match="full_campaign_hash"):
        cli._normalize_and_capture_summaries(
            (summary,),
            strategy_key="K",
            experiment_id_by_key=experiment_id_by_key,
            full_campaign_hash=_AliasStr("h" * 64),
            campaign_run_id="run-001",
        )


def test_lineage_arg_campaign_run_id_subclass_rejected_before_canonical_sha256(
    monkeypatch,
):
    import research_contracts.canonical_hash as canonical_hash_module

    class _AliasStr(str):
        pass

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}
    with pytest.raises(ValueError, match="campaign_run_id"):
        cli._normalize_and_capture_summaries(
            (summary,),
            strategy_key="K",
            experiment_id_by_key=experiment_id_by_key,
            full_campaign_hash="h" * 64,
            campaign_run_id=_AliasStr("run-001"),
        )


def test_experiment_id_by_key_dict_subclass_rejected_before_canonical_sha256(
    monkeypatch,
):
    """A dict-SUBCLASS experiment_id_by_key mapping must be rejected --
    exact type(x) is dict, never isinstance -- BEFORE any canonical_sha256
    call."""
    import research_contracts.canonical_hash as canonical_hash_module

    class _DictSubclass(dict):
        pass

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    bad_mapping = _DictSubclass({("K", summary.config_id): "exp-abc"})
    with pytest.raises(ValueError, match="experiment_id_by_key"):
        cli._normalize_and_capture_summaries(
            (summary,),
            strategy_key="K",
            experiment_id_by_key=bad_mapping,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


class _TupleSubclass(tuple):
    pass


class _AliasStrForMappingComponents(str):
    pass


def _mutate_experiment_id_by_key_tuple_key_subclass(mapping):
    ((strategy_key, config_id), exp_id) = next(iter(mapping.items()))
    return {_TupleSubclass((strategy_key, config_id)): exp_id}


def _mutate_experiment_id_by_key_strategy_key_component_str_subclass(mapping):
    ((strategy_key, config_id), exp_id) = next(iter(mapping.items()))
    return {(_AliasStrForMappingComponents(strategy_key), config_id): exp_id}


def _mutate_experiment_id_by_key_config_id_component_str_subclass(mapping):
    ((strategy_key, config_id), exp_id) = next(iter(mapping.items()))
    return {(strategy_key, _AliasStrForMappingComponents(config_id)): exp_id}


def _mutate_experiment_id_by_key_experiment_id_value_str_subclass(mapping):
    ((strategy_key, config_id), exp_id) = next(iter(mapping.items()))
    return {(strategy_key, config_id): _AliasStrForMappingComponents(exp_id)}


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_experiment_id_by_key_tuple_key_subclass,
        _mutate_experiment_id_by_key_strategy_key_component_str_subclass,
        _mutate_experiment_id_by_key_config_id_component_str_subclass,
        _mutate_experiment_id_by_key_experiment_id_value_str_subclass,
    ],
    ids=[
        "tuple_key_subclass",
        "strategy_key_component_str_subclass",
        "config_id_component_str_subclass",
        "experiment_id_value_str_subclass",
    ],
)
def test_experiment_id_by_key_component_subclass_rejected_before_canonical_sha256(
    monkeypatch, mutate
):
    """Captain normalization reaudit (2026-07-18): not just the outer dict
    or the 2-tuple key's own container type -- each COMPONENT (the
    strategy_key/config_id inside the tuple key, and the experiment_id
    value) must ALSO be exact-str/exact-tuple gated individually. A str
    subclass in any of these three positions, or a tuple-subclass key
    itself, must be rejected BEFORE any canonical_sha256 call -- proven
    via the same zero-hash-calls spy pattern used throughout this
    module's pre-hash regressions."""
    import research_contracts.canonical_hash as canonical_hash_module

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    base_mapping = {("K", summary.config_id): "exp-abc"}
    bad_mapping = mutate(base_mapping)
    with pytest.raises(ValueError, match="experiment_id_by_key"):
        cli._normalize_and_capture_summaries(
            (summary,),
            strategy_key="K",
            experiment_id_by_key=bad_mapping,
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_canonical_scenario_and_fold_order_preserved_in_both_capture_and_evidence():
    """Captain design precision (2026-07-17, item 4): a deliberately
    reverse-ordered scenario_summaries/fold_selection_trace must come out
    canonically ordered (by scenario_name / fold_id) in BOTH the captured
    operator-visible object and the returned AttemptEvidence -- from a
    single normalization source, never independently re-derived (or
    forgotten) in one place but not the other."""
    from rob944_walkforward import ScenarioEvidenceSummary

    reversed_scenarios = (
        ScenarioEvidenceSummary(
            scenario_name="upward_stress",
            status="completed",
            reason_code=None,
            trade_count=2,
            artifact_hash="c" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="primary_stress",
            status="completed",
            reason_code=None,
            trade_count=3,
            artifact_hash="b" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="base",
            status="completed",
            reason_code=None,
            trade_count=3,
            artifact_hash="a" * 64,
            no_trade_reason_counts={},
        ),
    )
    summary = _fake_summary()
    object.__setattr__(summary, "scenario_summaries", reversed_scenarios)
    object.__setattr__(summary, "fold_selection_trace", _fake_full_fold_trace()[::-1])

    captured: list = []
    experiment_id_by_key = {("K", summary.config_id): "exp-abc"}
    evidence = cli._normalize_and_capture_summaries(
        (summary,),
        strategy_key="K",
        experiment_id_by_key=experiment_id_by_key,
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
        capture_summaries_into=captured,
    )
    expected_scenario_order = ("base", "primary_stress", "upward_stress")
    expected_fold_order = tuple(f"fold-{i:02d}" for i in range(8))

    assert (
        tuple(row.scenario_name for row in captured[0].scenario_summaries)
        == expected_scenario_order
    )
    assert (
        tuple(row.fold_id for row in captured[0].fold_selection_trace)
        == expected_fold_order
    )
    assert (
        tuple(se.scenario_name for se in evidence[0].scenario_evidence)
        == expected_scenario_order
    )


def test_sentinel_secret_reason_code_is_rejected_not_silently_persisted():
    """A caller that bypasses ``summarize_config_attempts_for_h6`` and hands
    ``_summary_to_attempt_evidence`` a raw-text/secret-looking reason_code
    directly must be REJECTED (fail closed), not silently persisted --
    ``_summary_to_attempt_evidence`` re-validates against the closed
    KNOWN_REASON_CODES allowlist."""
    sentinel = "sk-live-SUPERSECRETTOKEN-should-never-appear-in-hashes"
    summary = _fake_summary(status="crashed", reason_code=sentinel)
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_allowlisted_reason_code_is_accepted_and_never_leaks_into_hashes():
    from rob944_walkforward import REASON_CHILD_EXECUTION_CRASHED

    summary = _fake_summary(
        status="crashed", reason_code=REASON_CHILD_EXECUTION_CRASHED
    )
    evidence = cli._summary_to_attempt_evidence(
        summary,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert evidence.reason_code == REASON_CHILD_EXECUTION_CRASHED


def test_cross_status_pair_completed_status_with_crashed_reason_is_rejected():
    """Captain trust-boundary hole #2: a bare KNOWN_REASON_CODES membership
    check would WRONGLY accept status="completed" paired with
    reason_code="child_execution_crashed" (a known code, just not valid for
    "completed") -- the exact status-scoped contract must catch this
    cross-status pair before anything is hashed/persisted, since H6's own
    DTO has no status/reason-pair validation and the controller cannot
    catch it either (status/reason are folded into an opaque hash by then)."""
    from rob944_walkforward import REASON_CHILD_EXECUTION_CRASHED

    summary = _fake_summary(
        status="completed", reason_code=REASON_CHILD_EXECUTION_CRASHED
    )
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_cross_status_pair_rejected_status_with_timeout_reason_is_rejected():
    from rob944_walkforward import REASON_CHILD_EXECUTION_TIMEOUT

    summary = _fake_summary(
        status="rejected", reason_code=REASON_CHILD_EXECUTION_TIMEOUT
    )
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_scenario_level_cross_status_pair_is_rejected():
    """Same contract, applied to a per-scenario row (never_selected's
    reason is the fixed sentinel, never a crashed/timeout reason)."""
    from rob944_walkforward import REASON_CHILD_EXECUTION_CRASHED

    summary = _fake_summary()
    object.__setattr__(summary.scenario_summaries[0], "status", "never_selected")
    object.__setattr__(
        summary.scenario_summaries[0], "reason_code", REASON_CHILD_EXECUTION_CRASHED
    )
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


# ---------------------------------------------------------------------------
# Captain item E (2026-07-17): the pre-hash validation added to
# _summary_to_attempt_evidence (exact 3 unique canonical scenarios,
# per-scenario hex64 artifact_hash, non-bool/non-negative-int trade_count)
# had ZERO negative-path test coverage -- these prove rejection happens
# BEFORE anything is hashed/persisted, for each distinct malformed input.
# ---------------------------------------------------------------------------


def test_duplicate_scenario_name_is_rejected_before_hashing():
    """3 scenario_summaries rows, but two share the same scenario_name (a
    duplicate "base" instead of the canonical base/primary_stress/
    upward_stress trio) -- the set of unique names no longer equals the
    closed 3-name canonical set, even though the row COUNT is still 3."""
    from rob944_walkforward import ScenarioEvidenceSummary

    summary = _fake_summary()
    duplicated = (
        ScenarioEvidenceSummary(
            scenario_name="base",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="a" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="base",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="b" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="upward_stress",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="c" * 64,
            no_trade_reason_counts={},
        ),
    )
    object.__setattr__(summary, "scenario_summaries", duplicated)
    with pytest.raises(ValueError, match="exactly the 3 unique canonical scenarios"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_missing_scenario_name_is_rejected_before_hashing():
    """Only 2 scenario_summaries rows (upward_stress entirely absent) --
    row count alone (!= 3) must fail closed before hashing."""
    from rob944_walkforward import ScenarioEvidenceSummary

    summary = _fake_summary()
    only_two = (
        ScenarioEvidenceSummary(
            scenario_name="base",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="a" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="primary_stress",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="b" * 64,
            no_trade_reason_counts={},
        ),
    )
    object.__setattr__(summary, "scenario_summaries", only_two)
    with pytest.raises(ValueError, match="exactly the 3 unique canonical scenarios"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_wrong_scenario_name_is_rejected_before_hashing():
    """Exactly 3 unique names, but one is outside the closed canonical set
    (a caller-injected/typo'd scenario name) -- row count alone would pass,
    so this proves the SET-equality check (not just len==3) is what catches
    it."""
    from rob944_walkforward import ScenarioEvidenceSummary

    summary = _fake_summary()
    wrong_name = (
        ScenarioEvidenceSummary(
            scenario_name="base",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="a" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="primary_stress",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="b" * 64,
            no_trade_reason_counts={},
        ),
        ScenarioEvidenceSummary(
            scenario_name="SECRET-INJECTED-SCENARIO",
            status="completed",
            reason_code=None,
            trade_count=1,
            artifact_hash="c" * 64,
            no_trade_reason_counts={},
        ),
    )
    object.__setattr__(summary, "scenario_summaries", wrong_name)
    with pytest.raises(ValueError) as exc_info:
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )
    assert "SECRET-INJECTED-SCENARIO" not in str(exc_info.value)


def test_non_hex_artifact_hash_is_rejected_before_hashing():
    """A scenario's artifact_hash is not a well-formed lowercase 64-hex
    digest (e.g. a raw label accidentally passed through) -- rejected
    fail-closed, the offending value itself never echoed."""
    summary = _fake_summary()
    sentinel = "not-a-valid-hex64-hash-SECRET"
    object.__setattr__(summary.scenario_summaries[0], "artifact_hash", sentinel)
    with pytest.raises(ValueError) as exc_info:
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )
    assert sentinel not in str(exc_info.value)


def test_bool_trade_count_is_rejected_before_hashing():
    """trade_count=True must be rejected -- bool is a subclass of int in
    Python, so a bare ``isinstance(x, int)`` check alone would silently
    accept it as trade_count=1. Captain live-review correction
    (2026-07-17): now caught at NORMALIZATION time (``_assert_exact_int``,
    "must be an exact int"), earlier than the downstream nonnegative
    check's own message."""
    summary = _fake_summary()
    object.__setattr__(summary.scenario_summaries[0], "trade_count", True)
    with pytest.raises(ValueError, match="trade_count must be an exact int"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_non_int_trade_count_is_rejected_before_hashing():
    """A float trade_count (e.g. 3.0) must be rejected -- only a strict
    int is accepted, never anything merely numerically equal to one.
    Caught at NORMALIZATION time (``_assert_exact_int``)."""
    summary = _fake_summary()
    object.__setattr__(summary.scenario_summaries[0], "trade_count", 3.0)
    with pytest.raises(ValueError, match="trade_count must be an exact int"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_int_subclass_trade_count_is_rejected_before_hashing():
    """Captain live-review correction (2026-07-17): an int SUBCLASS (not
    merely bool) must also be rejected -- a downstream ``isinstance(x,
    int)`` check would have wrongly accepted it (isinstance follows the
    subclass MRO); ``_assert_exact_int``'s ``type(x) is int`` catches it
    at NORMALIZATION time instead, before it ever reaches the normalized
    DTO."""

    class _IntSubclass(int):
        pass

    summary = _fake_summary()
    object.__setattr__(summary.scenario_summaries[0], "trade_count", _IntSubclass(3))
    with pytest.raises(ValueError, match="trade_count must be an exact int"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_negative_trade_count_is_rejected_before_hashing():
    summary = _fake_summary()
    object.__setattr__(summary.scenario_summaries[0], "trade_count", -1)
    with pytest.raises(ValueError, match="non-nonnegative-int trade_count"):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def _mutate_duplicate_scenario_name(summary):
    from rob944_walkforward import ScenarioEvidenceSummary

    object.__setattr__(
        summary,
        "scenario_summaries",
        (
            ScenarioEvidenceSummary(
                scenario_name="base",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="a" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="base",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="b" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="upward_stress",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="c" * 64,
                no_trade_reason_counts={},
            ),
        ),
    )


def _mutate_missing_scenario(summary):
    from rob944_walkforward import ScenarioEvidenceSummary

    object.__setattr__(
        summary,
        "scenario_summaries",
        (
            ScenarioEvidenceSummary(
                scenario_name="base",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="a" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="primary_stress",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="b" * 64,
                no_trade_reason_counts={},
            ),
        ),
    )


def _mutate_wrong_scenario_name(summary):
    from rob944_walkforward import ScenarioEvidenceSummary

    object.__setattr__(
        summary,
        "scenario_summaries",
        (
            ScenarioEvidenceSummary(
                scenario_name="base",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="a" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="primary_stress",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="b" * 64,
                no_trade_reason_counts={},
            ),
            ScenarioEvidenceSummary(
                scenario_name="SECRET-INJECTED-SCENARIO",
                status="completed",
                reason_code=None,
                trade_count=1,
                artifact_hash="c" * 64,
                no_trade_reason_counts={},
            ),
        ),
    )


def _mutate_non_hex_artifact_hash(summary):
    object.__setattr__(
        summary.scenario_summaries[0], "artifact_hash", "not-a-valid-hex64-hash"
    )


def _mutate_bool_trade_count(summary):
    object.__setattr__(summary.scenario_summaries[0], "trade_count", True)


def _mutate_non_int_trade_count(summary):
    object.__setattr__(summary.scenario_summaries[0], "trade_count", 3.0)


def _mutate_negative_trade_count(summary):
    object.__setattr__(summary.scenario_summaries[0], "trade_count", -1)


def _mutate_fold_rejected_secret_control_string(summary):
    """Captain final-audit semantic correction (2026-07-17): a pure auditor
    probe fed ``rejected="SECRET-CONTROL\\n"`` (a truthy non-bool) straight
    into the ``if row.rejected:`` branch -- FoldSelectionEvidenceSummary's
    ``bool`` type hint is not runtime-enforced (a plain frozen dataclass,
    no validator)."""
    object.__setattr__(summary.fold_selection_trace[0], "rejected", "SECRET-CONTROL\n")


def _mutate_fold_rejected_int_one(summary):
    """int 1 is truthy and would pass silently through a naive
    ``if row.rejected:`` check as if it were ``True`` -- must still be
    rejected as not a bool (``type(x) is bool``), not merely accepted
    because it is truthy or numerically equal to ``True``."""
    object.__setattr__(summary.fold_selection_trace[0], "rejected", 1)


def _mutate_fold_eligible_symbols_as_set(summary):
    """Captain adjacent trust-boundary correction (2026-07-17, item A): a
    set-valued eligible_symbols would still pass every membership/
    uniqueness/coverage check (same content), but its iteration order
    varies with PYTHONHASHSEED once accepted -- corrupting
    canonical_sha256/operator JSON determinism. Rejected purely for being
    the wrong container TYPE, before any of that."""
    object.__setattr__(
        summary.fold_selection_trace[0],
        "eligible_symbols",
        frozenset({"BTCUSDT", "XRPUSDT"}),
    )


def _mutate_fold_excluded_symbols_as_set(summary):
    object.__setattr__(
        summary.fold_selection_trace[0],
        "excluded_symbols",
        frozenset(
            {
                ("DOGEUSDT", "insufficient_symbol_evidence"),
                ("SOLUSDT", "insufficient_symbol_evidence"),
            }
        ),
    )


def _mutate_fold_excluded_entry_not_2_tuple(summary):
    object.__setattr__(
        summary.fold_selection_trace[0],
        "excluded_symbols",
        (
            ["DOGEUSDT", "insufficient_symbol_evidence"],  # list, not a tuple
            ("SOLUSDT", "insufficient_symbol_evidence"),
        ),
    )


def _mutate_fold_eligible_symbols_reversed_order(summary):
    """Same 2 symbols, same set, a genuinely valid tuple container -- but
    in the REVERSE of the frozen UNIVERSE's own order. Valid content is
    not enough; canonical order is required too."""
    object.__setattr__(
        summary.fold_selection_trace[0], "eligible_symbols", ("XRPUSDT", "BTCUSDT")
    )


def _mutate_fold_excluded_symbols_reversed_order(summary):
    object.__setattr__(
        summary.fold_selection_trace[0],
        "excluded_symbols",
        (
            ("SOLUSDT", "insufficient_symbol_evidence"),
            ("DOGEUSDT", "insufficient_symbol_evidence"),
        ),
    )


def _mutate_fold_equal_weight_expectancy_bool(summary):
    object.__setattr__(
        summary.fold_selection_trace[0], "equal_weight_expectancy_bps", True
    )


def _mutate_fold_pooled_expectancy_decimal(summary):
    from decimal import Decimal

    object.__setattr__(
        summary.fold_selection_trace[0], "pooled_expectancy_bps", Decimal("1.25")
    )


def _mutate_fold_profit_factor_none(summary):
    object.__setattr__(summary.fold_selection_trace[0], "profit_factor", None)


def _mutate_fold_rejected_true_but_expectancy_non_null(summary):
    """A rejected/non-rejected consistency violation: rejected=True with a
    VALID matching rejection_reason (so the earlier rejected/reason-code
    check does not mask this one), but equal_weight_expectancy_bps is
    still a real (non-None) float -- must be rejected as None-required-
    when-rejected."""
    from rob944_selection import INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON

    row = summary.fold_selection_trace[0]
    object.__setattr__(row, "rejected", True)
    object.__setattr__(row, "rejection_reason", INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON)
    # equal_weight_expectancy_bps stays a real float (5.0) -- the violation.


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_duplicate_scenario_name,
        _mutate_missing_scenario,
        _mutate_wrong_scenario_name,
        _mutate_non_hex_artifact_hash,
        _mutate_bool_trade_count,
        _mutate_non_int_trade_count,
        _mutate_negative_trade_count,
        _mutate_fold_rejected_secret_control_string,
        _mutate_fold_rejected_int_one,
        _mutate_fold_eligible_symbols_as_set,
        _mutate_fold_excluded_symbols_as_set,
        _mutate_fold_excluded_entry_not_2_tuple,
        _mutate_fold_eligible_symbols_reversed_order,
        _mutate_fold_excluded_symbols_reversed_order,
        _mutate_fold_equal_weight_expectancy_bool,
        _mutate_fold_pooled_expectancy_decimal,
        _mutate_fold_profit_factor_none,
        _mutate_fold_rejected_true_but_expectancy_non_null,
    ],
    ids=[
        "duplicate_scenario_name",
        "missing_scenario",
        "wrong_scenario_name",
        "non_hex_artifact_hash",
        "bool_trade_count",
        "non_int_trade_count",
        "negative_trade_count",
        "fold_rejected_secret_control_string",
        "fold_rejected_int_one",
        "fold_eligible_symbols_as_set",
        "fold_excluded_symbols_as_set",
        "fold_excluded_entry_not_2_tuple",
        "fold_eligible_symbols_reversed_order",
        "fold_excluded_symbols_reversed_order",
        "fold_equal_weight_expectancy_bool",
        "fold_pooled_expectancy_decimal",
        "fold_profit_factor_none",
        "fold_rejected_true_but_expectancy_non_null",
    ],
)
def test_pre_hash_validation_rejects_before_any_canonical_sha256_call(
    monkeypatch, mutate
):
    """P1-E ordering proof: naming a test "before_hashing" is not itself
    evidence of ordering -- this spies on canonical_sha256 directly (the
    function _summary_to_attempt_evidence locally imports and calls to
    build fold_evidence_hash/run_identity) and monkeypatches it to raise
    AssertionError if ever called. For each representative invalid mutation,
    only a ValueError (never the AssertionError) may propagate -- proving
    genuine pre-hash rejection, not merely an eventual ValueError from
    somewhere downstream. _summary_to_attempt_evidence has no persistence
    call of its own (that's the controller's job), so zero hash calls is
    the correct pre-persistence proof at this layer."""
    import research_contracts.canonical_hash as canonical_hash_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "canonical_sha256 must never be called -- pre-hash validation should have "
            "rejected this input first"
        )

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    summary = _fake_summary()
    mutate(summary)
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_fold_rejected_non_bool_secret_control_string_never_leaks_and_is_type_exact(
    monkeypatch, capsys
):
    """Captain final-audit semantic correction (2026-07-17), dedicated
    sentinel-leak proof: the exact offending value
    ``"SECRET-CONTROL\\n"`` must never appear in the raised exception's own
    message, and (since --run's stdout print only ever happens AFTER a
    successful, fully-validated _summary_to_attempt_evidence call, which
    this input never reaches) there is no JSON/persistence side effect to
    check either -- captured stdout must remain empty."""
    import research_contracts.canonical_hash as canonical_hash_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "canonical_sha256 must never be called for this rejected input"
        )

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)

    sentinel = "SECRET-CONTROL\n"
    summary = _fake_summary()
    object.__setattr__(summary.fold_selection_trace[0], "rejected", sentinel)
    with pytest.raises(ValueError) as exc_info:
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )
    message = str(exc_info.value)
    assert sentinel not in message
    assert "SECRET-CONTROL" not in message
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Captain normalization-boundary correction (2026-07-17): a caller-owned
# mapping/tuple/dataclass can be a SUBCLASS/PROXY that returns benign
# content on a first read and secret-bearing content on a LATER read
# (validate-then-hash/output is a TOCTOU hole if the same object is read
# twice). The fix (_normalize_config_attempt_evidence_summary et al.)
# requires EXACT canonical runtime types everywhere and snapshots every
# field/container/dict in ONE pass -- these regressions prove: (a) the
# stateful hook is never even invoked (exact-type rejection happens BEFORE
# any .items()/__iter__ call), and (b) the secret never appears in any
# exception message, captured stdout, or (transitively) any hash payload,
# since canonical_sha256 is never reached at all.
# ---------------------------------------------------------------------------


class _StatefulDict(dict):
    """A dict SUBCLASS whose .items() returns a benign empty view on its
    first call and a SECRET-bearing view on any later call -- pure/
    deterministic (call-count based, no wall-clock/randomness)."""

    def __init__(self, secret_items):
        super().__init__()
        self.secret_items = secret_items
        self.items_call_count = 0

    def items(self):
        self.items_call_count += 1
        if self.items_call_count == 1:
            return {}.items()
        return dict(self.secret_items).items()


class _StatefulSecretTuple(tuple):
    """A tuple SUBCLASS whose __iter__ returns a benign empty view on its
    first call and a SECRET-bearing view on any later call. Used for every
    tuple-typed container field (scenario_summaries/fold_selection_trace/
    eligible_symbols/excluded_symbols) to prove exact-type rejection
    happens BEFORE any iteration at all -- ``iter_call_count`` stays 0."""

    def __new__(cls, secret_items):
        obj = super().__new__(cls, ())
        obj.secret_items = tuple(secret_items)
        obj.iter_call_count = 0
        return obj

    def __iter__(self):
        self.iter_call_count += 1
        if self.iter_call_count == 1:
            return iter(())
        return iter(self.secret_items)


def _assert_secret_absent_everywhere(monkeypatch, secret, run_fn):
    """Shared assertion shape for every normalization-boundary regression:
    canonical_sha256 must never be called (spy raises AssertionError if
    it is), a ValueError (never the spy's AssertionError) must propagate,
    and the secret must never appear in the exception message or captured
    stdout/stderr."""
    import research_contracts.canonical_hash as canonical_hash_module

    def _boom(*args, **kwargs):
        raise AssertionError("canonical_sha256 must never be called for this input")

    monkeypatch.setattr(canonical_hash_module, "canonical_sha256", _boom)
    with pytest.raises(ValueError) as exc_info:
        run_fn()
    message = str(exc_info.value)
    assert secret not in message
    return exc_info


def test_stateful_dict_subclass_scenario_no_trade_reason_counts_rejected_before_any_items_call(
    monkeypatch, capsys
):
    secret = "SECRET-CONTROL-scenario-counts\n"
    stateful = _StatefulDict({secret: 1})
    summary = _fake_summary()
    object.__setattr__(
        summary.scenario_summaries[0], "no_trade_reason_counts", stateful
    )
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    # Exact-type rejection (type(value) is not dict) fires BEFORE any
    # .items() call -- the stateful hook is never even invoked.
    assert stateful.items_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_stateful_dict_subclass_fold_no_trade_reason_counts_rejected_before_any_items_call(
    monkeypatch, capsys
):
    secret = "SECRET-CONTROL-fold-counts\n"
    stateful = _StatefulDict({secret: 1})
    summary = _fake_summary()
    object.__setattr__(
        summary.fold_selection_trace[0], "no_trade_reason_counts", stateful
    )
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert stateful.items_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_stateful_tuple_subclass_scenario_summaries_rejected_before_any_iteration(
    monkeypatch, capsys
):
    from rob944_walkforward import ScenarioEvidenceSummary

    secret = "SECRET-CONTROL-scenario-name\n"
    secret_row = ScenarioEvidenceSummary(
        scenario_name=secret,
        status="completed",
        reason_code=None,
        trade_count=1,
        artifact_hash="a" * 64,
        no_trade_reason_counts={},
    )
    stateful = _StatefulSecretTuple((secret_row,))
    summary = _fake_summary()
    object.__setattr__(summary, "scenario_summaries", stateful)
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert stateful.iter_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_stateful_tuple_subclass_fold_selection_trace_rejected_before_any_iteration(
    monkeypatch, capsys
):
    secret = "SECRET-CONTROL-fold-trace\n"
    secret_row = _fake_fold_trace_row(fold_id=secret)
    stateful = _StatefulSecretTuple((secret_row,))
    summary = _fake_summary()
    object.__setattr__(summary, "fold_selection_trace", stateful)
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert stateful.iter_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_stateful_tuple_subclass_eligible_symbols_rejected_before_any_iteration(
    monkeypatch, capsys
):
    secret = "SECRET-CONTROL-eligible-symbol\n"
    stateful = _StatefulSecretTuple((secret,))
    summary = _fake_summary()
    object.__setattr__(summary.fold_selection_trace[0], "eligible_symbols", stateful)
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert stateful.iter_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_stateful_tuple_subclass_excluded_symbols_rejected_before_any_iteration(
    monkeypatch, capsys
):
    secret = "SECRET-CONTROL-excluded-symbol\n"
    stateful = _StatefulSecretTuple(((secret, "insufficient_symbol_evidence"),))
    summary = _fake_summary()
    object.__setattr__(summary.fold_selection_trace[0], "excluded_symbols", stateful)
    _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert stateful.iter_call_count == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_config_attempt_evidence_summary_subclass_proxy_is_rejected():
    """The OUTER summary object itself being a subclass/proxy of
    ConfigAttemptEvidenceSummary (not merely a nested field) must also be
    rejected -- type(summary) is not ConfigAttemptEvidenceSummary."""
    from rob944_walkforward import ConfigAttemptEvidenceSummary

    class _ProxySummary(ConfigAttemptEvidenceSummary):
        pass

    base = _fake_summary()
    proxy = _ProxySummary(
        strategy=base.strategy,
        config_id=base.config_id,
        status=base.status,
        reason_code=base.reason_code,
        scenario_summaries=base.scenario_summaries,
        fold_selection_trace=base.fold_selection_trace,
    )
    with pytest.raises(ValueError, match="exact ConfigAttemptEvidenceSummary"):
        cli._summary_to_attempt_evidence(
            proxy,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_alias_str_subclass_scenario_name_never_bypasses_membership_check(monkeypatch):
    """Captain normalization detail: an AliasStr-style str SUBCLASS whose
    __eq__/__hash__ are overridden to equal an allowed literal (so a bare
    membership check would wrongly accept it) must still be rejected --
    exact type(x) is str is checked BEFORE any allowlist/membership
    comparison, so the override can never even reach that comparison."""

    class _AliasStr(str):
        def __new__(cls, real_value, alias_target):
            obj = super().__new__(cls, real_value)
            obj._alias_target = alias_target
            return obj

        def __eq__(self, other):
            return other == self._alias_target

        def __hash__(self):
            return hash(self._alias_target)

    secret = "SECRET-CONTROL-alias-scenario-name"
    alias = _AliasStr(secret, "base")  # claims to equal "base" via __eq__/__hash__
    assert alias == "base"  # the override genuinely fools bare equality/membership
    assert str(alias) == secret  # but its ACTUAL buffer content is the secret

    summary = _fake_summary()
    rows = list(summary.scenario_summaries)
    from rob944_walkforward import ScenarioEvidenceSummary

    rows[0] = ScenarioEvidenceSummary(
        scenario_name=alias,
        status="completed",
        reason_code=None,
        trade_count=rows[0].trade_count,
        artifact_hash=rows[0].artifact_hash,
        no_trade_reason_counts={},
    )
    object.__setattr__(summary, "scenario_summaries", tuple(rows))

    exc_info = _assert_secret_absent_everywhere(
        monkeypatch,
        secret,
        lambda: cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        ),
    )
    assert "scenario_name" in str(exc_info.value)


def test_known_reason_histogram_content_preserved_through_normalization():
    """Preserve existing known-reason-histogram behavior: a genuine (non-
    adversarial) no_trade_reason_counts dict with real known-reason keys
    and correct int counts must survive normalization UNCHANGED and still
    be reachable in the fold_evidence_hash payload -- proven indirectly
    via a content-sensitivity check (changing a real count changes
    run_identity)."""
    # Captain live test review (2026-07-17): a FIXED, documented allowed
    # key -- not next(iter(frozenset)) (seed-dependent iteration order) --
    # confirmed to be an actual member of the closed no-trade allowlist.
    known_reason = "next_bar_unavailable"
    assert known_reason in cli._known_no_trade_reasons()

    summary_a = _fake_summary()
    summary_b = _fake_summary()
    object.__setattr__(
        summary_a.scenario_summaries[0],
        "no_trade_reason_counts",
        {known_reason: 3},
    )
    object.__setattr__(
        summary_b.scenario_summaries[0],
        "no_trade_reason_counts",
        {known_reason: 7},
    )
    evidence_a = cli._summary_to_attempt_evidence(
        summary_a,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    evidence_b = cli._summary_to_attempt_evidence(
        summary_b,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert evidence_a.run_identity != evidence_b.run_identity
    assert evidence_a.fold_evidence_hash != evidence_b.fold_evidence_hash


# ---------------------------------------------------------------------------
# Captain trust-boundary addendum (2026-07-17): no_trade_reason_counts is
# untrusted input from a caller-injected callback -- keys must be checked
# against the closed known-reasons allowlist and values must be
# nonnegative ints (never bool-as-int), BEFORE anything is hashed/printed.
# ---------------------------------------------------------------------------


def test_sentinel_secret_no_trade_reason_key_is_rejected_not_silently_persisted():
    sentinel_key = "SECRET-INJECTED-KEY-should-never-appear"
    summary = _fake_summary()
    object.__setattr__(
        summary.scenario_summaries[0], "no_trade_reason_counts", {sentinel_key: 1}
    )
    with pytest.raises(ValueError) as exc_info:
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )
    assert sentinel_key not in str(exc_info.value)


def test_negative_no_trade_reason_count_is_rejected():
    summary = _fake_summary()
    object.__setattr__(
        summary.scenario_summaries[0],
        "no_trade_reason_counts",
        {"next_bar_unavailable": -1},
    )
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_bool_masquerading_as_no_trade_reason_count_is_rejected():
    summary = _fake_summary()
    object.__setattr__(
        summary.scenario_summaries[0],
        "no_trade_reason_counts",
        {"next_bar_unavailable": True},
    )
    with pytest.raises(ValueError):
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )


def test_known_no_trade_reasons_is_the_exact_12_code_closed_set():
    """Captain allowlist correction (2026-07-17): H3 S2's fixed rejection
    set is SIX codes (rob940_signal_s2.py:142-150,213,227), not two --
    confirmation_failed, next_bar_unavailable, target_direction_invalid,
    tp_above_max, tp_below_r_min_sl, tp_below_abs_floor (next_bar_unavailable
    shared with H2). Combined with H2's other 4 and H4's 2 funding-gate
    reasons: exactly 12 unique codes total (5 + 2 + 5, since S2's
    next_bar_unavailable dedupes against H2's)."""
    known = cli._known_no_trade_reasons()
    assert known == frozenset(
        {
            "next_bar_unavailable",
            "daily_stop_active",
            "daily_entry_cap",
            "cooldown_active",
            "tp_below_min_distance",
            "funding_evidence_unavailable",
            "expected_funding_cost_above_3bps",
            "confirmation_failed",
            "target_direction_invalid",
            "tp_above_max",
            "tp_below_r_min_sl",
            "tp_below_abs_floor",
        }
    )
    assert len(known) == 12


def test_known_no_trade_reasons_matches_real_h3_s2_gate_rejections_no_drift():
    """Captain correction (2026-07-17): H3's merged rob940_signal_s2.py
    source (pinned SHA 762e850e46a0e5f9529e019a1daa34aab0f6f3e4601678aef6db55b95db6f830)
    must NEVER be edited just to export this allowlist -- it stays a
    literal, hand-verified copy. This test is the drift guard: it calls
    the REAL H3 ``_evaluate_target_gates`` function with synthetic inputs
    engineered to trigger each of its 4 magnitude/direction rejection
    reasons, and asserts each one it ACTUALLY returns is present in this
    module's known-reasons set -- if H3 ever renames/adds/removes a reason,
    this test (not just a hand-maintained literal) fails."""
    from rob940_signal_s2 import _evaluate_target_gates

    known = cli._known_no_trade_reasons()

    _passed, reason_direction, _d = _evaluate_target_gates(
        side="long", entry_price=100.0, target_price=99.0, sl_distance=100.0, r_min=1.2
    )
    assert reason_direction == "target_direction_invalid"
    assert reason_direction in known

    _passed, reason_above_max, _d = _evaluate_target_gates(
        side="long", entry_price=100.0, target_price=101.5, sl_distance=100.0, r_min=1.2
    )
    assert reason_above_max == "tp_above_max"
    assert reason_above_max in known

    # sl_distance is a FRACTIONAL price distance (e.g. 0.0075 == 75bp), the
    # same unit as (target_price/entry_price - 1) -- not a bare bps number.
    _passed, reason_below_r_min, _d = _evaluate_target_gates(
        side="long",
        entry_price=100.0,
        target_price=100.80,
        sl_distance=0.0075,
        r_min=1.2,
    )
    assert (
        reason_below_r_min == "tp_below_r_min_sl"
    )  # r_min_sl_bps=90 > d_tp_bps=80 > floor=68
    assert reason_below_r_min in known

    _passed, reason_below_floor, _d = _evaluate_target_gates(
        side="long",
        entry_price=100.0,
        target_price=100.50,
        sl_distance=0.0010,
        r_min=1.0,
    )
    assert (
        reason_below_floor == "tp_below_abs_floor"
    )  # r_min_sl_bps=10 < d_tp_bps=50 < floor=68
    assert reason_below_floor in known

    # confirmation_failed/next_bar_unavailable require full bar-level
    # generate_s2_signals integration to trigger directly; verified via a
    # textual presence check against the real (pinned) H3 source instead.
    import inspect

    import rob940_signal_s2

    source = inspect.getsource(rob940_signal_s2)
    assert '"confirmation_failed"' in source
    assert '"next_bar_unavailable"' in source


@pytest.mark.parametrize(
    "reason",
    [
        "next_bar_unavailable",
        "daily_stop_active",
        "daily_entry_cap",
        "cooldown_active",
        "tp_below_min_distance",
        "funding_evidence_unavailable",
        "expected_funding_cost_above_3bps",
        "confirmation_failed",
        "target_direction_invalid",
        "tp_above_max",
        "tp_below_r_min_sl",
        "tp_below_abs_floor",
    ],
)
def test_each_known_no_trade_reason_is_individually_accepted(reason):
    summary = _fake_summary()
    object.__setattr__(
        summary.scenario_summaries[0], "no_trade_reason_counts", {reason: 1}
    )
    evidence = cli._summary_to_attempt_evidence(  # must not raise
        summary,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert evidence is not None


def test_known_no_trade_reason_counts_are_accepted_and_bound_into_the_hash():
    summary_a = _fake_summary()
    summary_b = _fake_summary()
    object.__setattr__(
        summary_a.scenario_summaries[0],
        "no_trade_reason_counts",
        {"next_bar_unavailable": 3, "cooldown_active": 1},
    )
    object.__setattr__(
        summary_b.scenario_summaries[0],
        "no_trade_reason_counts",
        {"next_bar_unavailable": 5},
    )
    ev_a = cli._summary_to_attempt_evidence(  # must not raise
        summary_a,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    ev_b = cli._summary_to_attempt_evidence(  # must not raise
        summary_b,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert (
        ev_a.fold_evidence_hash != ev_b.fold_evidence_hash
    )  # genuinely bound into the hash


def test_sentinel_secret_no_trade_reason_key_in_fold_selection_trace_is_rejected():
    summary = _fake_summary()
    sentinel_key = "SECRET-INJECTED-FOLD-KEY"
    bad_row = _fake_fold_trace_row()
    object.__setattr__(bad_row, "no_trade_reason_counts", {sentinel_key: 1})
    object.__setattr__(summary, "fold_selection_trace", (bad_row,))
    with pytest.raises(ValueError) as exc_info:
        cli._summary_to_attempt_evidence(
            summary,
            strategy_key="K",
            experiment_id="exp-abc",
            full_campaign_hash="h" * 64,
            campaign_run_id="run-001",
        )
    assert sentinel_key not in str(exc_info.value)


# ---------------------------------------------------------------------------
# captain BLOCKING controller audit (item 7): empirical_success must be
# PRIMARY-only and retry-safe -- report.status_counts aggregates ALL
# attempts including retries, so a naive completed-count check alone is
# fooled by a completed retry masking a crashed primary.
# ---------------------------------------------------------------------------


def _fake_report(
    *,
    verdict="complete",
    expected_total=24,
    total_attempts=24,
    retry_attempts=0,
    completed=24,
):
    return SimpleNamespace(
        verdict=verdict,
        expected_total=expected_total,
        total_attempts=total_attempts,
        retry_attempts=retry_attempts,
        status_counts={"completed": completed},
    )


def test_all_24_primaries_completed_no_retries_is_empirical_success():
    assert cli._is_empirical_success(_fake_report()) is True


def test_accounting_incomplete_is_never_empirical_success():
    assert cli._is_empirical_success(_fake_report(verdict="incomplete")) is False


def test_all_24_accounted_but_not_all_completed_is_not_empirical_success():
    """Accounting-complete (every attempt recorded) but every attempt
    crashed/rejected/timeout -- accounting completeness is NOT empirical
    success."""
    assert cli._is_empirical_success(_fake_report(completed=0)) is False


def test_23_completed_plus_1_crashed_primary_plus_1_completed_retry_is_not_empirical_success():
    """The exact captain-specified regression fixture: a completed RETRY
    of a crashed primary experiment inflates status_counts["completed"] to
    24 without every PRIMARY having actually succeeded. total_attempts=25
    (24 primaries + 1 retry) and retry_attempts=1 must both independently
    catch this -- a naive ``status_counts["completed"] == expected_total``
    check alone would have wrongly returned True here."""
    report = _fake_report(total_attempts=25, retry_attempts=1, completed=24)
    assert (
        report.status_counts["completed"] == report.expected_total
    )  # the naive check would pass
    assert cli._is_empirical_success(report) is False


def test_only_total_attempts_mismatch_alone_is_caught_retry_attempts_otherwise_correct():
    """Isolate the ``total_attempts == expected_total`` conjunct: mutate
    ONLY total_attempts (an extra row exists) while retry_attempts=0 and the
    completed-count naive check would still pass -- proving this conjunct
    alone (not merely in combination with a simultaneously-wrong
    retry_attempts) is sufficient to fail closed."""
    report = _fake_report(total_attempts=25, retry_attempts=0, completed=24)
    assert (
        report.status_counts["completed"] == report.expected_total
    )  # naive check would pass
    assert report.retry_attempts == 0  # this conjunct alone is NOT what catches it
    assert cli._is_empirical_success(report) is False


def test_only_retry_attempts_mismatch_alone_is_caught_total_attempts_otherwise_correct():
    """Isolate the ``retry_attempts == 0`` conjunct: mutate ONLY
    retry_attempts (a retry was recorded) while total_attempts still equals
    expected_total and the completed-count naive check would still pass --
    proving this conjunct alone (not merely in combination with a
    simultaneously-wrong total_attempts) is sufficient to fail closed."""
    report = _fake_report(total_attempts=24, retry_attempts=1, completed=24)
    assert (
        report.status_counts["completed"] == report.expected_total
    )  # naive check would pass
    assert (
        report.total_attempts == report.expected_total
    )  # this conjunct alone is NOT what catches it
    assert cli._is_empirical_success(report) is False


# ---------------------------------------------------------------------------
# Captain P1 findings (2026-07-17): fold_evidence_hash/run_identity must
# bind the attempt's own status/reason_code (two rejected summaries with
# identical scenario rows but DIFFERENT rejection reasons previously
# collided) AND the per-fold TRAIN selection trace (previously entirely
# absent -- a TRAIN-only mutation that never changed which config won OOS
# was invisible to the final hash).
# ---------------------------------------------------------------------------


def _fake_fold_trace_row(
    fold_id="fold-00",
    train_input_hash="1" * 64,
    profit_factor=1.5,
    equal_weight_expectancy_bps=5.0,
    pooled_expectancy_bps=5.0,
):
    from rob944_walkforward import FoldSelectionEvidenceSummary

    return FoldSelectionEvidenceSummary(
        fold_id=fold_id,
        fold_selected_config_id="S1-00",
        eligible_symbols=("BTCUSDT", "XRPUSDT"),
        excluded_symbols=(
            ("DOGEUSDT", "insufficient_symbol_evidence"),
            ("SOLUSDT", "insufficient_symbol_evidence"),
        ),
        equal_weight_expectancy_bps=equal_weight_expectancy_bps,
        pooled_expectancy_bps=pooled_expectancy_bps,
        profit_factor=profit_factor,
        rejected=False,
        rejection_reason=None,
        train_input_hash=train_input_hash,
        no_trade_reason_counts={},
    )


def _fake_full_fold_trace(*, override_fold_id="fold-00", **row_kwargs):
    """8 canonical fold rows (fold-00..fold-07) -- the closed
    fold_selection_trace contract requires exactly these 8 unique fold IDs
    for every NON-global attempt. ``row_kwargs`` overrides apply ONLY to
    the row at ``override_fold_id``; every other row uses plain defaults."""
    rows = []
    for i in range(8):
        fid = f"fold-{i:02d}"
        if fid == override_fold_id:
            rows.append(_fake_fold_trace_row(fold_id=fid, **row_kwargs))
        else:
            rows.append(_fake_fold_trace_row(fold_id=fid))
    return tuple(rows)


def test_train_only_mutation_changes_fold_evidence_hash_and_run_identity():
    """A TRAIN-only mutation (train_input_hash differs; attempt status/
    scenario_summaries/which-config-won are UNCHANGED) must still change
    the final fold_evidence_hash/run_identity -- previously invisible since
    fold_selection_trace was never bound into either hash."""
    summary_a = _fake_summary()
    summary_b = _fake_summary()
    object.__setattr__(
        summary_a,
        "fold_selection_trace",
        _fake_full_fold_trace(train_input_hash="a" * 64),
    )
    object.__setattr__(
        summary_b,
        "fold_selection_trace",
        _fake_full_fold_trace(train_input_hash="b" * 64),
    )

    ev_a = cli._summary_to_attempt_evidence(
        summary_a,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    ev_b = cli._summary_to_attempt_evidence(
        summary_b,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert ev_a.fold_evidence_hash != ev_b.fold_evidence_hash
    assert ev_a.run_identity != ev_b.run_identity


def test_fold_selection_trace_reorder_does_not_change_the_hash():
    summary_a = _fake_summary()
    summary_b = _fake_summary()
    forward = _fake_full_fold_trace()
    reversed_trace = tuple(reversed(forward))
    object.__setattr__(summary_a, "fold_selection_trace", forward)
    object.__setattr__(summary_b, "fold_selection_trace", reversed_trace)

    ev_a = cli._summary_to_attempt_evidence(
        summary_a,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    ev_b = cli._summary_to_attempt_evidence(
        summary_b,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert ev_a.fold_evidence_hash == ev_b.fold_evidence_hash
    assert ev_a.run_identity == ev_b.run_identity


def test_nonfinite_profit_factor_does_not_raise_and_still_changes_the_hash():
    """A rejected candidate's profit_factor is math.nan; a legitimate
    zero-loss winner's can be math.inf -- both must be bound into the hash
    (never dropped) without ever raising (canonical_sha256 itself rejects
    raw non-finite floats)."""
    summary_nan = _fake_summary()
    summary_inf = _fake_summary()
    object.__setattr__(
        summary_nan,
        "fold_selection_trace",
        _fake_full_fold_trace(profit_factor=float("nan")),
    )
    object.__setattr__(
        summary_inf,
        "fold_selection_trace",
        _fake_full_fold_trace(profit_factor=float("inf")),
    )

    ev_nan = cli._summary_to_attempt_evidence(  # must not raise
        summary_nan,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    ev_inf = cli._summary_to_attempt_evidence(  # must not raise
        summary_inf,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert ev_nan.fold_evidence_hash != ev_inf.fold_evidence_hash


def test_identical_scenarios_but_different_rejection_reason_changes_fold_evidence_hash():
    """Captain P1 (independent audit): two rejected attempt summaries with
    IDENTICAL scenario_summaries but DIFFERENT rejection reasons
    (data_gap vs insufficient_train_evidence) must no longer collide --
    the attempt's own status/reason_code is now bound into fold_evidence_hash."""
    from rob944_walkforward import (
        REASON_DATA_GAP_IN_POSITION,
        REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
    )

    summary_gap = _fake_summary(
        status="rejected", reason_code=REASON_DATA_GAP_IN_POSITION
    )
    summary_insufficient = _fake_summary(
        status="rejected", reason_code=REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS
    )

    ev_gap = cli._summary_to_attempt_evidence(
        summary_gap,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    ev_insufficient = cli._summary_to_attempt_evidence(
        summary_insufficient,
        strategy_key="K",
        experiment_id="exp-abc",
        full_campaign_hash="h" * 64,
        campaign_run_id="run-001",
    )
    assert ev_gap.fold_evidence_hash != ev_insufficient.fold_evidence_hash
    assert ev_gap.run_identity != ev_insufficient.run_identity


# ---------------------------------------------------------------------------
# Fable Q1 FINAL (orch-fable-answer-rob944b-20260717.md): campaign_run_id
# suffix is 43-char unpadded URL-safe base64 (full 256-bit entropy), NOT
# hex, NOT a truncation. CLI and controller derivations must agree
# bit-for-bit, and the resulting AttemptKey.idempotency_key() must fit
# H6's existing trial_idempotency_key VARCHAR(128) (exact length 125).
# ---------------------------------------------------------------------------

_B64URL_NO_PAD_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def test_campaign_run_id_suffix_is_43_char_unpadded_base64url_not_hex():
    full_campaign_hash = "3" * 64
    run_id = cli._derive_primary_campaign_run_id(full_campaign_hash)
    assert run_id.startswith("rob944-primary-")
    suffix = run_id[len("rob944-primary-") :]
    assert len(suffix) == 43
    assert _B64URL_NO_PAD_RE.match(suffix)
    assert "=" not in suffix
    # Not a (truncated-or-not) hex digest -- a 43-char base64url string will
    # essentially always contain at least one non-hex-alphabet character
    # (A-Z, or one of "-_"); assert this explicitly rather than just by length.
    assert not _HEX_RE.match(suffix)


def test_campaign_run_id_suffix_round_trips_the_full_32_byte_digest():
    from research_contracts.canonical_hash import canonical_sha256

    full_campaign_hash = "4" * 64
    run_id = cli._derive_primary_campaign_run_id(full_campaign_hash)
    suffix = run_id[len("rob944-primary-") :]

    expected_digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    expected_bytes = bytes.fromhex(expected_digest_hex)

    # Un-pad -> re-pad -> decode: must recover the exact same 32 raw bytes.
    padded = suffix + "=" * (-len(suffix) % 4)
    decoded_bytes = base64.urlsafe_b64decode(padded)
    assert decoded_bytes == expected_bytes
    assert len(decoded_bytes) == 32


def test_cli_and_controller_campaign_run_id_derivation_agree_bit_for_bit():
    from app.services.rob944_campaign_controller import _derive_expected_campaign_run_id

    full_campaign_hash = "5" * 64
    assert cli._derive_primary_campaign_run_id(
        full_campaign_hash
    ) == _derive_expected_campaign_run_id(full_campaign_hash)


def test_attempt_key_idempotency_key_exact_length_125_for_real_production_format():
    from app.schemas.research_campaign_bridge import AttemptKey

    full_campaign_hash = "6" * 64
    campaign_run_id = cli._derive_primary_campaign_run_id(
        full_campaign_hash
    )  # 58 chars
    experiment_id = "e" * 64  # H6's own unchanged full-hex experiment_id format
    key = AttemptKey(
        campaign_run_id=campaign_run_id, experiment_id=experiment_id, retry_index=0
    )
    assert len(campaign_run_id) == 58
    assert len(key.idempotency_key()) == 125
    assert len(key.idempotency_key()) <= 128


def test_campaign_run_id_matches_the_independent_golden_vector():
    """Captain Fable-b test completeness: an independent, hand-verified
    golden vector (not derived FROM the implementation under test, so the
    implementation cannot be its own oracle) for
    full_campaign_hash="0"*64. If this ever changes, the derivation
    recipe itself changed -- a fact that must be deliberate, documented,
    and Fable-consulted, never an accidental byproduct of an unrelated
    refactor."""
    full_campaign_hash = "0" * 64
    run_id = cli._derive_primary_campaign_run_id(full_campaign_hash)
    assert run_id == "rob944-primary-4BXyzKXLdP4yxjb_7E5k6IhukhDhOlOwJEya22gc5-o"
    suffix = run_id[len("rob944-primary-") :]
    assert len(suffix) == 43
    assert len(run_id) == 58
    assert _B64URL_NO_PAD_RE.match(suffix)

    from app.schemas.research_campaign_bridge import AttemptKey
    from app.services.rob944_campaign_controller import _derive_expected_campaign_run_id

    assert _derive_expected_campaign_run_id(full_campaign_hash) == run_id
    key = AttemptKey(campaign_run_id=run_id, experiment_id="e" * 64, retry_index=0)
    assert len(key.idempotency_key()) == 125


# ---------------------------------------------------------------------------
# Captain item D (2026-07-17): direct --plan/--run output-contract
# regressions for spec_deviations/campaign_run_id_derivation -- these two
# top-level fields were added earlier this session with NO test asserting
# their exact shape/content.
# ---------------------------------------------------------------------------


def test_plan_spec_deviations_matches_real_h3_s2_source_verbatim():
    """The verbatim Korean S2 spec-deviation sentence(s) (Fable condition 2)
    must survive unchanged from H3's own pinned SPEC_DEVIATIONS constant --
    never re-worded/paraphrased by this CLI's own plan-building code."""
    from rob940_signal_s2 import SPEC_DEVIATIONS

    plan = cli.build_plan()
    assert plan["spec_deviations"] == list(SPEC_DEVIATIONS)
    assert plan["spec_deviations"] == plan["h3_fixed_constants"]["s2_spec_deviations"]


def test_plan_campaign_run_id_derivation_contract_exact_shape():
    """The campaign_run_id_derivation dict must document the EXACT recipe
    actually implemented by _derive_primary_campaign_run_id -- prefix/
    lengths in particular must match the real derived value, not just be
    plausible-looking documentation text."""
    plan = cli.build_plan()
    derivation = plan["campaign_run_id_derivation"]
    assert derivation == {
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
    }
    # Cross-check the documented lengths/prefix against the REAL derivation
    # for THIS plan's own full_campaign_hash -- not just a hardcoded example.
    real_run_id = cli._derive_primary_campaign_run_id(plan["full_campaign_hash"])
    assert real_run_id == plan["expected_campaign_run_id"]
    assert real_run_id.startswith(derivation["prefix"])
    assert len(real_run_id) == derivation["campaign_run_id_length"]


def test_run_output_spec_deviations_matches_plan(monkeypatch, capsys):
    """--run's JSON output must echo the SAME spec_deviations value --plan
    reports (both sourced from the identical plain["h3_fixed_constants"]
    field), never a divergent/independently-maintained copy."""
    plan = cli.build_plan()
    exit_code = _run_main_with_fake_controller(
        monkeypatch, lambda: _lifecycle_fake_report()
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spec_deviations"] == plan["spec_deviations"]


def test_run_output_train_selection_trace_includes_expectancy_and_train_input_hash_fields(
    monkeypatch, capsys
):
    """Captain item D: the operator-visible --run train_selection_trace was
    missing equal_weight_expectancy_bps/pooled_expectancy_bps/profit_factor/
    train_input_hash (already bound into fold_evidence_hash upstream) --
    must now be present with the exact values the summary carries."""
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _FakeSessionLocal())

    fake_summary = _fake_summary(
        fold_selection_trace=_fake_full_fold_trace(
            train_input_hash="7" * 64, profit_factor=2.25
        )
    )

    def _fake_build_real_attempt_evidence(
        experiment_id_by_key,
        *,
        full_campaign_hash,
        campaign_run_id,
        capture_summaries_into=None,
    ):
        if capture_summaries_into is not None:
            capture_summaries_into.append(fake_summary)
        return []

    monkeypatch.setattr(
        cli, "_build_real_attempt_evidence", _fake_build_real_attempt_evidence
    )

    class _FakeReport:
        verdict = "complete"
        total_attempts = 24
        expected_total = 24
        retry_attempts = 0
        status_counts = {"completed": 24}
        actual_registrations = 24
        primary_attempts = 24

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            kwargs["build_attempt_evidence"]({})
            return _FakeReport()

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)

    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    trace = out["per_config_scenario_evidence"][0]["train_selection_trace"]
    assert len(trace) == 8
    fold_00 = next(row for row in trace if row["fold_id"] == "fold-00")
    assert fold_00["equal_weight_expectancy_bps"] == 5.0
    assert fold_00["pooled_expectancy_bps"] == 5.0
    assert fold_00["profit_factor"] == 2.25
    assert fold_00["train_input_hash"] == "7" * 64


def test_run_output_train_selection_trace_encodes_nonfinite_metrics_as_stable_sentinels(
    monkeypatch, capsys
):
    """P1-D last test gap: NaN/+Inf/-Inf across the three float metrics
    (equal_weight_expectancy_bps/pooled_expectancy_bps/profit_factor) must
    survive into --run stdout as the EXACT _json_safe_float_or_sentinel
    string tokens -- never a bare (non-strict-JSON) NaN/Infinity token, and
    never silently dropped/coerced to null."""
    import math

    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _FakeSessionLocal())

    fake_summary = _fake_summary(
        fold_selection_trace=_fake_full_fold_trace(
            equal_weight_expectancy_bps=math.nan,
            pooled_expectancy_bps=math.inf,
            profit_factor=-math.inf,
        )
    )

    def _fake_build_real_attempt_evidence(
        experiment_id_by_key,
        *,
        full_campaign_hash,
        campaign_run_id,
        capture_summaries_into=None,
    ):
        if capture_summaries_into is not None:
            capture_summaries_into.append(fake_summary)
        return []

    monkeypatch.setattr(
        cli, "_build_real_attempt_evidence", _fake_build_real_attempt_evidence
    )

    class _FakeReport:
        verdict = "complete"
        total_attempts = 24
        expected_total = 24
        retry_attempts = 0
        status_counts = {"completed": 24}
        actual_registrations = 24
        primary_attempts = 24

    class _FakeController:
        @staticmethod
        async def run_full_campaign(*args, **kwargs):
            kwargs["build_attempt_evidence"]({})
            return _FakeReport()

    monkeypatch.setattr(cli, "_import_campaign_controller", lambda: _FakeController)

    plan = cli.build_plan()
    exit_code = cli.main(
        [
            "--run",
            "--expected-full-campaign-hash",
            plan["full_campaign_hash"],
            "--campaign-run-id",
            plan["expected_campaign_run_id"],
        ]
    )
    assert exit_code == 0
    raw = capsys.readouterr().out
    assert "NaN" not in raw
    assert "Infinity" not in raw
    out = json.loads(
        raw
    )  # would still parse even with a leaked bare NaN token -- the substring checks above are the real assertion
    trace = out["per_config_scenario_evidence"][0]["train_selection_trace"]
    fold_00 = next(row for row in trace if row["fold_id"] == "fold-00")
    assert fold_00["equal_weight_expectancy_bps"] == "nonfinite:nan"
    assert fold_00["pooled_expectancy_bps"] == "nonfinite:inf"
    assert fold_00["profit_factor"] == "nonfinite:-inf"
