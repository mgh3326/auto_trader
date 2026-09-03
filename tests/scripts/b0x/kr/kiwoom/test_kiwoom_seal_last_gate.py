"""ROB-1345/ROB-1343 seal-last-gate and runtime-binding contracts."""

from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services.mock_integration import coordination as core_coordination
from app.services.mock_lane_registry import (
    ActivationStatus,
    LaneGuardError,
    PolicyBinding,
)
from scripts.b0x.kr import kiwoom_bounded_send as bounded_send
from scripts.b0x.kr import kiwoom_coordination, kiwoom_cycle
from scripts.b0x.kr.kiwoom_coordination import (
    build_bounded_send_kiwoom_coordination_factory,
    make_grant_only_kiwoom_coordination_adapter,
    resolve_kiwoom_lane_entry,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
_NOW = dt.datetime(2026, 8, 28, 5, 0, tzinfo=dt.UTC)


def _canary_scope_ready_entry():  # noqa: ANN202 - fixture value
    entry = resolve_kiwoom_lane_entry()
    return replace(
        entry,
        lane_status=LaneStatus.AUTO_ENABLED,
        activation_status=ActivationStatus.RUNTIME_ACCEPTANCE_PENDING,
        activation_reason="seal-last-gate test",
        policy_binding=PolicyBinding("seal-last-gate.v1", "sha256:test-policy"),
        execution_mode="test-only-bounded",
        scheduler_owner=SchedulerOwner.MANUAL,
        timing_owner="test-only-timing",
        writer=False,
        auto_order_enabled=False,
        max_order_notional=Decimal("10000000"),
        max_orders_per_session=8,
        max_open_orders=8,
        allowed_order_types=("limit",),
        allowed_time_in_force=("day",),
        reconcile_required=True,
        canary_binding="test-only-bounded-canary",
        missing_bindings=(),
    )


def _seal(entry, *, expiry_minutes: int) -> dict[str, str]:  # noqa: ANN001
    assert entry.physical_account_id is not None
    expiry = _NOW + dt.timedelta(minutes=expiry_minutes)
    expires_at = expiry.isoformat().replace("+00:00", "Z")
    digest = bounded_send.compute_bounded_send_seal_digest(
        lane_id=entry.lane_id,
        physical_account_id=entry.physical_account_id,
        expires_at=expires_at,
    )
    return {
        "lane_id": entry.lane_id,
        "physical_account_id": entry.physical_account_id,
        "expires_at": expires_at,
        "seal_digest": digest,
    }


def _write_registry(path: Path, seal: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "schema_version = "
        + json.dumps(bounded_send.KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA),
        "[[registered_seals]]",
    ]
    for key in ("lane_id", "physical_account_id", "expires_at", "seal_digest"):
        lines.append(f"{key} = {json.dumps(seal[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ports(entry):  # noqa: ANN001, ANN202 - production-shaped test seam
    return make_grant_only_kiwoom_coordination_adapter(entry).ports


def _install_isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    expiry_minutes: int,
):  # noqa: ANN202 - fixture helper
    registry_path = root / "registered-seals.toml"
    marker_root = root / "consumed"
    entry = _canary_scope_ready_entry()
    seal = _seal(entry, expiry_minutes=expiry_minutes)
    _write_registry(registry_path, seal)
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
    monkeypatch.setattr(bounded_send, "_wall_clock_now", lambda: _NOW)
    monkeypatch.setattr(
        kiwoom_coordination,
        "resolve_kiwoom_lane_entry",
        lambda _lane_id=kiwoom_coordination.KIWOOM_KR_LANE_ID: entry,
    )
    factory = build_bounded_send_kiwoom_coordination_factory(
        seal=seal,
        ports_factory=_ports,
    )
    return entry, seal, marker_root, factory


def _called_name(call: ast.Call) -> str | None:
    """Return both direct and attribute-form call names (MUTANT4 guard)."""

    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _raised_name(statement: ast.Raise) -> str | None:
    if isinstance(statement.exc, ast.Call):
        return _called_name(statement.exc)
    if isinstance(statement.exc, ast.Name):
        return statement.exc.id
    if isinstance(statement.exc, ast.Attribute):
        return statement.exc.attr
    return None


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_static_post_consumption_ast_walk_is_exact_closed_allowlist() -> None:
    """AC2: enumerate every normalized post-consumption reject statically."""

    attribute_probe = ast.parse("module.consume_registered_bounded_send_seal(seal)")
    probe_call = next(
        node for node in ast.walk(attribute_probe) if isinstance(node, ast.Call)
    )
    assert _called_name(probe_call) == "consume_registered_bounded_send_seal"

    bounded_tree = ast.parse(Path(bounded_send.__file__).read_text(encoding="utf-8"))
    consume = _function(bounded_tree, "consume_registered_bounded_send_seal")
    marker_write_line = min(
        call.lineno
        for call in ast.walk(consume)
        if isinstance(call, ast.Call)
        and _called_name(call) == "_write_consumption_marker"
    )
    assert not [
        call
        for call in ast.walk(consume)
        if isinstance(call, ast.Call)
        and call.lineno > marker_write_line
        and _called_name(call) == "_reject"
    ]
    assert not [
        statement
        for statement in ast.walk(consume)
        if isinstance(statement, ast.Raise) and statement.lineno > marker_write_line
    ]
    bounded_reject_calls = [
        call
        for call in ast.walk(consume)
        if isinstance(call, ast.Call)
        and _called_name(call) == "_reject_after_consumption"
    ]
    bounded_reason_values = [
        keyword.value
        for call in bounded_reject_calls
        for keyword in call.keywords
        if keyword.arg == "reason"
    ]
    assert len(bounded_reason_values) == len(bounded_reject_calls)
    assert all(isinstance(value, ast.Name) for value in bounded_reason_values)
    bounded_reason_names = {
        value.id for value in bounded_reason_values if isinstance(value, ast.Name)
    }

    coordination_tree = ast.parse(
        Path(kiwoom_coordination.__file__).read_text(encoding="utf-8")
    )
    collapse = _function(coordination_tree, "_closed_post_consumption_reason")
    collapse_code_names = {
        key.id
        for node in ast.walk(collapse)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Name)
    }
    collapse_reason_names = {
        value.id
        for node in ast.walk(collapse)
        if isinstance(node, ast.Dict)
        for value in node.values
        if isinstance(value, ast.Name)
    }
    collapse_reason_names.update(
        node.value.id
        for node in ast.walk(collapse)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id.isupper()
    )
    observed_names = bounded_reason_names | collapse_reason_names
    observed = {getattr(bounded_send, name) for name in observed_names}

    binding_reject_code_names: set[str] = set()
    for function_name in (
        "assert_consumed_bounded_send_seal_binding",
        "_load_exact_marker",
    ):
        function = _function(bounded_tree, function_name)
        for call in ast.walk(function):
            if (
                isinstance(call, ast.Call)
                and _called_name(call) == "_reject"
                and call.args
                and isinstance(call.args[0], ast.Name)
            ):
                binding_reject_code_names.add(call.args[0].id)
            if (
                isinstance(call, ast.Call)
                and _called_name(call) == "_parse_canonical_utc"
            ):
                binding_reject_code_names.update(
                    keyword.value.id
                    for keyword in call.keywords
                    if keyword.arg == "code" and isinstance(keyword.value, ast.Name)
                )
    assert binding_reject_code_names <= collapse_code_names

    factory = _function(
        _function(
            coordination_tree,
            "build_bounded_send_kiwoom_coordination_factory",
        ),
        "_factory",
    )
    consume_line = min(
        call.lineno
        for call in ast.walk(factory)
        if isinstance(call, ast.Call)
        and _called_name(call) == "consume_registered_bounded_send_seal"
    )
    call_lines = {
        name: sorted(
            call.lineno
            for call in ast.walk(factory)
            if isinstance(call, ast.Call) and _called_name(call) == name
        )
        for name in (
            "_validate_canary_scope_authority_inputs",
            "_register_approved_adapter",
            "assert_kiwoom_coordination_owner",
            "_issue_canary_scope_authority",
        )
    }
    assert len(call_lines["_validate_canary_scope_authority_inputs"]) == 1
    assert call_lines["_validate_canary_scope_authority_inputs"][0] < consume_line
    assert len(call_lines["_register_approved_adapter"]) == 3
    assert all(
        line < consume_line for line in call_lines["_register_approved_adapter"][:2]
    )
    assert call_lines["_register_approved_adapter"][2] > consume_line
    assert len(call_lines["assert_kiwoom_coordination_owner"]) == 1
    assert call_lines["assert_kiwoom_coordination_owner"][0] < consume_line
    assert len(call_lines["_issue_canary_scope_authority"]) == 1
    assert call_lines["_issue_canary_scope_authority"][0] > consume_line

    register = _function(coordination_tree, "_register_approved_adapter")
    for single_validation_call in (
        "assert_bounded_send_seal_self_consistent",
        "_has_current_canary_scope_authority",
    ):
        assert (
            sum(
                isinstance(call, ast.Call)
                and _called_name(call) == single_validation_call
                for call in ast.walk(register)
            )
            == 1
        )
    post_raises = sorted(
        (
            node
            for node in ast.walk(factory)
            if isinstance(node, ast.Raise) and node.lineno > consume_line
        ),
        key=lambda node: node.lineno,
    )
    post_raise_names = [_raised_name(node) for node in post_raises]
    assert post_raise_names == [
        "KiwoomPostConsumptionOwnerRejected",
        "KiwoomCoordinationOwnerRejected",
        "KiwoomCoordinationOwnerRejected",
        "KiwoomPostConsumptionOwnerRejected",
    ]
    print("POST_CONSUMPTION_REJECT_REASONS_STATIC=" + ",".join(sorted(observed)))
    assert observed == set(bounded_send.POST_CONSUMPTION_REJECT_REASONS)
    assert observed <= bounded_send.POST_CONSUMPTION_REJECT_REASONS


