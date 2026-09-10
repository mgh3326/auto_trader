"""Hermetic production-path tests for the fixed B0X dispatcher."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services.b0x_lane_consumer import consume_lane_event
from app.services.b0x_lane_event_dispatcher import (
    B0XDispatchError,
    OwnedChildExecutor,
    ProcessResult,
    ProcessStart,
    StagePlan,
    dispatch_once,
    dispatch_readback,
    load_dispatch_binding,
)
from scripts import b0x_lane_event_dispatcher as dispatcher_cli

KST = ZoneInfo("Asia/Seoul")
TABLE_HASH = "sha256:" + "a" * 64
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEX64 = "c" * 64


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> tuple[Path, str]:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.name", "Fixture")
    _git(path, "config", "user.email", "fixture@example.invalid")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "fixture")
    return path.resolve(), _git(path, "rev-parse", "HEAD")


def _policy_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = (tmp_path / "policy-origin.git").resolve()
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    checkout = (tmp_path / "isolated-policy").resolve()
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.name", "Fixture")
    _git(checkout, "config", "user.email", "fixture@example.invalid")
    tables = checkout / "policy-tables"
    tables.mkdir()
    for market in ("kr", "us", "crypto"):
        payload = {
            "schema": "policy_table.v1",
            "market": market,
            "generated_at": "2026-09-10T00:00:00+00:00",
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2).encode()
            + b"\n"
        ).hexdigest()
        payload["stamps"] = {"policy_table_hash": f"sha256:{digest}"}
        name = f"20260910T000000Z-{market}.json"
        (tables / name).write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (tables / f"latest-{market}.json").symlink_to(name)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "tables")
    _git(checkout, "push", "-q", "-u", "origin", "main")
    return checkout, str(origin)


def _non_table_tree_sha256(repo: Path, ref: str = "HEAD") -> str:
    entries = _git(repo, "ls-tree", "-r", "--full-tree", ref).splitlines()
    kept = [
        entry
        for entry in entries
        if not entry.partition("\t")[2].startswith("policy-tables/")
    ]
    payload = ("\n".join(kept) + ("\n" if kept else "")).encode()
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def dispatch_fixture(tmp_path: Path) -> dict[str, Any]:
    auto, auto_head = _repo(
        tmp_path / "auto",
        {
            "scripts/run_b0x_kr_kiwoom_cycle.py": "# fixture\n",
            "scripts/run_b0x_us_cycle.py": "# fixture\n",
            "scripts/run_b0x_cycle.py": "# fixture\n",
        },
    )
    prefect, prefect_head = _repo(
        tmp_path / "prefect",
        {"src/robin_automation/b0x_policy_dispatch.py": "# fixture\n"},
    )
    policy, identity = _policy_repo(tmp_path)
    observation = (tmp_path / "observations").resolve()
    observation.mkdir()
    receipts = (tmp_path / "receipts").resolve()
    receipts.mkdir()
    envs = (tmp_path / "env-refs").resolve()
    envs.mkdir()
    for market in ("kr", "us", "crypto"):
        (envs / f"{market}.ref").write_text("reference-only\n", encoding="utf-8")
    state = (tmp_path / "state/b0x.sqlite3").resolve()
    state.parent.mkdir()
    account = {
        "version": "fixture-approved-v1",
        "source_accounts": {
            "b0x-nudge-kr": "fixture-kr-account",
            "b0x-nudge-us": "fixture-us-account",
            "b0x-nudge-crypto": "fixture-crypto-account",
        },
        "single_writer_receipt_sha256": HEX64,
    }
    account_hash = hashlib.sha256(
        json.dumps(
            account, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    python = str(Path(sys.executable).resolve())
    runners: dict[str, Any] = {}
    for runner, lane, market, account_ref in (
        ("kr_observation", "kiwoom_mock", "kr", "fixture-kr-account"),
        ("us_observation", "alpaca_paper_lab", "us", "fixture-us-account"),
        ("crypto_shadow", "upbit_shadow", "crypto", "fixture-crypto-account"),
    ):
        runners[runner] = {
            "ready": True,
            "table_dir": str(policy / "policy-tables"),
            "output_root": str(observation),
            "environment_ref": str(envs / f"{market}.ref"),
            "writer_lock_path": str(observation / f".{lane}.writer.lock"),
            "account_ref": account_ref,
            "lower_lock_owner_receipt": f"fixture-{market}-lock-owner",
            "no_incomplete_attempt_receipt": f"fixture-{market}-no-incomplete",
        }
    raw = {
        "version": "b0x-dispatch/v1",
        "status": "READY_NOT_ACTIVATED",
        "active": True,
        "gates": {
            "ingress_enabled": True,
            "dispatch_enabled": True,
            "source_enabled": True,
        },
        "owner": {
            "machine": "fixture-machine",
            "id": "fixture-owner",
            "epoch": "fixture-epoch-1",
            "lane": "fixture-b0x-lane",
        },
        "runtime": {
            "state_db": str(state),
            "lock_path": str(state.parent / "stable.lock"),
            "auto_trader_root": str(auto),
            "auto_trader_head": auto_head,
            "python": python,
            "uv": python,
            "prefect_root": str(prefect),
            "prefect_head": prefect_head,
            "prefect_python": python,
            "observation_root": str(observation),
        },
        "policy": {
            "checkout": str(policy),
            "repo_identity": identity,
            "approved_ref": "refs/remotes/origin/main",
            "table_subtree": "policy-tables",
            "output_root": str(policy / "policy-tables"),
            "projection_sha256": account_hash,
            "approved_non_table_tree_sha256": _non_table_tree_sha256(policy),
            "isolation_receipt_sha256": HEX64,
            "receipt_root": str(receipts),
            "git_identity_source": "fixture-existing-git-identity",
            "credential_source": "fixture-existing-credential-source",
        },
        "runners": runners,
        "source_runners": {
            "b0x-nudge-kr": "kr_observation",
            "b0x-nudge-us": "us_observation",
            "b0x-nudge-crypto": "crypto_shadow",
            "b0x-table-kr": "policy_table_build_kr",
            "b0x-table-us": "policy_table_build_us",
            "b0x-harvest": "harvest_observe_only",
        },
        "source_owners": {
            "kr_policy": {
                "selected": "direct_scheduled",
                "direct_scheduled_enabled": True,
                "queued_dispatch_enabled": False,
            },
            "us_policy": {
                "selected": "direct_scheduled",
                "direct_scheduled_enabled": True,
                "queued_dispatch_enabled": False,
            },
            "crypto_policy": {
                "selected": "crypto_pipeline",
                "crypto_pipeline_enabled": True,
            },
        },
        "account_contract": account,
        "account_contract_sha256": account_hash,
        "install_receipt": {
            "id": "fixture-install",
            "manifest_sha256": HEX64,
            "owner_fence_sha256": HEX64,
        },
        "ready_receipt": {
            "id": "fixture-ready",
            "code_head": auto_head,
            "ingress_binding_sha256": HEX64,
            "route_sink_sha256": HEX64,
            "source_owner_sha256": HEX64,
            "no_other_host_owner_sha256": HEX64,
        },
    }
    binding_path = (tmp_path / "dispatch.json").resolve()
    binding_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return {
        "raw": raw,
        "binding": binding_path,
        "state": state,
        "auto": auto,
        "prefect": prefect,
        "policy": policy,
        "observation": observation,
        "now": datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST),
    }


def _rewrite(fixture: dict[str, Any]) -> None:
    fixture["binding"].write_text(
        json.dumps(fixture["raw"], sort_keys=True), encoding="utf-8"
    )


def _load(fixture: dict[str, Any]):  # noqa: ANN202
    return load_dispatch_binding(fixture["binding"], state_db=fixture["state"])


def _event(source: str, now: datetime) -> dict[str, object]:
    disposition, playbook, tick = {
        "b0x-nudge-kr": ("cycle_kickoff", "docs/runbooks/b0x-kr-cycle.md", None),
        "b0x-nudge-us": ("cycle_kickoff", "docs/runbooks/b0x-us-cycle.md", None),
        "b0x-nudge-crypto": (
            "cycle_kickoff",
            "docs/runbooks/b0x-crypto-cycle.md",
            now.strftime("%H%M"),
        ),
        "b0x-table-kr": (
            "policy_table_build",
            "docs/runbooks/b0x-policy-table-build.md",
            None,
        ),
        "b0x-harvest": (
            "observe_only_harvest",
            "docs/runbooks/b0x-harvest.md",
            now.strftime("%H%M"),
        ),
    }[source]
    day = now.date().isoformat()
    event_id = f"kickoff-{source}-{day}" + (f"-T{tick}" if tick else "")
    text = json.dumps(
        {
            "date": day,
            "disposition": disposition,
            "playbook": playbook,
            "source": "b0x",
            "slot": source,
            "tick": tick,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "type": "lane.event",
        "owner_lane": "fixture-b0x-lane",
        "event_id": event_id,
        "text": text,
    }


def _queue(fixture: dict[str, Any], source: str, now: datetime) -> dict[str, object]:
    event = _event(source, now)
    consume_lane_event(
        event,
        state_db=fixture["state"],
        received_at=now,
        hub_received_at=now,
    )
    return event


class FakeExecutor:
    def __init__(
        self,
        fixture: dict[str, Any],
        *,
        mode: str = "success",
        preflight_head: str = HEAD_A,
    ) -> None:
        self.fixture = fixture
        self.mode = mode
        self.preflight_head = preflight_head
        self.calls: list[StagePlan] = []

    def _policy(self, stage: StagePlan) -> dict[str, object]:
        argv = list(stage.argv)
        source = argv[argv.index("--source") + 1]
        attempt_id = argv[argv.index("--attempt-id") + 1]
        phase = argv[argv.index("--phase") + 1]
        return {
            "version": "b0x-policy-attempt/v1",
            "source": source,
            "attempt_id": attempt_id,
            "phase": phase,
            "policy_checkout": str(self.fixture["policy"]),
            "approved_ref": "refs/remotes/origin/main",
            "projection_sha256": self.fixture["raw"]["account_contract_sha256"],
            "steps": [
                "dirty_check",
                "fetch_approved_ref",
                "detached_checkout",
                "non_table_tree_hash_compare",
                "pointer_readlink_blob_compare",
                "table_hash_capture",
            ],
            "preflight_head": self.preflight_head,
            "consumed_table_hash": TABLE_HASH,
            "post_build_head": HEAD_B if phase != "preflight" else None,
            "post_commit_head": HEAD_B
            if phase in {"post-cycle", "build-and-commit"}
            else None,
            "outside_scope_diff": self.mode == "policy_mismatch",
            "pointer_blob_match": self.mode != "pointer_mismatch",
            "non_fast_forward": self.mode == "non_fast_forward"
            and phase == "post-cycle",
            "push_reapplications": 0,
            "cycle_starts": 1 if phase == "post-cycle" else 0,
        }

    def _cycle(self, stage: StagePlan, started_at: datetime) -> None:
        runner_id = {
            "scripts.run_b0x_kr_kiwoom_cycle": "kr_observation",
            "scripts.run_b0x_us_cycle": "us_observation",
            "scripts.run_b0x_cycle": "crypto_shadow",
        }[stage.argv[2]]
        lane = {
            "kr_observation": "kiwoom_mock",
            "us_observation": "alpaca_paper_lab",
            "crypto_shadow": "upbit_shadow",
        }[runner_id]
        lane_dir = self.fixture["observation"] / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "missing_artifact":
            return
        record: dict[str, object] = {
            "lane": "wrong" if self.mode == "wrong_lane" else lane,
            "at": started_at.isoformat(),
            "policy_table_hash": (
                "sha256:" + "f" * 64 if self.mode == "wrong_table" else TABLE_HASH
            ),
            "cycle_id": "b0x-fixture-cycle",
        }
        if runner_id == "kr_observation":
            record.update(
                confirm=False,
                ordering=False,
                submitted=[],
                round_trip=[],
                day_orders=[],
            )
        elif runner_id == "us_observation":
            record.update(confirm=False, submitted=[])
        else:
            record.update(real_orders=0, live_contact=0)
        if self.mode in {"zero_order", "pre_table_zero_order", "zero_order_action"}:
            record.pop("cycle_id")
            record["zero_order_reason"] = "truth_unavailable_observed"
        if self.mode == "pre_table_zero_order":
            record.pop("policy_table_hash")
        if self.mode == "missing_table_hash":
            record.pop("policy_table_hash")
        if self.mode == "zero_order_action":
            if runner_id == "crypto_shadow":
                record["real_orders"] = 1
            else:
                record["submitted"] = [{"fixture": True}]
        if self.mode == "action_present":
            record["submitted"] = [{"fixture": True}]
        with (lane_dir / "cycles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        (lane_dir / f"{len(self.calls):02d}-cycle.md").write_text(
            "fixture artifact\n", encoding="utf-8"
        )

    def __call__(self, stage: StagePlan, *, on_started, timeout_seconds: float):  # noqa: ANN001, ANN204
        del timeout_seconds
        self.calls.append(stage)
        started_at = self.fixture["now"].astimezone(UTC) + timedelta(
            seconds=len(self.calls)
        )
        started = ProcessStart(
            pid=1000 + len(self.calls),
            start_identity=f"fixture-start-{len(self.calls)}",
            started_at=started_at,
        )
        on_started(started)
        if stage.kind == "cycle":
            self._cycle(stage, started_at)
        non_fast_forward = (
            self.mode == "non_fast_forward" and stage.stage_id == "policy:post-cycle"
        )
        return ProcessResult(
            exit_code=2
            if non_fast_forward
            else (1 if self.mode == "exit_failure" else 0),
            started=started,
            ended_at=started_at + timedelta(seconds=1),
            stdout=(
                json.dumps(
                    {"status": "blocked", "reason": "crypto_commit_push_stop_esc"}
                )
                if non_fast_forward
                else ""
            ),
            timed_out=self.mode == "timeout",
            receipt=self._policy(stage) if stage.kind == "policy" else None,
        )


@pytest.mark.parametrize(
    ("source", "now", "module", "lane_args"),
    [
        (
            "b0x-nudge-kr",
            datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST),
            "scripts.run_b0x_kr_kiwoom_cycle",
            (),
        ),
        (
            "b0x-nudge-us",
            datetime(2026, 9, 10, 22, 35, 15, tzinfo=KST),
            "scripts.run_b0x_us_cycle",
            (),
        ),
        (
            "b0x-nudge-crypto",
            datetime(2026, 9, 10, 9, 0, 15, tzinfo=KST),
            "scripts.run_b0x_cycle",
            ("--lane", "shadow"),
        ),
    ],
)
def test_fixed_runner_registry(
    dispatch_fixture: dict[str, Any],
    source: str,
    now: datetime,
    module: str,
    lane_args: tuple[str, ...],
) -> None:
    dispatch_fixture["now"] = now
    _queue(dispatch_fixture, source, now)
    executor = FakeExecutor(dispatch_fixture)
    result = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: now,
        executor=executor,
        attempt_id_factory=lambda: "fixed-attempt",
    )
    cycle = [call for call in executor.calls if call.kind == "cycle"]
    assert result.terminal_type == "success_observed"
    assert result.cycle_starts == 1
    assert len(cycle) == 1
    assert cycle[0].argv[:3] == (str(Path(sys.executable).resolve()), "-m", module)
    assert cycle[0].argv[3 : 3 + len(lane_args)] == lane_args
    assert {"--table-dir", "--out-dir", "--json"}.issubset(cycle[0].argv)
    expected_market = {
        "b0x-nudge-kr": "kr",
        "b0x-nudge-us": "us",
        "b0x-nudge-crypto": "crypto",
    }[source]
    assert cycle[0].environment_ref == (
        dispatch_fixture["binding"].parent / "env-refs" / f"{expected_market}.ref"
    )
    assert {
        "--confirm",
        "--ordering",
        "--bounded-send",
        "--readiness",
        "--now",
        "--sidecar",
        "--repeat",
        "--derivation-only",
    }.isdisjoint(cycle[0].argv)


def test_owned_child_binds_only_exact_environment_reference_path(
    dispatch_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4242
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            return "", ""

        def terminate(self) -> None:
            raise AssertionError("successful fixture child must not be terminated")

        def kill(self) -> None:
            raise AssertionError("successful fixture child must not be killed")

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        observed["argv"] = argv
        observed.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "app.services.b0x_lane_event_dispatcher.subprocess.Popen", fake_popen
    )
    environment_ref = Path(
        dispatch_fixture["raw"]["runners"]["us_observation"]["environment_ref"]
    )
    environment_ref.write_text(
        "SYNTHETIC_CREDENTIAL_SENTINEL=never-log-this\n", encoding="utf-8"
    )
    monkeypatch.setenv("KR_API_KEY_SYNTHETIC", "never-inherit-this")
    stage = StagePlan(
        stage_id="cycle:us_observation",
        kind="cycle",
        argv=(str(Path(sys.executable).resolve()), "-m", "fixture.module"),
        cwd=dispatch_fixture["auto"],
        installed_head=dispatch_fixture["raw"]["runtime"]["auto_trader_head"],
        environment_ref=environment_ref,
    )
    starts: list[ProcessStart] = []
    result = OwnedChildExecutor()(stage, on_started=starts.append, timeout_seconds=1)
    child_env = observed["env"]
    assert isinstance(child_env, dict)
    assert child_env["ENV_FILE"] == str(environment_ref)
    assert (
        child_env["ENV_FILE"]
        != dispatch_fixture["raw"]["runners"]["kr_observation"]["environment_ref"]
    )
    assert "KR_API_KEY_SYNTHETIC" not in child_env
    assert "never-log-this" not in json.dumps(result.__dict__, default=str)
    assert len(starts) == 1


@pytest.mark.parametrize(
    ("source", "now"),
    [
        ("b0x-nudge-kr", datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST)),
        ("b0x-nudge-us", datetime(2026, 9, 10, 22, 35, 15, tzinfo=KST)),
        ("b0x-nudge-crypto", datetime(2026, 9, 10, 9, 0, 15, tzinfo=KST)),
    ],
)
def test_real_shaped_pre_table_zero_order_is_observed_without_action_authority(
    dispatch_fixture: dict[str, Any], source: str, now: datetime
) -> None:
    dispatch_fixture["now"] = now
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, source, now)
    result = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: now,
        executor=FakeExecutor(dispatch_fixture, mode="pre_table_zero_order"),
    )
    assert result.terminal_type == "zero_order_observed"
    assert result.reason == "truth_unavailable_observed"
    assert result.consumed_table_hash is None
    assert result.cycle_starts == 1


@pytest.mark.parametrize(
    ("source", "now"),
    [
        ("b0x-nudge-kr", datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST)),
        ("b0x-nudge-us", datetime(2026, 9, 10, 22, 35, 15, tzinfo=KST)),
        ("b0x-nudge-crypto", datetime(2026, 9, 10, 9, 0, 15, tzinfo=KST)),
    ],
)
def test_zero_order_with_action_evidence_is_not_accepted(
    dispatch_fixture: dict[str, Any], source: str, now: datetime
) -> None:
    dispatch_fixture["now"] = now
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, source, now)
    result = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: now,
        executor=FakeExecutor(dispatch_fixture, mode="zero_order_action"),
    )
    assert result.terminal_type == "failed_preserved"
    assert result.cycle_starts == 1


def test_duplicate_restart_and_concurrent_attempt_are_at_most_once(
    dispatch_fixture: dict[str, Any],
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture)
    first = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
        attempt_id_factory=lambda: "only-attempt",
    )
    restarted = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert first.cycle_starts == 1
    assert restarted.status == "idle"
    assert len([call for call in executor.calls if call.kind == "cycle"]) == 1

    second_db = dispatch_fixture["state"].with_name("concurrent.sqlite3")
    dispatch_fixture["state"] = second_db
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(second_db)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    concurrent_executor = FakeExecutor(dispatch_fixture)
    barrier = Barrier(2)
    outcomes: list[object] = []

    def run() -> None:
        barrier.wait()
        try:
            outcomes.append(
                dispatch_once(
                    _load(dispatch_fixture),
                    second_db,
                    lambda: dispatch_fixture["now"],
                    executor=concurrent_executor,
                )
            )
        except B0XDispatchError as exc:
            outcomes.append(exc.code)

    threads = [Thread(target=run), Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (
        len([call for call in concurrent_executor.calls if call.kind == "cycle"]) == 1
    )
    with sqlite3.connect(second_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM b0x_dispatch_attempt"
        ).fetchone() == (1,)


def test_claim_and_process_crashes_become_unknown_without_retry(
    dispatch_fixture: dict[str, Any],
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture)

    def crash_claim(point: str) -> None:
        if point == "after_claim":
            raise SystemExit(9)

    with pytest.raises(SystemExit):
        dispatch_once(
            _load(dispatch_fixture),
            dispatch_fixture["state"],
            lambda: dispatch_fixture["now"],
            executor=executor,
            attempt_id_factory=lambda: "claim-crash",
            fault=crash_claim,
        )
    recovered = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert recovered.terminal_type == "unknown_preserved"
    assert recovered.cycle_starts == 0
    assert recovered.process_started_at is None
    assert recovered.process_ended_at is None
    assert recovered.terminal_ended_at is not None
    assert executor.calls == []

    next_slot = dispatch_fixture["now"] + timedelta(days=1)
    dispatch_fixture["now"] = next_slot
    held_event = _queue(dispatch_fixture, "b0x-nudge-kr", next_slot)
    with sqlite3.connect(dispatch_fixture["state"]) as connection:
        assert connection.execute(
            "SELECT disposition FROM b0x_lane_event WHERE lane=? AND event_id=?",
            ("fixture-b0x-lane", held_event["event_id"]),
        ).fetchone() == ("held_unconsumed_active_slot",)
    no_promotion = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: next_slot,
        executor=executor,
    )
    assert no_promotion.status == "idle"
    assert executor.calls == []

    process_db = dispatch_fixture["state"].with_name("process-crash.sqlite3")
    dispatch_fixture["state"] = process_db
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(process_db)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    process_executor = FakeExecutor(dispatch_fixture)

    def crash_process(point: str) -> None:
        if point == "after_cycle:kr_observation":
            raise KeyboardInterrupt

    unknown = dispatch_once(
        _load(dispatch_fixture),
        process_db,
        lambda: dispatch_fixture["now"],
        executor=process_executor,
        fault=crash_process,
    )
    again = dispatch_once(
        _load(dispatch_fixture),
        process_db,
        lambda: dispatch_fixture["now"],
        executor=process_executor,
    )
    assert unknown.terminal_type == "unknown_preserved"
    assert unknown.cycle_starts == 1
    assert again.status == "idle"
    assert len([call for call in process_executor.calls if call.kind == "cycle"]) == 1


@pytest.mark.parametrize(
    ("mode", "terminal"),
    [
        ("success", "success_observed"),
        ("zero_order", "zero_order_observed"),
        ("exit_failure", "failed_preserved"),
        ("timeout", "unknown_preserved"),
        ("missing_artifact", "failed_preserved"),
        ("missing_table_hash", "failed_preserved"),
        ("wrong_lane", "failed_preserved"),
        ("wrong_table", "failed_preserved"),
        ("action_present", "failed_preserved"),
    ],
)
def test_typed_outcomes_and_artifact_validation(
    dispatch_fixture: dict[str, Any], mode: str, terminal: str
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    result = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture, mode=mode),
        attempt_id_factory=lambda: f"attempt-{mode}",
    )
    assert result.terminal_type == terminal
    assert result.terminal_verified is True


def test_draft_out_of_window_us_unready_and_owner_conflict_start_zero(
    dispatch_fixture: dict[str, Any],
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture)
    late = dispatch_fixture["now"] + timedelta(minutes=1)
    held = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: late,
        executor=executor,
    )
    assert held.disposition == "dispatch_held_out_of_window"
    assert executor.calls == []

    for index, mutation in enumerate(("draft", "us", "owner"), start=1):
        state = dispatch_fixture["state"].with_name(f"blocked-{index}.sqlite3")
        dispatch_fixture["state"] = state
        dispatch_fixture["raw"]["runtime"]["state_db"] = str(state)
        dispatch_fixture["raw"]["status"] = "READY_NOT_ACTIVATED"
        dispatch_fixture["raw"]["runners"]["us_observation"]["ready"] = True
        dispatch_fixture["raw"]["source_owners"]["kr_policy"] = {
            "selected": "direct_scheduled",
            "direct_scheduled_enabled": True,
            "queued_dispatch_enabled": False,
        }
        now = datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST)
        source = "b0x-nudge-kr"
        if mutation == "draft":
            dispatch_fixture["raw"]["status"] = "DRAFT_NOT_INSTALLED"
        elif mutation == "us":
            source = "b0x-nudge-us"
            now = datetime(2026, 9, 10, 22, 35, 15, tzinfo=KST)
            dispatch_fixture["raw"]["runners"]["us_observation"]["ready"] = False
        else:
            dispatch_fixture["raw"]["source_owners"]["kr_policy"].update(
                queued_dispatch_enabled=True
            )
        dispatch_fixture["now"] = now
        _rewrite(dispatch_fixture)
        _queue(dispatch_fixture, source, now)
        blocked_executor = FakeExecutor(dispatch_fixture)
        result = dispatch_once(
            _load(dispatch_fixture),
            state,
            lambda instant=now: instant,
            executor=blocked_executor,
        )
        assert result.status in {"blocked", "held"}
        assert blocked_executor.calls == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("auto_head", "installed_code_head_mismatch"),
        ("account_hash", "account_contract_hash_mismatch"),
        ("owner", "binding_owner_machine_invalid"),
    ],
)
def test_stale_head_hash_and_owner_binding_fail_before_execution(
    dispatch_fixture: dict[str, Any], mutation: str, reason: str
) -> None:
    if mutation == "auto_head":
        dispatch_fixture["raw"]["runtime"]["auto_trader_head"] = "f" * 40
    elif mutation == "account_hash":
        dispatch_fixture["raw"]["account_contract_sha256"] = "f" * 64
        dispatch_fixture["raw"]["policy"]["projection_sha256"] = "f" * 64
    else:
        dispatch_fixture["raw"]["owner"]["machine"] = ""
    _rewrite(dispatch_fixture)
    with pytest.raises(B0XDispatchError, match=reason):
        _load(dispatch_fixture)


def test_wrong_state_owner_receipt_and_absent_handler_start_zero(
    dispatch_fixture: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    other_state = dispatch_fixture["state"].with_name("other.sqlite3")
    with pytest.raises(B0XDispatchError, match="binding_state_db_mismatch"):
        load_dispatch_binding(dispatch_fixture["binding"], state_db=other_state)

    missing = dispatch_fixture["binding"].with_name("missing-binding.json")
    assert (
        dispatcher_cli.main(
            ["--binding", str(missing), "--state-db", str(other_state), "--once"]
        )
        == 2
    )
    refused = json.loads(capsys.readouterr().out)
    assert refused["children_started"] == 0
    assert refused["cycle_starts"] == 0

    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    dispatch_fixture["raw"]["ready_receipt"]["no_other_host_owner_sha256"] = "invalid"
    _rewrite(dispatch_fixture)
    executor = FakeExecutor(dispatch_fixture)
    blocked = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert blocked.status == "blocked"
    assert "no_other_host_owner_sha256_invalid" in str(blocked.reason)
    assert executor.calls == []

    handler_state = dispatch_fixture["state"].with_name("handler.sqlite3")
    dispatch_fixture["state"] = handler_state
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(handler_state)
    dispatch_fixture["raw"]["ready_receipt"]["no_other_host_owner_sha256"] = HEX64
    handler = dispatch_fixture["auto"] / "scripts/run_b0x_kr_kiwoom_cycle.py"
    handler.unlink()
    _git(dispatch_fixture["auto"], "add", "scripts/run_b0x_kr_kiwoom_cycle.py")
    _git(dispatch_fixture["auto"], "commit", "-q", "-m", "fixture missing handler")
    missing_handler_head = _git(dispatch_fixture["auto"], "rev-parse", "HEAD")
    dispatch_fixture["raw"]["runtime"]["auto_trader_head"] = missing_handler_head
    dispatch_fixture["raw"]["ready_receipt"]["code_head"] = missing_handler_head
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    absent_executor = FakeExecutor(dispatch_fixture)
    held = dispatch_once(
        _load(dispatch_fixture),
        handler_state,
        lambda: dispatch_fixture["now"],
        executor=absent_executor,
    )
    assert held.reason == "dispatch_held_runner_kr_observation_handler_absent"
    assert absent_executor.calls == []


def test_policy_checkout_account_and_attempt_receipts_fail_closed(
    dispatch_fixture: dict[str, Any], tmp_path: Path
) -> None:
    shared = (tmp_path / "shared-policy-tables").resolve()
    shared.mkdir()
    dispatch_fixture["raw"]["runners"]["kr_observation"]["table_dir"] = str(shared)
    _rewrite(dispatch_fixture)
    with pytest.raises(B0XDispatchError, match="policy_checkout_mismatch"):
        _load(dispatch_fixture)
    dispatch_fixture["raw"]["runners"]["kr_observation"]["table_dir"] = str(
        dispatch_fixture["policy"] / "policy-tables"
    )
    dispatch_fixture["raw"]["account_contract"]["source_accounts"]["b0x-nudge-kr"] = (
        "fallback-account"
    )
    _rewrite(dispatch_fixture)
    with pytest.raises(B0XDispatchError, match="account_source_runner_mismatch"):
        _load(dispatch_fixture)


def test_advanced_policy_head_is_attempt_scoped_and_mismatch_starts_no_cycle(
    dispatch_fixture: dict[str, Any],
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    advanced = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture, preflight_head=HEAD_B),
        attempt_id_factory=lambda: "advanced-head",
    )
    assert advanced.policy_preflight_head == HEAD_B

    state = dispatch_fixture["state"].with_name("policy-mismatch.sqlite3")
    dispatch_fixture["state"] = state
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(state)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture, mode="policy_mismatch")
    rejected = dispatch_once(
        _load(dispatch_fixture),
        state,
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert rejected.terminal_type == "failed_preserved"
    assert rejected.cycle_starts == 0
    assert all(call.kind != "cycle" for call in executor.calls)


def test_upper_claim_precedes_busy_lower_lock_and_starts_zero_cycles(
    dispatch_fixture: dict[str, Any],
) -> None:
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    lower = Path(
        dispatch_fixture["raw"]["runners"]["kr_observation"]["writer_lock_path"]
    )
    handle = os.open(lower, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    executor = FakeExecutor(dispatch_fixture)
    try:
        result = dispatch_once(
            _load(dispatch_fixture),
            dispatch_fixture["state"],
            lambda: dispatch_fixture["now"],
            executor=executor,
        )
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    assert result.claimed_at is not None
    assert result.reason == "lower_writer_lock_busy"
    assert result.cycle_starts == 0
    assert executor.calls == []

    unknown_state = dispatch_fixture["state"].with_name("lower-unknown.sqlite3")
    dispatch_fixture["state"] = unknown_state
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(unknown_state)
    lower.unlink()
    lower.mkdir()
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    unknown_executor = FakeExecutor(dispatch_fixture)
    unknown = dispatch_once(
        _load(dispatch_fixture),
        unknown_state,
        lambda: dispatch_fixture["now"],
        executor=unknown_executor,
    )
    assert unknown.reason == "lower_writer_lock_state_unknown"
    assert unknown.terminal_type == "unknown_preserved"
    assert unknown.cycle_starts == 0
    assert unknown_executor.calls == []


def test_policy_only_persists_hash_and_crypto_counts_stay_independent(
    dispatch_fixture: dict[str, Any],
) -> None:
    dispatch_fixture["raw"]["source_owners"]["kr_policy"] = {
        "selected": "queued_dispatch",
        "direct_scheduled_enabled": False,
        "queued_dispatch_enabled": True,
    }
    dispatch_fixture["now"] = datetime(2026, 9, 10, 7, 45, 15, tzinfo=KST)
    _rewrite(dispatch_fixture)
    event = _queue(dispatch_fixture, "b0x-table-kr", dispatch_fixture["now"])
    built = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture),
    )
    assert built.terminal_type == "success_observed"
    assert built.consumed_table_hash == TABLE_HASH
    assert built.cycle_starts == 0
    assert built.push_reapplications == 0
    assert (
        dispatch_readback(
            _load(dispatch_fixture),
            lane="fixture-b0x-lane",
            event_id=str(event["event_id"]),
        ).consumed_table_hash
        == TABLE_HASH
    )

    state = dispatch_fixture["state"].with_name("crypto.sqlite3")
    dispatch_fixture["state"] = state
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(state)
    dispatch_fixture["now"] = datetime(2026, 9, 10, 9, 0, 15, tzinfo=KST)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-crypto", dispatch_fixture["now"])
    crypto = dispatch_once(
        _load(dispatch_fixture),
        state,
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture),
    )
    assert crypto.cycle_starts == 1
    assert crypto.push_reapplications == 0


def test_crypto_non_fast_forward_stops_after_one_cycle_without_reapplication(
    dispatch_fixture: dict[str, Any],
) -> None:
    dispatch_fixture["now"] = datetime(2026, 9, 10, 9, 0, 15, tzinfo=KST)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-crypto", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture, mode="non_fast_forward")
    result = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert result.terminal_type == "failed_preserved"
    assert result.reason == "policy_non_fast_forward_stop_esc"
    assert result.cycle_starts == 1
    assert result.push_reapplications == 0
    assert len([stage for stage in executor.calls if stage.kind == "cycle"]) == 1
    assert (
        dispatch_once(
            _load(dispatch_fixture),
            dispatch_fixture["state"],
            lambda: dispatch_fixture["now"],
            executor=executor,
        ).status
        == "idle"
    )
    assert len([stage for stage in executor.calls if stage.kind == "cycle"]) == 1
    next_tick = dispatch_fixture["now"] + timedelta(hours=4)
    dispatch_fixture["now"] = next_tick
    held_event = _queue(dispatch_fixture, "b0x-nudge-crypto", next_tick)
    with sqlite3.connect(dispatch_fixture["state"]) as connection:
        assert connection.execute(
            "SELECT disposition FROM b0x_lane_event WHERE lane=? AND event_id=?",
            ("fixture-b0x-lane", held_event["event_id"]),
        ).fetchone() == ("held_unconsumed_active_slot",)
    assert (
        dispatch_once(
            _load(dispatch_fixture),
            dispatch_fixture["state"],
            lambda: next_tick,
            executor=executor,
        ).status
        == "idle"
    )
    assert len([stage for stage in executor.calls if stage.kind == "cycle"]) == 1


def test_harvest_starts_no_process_and_clocks_remain_separate(
    dispatch_fixture: dict[str, Any],
) -> None:
    dispatch_fixture["now"] = datetime(2026, 9, 10, 9, 13, 15, tzinfo=KST)
    _queue(dispatch_fixture, "b0x-harvest", dispatch_fixture["now"])
    executor = FakeExecutor(dispatch_fixture)
    harvest = dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=executor,
    )
    assert harvest.terminal_type == "zero_order_observed"
    assert harvest.reason == "harvest_observation_only_no_cycle"
    assert harvest.cycle_starts == 0
    assert harvest.process_started_at is None
    assert harvest.process_ended_at is None
    assert harvest.terminal_ended_at is not None
    assert executor.calls == []

    state = dispatch_fixture["state"].with_name("clocks.sqlite3")
    dispatch_fixture["state"] = state
    dispatch_fixture["raw"]["runtime"]["state_db"] = str(state)
    dispatch_fixture["now"] = datetime(2026, 9, 10, 9, 5, 15, tzinfo=KST)
    _rewrite(dispatch_fixture)
    _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    result = dispatch_once(
        _load(dispatch_fixture),
        state,
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture),
    )
    assert result.claimed_at != result.process_started_at
    assert result.process_started_at != result.cycle_observed_at
    assert result.post_build_head is None


def test_public_cli_readback_is_typed_mutation_free_and_detects_tamper(
    dispatch_fixture: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    event = _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    arguments = [
        "--binding",
        str(dispatch_fixture["binding"]),
        "--state-db",
        str(dispatch_fixture["state"]),
        "--once",
    ]
    executor = FakeExecutor(dispatch_fixture)
    assert (
        dispatcher_cli.main(
            arguments,
            clock=lambda: dispatch_fixture["now"],
            executor=executor,
            attempt_id_factory=lambda: "cli-attempt",
        )
        == 0
    )
    capsys.readouterr()
    before_db = dispatch_fixture["state"].read_bytes()
    before_files = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dispatch_fixture["observation"].rglob("*")
        if path.is_file()
    }
    readback_args = [
        "--binding",
        str(dispatch_fixture["binding"]),
        "--state-db",
        str(dispatch_fixture["state"]),
        "--readback",
        "--lane",
        "fixture-b0x-lane",
        "--event-id",
        str(event["event_id"]),
    ]
    assert dispatcher_cli.main(readback_args) == 0
    raw = json.loads(capsys.readouterr().out)
    assert raw["lane"] == "fixture-b0x-lane"
    assert raw["event_id"] == event["event_id"]
    assert raw["attempt_id"] == "cli-attempt"
    assert len(raw["binding_sha256"]) == 64
    assert raw["source"] == "b0x-nudge-kr"
    assert raw["runner_id"] == "kr_observation"
    assert raw["owner_epoch"] == "fixture-epoch-1"
    assert len(raw["payload_sha256"]) == 64
    assert raw["queue_disposition"] == "success_observed"
    assert raw["disposition"] == raw["queue_disposition"]
    assert raw["claimed_at"]
    assert raw["process_started_at"]
    assert raw["process_ended_at"]
    assert raw["terminal_ended_at"]
    assert raw["cycle_observed_at"]
    assert raw["terminal_type"] == "success_observed"
    assert raw["terminal_verified"] is True
    assert raw["reason"] == "cycle_artifact_verified"
    assert raw["exit_code"] == 0
    assert raw["artifact_path"]
    assert raw["artifact_sha256"]
    assert raw["artifact_bytes"] > 0
    artifact = Path(raw["artifact_path"])
    assert raw["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert raw["artifact_bytes"] == len(artifact.read_bytes())
    assert raw["consumed_table_hash"] == TABLE_HASH
    assert raw["cycle_id"] == "b0x-fixture-cycle"
    assert len(raw["processes"]) == 2
    process = raw["processes"][0]
    assert set(process) == {
        "attempt_id",
        "stage_index",
        "stage_id",
        "executable",
        "argv_sha256",
        "cwd",
        "installed_head",
        "owner_epoch",
        "environment_ref_sha256",
        "pid",
        "start_identity",
        "started_at",
        "ended_at",
        "exit_code",
    }
    expected_stage = executor.calls[0]
    assert process["executable"] == expected_stage.argv[0]
    assert (
        process["argv_sha256"]
        == hashlib.sha256(
            json.dumps(
                expected_stage.argv,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert process["cwd"] == str(expected_stage.cwd)
    assert process["installed_head"] == expected_stage.installed_head
    assert process["owner_epoch"] == "fixture-epoch-1"
    assert process["environment_ref_sha256"] is None
    assert process["pid"] > 0
    assert process["start_identity"]
    assert process["started_at"]
    assert process["ended_at"]
    assert process["exit_code"] == 0
    cycle_process = raw["processes"][1]
    assert (
        cycle_process["environment_ref_sha256"]
        == hashlib.sha256(
            json.dumps(
                str(executor.calls[1].environment_ref),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert raw["process_started_at"] == process["started_at"]
    assert raw["process_ended_at"] == cycle_process["ended_at"]
    serialized = json.dumps(raw, sort_keys=True)
    assert "stdout" not in serialized
    assert "stderr" not in serialized
    assert "SYNTHETIC_CREDENTIAL_SENTINEL" not in serialized
    assert dispatch_fixture["state"].read_bytes() == before_db
    assert {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dispatch_fixture["observation"].rglob("*")
        if path.is_file()
    } == before_files

    original_payload = artifact.read_bytes()
    same_bytes_target = artifact.with_name("same-bytes-target.md")
    same_bytes_target.write_bytes(original_payload)
    artifact.unlink()
    artifact.symlink_to(same_bytes_target.name)
    assert dispatcher_cli.main(readback_args) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == (
        "readback_artifact_path_invalid"
    )

    artifact.unlink()
    artifact.write_bytes(original_payload)
    artifact.write_text("tampered\n", encoding="utf-8")
    assert dispatcher_cli.main(readback_args) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "readback_artifact_tampered"


def test_readback_rejects_process_from_different_attempt(
    dispatch_fixture: dict[str, Any],
) -> None:
    event = _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture),
        attempt_id_factory=lambda: "right-attempt",
    )
    with sqlite3.connect(dispatch_fixture["state"]) as connection:
        connection.execute(
            "UPDATE b0x_dispatch_process SET attempt_id='different-attempt' "
            "WHERE stage_index=0"
        )
    with pytest.raises(
        B0XDispatchError, match="readback_process_attempt_or_index_mismatch"
    ):
        dispatch_readback(
            _load(dispatch_fixture),
            lane="fixture-b0x-lane",
            event_id=str(event["event_id"]),
        )


def test_readback_rejects_process_registry_evidence_tamper(
    dispatch_fixture: dict[str, Any],
) -> None:
    event = _queue(dispatch_fixture, "b0x-nudge-kr", dispatch_fixture["now"])
    dispatch_once(
        _load(dispatch_fixture),
        dispatch_fixture["state"],
        lambda: dispatch_fixture["now"],
        executor=FakeExecutor(dispatch_fixture),
    )
    with sqlite3.connect(dispatch_fixture["state"]) as connection:
        connection.execute(
            "UPDATE b0x_dispatch_process SET executable='/fixture/not-approved' "
            "WHERE stage_index=0"
        )
    with pytest.raises(B0XDispatchError, match="readback_process_registry_mismatch"):
        dispatch_readback(
            _load(dispatch_fixture),
            lane="fixture-b0x-lane",
            event_id=str(event["event_id"]),
        )


def test_public_template_is_draft_off_and_contains_no_private_runtime_values() -> None:
    text = Path("config/b0x_dispatch_binding.json.in").read_text(encoding="utf-8")
    raw = json.loads(text)
    assert raw["status"] == "DRAFT_NOT_INSTALLED"
    assert raw["active"] is False
    assert raw["gates"] == {
        "ingress_enabled": False,
        "dispatch_enabled": False,
        "source_enabled": False,
    }
    assert all(runner["ready"] is False for runner in raw["runners"].values())
    runbook = " ".join(
        Path("docs/runbooks/b0x-portability-install.md")
        .read_text(encoding="utf-8")
        .split()
    )
    for required in (
        "python -m scripts.b0x_lane_event_dispatcher",
        "binding-selected `ENV_FILE` path",
        "US never inherits another runner's reference",
        "push_reapplications=0",
        "queue disposition",
        "artifact path/hash/bytes/table hash/cycle id",
        "paused with an inactive schedule and zero retries",
    ):
        assert required in runbook
    assert raw["owner"]["lane"] == "INSTALLER_INPUT_LANE"

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [item for child in value.values() for item in strings(child)]
        if isinstance(value, list):
            return [item for child in value for item in strings(child)]
        return []

    absolute_values = [value for value in strings(raw) if value.startswith("/")]
    assert absolute_values
    assert all(
        value.startswith("/ABSOLUTE/INSTALLER_INPUT/") for value in absolute_values
    )
