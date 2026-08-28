"""ROB-1319 bounded-send seal, one-shot, and owner-proof adversarial tests."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import tomllib
from pathlib import Path

import pytest

from scripts.b0x.kr import kiwoom_bounded_send as bounded_send
from scripts.b0x.kr import kiwoom_coordination, kiwoom_cycle, kiwoom_ordering
from scripts.b0x.kr.kiwoom_coordination import (
    build_bounded_send_kiwoom_coordination_factory,
    make_grant_only_kiwoom_coordination_adapter,
    resolve_kiwoom_lane_entry,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def isolated_seal_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    registry_path = tmp_path / "registered-seals.toml"
    marker_root = tmp_path / "consumed"
    monkeypatch.setattr(
        bounded_send, "KIWOOM_BOUNDED_SEND_SEAL_REGISTRY_PATH", registry_path
    )
    monkeypatch.setattr(
        bounded_send, "KIWOOM_BOUNDED_SEND_CONSUMPTION_ROOT", marker_root
    )
    monkeypatch.setattr(bounded_send, "_PROCESS_CONSUMED_SEAL_DIGESTS", set())
    monkeypatch.setattr(
        kiwoom_coordination, "_BOUNDED_SEND_CONSTRUCTED_SEAL_DIGESTS", set()
    )
    return registry_path, marker_root


def _expires_at(instant: dt.datetime) -> str:
    assert instant.tzinfo is not None
    return instant.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _seal(*, expires_at: dt.datetime) -> dict[str, str]:
    entry = resolve_kiwoom_lane_entry()
    assert entry.physical_account_id is not None
    expires_at_text = _expires_at(expires_at)
    digest = bounded_send.compute_bounded_send_seal_digest(
        lane_id=entry.lane_id,
        physical_account_id=entry.physical_account_id,
        expires_at=expires_at_text,
    )
    return {
        "lane_id": entry.lane_id,
        "physical_account_id": entry.physical_account_id,
        "expires_at": expires_at_text,
        "seal_digest": digest,
    }


def _write_registry(path: Path, seals: list[dict[str, str]]) -> None:
    lines = [
        "schema_version = "
        + json.dumps(bounded_send.KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA),
    ]
    if not seals:
        lines.append("registered_seals = []")
    for seal in seals:
        lines.append("[[registered_seals]]")
        for key in ("lane_id", "physical_account_id", "expires_at", "seal_digest"):
            lines.append(f"{key} = {json.dumps(seal[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ports(entry):  # noqa: ANN001, ANN202 - exact production-shaped test seam
    return make_grant_only_kiwoom_coordination_adapter(entry).ports


def _factory(seal: dict[str, str]):  # noqa: ANN202 - test helper
    return build_bounded_send_kiwoom_coordination_factory(
        seal=seal,
        ports_factory=_ports,
    )


def _resolve(factory):  # noqa: ANN001, ANN202 - test helper
    entry = resolve_kiwoom_lane_entry()
    return kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=factory,
        expected_entry=entry,
    )


def test_process_one_shot_cannot_be_bypassed_by_removing_durable_marker(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    """① Deleting durable evidence cannot revive a digest in this process."""

    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])

    owner, first = _resolve(_factory(seal))
    assert owner is not None
    assert first["authorizes_send"] is True

    same_owner, repeated = _resolve(lambda: owner)
    assert same_owner is None
    assert repeated["authorizes_send"] is False

    bounded_send.bounded_send_consumption_marker_path(seal["seal_digest"]).unlink()
    reused, second = _resolve(_factory(seal))
    assert reused is None
    assert second["authorizes_send"] is False


def test_durable_consumption_marker_blocks_reuse_after_process_state_reset(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    """② A new-process-shaped request still sees the durable latch."""

    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])

    owner, first = _resolve(_factory(seal))
    assert owner is not None
    assert first["authorizes_send"] is True

    bounded_send._PROCESS_CONSUMED_SEAL_DIGESTS.clear()
    reused, second = _resolve(_factory(seal))
    assert reused is None
    assert second["authorizes_send"] is False


def test_consumed_digest_cannot_construct_a_second_private_owner(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])
    sealed = bounded_send.snapshot_bounded_send_seal(seal)
    bounded_send.consume_registered_bounded_send_seal(sealed)
    ports = _ports(resolve_kiwoom_lane_entry())

    first = kiwoom_coordination._register_approved_adapter(
        ports,
        grant_only=False,
        bounded_send_seal=sealed,
    )
    assert first.grant_only is False
    with pytest.raises(kiwoom_coordination.KiwoomCoordinationOwnerRejected):
        kiwoom_coordination._register_approved_adapter(
            ports,
            grant_only=False,
            bounded_send_seal=sealed,
        )


def test_factory_snapshots_seal_before_expiry_mutation(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    """③ Mutating the caller's dict cannot extend the snapshotted authority."""

    registry_path, _ = isolated_seal_runtime
    original = _seal(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    _write_registry(registry_path, [original.copy()])
    factory = _factory(original)

    original.clear()
    original.update(_seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)))

    owner, record = _resolve(factory)
    assert owner is None
    assert record["authorizes_send"] is False