class _TrackingDigestSet(set[str]):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def __contains__(self, value: object) -> bool:
        self._events.append("one_owner_checked")
        return super().__contains__(value)


def test_eligible_canary_dry_validates_and_asserts_owner_before_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive lane: all pre-gates pass, then consume, then owner proceeds."""

    entry, seal, _, factory = _install_isolated_runtime(
        monkeypatch,
        root=tmp_path,
        expiry_minutes=35,
    )
    events: list[str] = []
    tracking_set = _TrackingDigestSet(events)
    monkeypatch.setattr(
        kiwoom_coordination,
        "_BOUNDED_SEND_CONSTRUCTED_SEAL_DIGESTS",
        tracking_set,
    )
    original_assert = kiwoom_coordination.assert_kiwoom_coordination_owner
    original_write = bounded_send._write_consumption_marker

    def tracking_assert(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        events.append("owner_asserted")
        return original_assert(*args, **kwargs)

    def tracking_write(path: Path, payload: bytes) -> None:
        events.append("seal_consumed")
        original_write(path, payload)

    monkeypatch.setattr(
        kiwoom_coordination, "assert_kiwoom_coordination_owner", tracking_assert
    )
    monkeypatch.setattr(bounded_send, "_write_consumption_marker", tracking_write)

    owner = factory()
    resolved, record = kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=lambda: owner,
        expected_entry=entry,
    )

    assert resolved is owner
    assert record["authorizes_send"] is True
    assert bounded_send.bounded_send_consumption_marker_path(
        seal["seal_digest"]
    ).is_file()
    assert events.index("one_owner_checked") < events.index("owner_asserted")
    assert events.index("owner_asserted") < events.index("seal_consumed")
    assert events.count("one_owner_checked") == 1
    assert events.count("owner_asserted") == 1


def test_current_authority_rejects_before_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live same-seal authority is Class II and preserves the marker."""

    entry, seal, _, factory = _install_isolated_runtime(
        monkeypatch,
        root=tmp_path,
        expiry_minutes=35,
    )
    assert entry.physical_account_id is not None
    held_authority = core_coordination._issue_canary_scope_authority(
        lane_id=entry.lane_id,
        physical_account_id=entry.physical_account_id,
        seal_digest=seal["seal_digest"],
    )

    with pytest.raises(kiwoom_coordination.KiwoomCoordinationOwnerRejected):
        factory()

    assert held_authority.seal_digest == seal["seal_digest"]
    assert not bounded_send.bounded_send_consumption_marker_path(
        seal["seal_digest"]
    ).exists()


