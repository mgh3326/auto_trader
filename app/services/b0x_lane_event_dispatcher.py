"""Default-off, fixed-registry dispatcher for durable B0X lane events.

The ingress poller owns transport observation.  This module starts only a
closed set of already-reviewed B0X runners after an installer-owned binding,
the durable event receipt, a host-local dispatch fence, and the runner's own
lower writer-lock namespace all agree.  Event text can never supply argv,
paths, accounts, environment values, or an executable.

The dispatch claim is the upper event-start authority.  Each existing runner
retains its per-lane :mod:`scripts.b0x.ledger` lock as a lower defence.  Neither
SQLite nor the host-local ``flock`` is represented as a distributed lease.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.services.b0x_lane_consumer import (
    _connect,
    _eligible_at,
    _payload,
    _received_clock,
)

DISPATCH_BINDING_VERSION = "b0x-dispatch/v1"
DISPATCH_READY_STATUS = "READY_NOT_ACTIVATED"
DISPATCH_DRAFT_STATUS = "DRAFT_NOT_INSTALLED"
POLICY_RECEIPT_MARKER = "B0X_POLICY_STAGE_RECEIPT_JSON="

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TABLE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_REF = re.compile(r"^refs/remotes/origin/[A-Za-z0-9._/-]+$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

SOURCE_RUNNERS = {
    "b0x-nudge-kr": "kr_observation",
    "b0x-nudge-us": "us_observation",
    "b0x-nudge-crypto": "crypto_shadow",
    "b0x-table-kr": "policy_table_build_kr",
    "b0x-table-us": "policy_table_build_us",
    "b0x-harvest": "harvest_observe_only",
}
RUNNER_MODULES = {
    "kr_observation": "scripts.run_b0x_kr_kiwoom_cycle",
    "us_observation": "scripts.run_b0x_us_cycle",
    "crypto_shadow": "scripts.run_b0x_cycle",
}
RUNNER_LANES = {
    "kr_observation": "kiwoom_mock",
    "us_observation": "alpaca_paper_lab",
    "crypto_shadow": "upbit_shadow",
}
RUNNER_MARKETS = {
    "kr_observation": "kr",
    "us_observation": "us",
    "crypto_shadow": "crypto",
    "policy_table_build_kr": "kr",
    "policy_table_build_us": "us",
}
_CYCLE_RUNNERS = frozenset(RUNNER_MODULES)
_ELIGIBLE_DISPOSITIONS = frozenset(
    {"queued_cycle", "queued_policy_table_build", "observed_harvest_no_cycle"}
)
_TERMINAL_TYPES = frozenset(
    {
        "success_observed",
        "zero_order_observed",
        "failed_preserved",
        "unknown_preserved",
    }
)
_FORBIDDEN_ARGV = frozenset(
    {
        "--confirm",
        "--ordering",
        "--bounded-send",
        "--readiness",
        "--now",
        "--sidecar",
        "--repeat",
        "--derivation-only",
        "--seal",
        "--durable-ports-factory",
    }
)
_NON_CREDENTIAL_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
    }
)
_POLICY_GIT_AUTH_ENV_ALLOWLIST = frozenset({"HOME", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME"})
_POLICY_STEPS = (
    "dirty_check",
    "fetch_approved_ref",
    "detached_checkout",
    "non_table_tree_hash_compare",
    "pointer_readlink_blob_compare",
    "table_hash_capture",
)


class B0XDispatchError(ValueError):
    """A safe, stable refusal code for a dispatch contract violation."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LowerWriterLockBusy(B0XDispatchError):
    """The subordinate runner writer lock is already owned."""


@dataclass(frozen=True)
class RunnerBinding:
    ready: bool
    table_dir: Path
    output_root: Path
    environment_ref: Path
    writer_lock_path: Path
    account_ref: str
    lower_lock_owner_receipt: str
    no_incomplete_attempt_receipt: str


@dataclass(frozen=True)
class PolicyBinding:
    checkout: Path
    repo_identity: str
    approved_ref: str
    table_subtree: str
    output_root: Path
    projection_sha256: str
    approved_non_table_tree_sha256: str
    isolation_receipt_sha256: str
    receipt_root: Path
    git_identity_source: str
    credential_source: str


@dataclass(frozen=True)
class RuntimeBinding:
    state_db: Path
    lock_path: Path
    auto_trader_root: Path
    auto_trader_head: str
    python: Path
    uv: Path
    prefect_root: Path
    prefect_head: str
    prefect_python: Path
    observation_root: Path


@dataclass(frozen=True)
class B0XDispatchBinding:
    path: Path
    binding_sha256: str
    status: str
    active: bool
    gates: Mapping[str, bool]
    owner_machine: str
    owner_id: str
    owner_epoch: str
    lane: str
    runtime: RuntimeBinding
    policy: PolicyBinding
    runners: Mapping[str, RunnerBinding]
    source_runners: Mapping[str, str]
    source_owners: Mapping[str, Mapping[str, object]]
    account_contract: Mapping[str, object]
    account_contract_sha256: str
    install_receipt: Mapping[str, object]
    ready_receipt: Mapping[str, object]


@dataclass(frozen=True)
class ProcessStart:
    pid: int
    start_identity: str
    started_at: datetime


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    started: ProcessStart
    ended_at: datetime
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    receipt: Mapping[str, object] | None = None


@dataclass(frozen=True)
class StagePlan:
    stage_id: str
    kind: str
    argv: tuple[str, ...]
    cwd: Path
    installed_head: str
    environment_ref: Path | None = None


@dataclass(frozen=True)
class ArtifactSnapshot:
    lane_dir: Path
    cycle_log: Path
    cycle_log_size: int
    artifact_names: frozenset[str]


@dataclass(frozen=True)
class ArtifactEvidence:
    path: str
    sha256: str
    byte_count: int
    cycle_id: str | None
    table_hash: str | None
    observed_at: str
    zero_order_reason: str | None