def test_unsealed_owner_construction_paths_never_authorize_send(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    """④ No direct/flip/private path may make the cycle report true."""

    registry_path, _ = isolated_seal_runtime
    _write_registry(registry_path, [])
    entry = resolve_kiwoom_lane_entry()
    ports = _ports(entry)

    direct = kiwoom_ordering.KiwoomCoordinationAdapter(ports, grant_only=False)
    flipped = make_grant_only_kiwoom_coordination_adapter(entry)
    flipped._grant_only = False  # type: ignore[misc] - adversarial mutation

    for candidate in (direct, flipped):
        owner, record = _resolve(lambda candidate=candidate: candidate)
        assert owner is None
        assert record["authorizes_send"] is False

    with pytest.raises(kiwoom_coordination.KiwoomCoordinationOwnerRejected):
        kiwoom_coordination._register_approved_adapter(ports, grant_only=False)


def test_matching_but_unregistered_digest_is_rejected(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    """⑤ A correctly forged digest is not registration authority."""

    registry_path, marker_root = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [])

    owner, record = _resolve(_factory(seal))
    assert owner is None
    assert record["authorizes_send"] is False
    assert list(marker_root.glob("*")) == []


def test_seal_physical_account_must_match_current_registry(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    registry_path, marker_root = isolated_seal_runtime
    entry = resolve_kiwoom_lane_entry()
    expires_at = _expires_at(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    wrong_account = "kiwoom_mock:kr:credential_fingerprint=sha256:wrong:test"
    seal = {
        "lane_id": entry.lane_id,
        "physical_account_id": wrong_account,
        "expires_at": expires_at,
        "seal_digest": bounded_send.compute_bounded_send_seal_digest(
            lane_id=entry.lane_id,
            physical_account_id=wrong_account,
            expires_at=expires_at,
        ),
    }
    _write_registry(registry_path, [seal])

    owner, record = _resolve(_factory(seal))
    assert owner is None
    assert record["authorizes_send"] is False
    assert list(marker_root.glob("*")) == []


@pytest.mark.parametrize(
    ("clock_offset", "expected_authorization"),
    [
        (-dt.timedelta(microseconds=1), True),
        (dt.timedelta(0), False),
        (dt.timedelta(microseconds=1), False),
    ],
    ids=("just-before", "at-boundary", "just-after"),
)
def test_expiry_boundary_is_open_only_strictly_before_expiry(
    isolated_seal_runtime: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    clock_offset: dt.timedelta,
    expected_authorization: bool,
) -> None:
    """⑥ The exact boundary is closed; the public factory has no clock input."""

    registry_path, _ = isolated_seal_runtime
    expiry = dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    seal = _seal(expires_at=expiry)
    _write_registry(registry_path, [seal])
    monkeypatch.setattr(bounded_send, "_wall_clock_now", lambda: expiry + clock_offset)

    assert (
        "now"
        not in inspect.signature(
            build_bounded_send_kiwoom_coordination_factory
        ).parameters
    )
    owner, record = _resolve(_factory(seal))
    assert (owner is not None) is expected_authorization
    assert record["authorizes_send"] is expected_authorization


@pytest.mark.parametrize("failure_mode", ("raised", "silent"))
def test_marker_write_failure_is_fail_closed(
    isolated_seal_runtime: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """⑦ No durable evidence means no owner authorization."""

    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])

    calls = 0

    def fail_marker_write(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        nonlocal calls
        calls += 1
        if failure_mode == "raised":
            raise OSError("test filesystem refused the durable marker")

    monkeypatch.setattr(bounded_send, "_write_consumption_marker", fail_marker_write)
    with pytest.raises(kiwoom_coordination.KiwoomCoordinationOwnerRejected):
        _factory(seal)()
    assert calls == 1


def test_seal_digest_is_bound_into_the_owner_construction_proof(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])
    owner = _factory(seal)()
    owner._bounded_send_seal_digest = "0" * 64  # type: ignore[attr-defined]

    accepted, record = _resolve(lambda: owner)
    assert accepted is None
    assert record["authorizes_send"] is False


def test_consumption_marker_is_exact_durable_evidence_and_latch(
    isolated_seal_runtime: tuple[Path, Path],
) -> None:
    registry_path, _ = isolated_seal_runtime
    seal = _seal(expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5))
    _write_registry(registry_path, [seal])

    owner, record = _resolve(_factory(seal))
    assert owner is not None
    assert record["authorizes_send"] is True
    marker = json.loads(
        bounded_send.bounded_send_consumption_marker_path(
            seal["seal_digest"]
        ).read_text(encoding="utf-8")
    )
    assert marker == {
        "consumed_at": marker["consumed_at"],
        "expires_at": seal["expires_at"],
        "lane_id": seal["lane_id"],
        "physical_account_id": seal["physical_account_id"],
        "schema_version": bounded_send.KIWOOM_BOUNDED_SEND_MARKER_SCHEMA,
        "seal_digest": seal["seal_digest"],
    }


def test_production_registry_ships_empty() -> None:
    payload = tomllib.loads(
        bounded_send.KIWOOM_BOUNDED_SEND_SEAL_REGISTRY_PATH.read_text(encoding="utf-8")
    )
    assert payload == {
        "schema_version": bounded_send.KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA,
        "registered_seals": [],
    }


def test_nonlegacy_false_registration_has_one_production_call_site() -> None:
    false_calls: list[tuple[str, int]] = []
    true_calls: list[tuple[str, int]] = []
    for path in sorted((REPO_ROOT / "scripts" / "b0x" / "kr").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "_register_approved_adapter":
                continue
            grant_only = next(
                (kw.value for kw in node.keywords if kw.arg == "grant_only"), None
            )
            if isinstance(grant_only, ast.Constant) and grant_only.value is False:
                false_calls.append((path.name, node.lineno))
            if isinstance(grant_only, ast.Constant) and grant_only.value is True:
                true_calls.append((path.name, node.lineno))

    assert [name for name, _ in false_calls] == ["kiwoom_coordination.py"]
    assert [name for name, _ in true_calls] == [
        "kiwoom_coordination.py",
        "kiwoom_coordination.py",
    ]