async def _run_post_consumption_probe(
    *,
    mode: str,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_minutes: int,
) -> str:
    with monkeypatch.context() as scoped:
        entry, seal, marker_root, factory = _install_isolated_runtime(
            scoped,
            root=root,
            expiry_minutes=expiry_minutes,
        )
        original_write = bounded_send._write_consumption_marker
        expiry = dt.datetime.fromisoformat(seal["expires_at"].replace("Z", "+00:00"))

        if mode == "marker_write_failed":

            def marker_write_failed(_path: Path, _payload: bytes) -> None:
                raise OSError("injected marker write failure")

            scoped.setattr(
                bounded_send, "_write_consumption_marker", marker_write_failed
            )
        elif mode == "marker_invalid":

            def marker_invalid(path: Path, payload: bytes) -> None:
                original_write(path, payload)
                path.write_bytes(payload + b"corrupt")

            scoped.setattr(bounded_send, "_write_consumption_marker", marker_invalid)
        elif mode == "durable_write_expired":

            def expire_during_write(path: Path, payload: bytes) -> None:
                original_write(path, payload)
                scoped.setattr(bounded_send, "_wall_clock_now", lambda: expiry)

            scoped.setattr(
                bounded_send, "_write_consumption_marker", expire_during_write
            )
        elif mode == "invalid_final_clock":

            def invalidate_clock_after_write(path: Path, payload: bytes) -> None:
                original_write(path, payload)
                scoped.setattr(bounded_send, "_wall_clock_now", lambda: object())

            scoped.setattr(
                bounded_send,
                "_write_consumption_marker",
                invalidate_clock_after_write,
            )
        elif mode == "already_consumed":
            marker_path = bounded_send.bounded_send_consumption_marker_path(
                seal["seal_digest"]
            )
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("already occupied\n", encoding="utf-8")
        elif mode == "ownership_lost":

            def lose_ownership(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
                raise kiwoom_coordination.KiwoomCoordinationOwnerRejected(
                    kiwoom_coordination.KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH,
                    lane_id=entry.lane_id,
                )

            scoped.setattr(
                kiwoom_cycle, "assert_kiwoom_coordination_owner", lose_ownership
            )
        else:  # pragma: no cover - closed local probe vocabulary
            raise AssertionError(f"unknown probe mode: {mode}")

        notices: list[str] = []
        out_dir = root / "observations"

        async def assert_durable_before_notify(incident: dict[str, object]) -> None:
            cycle_path = out_dir / kiwoom_cycle.LANE / "cycles.jsonl"
            rows = [
                json.loads(line)
                for line in cycle_path.read_text(encoding="utf-8").splitlines()
            ]
            assert any(
                row.get("cycle_record_phase") == "seal_consumed_no_send_immediate"
                and row.get("seal_consumed_no_send", {}).get("reason")
                == incident["reason"]
                for row in rows
            )
            notices.append(str(incident["reason"]))

        outcome = await kiwoom_cycle.run_kiwoom_cycle(
            now=_NOW,
            table_dir=root / "unused-table",
            out_dir=out_dir,
            confirm=True,
            coordination_factory=factory,
            coordination_entry=entry,
            authority_risk_notifier=assert_durable_before_notify,
        )
        reason = outcome.record["seal_consumed_no_send"]["reason"]
        assert notices == [reason]
        assert outcome.exit_code == 2
        assert outcome.zero_order_reason == kiwoom_coordination.SEAL_CONSUMED_NO_SEND
        assert outcome.record["submitted"] == []
        assert (
            outcome.record["seal_consumed_no_send"]["operator_notification"] == "sent"
        )
        assert marker_root.is_relative_to(root)
        return reason


@pytest.mark.asyncio
async def test_dynamic_post_consumption_probes_observe_exact_closed_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2/AC3: every Class I route is observed, recorded, then notified."""

    modes = (
        "marker_write_failed",
        "marker_invalid",
        "durable_write_expired",
        "invalid_final_clock",
        "already_consumed",
        "ownership_lost",
    )
    observed = {
        await _run_post_consumption_probe(
            mode=mode,
            root=tmp_path / mode,
            monkeypatch=monkeypatch,
            expiry_minutes=35 + (index * 5),
        )
        for index, mode in enumerate(modes)
    }

    print("POST_CONSUMPTION_REJECT_REASONS_DYNAMIC=" + ",".join(sorted(observed)))
    assert observed == set(bounded_send.POST_CONSUMPTION_REJECT_REASONS)
    assert observed <= bounded_send.POST_CONSUMPTION_REJECT_REASONS


def test_forged_authority_without_consumed_seal_fails_runtime_binding() -> None:
    """ROB-1343: an issued-looking digest without marker evidence is no authority."""

    entry = resolve_kiwoom_lane_entry()
    assert entry.physical_account_id is not None
    forged = core_coordination._issue_canary_scope_authority(
        lane_id=entry.lane_id,
        physical_account_id=entry.physical_account_id,
        seal_digest="f" * 64,
    )

    try:
        core_coordination.assert_canary_scope_authority_binding(entry, forged)
    except LaneGuardError as refusal:
        assert refusal.code == "canary_scope_authority_seal_unbound"
    else:
        raise AssertionError(
            "forged canary authority without a consumed seal passed runtime binding"
        )