@dataclass(frozen=True)
class DispatchReceipt:
    status: str
    lane: str | None = None
    event_id: str | None = None
    attempt_id: str | None = None
    runner_id: str | None = None
    disposition: str | None = None
    queue_disposition: str | None = None
    claimed_at: str | None = None
    process_started_at: str | None = None
    cycle_observed_at: str | None = None
    terminal_type: str | None = None
    terminal_verified: bool = False
    cycle_starts: int = 0
    push_reapplications: int = 0
    children_started: int = 0
    reason: str | None = None
    binding_sha256: str | None = None
    source: str | None = None
    owner_epoch: str | None = None
    payload_sha256: str | None = None
    process_ended_at: str | None = None
    terminal_ended_at: str | None = None
    exit_code: int | None = None
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    artifact_bytes: int | None = None
    consumed_table_hash: str | None = None
    cycle_id: str | None = None
    policy_preflight_head: str | None = None
    post_build_head: str | None = None
    post_commit_head: str | None = None
    processes: tuple[Mapping[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class Executor(Protocol):
    def __call__(
        self,
        stage: StagePlan,
        *,
        on_started: Callable[[ProcessStart], None],
        timeout_seconds: float,
    ) -> ProcessResult: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object, *, keys: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise B0XDispatchError(code)
    return value


def _string(value: object, *, code: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise B0XDispatchError(code)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise B0XDispatchError(code)
    return value


def _absolute_path(
    value: object,
    *,
    code: str,
    kind: str,
    must_exist: bool = True,
) -> Path:
    if not isinstance(value, str):
        raise B0XDispatchError(code)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise B0XDispatchError(code)
    try:
        parent = path.parent.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
    except (FileNotFoundError, OSError) as exc:
        raise B0XDispatchError(code) from exc
    if resolved != path or resolved.parent != parent and kind == "file":
        raise B0XDispatchError(code)
    if must_exist:
        valid = resolved.is_dir() if kind == "directory" else resolved.is_file()
        if not valid:
            raise B0XDispatchError(code)
    if kind == "executable" and (
        not resolved.is_file() or not os.access(resolved, os.X_OK)
    ):
        raise B0XDispatchError(code)
    return resolved


def _git_value(
    root: Path,
    args: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    result = runner(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise B0XDispatchError("binding_git_identity_unavailable")
    return result.stdout.strip()


def _attest_git_worktree(
    root: Path,
    *,
    expected_head: str | None,
    expected_identity: str | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    if (
        Path(
            _git_value(root, ("rev-parse", "--show-toplevel"), runner=runner)
        ).resolve()
        != root
    ):
        raise B0XDispatchError("binding_git_root_mismatch")
    head = _git_value(root, ("rev-parse", "HEAD"), runner=runner)
    if _HEX40.fullmatch(head) is None:
        raise B0XDispatchError("binding_git_head_invalid")
    if expected_head is not None and head != expected_head:
        raise B0XDispatchError("installed_code_head_mismatch")
    if expected_identity is not None:
        identity = _git_value(root, ("remote", "get-url", "origin"), runner=runner)
        if identity != expected_identity:
            raise B0XDispatchError("policy_repo_identity_mismatch")
    if _git_value(
        root,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        runner=runner,
    ):
        raise B0XDispatchError("binding_git_worktree_dirty")
    return head


def _runner_binding(
    raw: object,
    *,
    runner_id: str,
    observation_root: Path,
    policy_output: Path,
) -> RunnerBinding:
    value = _mapping(
        raw,
        keys={
            "ready",
            "table_dir",
            "output_root",
            "environment_ref",
            "writer_lock_path",
            "account_ref",
            "lower_lock_owner_receipt",
            "no_incomplete_attempt_receipt",
        },
        code=f"runner_{runner_id}_fields_invalid",
    )
    lane = RUNNER_LANES[runner_id]
    table_dir = _absolute_path(
        value["table_dir"],
        code=f"runner_{runner_id}_table_dir_invalid",
        kind="directory",
    )
    output_root = _absolute_path(
        value["output_root"],
        code=f"runner_{runner_id}_output_root_invalid",
        kind="directory",
    )
    environment_ref = _absolute_path(
        value["environment_ref"],
        code=f"runner_{runner_id}_environment_ref_invalid",
        kind="file",
    )
    writer_lock_path = _absolute_path(
        value["writer_lock_path"],
        code=f"runner_{runner_id}_writer_lock_invalid",
        kind="file",
        must_exist=False,
    )
    expected_lock = observation_root / f".{lane}.writer.lock"
    if table_dir != policy_output:
        raise B0XDispatchError(f"runner_{runner_id}_policy_checkout_mismatch")
    if output_root != observation_root or writer_lock_path != expected_lock:
        raise B0XDispatchError(f"runner_{runner_id}_output_lock_namespace_mismatch")
    ready = value["ready"]
    if type(ready) is not bool:
        raise B0XDispatchError(f"runner_{runner_id}_ready_invalid")
    return RunnerBinding(
        ready=ready,
        table_dir=table_dir,
        output_root=output_root,
        environment_ref=environment_ref,
        writer_lock_path=writer_lock_path,
        account_ref=_string(
            value["account_ref"], code=f"runner_{runner_id}_account_ref_invalid"
        ),
        lower_lock_owner_receipt=_string(
            value["lower_lock_owner_receipt"],
            code=f"runner_{runner_id}_lower_lock_receipt_missing",
        ),
        no_incomplete_attempt_receipt=_string(
            value["no_incomplete_attempt_receipt"],
            code=f"runner_{runner_id}_incomplete_attempt_receipt_missing",
        ),
    )


def load_dispatch_binding(
    path: Path,
    *,
    state_db: Path,
    git_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> B0XDispatchBinding:
    """Load a closed public schema without reading any environment file."""

    binding_path = _absolute_path(
        path.as_posix(), code="binding_path_invalid", kind="file"
    )
    try:
        raw_bytes = binding_path.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise B0XDispatchError("binding_json_invalid") from exc
    value = _mapping(
        raw,
        keys={
            "version",
            "status",
            "active",
            "gates",
            "owner",
            "runtime",
            "policy",
            "runners",
            "source_runners",
            "source_owners",
            "account_contract",
            "account_contract_sha256",
            "install_receipt",
            "ready_receipt",
        },
        code="binding_fields_invalid",
    )
    if value["version"] != DISPATCH_BINDING_VERSION:
        raise B0XDispatchError("binding_version_invalid")
    if value["status"] not in {DISPATCH_READY_STATUS, DISPATCH_DRAFT_STATUS}:
        raise B0XDispatchError("binding_status_invalid")
    if type(value["active"]) is not bool:
        raise B0XDispatchError("binding_active_invalid")
    gates = _mapping(
        value["gates"],
        keys={"ingress_enabled", "dispatch_enabled", "source_enabled"},
        code="binding_gates_invalid",
    )
    if any(type(gates[key]) is not bool for key in gates):
        raise B0XDispatchError("binding_gates_invalid")
    owner = _mapping(
        value["owner"],
        keys={"machine", "id", "epoch", "lane"},
        code="binding_owner_invalid",
    )
    owner_machine = _string(owner["machine"], code="binding_owner_machine_invalid")
    owner_id = _string(owner["id"], code="binding_owner_id_invalid")
    owner_epoch = _string(owner["epoch"], code="binding_owner_epoch_invalid")
    lane = _string(owner["lane"], code="binding_lane_invalid", pattern=_SAFE_ID)

    runtime_raw = _mapping(
        value["runtime"],
        keys={
            "state_db",
            "lock_path",
            "auto_trader_root",
            "auto_trader_head",
            "python",
            "uv",
            "prefect_root",
            "prefect_head",
            "prefect_python",
            "observation_root",
        },
        code="binding_runtime_invalid",
    )
    runtime_state_db = _absolute_path(
        runtime_raw["state_db"],
        code="binding_state_db_invalid",
        kind="file",
        must_exist=False,
    )
    supplied_state_db = _absolute_path(
        state_db.as_posix(),
        code="runtime_state_db_invalid",
        kind="file",
        must_exist=False,
    )
    if runtime_state_db != supplied_state_db:
        raise B0XDispatchError("binding_state_db_mismatch")
    runtime = RuntimeBinding(
        state_db=runtime_state_db,
        lock_path=_absolute_path(
            runtime_raw["lock_path"],
            code="binding_lock_path_invalid",
            kind="file",
            must_exist=False,
        ),
        auto_trader_root=_absolute_path(
            runtime_raw["auto_trader_root"],
            code="binding_auto_root_invalid",
            kind="directory",
        ),
        auto_trader_head=_string(
            runtime_raw["auto_trader_head"],
            code="binding_auto_head_invalid",
            pattern=_HEX40,
        ),
        python=_absolute_path(
            runtime_raw["python"], code="binding_python_invalid", kind="executable"
        ),
        uv=_absolute_path(
            runtime_raw["uv"], code="binding_uv_invalid", kind="executable"
        ),
        prefect_root=_absolute_path(
            runtime_raw["prefect_root"],
            code="binding_prefect_root_invalid",
            kind="directory",
        ),
        prefect_head=_string(
            runtime_raw["prefect_head"],
            code="binding_prefect_head_invalid",
            pattern=_HEX40,
        ),
        prefect_python=_absolute_path(
            runtime_raw["prefect_python"],
            code="binding_prefect_python_invalid",
            kind="executable",
        ),
        observation_root=_absolute_path(
            runtime_raw["observation_root"],
            code="binding_observation_root_invalid",
            kind="directory",
        ),
    )
    _attest_git_worktree(
        runtime.auto_trader_root,
        expected_head=runtime.auto_trader_head,
        expected_identity=None,
        runner=git_runner,
    )
    _attest_git_worktree(
        runtime.prefect_root,
        expected_head=runtime.prefect_head,
        expected_identity=None,
        runner=git_runner,
    )

    policy_raw = _mapping(
        value["policy"],
        keys={
            "checkout",
            "repo_identity",
            "approved_ref",
            "table_subtree",
            "output_root",
            "projection_sha256",
            "approved_non_table_tree_sha256",
            "isolation_receipt_sha256",
            "receipt_root",
            "git_identity_source",
            "credential_source",
        },
        code="binding_policy_invalid",
    )
    policy_checkout = _absolute_path(
        policy_raw["checkout"], code="policy_checkout_invalid", kind="directory"
    )
    table_subtree = _string(
        policy_raw["table_subtree"], code="policy_table_subtree_invalid"
    )
    if table_subtree != "policy-tables":
        raise B0XDispatchError("policy_table_subtree_invalid")
    policy_output = _absolute_path(
        policy_raw["output_root"], code="policy_output_root_invalid", kind="directory"
    )
    if policy_output != policy_checkout / table_subtree:
        raise B0XDispatchError("policy_output_checkout_mismatch")
    approved_ref = _string(
        policy_raw["approved_ref"],
        code="policy_approved_ref_invalid",
        pattern=_SAFE_REF,
    )
    repo_identity = _string(
        policy_raw["repo_identity"], code="policy_repo_identity_invalid"
    )
    _attest_git_worktree(
        policy_checkout,
        expected_head=None,
        expected_identity=repo_identity,
        runner=git_runner,
    )
    policy = PolicyBinding(
        checkout=policy_checkout,
        repo_identity=repo_identity,
        approved_ref=approved_ref,
        table_subtree=table_subtree,
        output_root=policy_output,
        projection_sha256=_string(
            policy_raw["projection_sha256"],
            code="policy_projection_hash_invalid",
            pattern=_HEX64,
        ),
        approved_non_table_tree_sha256=_string(
            policy_raw["approved_non_table_tree_sha256"],
            code="policy_non_table_tree_hash_invalid",
            pattern=_HEX64,
        ),
        isolation_receipt_sha256=_string(
            policy_raw["isolation_receipt_sha256"],
            code="policy_isolation_receipt_invalid",
            pattern=_HEX64,
        ),
        receipt_root=_absolute_path(
            policy_raw["receipt_root"],
            code="policy_receipt_root_invalid",
            kind="directory",
        ),
        git_identity_source=_string(
            policy_raw["git_identity_source"], code="policy_git_identity_source_invalid"
        ),
        credential_source=_string(
            policy_raw["credential_source"], code="policy_credential_source_invalid"
        ),
    )

    runners_raw = _mapping(
        value["runners"], keys=set(_CYCLE_RUNNERS), code="binding_runners_invalid"
    )
    runners = {
        runner_id: _runner_binding(
            runners_raw[runner_id],
            runner_id=runner_id,
            observation_root=runtime.observation_root,
            policy_output=policy.output_root,
        )
        for runner_id in sorted(_CYCLE_RUNNERS)
    }
    source_runners = _mapping(
        value["source_runners"],
        keys=set(SOURCE_RUNNERS),
        code="source_runner_registry_invalid",
    )
    if dict(source_runners) != SOURCE_RUNNERS:
        raise B0XDispatchError("source_runner_registry_mismatch")
    source_owners = _mapping(
        value["source_owners"],
        keys={"kr_policy", "us_policy", "crypto_policy"},
        code="source_owner_registry_invalid",
    )
    account_contract = _mapping(
        value["account_contract"],
        keys={"version", "source_accounts", "single_writer_receipt_sha256"},
        code="account_contract_fields_invalid",
    )
    source_accounts = _mapping(
        account_contract["source_accounts"],
        keys={"b0x-nudge-kr", "b0x-nudge-us", "b0x-nudge-crypto"},
        code="account_source_map_invalid",
    )
    for source, runner_id in (
        ("b0x-nudge-kr", "kr_observation"),
        ("b0x-nudge-us", "us_observation"),
        ("b0x-nudge-crypto", "crypto_shadow"),
    ):
        if source_accounts[source] != runners[runner_id].account_ref:
            raise B0XDispatchError("account_source_runner_mismatch")
    _string(
        account_contract["single_writer_receipt_sha256"],
        code="account_single_writer_receipt_invalid",
        pattern=_HEX64,
    )
    account_contract_sha256 = _string(
        value["account_contract_sha256"],
        code="account_contract_hash_invalid",
        pattern=_HEX64,
    )
    if _sha256(_canonical_json(account_contract)) != account_contract_sha256:
        raise B0XDispatchError("account_contract_hash_mismatch")
    if policy.projection_sha256 != account_contract_sha256:
        raise B0XDispatchError("policy_account_projection_mismatch")
    install_receipt = _mapping(
        value["install_receipt"],
        keys={"id", "manifest_sha256", "owner_fence_sha256"},
        code="install_receipt_invalid",
    )
    ready_receipt = _mapping(
        value["ready_receipt"],
        keys={
            "id",
            "code_head",
            "ingress_binding_sha256",
            "route_sink_sha256",
            "source_owner_sha256",
            "no_other_host_owner_sha256",
        },
        code="ready_receipt_invalid",
    )
    return B0XDispatchBinding(
        path=binding_path,
        binding_sha256=_sha256(raw_bytes),
        status=str(value["status"]),
        active=bool(value["active"]),
        gates={key: bool(gates[key]) for key in gates},
        owner_machine=owner_machine,
        owner_id=owner_id,
        owner_epoch=owner_epoch,
        lane=lane,
        runtime=runtime,
        policy=policy,
        runners=runners,
        source_runners=dict(source_runners),
        source_owners={key: dict(source_owners[key]) for key in source_owners},
        account_contract=dict(account_contract),
        account_contract_sha256=account_contract_sha256,
        install_receipt=dict(install_receipt),
        ready_receipt=dict(ready_receipt),
    )


def binding_blockers(binding: B0XDispatchBinding) -> tuple[str, ...]:
    blockers: list[str] = []
    if binding.status != DISPATCH_READY_STATUS:
        blockers.append("binding_not_ready")
    if not binding.active:
        blockers.append("binding_not_active")
    for gate in ("ingress_enabled", "dispatch_enabled", "source_enabled"):
        if binding.gates[gate] is not True:
            blockers.append(f"{gate}_false")
    for label, receipt in (
        ("install", binding.install_receipt),
        ("ready", binding.ready_receipt),
    ):
        if any(not isinstance(value, str) or not value for value in receipt.values()):
            blockers.append(f"{label}_receipt_incomplete")
    if binding.ready_receipt.get("code_head") != binding.runtime.auto_trader_head:
        blockers.append("ready_receipt_code_head_mismatch")
    for field in (
        "manifest_sha256",
        "owner_fence_sha256",
    ):
        if _HEX64.fullmatch(str(binding.install_receipt.get(field, ""))) is None:
            blockers.append(f"install_receipt_{field}_invalid")
    for field in (
        "ingress_binding_sha256",
        "route_sink_sha256",
        "source_owner_sha256",
        "no_other_host_owner_sha256",
    ):
        if _HEX64.fullmatch(str(binding.ready_receipt.get(field, ""))) is None:
            blockers.append(f"ready_receipt_{field}_invalid")
    return tuple(blockers)


def _source_owner_blocker(binding: B0XDispatchBinding, source: str) -> str | None:
    if source in {"b0x-table-kr", "b0x-nudge-kr"}:
        owner = binding.source_owners["kr_policy"]
        if set(owner) != {
            "selected",
            "direct_scheduled_enabled",
            "queued_dispatch_enabled",
        }:
            return "kr_policy_owner_fields_invalid"
        direct = owner["direct_scheduled_enabled"] is True
        queued = owner["queued_dispatch_enabled"] is True
        expected = "direct_scheduled" if direct else "queued_dispatch"
        if direct == queued or owner["selected"] != expected:
            return "kr_policy_source_double_or_wrong_owner"
        if source == "b0x-table-kr" and not queued:
            return "kr_policy_queue_source_not_owner"
    if source in {"b0x-table-us", "b0x-nudge-us"}:
        owner = binding.source_owners["us_policy"]
        if set(owner) != {
            "selected",
            "direct_scheduled_enabled",
            "queued_dispatch_enabled",
        }:
            return "us_policy_owner_fields_invalid"
        direct = owner["direct_scheduled_enabled"] is True
        queued = owner["queued_dispatch_enabled"] is True
        expected = "direct_scheduled" if direct else "queued_dispatch"
        if direct == queued or owner["selected"] != expected:
            return "us_policy_source_double_or_wrong_owner"
        if source == "b0x-table-us" and not queued:
            return "us_policy_queue_source_not_owner"
    if source == "b0x-nudge-crypto":
        owner = binding.source_owners["crypto_policy"]
        if dict(owner) != {
            "selected": "crypto_pipeline",
            "crypto_pipeline_enabled": True,
        }:
            return "crypto_policy_owner_invalid"
    return None


def _source_blocker(
    binding: B0XDispatchBinding, source: str, runner_id: str
) -> str | None:
    owner = _source_owner_blocker(binding, source)
    if owner is not None:
        return owner
    if runner_id in _CYCLE_RUNNERS:
        runner = binding.runners[runner_id]
        if not runner.ready:
            return f"runner_{runner_id}_not_ready"
        module_path = binding.runtime.auto_trader_root / (
            RUNNER_MODULES[runner_id].replace(".", "/") + ".py"
        )
        if not module_path.is_file() or module_path.is_symlink():
            return f"runner_{runner_id}_handler_absent"
    if runner_id in _CYCLE_RUNNERS or runner_id.startswith("policy_table_build_"):
        handler = (
            binding.runtime.prefect_root / "src/robin_automation/b0x_policy_dispatch.py"
        )
        if not handler.is_file() or handler.is_symlink():
            return "prefect_policy_handler_absent"
    return None


def _ensure_dispatch_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS b0x_dispatch_attempt (
            lane TEXT NOT NULL,
            event_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            runner_id TEXT NOT NULL,
            binding_sha256 TEXT NOT NULL,
            owner_epoch TEXT NOT NULL,
            state TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            process_started_at TEXT,
            cycle_observed_at TEXT,
            terminal_type TEXT,
            terminal_verified INTEGER NOT NULL DEFAULT 0 CHECK (terminal_verified IN (0,1)),
            ended_at TEXT,
            outcome_reason TEXT,
            artifact_path TEXT,
            artifact_sha256 TEXT,
            artifact_bytes INTEGER,
            cycle_id TEXT,
            consumed_table_hash TEXT,
            policy_preflight_head TEXT,
            post_build_head TEXT,
            post_commit_head TEXT,
            cycle_starts INTEGER NOT NULL DEFAULT 0,
            push_reapplications INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (lane, event_id)
        );
        CREATE TABLE IF NOT EXISTS b0x_dispatch_process (
            lane TEXT NOT NULL,
            event_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            stage_index INTEGER NOT NULL,
            stage_id TEXT NOT NULL,
            executable TEXT NOT NULL,
            argv_sha256 TEXT NOT NULL,
            cwd TEXT NOT NULL,
            installed_head TEXT NOT NULL,
            owner_epoch TEXT NOT NULL,
            environment_ref_sha256 TEXT,
            pid INTEGER NOT NULL,
            start_identity TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            exit_code INTEGER,
            PRIMARY KEY (lane, event_id, stage_index)
        );
        CREATE TABLE IF NOT EXISTS b0x_dispatch_active_source (
            source TEXT PRIMARY KEY,
            lane TEXT NOT NULL,
            event_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS b0x_dispatch_refusal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            lane TEXT,
            event_id TEXT,
            reason TEXT NOT NULL
        );
        """
    )


@contextmanager
def stable_dispatch_lock(binding: B0XDispatchBinding):  # noqa: ANN201
    """Acquire the binding's stable host-local fence; never delete it."""

    path = binding.runtime.lock_path
    if path.is_symlink():
        raise B0XDispatchError("dispatch_lock_symlink_rejected")
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise B0XDispatchError("dispatch_lock_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _record_refusal(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    reason: str,
    lane: str | None = None,
    event_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO b0x_dispatch_refusal(observed_at,lane,event_id,reason) VALUES (?,?,?,?)",
        (now.astimezone(UTC).isoformat(), lane, event_id, reason),
    )


def _recover_incomplete(
    connection: sqlite3.Connection, *, now: datetime
) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT lane,event_id,source,attempt_id FROM b0x_dispatch_attempt "
        "WHERE terminal_type IS NULL ORDER BY claimed_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    lane, event_id, source, attempt_id = map(str, row)
    ended = now.astimezone(UTC).isoformat()
    connection.execute(
        "UPDATE b0x_dispatch_attempt SET state='terminal',terminal_type='unknown_preserved',"
        "terminal_verified=1,ended_at=?,outcome_reason='incomplete_attempt_recovered_without_retry' "
        "WHERE lane=? AND event_id=? AND terminal_type IS NULL",
        (ended, lane, event_id),
    )
    connection.execute(
        "UPDATE b0x_lane_event SET disposition='unknown_preserved',terminal_evidence=? "
        "WHERE lane=? AND event_id=?",
        (f"dispatch_attempt:{attempt_id}", lane, event_id),
    )
    connection.execute(
        "UPDATE b0x_dispatch_active_source SET state='unknown_preserved' WHERE source=?",
        (source,),
    )
    return lane, event_id


def _next_event(
    connection: sqlite3.Connection, lane: str
) -> tuple[str, str, str, str] | None:
    row = connection.execute(
        "SELECT lane,event_id,source_payload,disposition FROM b0x_lane_event "
        "WHERE lane=? AND disposition IN (?,?,?) ORDER BY received_at,event_id LIMIT 1",
        (lane, *_ELIGIBLE_DISPOSITIONS),
    ).fetchone()
    return None if row is None else tuple(map(str, row))  # type: ignore[return-value]


def _hold_event(
    connection: sqlite3.Connection,
    *,
    lane: str,
    event_id: str,
    disposition: str,
    now: datetime,
) -> DispatchReceipt:
    connection.execute(
        "UPDATE b0x_lane_event SET disposition=? WHERE lane=? AND event_id=?",
        (disposition, lane, event_id),
    )
    _record_refusal(
        connection, now=now, reason=disposition, lane=lane, event_id=event_id
    )
    return DispatchReceipt(
        status="held",
        lane=lane,
        event_id=event_id,
        disposition=disposition,
        queue_disposition=disposition,
        reason=disposition,
    )


def _claim(
    connection: sqlite3.Connection,
    *,
    binding: B0XDispatchBinding,
    lane: str,
    event_id: str,
    body: Mapping[str, object],
    prior_disposition: str,
    runner_id: str,
    now: datetime,
    attempt_id: str,
) -> DispatchReceipt:
    source = str(body["slot"])
    active = connection.execute(
        "SELECT event_id,state FROM b0x_dispatch_active_source WHERE source=?",
        (source,),
    ).fetchone()
    if active is not None:
        return _hold_event(
            connection,
            lane=lane,
            event_id=event_id,
            disposition="held_unconsumed_active_or_unknown_source",
            now=now,
        )
    changed = connection.execute(
        "UPDATE b0x_lane_event SET disposition='dispatch_claimed' "
        "WHERE lane=? AND event_id=? AND disposition=?",
        (lane, event_id, prior_disposition),
    ).rowcount
    if changed != 1:
        return DispatchReceipt(
            status="duplicate_or_ineligible",
            lane=lane,
            event_id=event_id,
            runner_id=runner_id,
            disposition="not_claimed",
            queue_disposition="not_claimed",
        )
    payload_sha = _sha256(_canonical_json(body))
    claimed_at = now.astimezone(UTC).isoformat()
    connection.execute(
        "INSERT INTO b0x_dispatch_attempt "
        "(lane,event_id,attempt_id,source,payload_sha256,runner_id,binding_sha256,"
        "owner_epoch,state,claimed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            lane,
            event_id,
            attempt_id,
            source,
            payload_sha,
            runner_id,
            binding.binding_sha256,
            binding.owner_epoch,
            "claimed",
            claimed_at,
        ),
    )
    connection.execute(
        "INSERT INTO b0x_dispatch_active_source(source,lane,event_id,attempt_id,state) "
        "VALUES (?,?,?,?,?)",
        (source, lane, event_id, attempt_id, "claimed"),
    )
    return DispatchReceipt(
        status="claimed",
        lane=lane,
        event_id=event_id,
        attempt_id=attempt_id,
        runner_id=runner_id,
        disposition="dispatch_claimed",
        queue_disposition="dispatch_claimed",
        claimed_at=claimed_at,
    )


@contextmanager
def _probe_lower_writer_lock(runner: RunnerBinding):  # noqa: ANN201
    """Probe upper-then-lower order without replacing the runner's own lock."""

    path = runner.writer_lock_path
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise B0XDispatchError("lower_writer_lock_state_unknown")
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LowerWriterLockBusy("lower_writer_lock_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _policy_stage(
    binding: B0XDispatchBinding,
    *,
    source: str,
    attempt_id: str,
    phase: str,
) -> StagePlan:
    argv = (
        str(binding.runtime.prefect_python),
        "-m",
        "robin_automation.b0x_policy_dispatch",
        "--binding",
        str(binding.path),
        "--source",
        source,
        "--attempt-id",
        attempt_id,
        "--phase",
        phase,
    )
    return StagePlan(
        stage_id=f"policy:{phase}",
        kind="policy",
        argv=argv,
        cwd=binding.runtime.prefect_root,
        installed_head=binding.runtime.prefect_head,
        environment_ref=None,
    )


def _cycle_stage(binding: B0XDispatchBinding, runner_id: str) -> StagePlan:
    runner = binding.runners[runner_id]
    argv: list[str] = [
        str(binding.runtime.python),
        "-m",
        RUNNER_MODULES[runner_id],
    ]
    if runner_id == "crypto_shadow":
        argv.extend(("--lane", "shadow"))
    argv.extend(
        (
            "--table-dir",
            str(runner.table_dir),
            "--out-dir",
            str(runner.output_root),
            "--json",
        )
    )
    if any(argument in _FORBIDDEN_ARGV for argument in argv):
        raise B0XDispatchError("fixed_runner_contains_forbidden_flag")
    return StagePlan(
        stage_id=f"cycle:{runner_id}",
        kind="cycle",
        argv=tuple(argv),
        cwd=binding.runtime.auto_trader_root,
        installed_head=binding.runtime.auto_trader_head,
        environment_ref=runner.environment_ref,
    )


def _plan(
    binding: B0XDispatchBinding,
    *,
    source: str,
    runner_id: str,
    attempt_id: str,
) -> tuple[StagePlan, ...]:
    if runner_id == "harvest_observe_only":
        return ()
    if runner_id in {"kr_observation", "us_observation"}:
        return (
            _policy_stage(
                binding, source=source, attempt_id=attempt_id, phase="preflight"
            ),
            _cycle_stage(binding, runner_id),
        )
    if runner_id == "crypto_shadow":
        return (
            _policy_stage(
                binding, source=source, attempt_id=attempt_id, phase="pre-cycle"
            ),
            _cycle_stage(binding, runner_id),
            _policy_stage(
                binding, source=source, attempt_id=attempt_id, phase="post-cycle"
            ),
        )
    return (
        _policy_stage(
            binding, source=source, attempt_id=attempt_id, phase="build-and-commit"
        ),
    )


def _artifact_snapshot(runner: RunnerBinding, runner_id: str) -> ArtifactSnapshot:
    lane_dir = runner.output_root / RUNNER_LANES[runner_id]
    cycle_log = lane_dir / "cycles.jsonl"
    if cycle_log.is_symlink() or lane_dir.is_symlink():
        raise B0XDispatchError("artifact_namespace_symlink_rejected")
    size = cycle_log.stat().st_size if cycle_log.exists() else 0
    if cycle_log.exists() and not cycle_log.is_file():
        raise B0XDispatchError("artifact_cycle_log_wrong_kind")
    names = (
        frozenset(path.name for path in lane_dir.glob("*-cycle.md"))
        if lane_dir.exists()
        else frozenset()
    )
    return ArtifactSnapshot(lane_dir, cycle_log, size, names)


def _expected_cycle_artifact_path(lane_dir: Path, observed_at: datetime) -> Path:
    return lane_dir / f"{observed_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-cycle.md"


def _validate_cycle_artifact(
    snapshot: ArtifactSnapshot,
    *,
    runner_id: str,
    expected_table_hash: str,
    process_started_at: datetime,
) -> ArtifactEvidence:
    log = snapshot.cycle_log
    if (
        not log.is_file()
        or log.is_symlink()
        or log.stat().st_size <= snapshot.cycle_log_size
    ):
        raise B0XDispatchError("cycle_artifact_missing")
    with log.open("rb") as handle:
        handle.seek(snapshot.cycle_log_size)
        appended = handle.read()
    lines = [line for line in appended.splitlines() if line.strip()]
    if len(lines) != 1:
        raise B0XDispatchError("cycle_artifact_append_count_invalid")
    try:
        record = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise B0XDispatchError("cycle_artifact_json_invalid") from exc
    if not isinstance(record, Mapping):
        raise B0XDispatchError("cycle_artifact_record_invalid")
    if record.get("lane") != RUNNER_LANES[runner_id]:
        raise B0XDispatchError("cycle_artifact_lane_mismatch")
    zero_reason = record.get("zero_order_reason")
    if zero_reason is not None and (
        not isinstance(zero_reason, str) or not zero_reason.strip()
    ):
        raise B0XDispatchError("cycle_artifact_zero_order_reason_invalid")
    observed_table_hash = record.get("policy_table_hash")
    if observed_table_hash is None:
        if zero_reason is None:
            raise B0XDispatchError("cycle_artifact_table_hash_missing")
    elif observed_table_hash != expected_table_hash:
        raise B0XDispatchError("cycle_artifact_table_hash_mismatch")
    at = record.get("at")
    if not isinstance(at, str):
        raise B0XDispatchError("cycle_artifact_timestamp_missing")
    try:
        observed = datetime.fromisoformat(at)
    except ValueError as exc:
        raise B0XDispatchError("cycle_artifact_timestamp_invalid") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise B0XDispatchError("cycle_artifact_timestamp_invalid")
    observed_utc = observed.astimezone(UTC)
    if observed_utc < process_started_at.astimezone(UTC):
        raise B0XDispatchError("cycle_artifact_predates_process_start")
    new_paths = [
        path
        for path in snapshot.lane_dir.glob("*-cycle.md")
        if path.name not in snapshot.artifact_names
    ]
    if len(new_paths) != 1:
        raise B0XDispatchError("cycle_artifact_file_count_invalid")
    artifact = new_paths[0]
    if artifact.is_symlink() or artifact.resolve() != artifact:
        raise B0XDispatchError("cycle_artifact_path_invalid")
    if artifact != _expected_cycle_artifact_path(snapshot.lane_dir, observed_utc):
        raise B0XDispatchError("cycle_artifact_event_identity_mismatch")
    if artifact.stat().st_mtime_ns < int(
        process_started_at.timestamp() * 1_000_000_000
    ):
        raise B0XDispatchError("cycle_artifact_not_new")
    if runner_id == "kr_observation":
        if record.get("confirm") is not False or record.get("ordering") is not False:
            raise B0XDispatchError("kr_observation_authority_violation")
        if record.get("submitted") != [] or any(
            record.get(field) for field in ("round_trip", "day_orders")
        ):
            raise B0XDispatchError("kr_observation_action_present")
    elif runner_id == "us_observation":
        if record.get("confirm") is not False or record.get("submitted") != []:
            raise B0XDispatchError("us_observation_action_present")
    elif runner_id == "crypto_shadow":
        if record.get("real_orders") != 0 or record.get("live_contact") != 0:
            raise B0XDispatchError("crypto_shadow_live_action_present")
    cycle_id = record.get("cycle_id")
    if cycle_id is not None and not isinstance(cycle_id, str):
        raise B0XDispatchError("cycle_artifact_cycle_id_invalid")
    if not cycle_id and not isinstance(zero_reason, str):
        raise B0XDispatchError("cycle_artifact_outcome_missing")
    payload = artifact.read_bytes()
    return ArtifactEvidence(
        path=str(artifact),
        sha256=_sha256(payload),
        byte_count=len(payload),
        cycle_id=cycle_id,
        table_hash=(expected_table_hash if observed_table_hash is not None else None),
        observed_at=observed_utc.isoformat(),
        zero_order_reason=zero_reason if isinstance(zero_reason, str) else None,
    )


def _policy_receipt(result: ProcessResult) -> Mapping[str, object]:
    if result.receipt is not None:
        return result.receipt
    records = [
        line.removeprefix(POLICY_RECEIPT_MARKER)
        for line in result.stdout.splitlines()
        if line.startswith(POLICY_RECEIPT_MARKER)
    ]
    if len(records) != 1:
        raise B0XDispatchError("policy_stage_receipt_missing")
    try:
        value = json.loads(records[0])
    except json.JSONDecodeError as exc:
        raise B0XDispatchError("policy_stage_receipt_invalid") from exc
    if not isinstance(value, Mapping):
        raise B0XDispatchError("policy_stage_receipt_invalid")
    return value


def _policy_failure_reason(result: ProcessResult, stage: StagePlan) -> str:
    """Project only a closed, secret-free policy helper refusal."""

    if stage.stage_id != "policy:post-cycle":
        return f"{stage.stage_id}_exit_nonzero"
    records: list[Mapping[str, object]] = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            records.append(value)
    if len(records) == 1:
        record = records[0]
        reason = record.get("reason")
        if (
            record.get("status") == "blocked"
            and record.get("push_reapplications") == 0
            and type(record.get("non_fast_forward")) is bool
        ):
            if (
                reason == "crypto_commit_push_stop_esc"
                and record["non_fast_forward"] is True
            ):
                return "policy_non_fast_forward_stop_esc"
            generic = {
                "crypto_commit_add_stop_esc": "policy_git_add_stop_esc",
                "crypto_commit_commit_stop_esc": "policy_git_commit_stop_esc",
                "crypto_commit_push_failed_stop_esc": "policy_git_push_stop_esc",
            }
            if record["non_fast_forward"] is False and reason in generic:
                return generic[str(reason)]
    return f"{stage.stage_id}_exit_nonzero"


def _validate_policy_receipt(
    receipt: Mapping[str, object],
    *,
    binding: B0XDispatchBinding,
    source: str,
    attempt_id: str,
    phase: str,
) -> tuple[str, str, str | None, str | None, int]:
    required = {
        "version",
        "source",
        "attempt_id",
        "phase",
        "policy_checkout",
        "approved_ref",
        "projection_sha256",
        "steps",
        "preflight_head",
        "consumed_table_hash",
        "post_build_head",
        "post_commit_head",
        "outside_scope_diff",
        "pointer_blob_match",
        "non_fast_forward",
        "push_reapplications",
        "cycle_starts",
    }
    if set(receipt) != required:
        raise B0XDispatchError("policy_stage_receipt_fields_invalid")
    exact = {
        "version": "b0x-policy-attempt/v1",
        "source": source,
        "attempt_id": attempt_id,
        "phase": phase,
        "policy_checkout": str(binding.policy.checkout),
        "approved_ref": binding.policy.approved_ref,
        "projection_sha256": binding.policy.projection_sha256,
        "steps": list(_POLICY_STEPS),
        "outside_scope_diff": False,
        "pointer_blob_match": True,
        "push_reapplications": 0,
    }
    if any(receipt.get(key) != expected for key, expected in exact.items()):
        raise B0XDispatchError("policy_stage_receipt_contract_mismatch")
    if receipt.get("non_fast_forward") is True:
        raise B0XDispatchError("policy_non_fast_forward_stop_esc")
    if receipt.get("non_fast_forward") is not False:
        raise B0XDispatchError("policy_non_fast_forward_state_invalid")
    if receipt.get("cycle_starts") not in {0, 1}:
        raise B0XDispatchError("policy_cycle_start_count_invalid")
    preflight_head = receipt.get("preflight_head")
    if not isinstance(preflight_head, str) or _HEX40.fullmatch(preflight_head) is None:
        raise B0XDispatchError("policy_preflight_head_invalid")
    for field in ("post_build_head", "post_commit_head"):
        value = receipt.get(field)
        if value is not None and (
            not isinstance(value, str) or _HEX40.fullmatch(value) is None
        ):
            raise B0XDispatchError(f"policy_{field}_invalid")
    table_hash = receipt.get("consumed_table_hash")
    if not isinstance(table_hash, str) or _TABLE_HASH.fullmatch(table_hash) is None:
        raise B0XDispatchError("policy_consumed_table_hash_invalid")
    post_build = receipt.get("post_build_head")
    post_commit = receipt.get("post_commit_head")
    cycle_starts = int(receipt["cycle_starts"])
    phase_shape = {
        "preflight": (False, False, 0),
        "pre-cycle": (True, False, 0),
        "post-cycle": (True, True, 1),
        "build-and-commit": (True, True, 0),
    }[phase]
    if (
        (post_build is not None) != phase_shape[0]
        or (post_commit is not None) != phase_shape[1]
        or cycle_starts != phase_shape[2]
    ):
        raise B0XDispatchError("policy_stage_phase_evidence_mismatch")
    return (
        preflight_head,
        str(table_hash),
        None if post_build is None else str(post_build),
        None if post_commit is None else str(post_commit),
        int(receipt["push_reapplications"]),
    )


def _record_process_start(
    *,
    state_db: Path,
    binding: B0XDispatchBinding,
    lane: str,
    event_id: str,
    attempt_id: str,
    stage: StagePlan,
    stage_index: int,
    start: ProcessStart,
) -> None:
    if start.started_at.tzinfo is None or start.started_at.utcoffset() is None:
        raise B0XDispatchError("process_start_clock_invalid")
    if type(start.pid) is not int or start.pid <= 0:
        raise B0XDispatchError("process_pid_invalid")
    _string(start.start_identity, code="process_start_identity_invalid")
    environment_ref_sha256 = (
        None
        if stage.environment_ref is None
        else _sha256(_canonical_json(str(stage.environment_ref)))
    )
    connection = _connect(state_db)
    try:
        _ensure_dispatch_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO b0x_dispatch_process "
            "(lane,event_id,attempt_id,stage_index,stage_id,executable,argv_sha256,cwd,"
            "installed_head,owner_epoch,environment_ref_sha256,pid,start_identity,started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lane,
                event_id,
                attempt_id,
                stage_index,
                stage.stage_id,
                stage.argv[0],
                _sha256(_canonical_json(stage.argv)),
                str(stage.cwd),
                stage.installed_head,
                binding.owner_epoch,
                environment_ref_sha256,
                start.pid,
                start.start_identity,
                start.started_at.astimezone(UTC).isoformat(),
            ),
        )
        increments = ",cycle_starts=cycle_starts+1" if stage.kind == "cycle" else ""
        connection.execute(
            "UPDATE b0x_dispatch_attempt SET state='process_started',"
            f"process_started_at=COALESCE(process_started_at,?){increments} "
            "WHERE lane=? AND event_id=? AND attempt_id=? AND terminal_type IS NULL",
            (start.started_at.astimezone(UTC).isoformat(), lane, event_id, attempt_id),
        )
        connection.execute(
            "UPDATE b0x_dispatch_active_source SET state='process_started' "
            "WHERE attempt_id=?",
            (attempt_id,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_process_end(
    *,
    state_db: Path,
    lane: str,
    event_id: str,
    stage_index: int,
    result: ProcessResult,
) -> None:
    if result.ended_at.tzinfo is None or result.ended_at.utcoffset() is None:
        raise B0XDispatchError("process_end_clock_invalid")
    if result.ended_at.astimezone(UTC) < result.started.started_at.astimezone(UTC):
        raise B0XDispatchError("process_end_predates_start")
    if result.exit_code is not None and type(result.exit_code) is not int:
        raise B0XDispatchError("process_exit_code_invalid")
    connection = _connect(state_db)
    try:
        _ensure_dispatch_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE b0x_dispatch_process SET ended_at=?,exit_code=? "
            "WHERE lane=? AND event_id=? AND stage_index=?",
            (
                result.ended_at.astimezone(UTC).isoformat(),
                result.exit_code,
                lane,
                event_id,
                stage_index,
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(
    *,
    state_db: Path,
    lane: str,
    event_id: str,
    attempt_id: str,
    source: str,
    terminal_type: str,
    reason: str,
    ended_at: datetime,
    artifact: ArtifactEvidence | None = None,
    consumed_table_hash: str | None = None,
    policy_heads: tuple[str | None, str | None, str | None] = (None, None, None),
    push_reapplications: int = 0,
    preserve_source_fence: bool = False,
) -> DispatchReceipt:
    if terminal_type not in _TERMINAL_TYPES:
        raise B0XDispatchError("terminal_type_invalid")
    connection = _connect(state_db)
    try:
        _ensure_dispatch_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE b0x_dispatch_attempt SET state='terminal',terminal_type=?,"
            "terminal_verified=1,ended_at=?,outcome_reason=?,artifact_path=?,"
            "artifact_sha256=?,artifact_bytes=?,cycle_id=?,consumed_table_hash=?,"
            "cycle_observed_at=?,policy_preflight_head=?,post_build_head=?,post_commit_head=?,"
            "push_reapplications=? WHERE lane=? AND event_id=? AND attempt_id=? "
            "AND terminal_type IS NULL",
            (
                terminal_type,
                ended_at.astimezone(UTC).isoformat(),
                reason,
                artifact.path if artifact else None,
                artifact.sha256 if artifact else None,
                artifact.byte_count if artifact else None,
                artifact.cycle_id if artifact else None,
                artifact.table_hash if artifact else consumed_table_hash,
                artifact.observed_at if artifact else None,
                policy_heads[0],
                policy_heads[1],
                policy_heads[2],
                push_reapplications,
                lane,
                event_id,
                attempt_id,
            ),
        )
        connection.execute(
            "UPDATE b0x_lane_event SET disposition=?,terminal_evidence=? "
            "WHERE lane=? AND event_id=?",
            (terminal_type, f"dispatch_attempt:{attempt_id}", lane, event_id),
        )
        if terminal_type == "unknown_preserved" or preserve_source_fence:
            connection.execute(
                "UPDATE b0x_dispatch_active_source SET state=? WHERE attempt_id=?",
                (
                    "unknown_preserved"
                    if terminal_type == "unknown_preserved"
                    else "failed_preserved_stop_esc",
                    attempt_id,
                ),
            )
        else:
            connection.execute(
                "DELETE FROM b0x_dispatch_active_source WHERE attempt_id=?",
                (attempt_id,),
            )
            connection.execute(
                "DELETE FROM b0x_active_lane WHERE lane=? AND event_id=?",
                (lane, event_id),
            )
        row = connection.execute(
            "SELECT claimed_at,process_started_at,cycle_observed_at,runner_id,cycle_starts,"
            "push_reapplications,binding_sha256,source,owner_epoch,payload_sha256,ended_at,"
            "artifact_path,artifact_sha256,artifact_bytes,consumed_table_hash,cycle_id,"
            "policy_preflight_head,post_build_head,post_commit_head "
            "FROM b0x_dispatch_attempt WHERE lane=? AND event_id=?",
            (lane, event_id),
        ).fetchone()
        queue_row = connection.execute(
            "SELECT disposition FROM b0x_lane_event WHERE lane=? AND event_id=?",
            (lane, event_id),
        ).fetchone()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    assert row is not None
    assert queue_row is not None
    processes = _process_evidence(state_db, lane, event_id)
    return DispatchReceipt(
        status="terminal",
        lane=lane,
        event_id=event_id,
        attempt_id=attempt_id,
        runner_id=str(row[3]),
        disposition=terminal_type,
        queue_disposition=str(queue_row[0]),
        claimed_at=str(row[0]),
        process_started_at=None if row[1] is None else str(row[1]),
        cycle_observed_at=None if row[2] is None else str(row[2]),
        terminal_type=terminal_type,
        terminal_verified=True,
        cycle_starts=int(row[4]),
        push_reapplications=int(row[5]),
        children_started=len(processes),
        reason=reason,
        binding_sha256=str(row[6]),
        source=str(row[7]),
        owner_epoch=str(row[8]),
        payload_sha256=str(row[9]),
        process_ended_at=(
            None
            if not processes or processes[-1]["ended_at"] is None
            else str(processes[-1]["ended_at"])
        ),
        terminal_ended_at=None if row[10] is None else str(row[10]),
        artifact_path=None if row[11] is None else str(row[11]),
        artifact_sha256=None if row[12] is None else str(row[12]),
        artifact_bytes=None if row[13] is None else int(row[13]),
        consumed_table_hash=None if row[14] is None else str(row[14]),
        cycle_id=None if row[15] is None else str(row[15]),
        policy_preflight_head=None if row[16] is None else str(row[16]),
        post_build_head=None if row[17] is None else str(row[17]),
        post_commit_head=None if row[18] is None else str(row[18]),
        exit_code=(
            None
            if not processes or processes[-1]["exit_code"] is None
            else int(processes[-1]["exit_code"])
        ),
        processes=processes,
    )


def _process_evidence(
    state_db: Path, lane: str, event_id: str
) -> tuple[Mapping[str, object], ...]:
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT attempt_id,stage_index,stage_id,executable,argv_sha256,cwd,"
            "installed_head,owner_epoch,environment_ref_sha256,pid,start_identity,"
            "started_at,ended_at,exit_code "
            "FROM b0x_dispatch_process WHERE lane=? AND event_id=? ORDER BY stage_index",
            (lane, event_id),
        ).fetchall()
        return tuple(
            {
                "attempt_id": row[0],
                "stage_index": int(row[1]),
                "stage_id": row[2],
                "executable": row[3],
                "argv_sha256": row[4],
                "cwd": row[5],
                "installed_head": row[6],
                "owner_epoch": row[7],
                "environment_ref_sha256": row[8],
                "pid": int(row[9]),
                "start_identity": row[10],
                "started_at": row[11],
                "ended_at": row[12],
                "exit_code": row[13],
            }
            for row in rows
        )
    finally:
        connection.close()


def _readback_time(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise B0XDispatchError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise B0XDispatchError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise B0XDispatchError(code)
    return parsed.astimezone(UTC)


def _validated_process_evidence(
    *,
    binding: B0XDispatchBinding,
    source: str,
    runner_id: str,
    attempt_id: str,
    rows: Sequence[Sequence[object]],
) -> tuple[Mapping[str, object], ...]:
    """Rebind stored process facts to the immutable production registry."""

    if binding.source_runners.get(source) != runner_id:
        raise B0XDispatchError("readback_source_runner_mismatch")
    expected_stages = _plan(
        binding, source=source, runner_id=runner_id, attempt_id=attempt_id
    )
    if len(rows) > len(expected_stages):
        raise B0XDispatchError("readback_process_count_invalid")
    evidence: list[Mapping[str, object]] = []
    for index, row in enumerate(rows):
        if len(row) != 14 or row[0] != attempt_id or row[1] != index:
            raise B0XDispatchError("readback_process_attempt_or_index_mismatch")
        expected = expected_stages[index]
        expected_environment_sha256 = (
            None
            if expected.environment_ref is None
            else _sha256(_canonical_json(str(expected.environment_ref)))
        )
        if (
            row[2] != expected.stage_id
            or row[3] != expected.argv[0]
            or row[4] != _sha256(_canonical_json(expected.argv))
            or row[5] != str(expected.cwd)
            or row[6] != expected.installed_head
            or row[7] != binding.owner_epoch
            or row[8] != expected_environment_sha256
        ):
            raise B0XDispatchError("readback_process_registry_mismatch")
        pid = row[9]
        if type(pid) is not int or pid <= 0:
            raise B0XDispatchError("readback_process_pid_invalid")
        start_identity = row[10]
        if not isinstance(start_identity, str) or not start_identity:
            raise B0XDispatchError("readback_process_start_identity_invalid")
        started = _readback_time(row[11], code="readback_process_started_at_invalid")
        ended_raw = row[12]
        if ended_raw is not None:
            ended = _readback_time(ended_raw, code="readback_process_ended_at_invalid")
            if ended < started:
                raise B0XDispatchError("readback_process_end_predates_start")
        exit_code = row[13]
        if exit_code is not None and type(exit_code) is not int:
            raise B0XDispatchError("readback_process_exit_code_invalid")
        evidence.append(
            {
                "attempt_id": row[0],
                "stage_index": index,
                "stage_id": row[2],
                "executable": row[3],
                "argv_sha256": row[4],
                "cwd": row[5],
                "installed_head": row[6],
                "owner_epoch": row[7],
                "environment_ref_sha256": row[8],
                "pid": pid,
                "start_identity": start_identity,
                "started_at": row[11],
                "ended_at": ended_raw,
                "exit_code": exit_code,
            }
        )
    return tuple(evidence)


class OwnedChildExecutor:
    """Production executor that can signal/kill only the process it creates."""

    def __call__(
        self,
        stage: StagePlan,
        *,
        on_started: Callable[[ProcessStart], None],
        timeout_seconds: float,
    ) -> ProcessResult:
        started_at = datetime.now(UTC)
        if stage.kind not in {"cycle", "policy"}:
            raise B0XDispatchError("child_stage_kind_invalid")
        allowlist = _NON_CREDENTIAL_CHILD_ENV_ALLOWLIST
        if stage.kind == "policy":
            allowlist = allowlist | _POLICY_GIT_AUTH_ENV_ALLOWLIST
        child_env = {key: os.environ[key] for key in allowlist if key in os.environ}
        if stage.environment_ref is not None:
            # Bind only the reviewed path selector.  The dispatcher never
            # opens the file and never serializes the inherited environment.
            child_env["ENV_FILE"] = str(stage.environment_ref)
        process = subprocess.Popen(  # noqa: S603 - argv is the closed registry above
            list(stage.argv),
            cwd=stage.cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        start = ProcessStart(
            pid=process.pid,
            start_identity=f"pid:{process.pid}:monotonic_ns:{time.monotonic_ns()}",
            started_at=started_at,
        )
        on_started(start)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return ProcessResult(
                exit_code=process.returncode,
                started=start,
                ended_at=datetime.now(UTC),
                stdout=(stdout or "")[-65536:],
                stderr=(stderr or "")[-4096:],
            )
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            return ProcessResult(
                exit_code=None,
                started=start,
                ended_at=datetime.now(UTC),
                stdout=(stdout or "")[-65536:],
                stderr=(stderr or "")[-4096:],
                timed_out=True,
            )


def dispatch_once(
    binding: B0XDispatchBinding,
    state_db: Path,
    clock: Callable[[], datetime],
    *,
    executor: Executor | None = None,
    timeout_seconds: float = 3600,
    attempt_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    fault: Callable[[str], None] = lambda _point: None,
) -> DispatchReceipt:
    """Claim and dispatch at most one durable event under the fixed registry."""

    if state_db.resolve(strict=False) != binding.runtime.state_db:
        raise B0XDispatchError("runtime_state_db_mismatch")
    selected_executor = executor or OwnedChildExecutor()
    with stable_dispatch_lock(binding):
        connection = _connect(binding.runtime.state_db)
        try:
            _ensure_dispatch_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            transaction_now = clock()
            _received_clock(transaction_now)
            recovered = _recover_incomplete(connection, now=transaction_now)
            if recovered is not None:
                connection.commit()
                lane, event_id = recovered
                return dispatch_readback(
                    binding, lane=lane, event_id=event_id, _status="recovered"
                )
            blockers = binding_blockers(binding)
            if blockers:
                for reason in blockers:
                    _record_refusal(connection, now=transaction_now, reason=reason)
                connection.commit()
                return DispatchReceipt(status="blocked", reason=",".join(blockers))
            row = _next_event(connection, binding.lane)
            if row is None:
                connection.commit()
                return DispatchReceipt(status="idle")
            lane, event_id, source_payload, prior_disposition = row
            try:
                body = json.loads(source_payload)
                validated_lane, validated_event_id, validated_body = _payload(
                    {
                        "type": "lane.event",
                        "owner_lane": lane,
                        "event_id": event_id,
                        "text": json.dumps(body, separators=(",", ":"), sort_keys=True),
                    }
                )
            except (json.JSONDecodeError, B0XDispatchError, ValueError):
                receipt = _hold_event(
                    connection,
                    lane=lane,
                    event_id=event_id,
                    disposition="dispatch_rejected_source_payload",
                    now=transaction_now,
                )
                connection.commit()
                return receipt
            assert validated_lane == lane and validated_event_id == event_id
            source = str(validated_body["slot"])
            runner_id = binding.source_runners[source]
            blocker = _source_blocker(binding, source, runner_id)
            if blocker is not None:
                receipt = _hold_event(
                    connection,
                    lane=lane,
                    event_id=event_id,
                    disposition=f"dispatch_held_{blocker}",
                    now=transaction_now,
                )
                connection.commit()
                return receipt
            claim_now = clock()
            claim_kst, _ = _received_clock(claim_now)
            if not _eligible_at(validated_body, claim_kst):
                receipt = _hold_event(
                    connection,
                    lane=lane,
                    event_id=event_id,
                    disposition="dispatch_held_out_of_window",
                    now=claim_now,
                )
                connection.commit()
                return receipt
            attempt_id = attempt_id_factory()
            if _SAFE_ID.fullmatch(attempt_id) is None:
                raise B0XDispatchError("attempt_id_invalid")
            claim = _claim(
                connection,
                binding=binding,
                lane=lane,
                event_id=event_id,
                body=validated_body,
                prior_disposition=prior_disposition,
                runner_id=runner_id,
                now=claim_now,
                attempt_id=attempt_id,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        if claim.status != "claimed":
            return claim
        fault("after_claim")
        if runner_id == "harvest_observe_only":
            return _finish(
                state_db=state_db,
                lane=lane,
                event_id=event_id,
                attempt_id=attempt_id,
                source=source,
                terminal_type="zero_order_observed",
                reason="harvest_observation_only_no_cycle",
                ended_at=clock(),
            )
        if runner_id in _CYCLE_RUNNERS:
            try:
                with _probe_lower_writer_lock(binding.runners[runner_id]):
                    pass
            except LowerWriterLockBusy as exc:
                return _finish(
                    state_db=state_db,
                    lane=lane,
                    event_id=event_id,
                    attempt_id=attempt_id,
                    source=source,
                    terminal_type="failed_preserved",
                    reason=exc.code,
                    ended_at=clock(),
                )
            except B0XDispatchError as exc:
                return _finish(
                    state_db=state_db,
                    lane=lane,
                    event_id=event_id,
                    attempt_id=attempt_id,
                    source=source,
                    terminal_type="unknown_preserved",
                    reason=exc.code,
                    ended_at=clock(),
                )

        stages = _plan(
            binding, source=source, runner_id=runner_id, attempt_id=attempt_id
        )
        policy_preflight_head: str | None = None
        post_build_head: str | None = None
        post_commit_head: str | None = None
        table_hash: str | None = None
        push_reapplications = 0
        artifact: ArtifactEvidence | None = None
        cycle_snapshot: ArtifactSnapshot | None = None
        last_end = clock()
        current_stage_id: str | None = None
        try:
            for index, stage in enumerate(stages):
                current_stage_id = stage.stage_id
                if stage.kind == "cycle":
                    cycle_snapshot = _artifact_snapshot(
                        binding.runners[runner_id], runner_id
                    )

                def on_started(
                    start: ProcessStart, *, _stage=stage, _index=index
                ) -> None:
                    _record_process_start(
                        state_db=state_db,
                        binding=binding,
                        lane=lane,
                        event_id=event_id,
                        attempt_id=attempt_id,
                        stage=_stage,
                        stage_index=_index,
                        start=start,
                    )

                result = selected_executor(
                    stage, on_started=on_started, timeout_seconds=timeout_seconds
                )
                _record_process_end(
                    state_db=state_db,
                    lane=lane,
                    event_id=event_id,
                    stage_index=index,
                    result=result,
                )
                last_end = result.ended_at
                fault(f"after_{stage.stage_id}")
                if result.timed_out:
                    return _finish(
                        state_db=state_db,
                        lane=lane,
                        event_id=event_id,
                        attempt_id=attempt_id,
                        source=source,
                        terminal_type="unknown_preserved",
                        reason=f"{stage.stage_id}_timeout_ambiguous",
                        ended_at=last_end,
                        artifact=artifact,
                        consumed_table_hash=table_hash,
                        policy_heads=(
                            policy_preflight_head,
                            post_build_head,
                            post_commit_head,
                        ),
                        push_reapplications=push_reapplications,
                    )
                if result.exit_code != 0:
                    failure_reason = (
                        _policy_failure_reason(result, stage)
                        if stage.kind == "policy"
                        else f"{stage.stage_id}_exit_nonzero"
                    )
                    return _finish(
                        state_db=state_db,
                        lane=lane,
                        event_id=event_id,
                        attempt_id=attempt_id,
                        source=source,
                        terminal_type="failed_preserved",
                        reason=failure_reason,
                        ended_at=last_end,
                        artifact=artifact,
                        consumed_table_hash=table_hash,
                        policy_heads=(
                            policy_preflight_head,
                            post_build_head,
                            post_commit_head,
                        ),
                        push_reapplications=push_reapplications,
                        preserve_source_fence=(stage.stage_id == "policy:post-cycle"),
                    )
                if stage.kind == "policy":
                    phase = stage.stage_id.split(":", 1)[1]
                    values = _validate_policy_receipt(
                        _policy_receipt(result),
                        binding=binding,
                        source=source,
                        attempt_id=attempt_id,
                        phase=phase,
                    )
                    policy_preflight_head = values[0]
                    table_hash = values[1]
                    post_build_head = values[2] or post_build_head
                    post_commit_head = values[3] or post_commit_head
                    push_reapplications += values[4]
                else:
                    if cycle_snapshot is None or table_hash is None:
                        raise B0XDispatchError("cycle_artifact_preflight_missing")
                    artifact = _validate_cycle_artifact(
                        cycle_snapshot,
                        runner_id=runner_id,
                        expected_table_hash=table_hash,
                        process_started_at=result.started.started_at,
                    )
        except B0XDispatchError as exc:
            return _finish(
                state_db=state_db,
                lane=lane,
                event_id=event_id,
                attempt_id=attempt_id,
                source=source,
                terminal_type="failed_preserved",
                reason=exc.code,
                ended_at=last_end,
                artifact=artifact,
                consumed_table_hash=table_hash,
                policy_heads=(policy_preflight_head, post_build_head, post_commit_head),
                push_reapplications=push_reapplications,
                preserve_source_fence=(current_stage_id == "policy:post-cycle"),
            )
        except BaseException:
            # Claim committed before spawn. Any ambiguity after that point is
            # intentionally preserved and never automatically retried.
            return _finish(
                state_db=state_db,
                lane=lane,
                event_id=event_id,
                attempt_id=attempt_id,
                source=source,
                terminal_type="unknown_preserved",
                reason="dispatch_ambiguous_exception",
                ended_at=last_end,
                artifact=artifact,
                consumed_table_hash=table_hash,
                policy_heads=(policy_preflight_head, post_build_head, post_commit_head),
                push_reapplications=push_reapplications,
            )

        if runner_id in _CYCLE_RUNNERS:
            if artifact is None:
                terminal_type = "failed_preserved"
                reason = "exit_zero_without_valid_cycle_artifact"
            elif artifact.zero_order_reason is not None:
                terminal_type = "zero_order_observed"
                reason = artifact.zero_order_reason
            else:
                terminal_type = "success_observed"
                reason = "cycle_artifact_verified"
        else:
            terminal_type = "success_observed"
            reason = "policy_table_build_receipt_verified"
        return _finish(
            state_db=state_db,
            lane=lane,
            event_id=event_id,
            attempt_id=attempt_id,
            source=source,
            terminal_type=terminal_type,
            reason=reason,
            ended_at=last_end,
            artifact=artifact,
            consumed_table_hash=table_hash,
            policy_heads=(policy_preflight_head, post_build_head, post_commit_head),
            push_reapplications=push_reapplications,
        )


def dispatch_readback(
    binding: B0XDispatchBinding,
    *,
    lane: str,
    event_id: str,
    _status: str = "readback",
) -> DispatchReceipt:
    """Read lifecycle evidence without opening SQLite for writes."""

    if lane != binding.lane or not event_id:
        raise B0XDispatchError("readback_identity_mismatch")
    if not binding.runtime.state_db.exists():
        return DispatchReceipt(status=_status, lane=lane, event_id=event_id)
    connection = sqlite3.connect(
        f"file:{binding.runtime.state_db}?mode=ro", uri=True, timeout=10
    )
    try:
        queue_row = connection.execute(
            "SELECT source_payload,disposition FROM b0x_lane_event "
            "WHERE lane=? AND event_id=?",
            (lane, event_id),
        ).fetchone()
        row = connection.execute(
            "SELECT attempt_id,runner_id,claimed_at,process_started_at,cycle_observed_at,"
            "terminal_type,terminal_verified,cycle_starts,push_reapplications,outcome_reason,"
            "binding_sha256,source,owner_epoch,payload_sha256,ended_at,artifact_path,"
            "artifact_sha256,artifact_bytes,consumed_table_hash,cycle_id,"
            "policy_preflight_head,post_build_head,post_commit_head "
            "FROM b0x_dispatch_attempt WHERE lane=? AND event_id=?",
            (lane, event_id),
        ).fetchone()
        process_rows = connection.execute(
            "SELECT attempt_id,stage_index,stage_id,executable,argv_sha256,cwd,"
            "installed_head,owner_epoch,environment_ref_sha256,pid,start_identity,"
            "started_at,ended_at,exit_code "
            "FROM b0x_dispatch_process WHERE lane=? AND event_id=? ORDER BY stage_index",
            (lane, event_id),
        ).fetchall()
    finally:
        connection.close()
    if row is None:
        return DispatchReceipt(
            status=_status,
            lane=lane,
            event_id=event_id,
            disposition=None if queue_row is None else str(queue_row[1]),
            queue_disposition=None if queue_row is None else str(queue_row[1]),
        )
    if queue_row is None:
        raise B0XDispatchError("readback_queue_receipt_missing")
    if row[10] != binding.binding_sha256 or row[12] != binding.owner_epoch:
        raise B0XDispatchError("readback_binding_or_owner_mismatch")
    attempt_id = _string(row[0], code="readback_attempt_id_invalid", pattern=_SAFE_ID)
    runner_id = _string(row[1], code="readback_runner_id_invalid")
    source = _string(row[11], code="readback_source_invalid")
    if binding.source_runners.get(source) != runner_id:
        raise B0XDispatchError("readback_source_runner_mismatch")
    try:
        source_body = json.loads(str(queue_row[0]))
        _, _, validated_body = _payload(
            {
                "type": "lane.event",
                "owner_lane": lane,
                "event_id": event_id,
                "text": json.dumps(source_body, separators=(",", ":"), sort_keys=True),
            }
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise B0XDispatchError("readback_queue_payload_invalid") from exc
    if validated_body.get("slot") != source or row[13] != _sha256(
        _canonical_json(validated_body)
    ):
        raise B0XDispatchError("readback_queue_payload_identity_mismatch")
    claimed = _readback_time(row[2], code="readback_claimed_at_invalid")
    process_started = (
        None
        if row[3] is None
        else _readback_time(row[3], code="readback_process_started_at_invalid")
    )
    cycle_observed = (
        None
        if row[4] is None
        else _readback_time(row[4], code="readback_cycle_observed_at_invalid")
    )
    terminal_ended = (
        None
        if row[14] is None
        else _readback_time(row[14], code="readback_terminal_ended_at_invalid")
    )
    if process_started is not None and process_started < claimed:
        raise B0XDispatchError("readback_process_start_predates_claim")
    if cycle_observed is not None and cycle_observed < claimed:
        raise B0XDispatchError("readback_cycle_observation_predates_claim")
    if terminal_ended is not None and terminal_ended < claimed:
        raise B0XDispatchError("readback_terminal_predates_claim")
    terminal_type = row[5]
    if terminal_type is not None and terminal_type not in _TERMINAL_TYPES:
        raise B0XDispatchError("readback_terminal_type_invalid")
    if row[6] not in (0, 1) or bool(row[6]) is not (terminal_type is not None):
        raise B0XDispatchError("readback_terminal_verification_invalid")
    if type(row[7]) is not int or row[7] not in (0, 1):
        raise B0XDispatchError("readback_cycle_start_count_invalid")
    if row[8] != 0:
        raise B0XDispatchError("readback_push_reapplication_count_invalid")
    if not isinstance(row[13], str) or _HEX64.fullmatch(row[13]) is None:
        raise B0XDispatchError("readback_payload_hash_invalid")
    for index, field in (
        (18, "consumed_table_hash"),
        (20, "policy_preflight_head"),
        (21, "post_build_head"),
        (22, "post_commit_head"),
    ):
        value = row[index]
        pattern = _TABLE_HASH if index == 18 else _HEX40
        if value is not None and (
            not isinstance(value, str) or pattern.fullmatch(value) is None
        ):
            raise B0XDispatchError(f"readback_{field}_invalid")
    if row[19] is not None and (not isinstance(row[19], str) or not row[19]):
        raise B0XDispatchError("readback_cycle_id_invalid")
    processes = _validated_process_evidence(
        binding=binding,
        source=source,
        runner_id=runner_id,
        attempt_id=attempt_id,
        rows=process_rows,
    )
    if process_started is None and processes:
        raise B0XDispatchError("readback_process_started_at_missing")
    if process_started is not None and (
        not processes or process_started.isoformat() != processes[0]["started_at"]
    ):
        raise B0XDispatchError("readback_process_started_at_mismatch")
    if terminal_type is not None and str(queue_row[1]) != terminal_type:
        raise B0XDispatchError("readback_queue_terminal_mismatch")
    artifact_path = None if row[15] is None else Path(str(row[15]))
    if artifact_path is not None:
        try:
            resolved_artifact = artifact_path.resolve(strict=True)
            resolved_artifact.relative_to(binding.runtime.observation_root)
        except (FileNotFoundError, ValueError) as exc:
            raise B0XDispatchError("readback_artifact_path_invalid") from exc
        if artifact_path.is_symlink() or resolved_artifact != artifact_path:
            raise B0XDispatchError("readback_artifact_path_invalid")
        if (
            not isinstance(row[16], str)
            or _HEX64.fullmatch(row[16]) is None
            or type(row[17]) is not int
            or row[17] < 0
        ):
            raise B0XDispatchError("readback_artifact_metadata_invalid")
        payload = artifact_path.read_bytes()
        if len(payload) != row[17] or _sha256(payload) != row[16]:
            raise B0XDispatchError("readback_artifact_tampered")
        if cycle_observed is None:
            raise B0XDispatchError("readback_cycle_observation_missing")
        if runner_id not in _CYCLE_RUNNERS:
            raise B0XDispatchError("readback_artifact_unexpected_for_runner")
        expected_lane_dir = (
            binding.runners[runner_id].output_root / RUNNER_LANES[runner_id]
        )
        if artifact_path != _expected_cycle_artifact_path(
            expected_lane_dir, cycle_observed
        ):
            raise B0XDispatchError("readback_artifact_event_identity_mismatch")
        cycle_processes = [
            process
            for process in processes
            if process["stage_id"] == f"cycle:{runner_id}"
        ]
        if len(cycle_processes) != 1:
            raise B0XDispatchError("readback_artifact_attempt_identity_mismatch")
        cycle_process_started = _readback_time(
            cycle_processes[0]["started_at"],
            code="readback_cycle_process_started_at_invalid",
        )
        cycle_process_ended_raw = cycle_processes[0]["ended_at"]
        cycle_process_ended = (
            None
            if cycle_process_ended_raw is None
            else _readback_time(
                cycle_process_ended_raw,
                code="readback_cycle_process_ended_at_invalid",
            )
        )
        if cycle_process_ended is None:
            raise B0XDispatchError("readback_artifact_cycle_process_end_missing")
        if (
            cycle_observed < cycle_process_started
            or cycle_observed > cycle_process_ended
        ):
            raise B0XDispatchError("readback_artifact_attempt_identity_mismatch")
    elif any(row[index] is not None for index in (16, 17, 19)):
        raise B0XDispatchError("readback_artifact_metadata_without_path")
    if terminal_type in {"success_observed", "zero_order_observed"}:
        expected_stage_count = len(
            _plan(
                binding,
                source=source,
                runner_id=runner_id,
                attempt_id=attempt_id,
            )
        )
        if len(processes) != expected_stage_count:
            raise B0XDispatchError("readback_terminal_process_count_mismatch")
        if runner_id in _CYCLE_RUNNERS and artifact_path is None:
            raise B0XDispatchError("readback_terminal_cycle_artifact_missing")
        previous_end: datetime | None = None
        for process in processes:
            stage_started = _readback_time(
                process["started_at"], code="readback_terminal_process_start_invalid"
            )
            if previous_end is not None and stage_started < previous_end:
                raise B0XDispatchError("readback_terminal_process_order_invalid")
            if process["ended_at"] is None:
                raise B0XDispatchError("readback_terminal_process_end_missing")
            stage_ended = _readback_time(
                process["ended_at"], code="readback_terminal_process_end_invalid"
            )
            if process["exit_code"] is None:
                raise B0XDispatchError("readback_terminal_process_exit_missing")
            if process["exit_code"] != 0:
                raise B0XDispatchError("readback_terminal_process_exit_nonzero")
            previous_end = stage_ended
        if terminal_ended is None:
            raise B0XDispatchError("readback_terminal_end_missing")
        if previous_end is not None and terminal_ended < previous_end:
            raise B0XDispatchError("readback_terminal_end_predates_process_end")
    return DispatchReceipt(
        status=_status,
        lane=lane,
        event_id=event_id,
        attempt_id=attempt_id,
        runner_id=runner_id,
        disposition=str(queue_row[1]),
        queue_disposition=str(queue_row[1]),
        claimed_at=str(row[2]),
        process_started_at=None if row[3] is None else str(row[3]),
        cycle_observed_at=None if row[4] is None else str(row[4]),
        terminal_type=None if row[5] is None else str(row[5]),
        terminal_verified=bool(row[6]),
        cycle_starts=int(row[7]),
        push_reapplications=int(row[8]),
        children_started=len(process_rows),
        reason=None if row[9] is None else str(row[9]),
        binding_sha256=str(row[10]),
        source=source,
        owner_epoch=str(row[12]),
        payload_sha256=str(row[13]),
        process_ended_at=(
            None
            if not processes or processes[-1]["ended_at"] is None
            else str(processes[-1]["ended_at"])
        ),
        terminal_ended_at=None if row[14] is None else str(row[14]),
        exit_code=(
            None
            if not processes or processes[-1]["exit_code"] is None
            else int(processes[-1]["exit_code"])
        ),
        artifact_path=None if row[15] is None else str(row[15]),
        artifact_sha256=None if row[16] is None else str(row[16]),
        artifact_bytes=None if row[17] is None else int(row[17]),
        consumed_table_hash=None if row[18] is None else str(row[18]),
        cycle_id=None if row[19] is None else str(row[19]),
        policy_preflight_head=None if row[20] is None else str(row[20]),
        post_build_head=None if row[21] is None else str(row[21]),
        post_commit_head=None if row[22] is None else str(row[22]),
        processes=processes,
    )


__all__ = [
    "B0XDispatchBinding",
    "B0XDispatchError",
    "DISPATCH_BINDING_VERSION",
    "DISPATCH_DRAFT_STATUS",
    "DISPATCH_READY_STATUS",
    "DispatchReceipt",
    "OwnedChildExecutor",
    "POLICY_RECEIPT_MARKER",
    "ProcessResult",
    "ProcessStart",
    "RunnerBinding",
    "SOURCE_RUNNERS",
    "StagePlan",
    "binding_blockers",
    "dispatch_once",
    "dispatch_readback",
    "load_dispatch_binding",
    "stable_dispatch_lock",
]
