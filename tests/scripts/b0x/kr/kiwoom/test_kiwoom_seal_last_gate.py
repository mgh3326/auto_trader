"""ROB-1345/ROB-1343 seal-last-gate and runtime-binding contracts."""

from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services.mock_integration import coordination as core_coordination
from app.services.mock_lane_registry import (
    ActivationStatus,
    LaneGuardError,
    PolicyBinding,
)
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.kr import kiwoom_bounded_send as bounded_send
from scripts.b0x.kr import kiwoom_coordination, kiwoom_cycle
from scripts.b0x.kr.kiwoom_coordination import (
    build_bounded_send_kiwoom_coordination_factory,
    make_grant_only_kiwoom_coordination_adapter,
    resolve_kiwoom_lane_entry,
)
from scripts.policy_table.core.schema import compute_policy_table_hash
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount

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


def _write_table(path: Path, *, buy_l1: str | None = "70000") -> None:
    payload = make_payload(
        rows=[make_row(symbol="005930", previous_close="72000.00", buy_l1=buy_l1)],
        generated_at=_NOW - dt.timedelta(hours=4),
        market="kr",
    )
    payload["stamps"]["policy_table_hash"] = compute_policy_table_hash(
        {key: value for key, value in payload.items() if key != "stamps"}
    )
    write_table(path, payload, market="kr")


def _arm_cycle_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kiwoom_cycle.kiwoom_lane, "assert_kiwoom_lane_enabled", lambda: None
    )
    monkeypatch.setattr(
        kiwoom_cycle.kiwoom_lane,
        "account_identity_summary",
        lambda: {"fingerprint": "sha256:isolated-test", "product_suffix": "28"},
    )

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # noqa: ANN102
            return _NOW.astimezone(tz) if tz is not None else _NOW.replace(tzinfo=None)

    monkeypatch.setattr(
        kiwoom_cycle,
        "dt",
        SimpleNamespace(datetime=FrozenDateTime, UTC=dt.UTC),
    )


class _ProbeLease:
    def __init__(self, *, lose_on_assert: bool = False) -> None:
        self.held = False
        self.lose_on_assert = lose_on_assert

    def acquire(self) -> None:
        self.held = True

    def assert_held(self) -> None:
        if self.lose_on_assert or not self.held:
            raise RuntimeError("isolated lease lost")

    def release(self) -> None:
        self.held = False

    def canonical(self) -> dict[str, object]:
        return {"acquired": self.held, "isolated_probe": True}


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


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
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
    default_returns = [
        node.value.id
        for node in ast.walk(collapse)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id.isupper()
    ]
    assert default_returns == ["UNCLASSIFIED_POST_CONSUMPTION"]
    assert (
        bounded_send.UNCLASSIFIED_POST_CONSUMPTION
        not in bounded_send.POST_CONSUMPTION_REJECT_REASONS
    )

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

    factory_builder = _function(
        coordination_tree, "build_bounded_send_kiwoom_coordination_factory"
    )
    prepare = _function(factory_builder, "_prepare")
    complete = _function(
        coordination_tree, "_complete_bounded_send_factory_preparation"
    )
    consume_line = min(
        call.lineno
        for call in ast.walk(complete)
        if isinstance(call, ast.Call)
        and _called_name(call) == "consume_registered_bounded_send_seal"
    )
    pre_calls = {
        name: [
            call.lineno
            for call in ast.walk(prepare)
            if isinstance(call, ast.Call) and _called_name(call) == name
        ]
        for name in (
            "_validate_canary_scope_authority_inputs",
            "_register_approved_adapter",
            "assert_kiwoom_coordination_owner",
        )
    }
    post_calls = {
        name: [
            call.lineno
            for call in ast.walk(complete)
            if isinstance(call, ast.Call) and _called_name(call) == name
        ]
        for name in ("_register_approved_adapter", "_issue_canary_scope_authority")
    }
    assert len(pre_calls["_validate_canary_scope_authority_inputs"]) == 1
    assert len(pre_calls["_register_approved_adapter"]) == 2
    assert len(pre_calls["assert_kiwoom_coordination_owner"]) == 1
    assert all(line < consume_line for lines in pre_calls.values() for line in lines)
    assert len(post_calls["_register_approved_adapter"]) == 1
    assert len(post_calls["_issue_canary_scope_authority"]) == 1
    assert all(line > consume_line for lines in post_calls.values() for line in lines)
    post_raise_names = [
        _raised_name(node)
        for node in sorted(
            (
                node
                for node in ast.walk(complete)
                if isinstance(node, ast.Raise) and node.lineno > consume_line
            ),
            key=lambda node: node.lineno,
        )
    ]
    assert post_raise_names == [
        "KiwoomPostConsumptionOwnerRejected",
        "KiwoomCoordinationOwnerRejected",
        "KiwoomCoordinationOwnerRejected",
        "KiwoomPostConsumptionOwnerRejected",
    ]

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

    cycle_tree = ast.parse(Path(kiwoom_cycle.__file__).read_text(encoding="utf-8"))
    boundary_classifier = _function(cycle_tree, "_presend_recheck_reason")
    boundary_reason_names = {
        value.id
        for node in ast.walk(boundary_classifier)
        if isinstance(node, ast.Dict)
        for value in node.values
        if isinstance(value, ast.Name)
    }
    rejector_reason_names = {
        call.args[2].id
        for function_name in ("_run_prepared_cycle", "_run_ordering_cycle")
        for call in ast.walk(_function(cycle_tree, function_name))
        if isinstance(call, ast.Call)
        and _called_name(call) == "rejector"
        and len(call.args) == 3
        and isinstance(call.args[2], ast.Name)
    }
    observed_names = (
        bounded_reason_names
        | collapse_reason_names
        | boundary_reason_names
        | rejector_reason_names
        | {"OWNERSHIP_LOST_AFTER_CONSUMPTION"}
    )
    observed = {getattr(bounded_send, name) for name in observed_names}

    run_cycle = _function(cycle_tree, "run_kiwoom_cycle")
    resolve_line = min(
        call.lineno
        for call in ast.walk(run_cycle)
        if isinstance(call, ast.Call)
        and _called_name(call) == "_resolve_coordination_owner"
    )
    post_resolve_finish_zero = [
        call
        for call in ast.walk(run_cycle)
        if isinstance(call, ast.Call)
        and call.lineno > resolve_line
        and _called_name(call) == "_finish_zero_order"
    ]
    assert post_resolve_finish_zero == []
    for function_name in (
        "_run_prepared_cycle",
        "_run_ordering_cycle",
        "_cancel_own_pending_on_kill",
    ):
        assert not [
            call
            for call in ast.walk(_function(cycle_tree, function_name))
            if isinstance(call, ast.Call) and _called_name(call) == "_finish_zero_order"
        ]
    controlled_zero_functions = {
        "_finish_post_coordination_reject",
        "_finish_ordering_stopped",
    }
    for function_name in controlled_zero_functions:
        function = _function(cycle_tree, function_name)
        zero_calls = [
            call
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and _called_name(call) == "_finish_zero_order"
        ]
        seal_guards = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "seal_consumed"
        ]
        assert len(zero_calls) == 1
        assert len(seal_guards) == 1
        assert zero_calls[0].lineno > seal_guards[0].end_lineno

    top_level_functions = {
        node.name: node
        for node in cycle_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable = set()
    pending = [
        "_run_prepared_cycle",
        "_run_ordering_cycle",
        "_cancel_own_pending_on_kill",
    ]
    while pending:
        function_name = pending.pop()
        if function_name in reachable:
            continue
        reachable.add(function_name)
        function = top_level_functions[function_name]
        pending.extend(
            called
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            if (called := _called_name(call)) in top_level_functions
            and called not in {"_finish_seal_consumed_no_send", "_finish_zero_order"}
        )
    unsafe_finish_zero = [
        (function_name, call.lineno)
        for function_name in reachable - controlled_zero_functions
        for call in ast.walk(top_level_functions[function_name])
        if isinstance(call, ast.Call) and _called_name(call) == "_finish_zero_order"
    ]
    assert unsafe_finish_zero == []
    attribute_finish_probe = ast.parse("module._finish_zero_order(outcome=value)")
    assert any(
        isinstance(call, ast.Call) and _called_name(call) == "_finish_zero_order"
        for call in ast.walk(attribute_finish_probe)
    )

    prepare_lines = sorted(
        call.lineno
        for call in ast.walk(run_cycle)
        if isinstance(call, ast.Call)
        and _called_name(call) == "_prepare_coordination_owner"
    )
    lease_line = min(
        call.lineno
        for call in ast.walk(run_cycle)
        if isinstance(call, ast.Call) and _called_name(call) == "acquire"
    )
    assert prepare_lines[-1] < resolve_line
    assert lease_line < resolve_line

    print("POST_CONSUMPTION_FINISH_ZERO_ORDER_REACHABLE=0")
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
    assert events.count("owner_asserted") == 2
    assert events[-1] == "owner_asserted"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate", "expected_reason"),
    (
        ("ordering_requires_confirm", "ordering_requires_confirm"),
        ("outside_rth", "outside_krx_regular_session"),
        ("table_missing", "table_missing"),
        ("table_stale", "stale_marker_present"),
        ("confirm_gate", "confirm_gate_not_armed"),
        ("coordination_grant", "coordination_grant_unavailable"),
        ("writer_lease", "writer_lease_unavailable"),
    ),
)
async def test_class_ii_cycle_gate_preserves_seal_before_owner_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    expected_reason: str,
) -> None:
    """Every deterministic cycle gate refuses before the bounded marker write."""

    with monkeypatch.context() as scoped:
        root = tmp_path / gate
        entry, seal, _, bounded_factory = _install_isolated_runtime(
            scoped,
            root=root,
            expiry_minutes=35,
        )
        table_dir = root / "policy-table"
        now = _NOW
        confirm = True
        ordering = False
        coordination_factory = bounded_factory
        lease_factory = None

        if gate == "ordering_requires_confirm":
            confirm = False
            ordering = True
        elif gate == "outside_rth":
            now = dt.datetime(2026, 8, 27, 23, 35, tzinfo=dt.UTC)
        elif gate == "table_missing":
            pass
        elif gate == "table_stale":
            table_dir.mkdir(parents=True, exist_ok=True)
            (table_dir / "latest-kr.STALE").write_text(
                "isolated stale marker\n", encoding="utf-8"
            )
        elif gate == "confirm_gate":
            _write_table(table_dir)
        elif gate == "coordination_grant":
            _write_table(table_dir)
            _arm_cycle_runtime(scoped)
            ordering = True

            def grant_factory():  # noqa: ANN202
                return make_grant_only_kiwoom_coordination_adapter(entry)

            def available_lease_factory(*_args):  # noqa: ANN002, ANN202
                return _ProbeLease()

            coordination_factory = grant_factory
            lease_factory = available_lease_factory
        elif gate == "writer_lease":
            _write_table(table_dir)
            _arm_cycle_runtime(scoped)
            ordering = True

            class UnavailableLease(_ProbeLease):
                def acquire(self) -> None:
                    raise RuntimeError("isolated lease unavailable")

            def unavailable_lease_factory(*_args):  # noqa: ANN002, ANN202
                return UnavailableLease()

            lease_factory = unavailable_lease_factory
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(gate)

        outcome = await kiwoom_cycle.run_kiwoom_cycle(
            now=now,
            table_dir=table_dir,
            out_dir=root / "observations",
            confirm=confirm,
            ordering=ordering,
            account=FakeAccount(),
            lease_factory=lease_factory,
            coordination_factory=coordination_factory,
            coordination_entry=entry,
        )

        marker = bounded_send.bounded_send_consumption_marker_path(seal["seal_digest"])
        assert outcome.zero_order_reason == expected_reason
        assert not marker.exists()
        assert seal["seal_digest"] not in bounded_send._PROCESS_CONSUMED_SEAL_DIGESTS
        print(f"PRECONSUMPTION_GATE={gate} SEAL_MARKER_CREATED=False")


@pytest.mark.asyncio
async def test_eligible_cycle_keeps_record_fields_and_consumes_only_after_all_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive lane: gate pass, consume, then continue with the audit schema intact."""

    entry, seal, _, factory = _install_isolated_runtime(
        monkeypatch,
        root=tmp_path,
        expiry_minutes=35,
    )
    table_dir = tmp_path / "policy-table"
    _write_table(table_dir, buy_l1=None)
    _arm_cycle_runtime(monkeypatch)

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=_NOW,
        table_dir=table_dir,
        out_dir=tmp_path / "observations",
        confirm=True,
        account=FakeAccount(),
        coordination_factory=factory,
        coordination_entry=entry,
    )

    marker = bounded_send.bounded_send_consumption_marker_path(seal["seal_digest"])
    assert marker.is_file()
    assert outcome.record.get("seal_consumed_no_send") is None
    assert outcome.record["coordination"]["authorizes_send"] is True
    audit_order = [
        "confirm",
        "ordering",
        "execution_mode",
        "realized_pnl_source",
        "coordination",
        "krx_regular_session",
        "session_policy",
        "policy_table_hash",
        "policy_table_path",
        "policy_table_generated_at",
        "policy_table_age_seconds",
    ]
    positions = [list(outcome.record).index(field) for field in audit_order]
    assert positions == sorted(positions)
    assert all(field in outcome.record for field in audit_order)


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
        table_dir = root / "policy-table"
        _write_table(table_dir)
        _arm_cycle_runtime(scoped)
        original_write = bounded_send._write_consumption_marker
        expiry = dt.datetime.fromisoformat(seal["expires_at"].replace("Z", "+00:00"))
        account: FakeAccount = FakeAccount()
        ordering = False
        journal: kiwoom_attr.OwnOrderJournal | None = None
        lease_factory = None

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
        elif mode == "account_truth_unavailable":

            class AccountTruthUnavailable(FakeAccount):
                async def read_cash(self) -> Decimal:
                    raise RuntimeError("isolated account truth failure")

            account = AccountTruthUnavailable()
        elif mode == "preflight_not_clean":
            order_date = kiwoom_attr.kst_order_date(_NOW)
            account = FakeAccount(
                order_detail={
                    order_date: [
                        {
                            "order_id": "foreign-acceptance-order",
                            "symbol": "005930",
                            "status": "open",
                        }
                    ]
                }
            )
        elif mode in {
            "writer_lease_lost",
            "own_order_journal_unreadable",
            "mutation_boundary_read_unavailable",
            "foreign_same_day_orders_present",
            "ordering_preflight_not_clean",
        }:
            ordering = True

            def probe_lease_factory(*_args):  # noqa: ANN002, ANN202
                return _ProbeLease(lose_on_assert=mode == "writer_lease_lost")

            lease_factory = probe_lease_factory
            if mode == "own_order_journal_unreadable":
                journal_path = root / "corrupt-own-orders.jsonl"
                journal_path.write_text("{not-json\n", encoding="utf-8")
                journal = kiwoom_attr.OwnOrderJournal(path=journal_path)
            elif mode == "mutation_boundary_read_unavailable":
                account = FakeAccount(detail_error=RuntimeError("isolated kt00007"))
            elif mode == "foreign_same_day_orders_present":
                order_date = kiwoom_attr.kst_order_date(_NOW)
                account = FakeAccount(
                    order_detail={
                        order_date: [
                            {
                                "order_id": "foreign-ordering-order",
                                "symbol": "005930",
                                "status": "open",
                            }
                        ]
                    }
                )
            elif mode == "ordering_preflight_not_clean":
                account = FakeAccount(cash=Decimal("0"))
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
            table_dir=table_dir,
            out_dir=out_dir,
            confirm=True,
            ordering=ordering,
            account=account,
            journal=journal,
            lease_factory=lease_factory,
            coordination_factory=factory,
            coordination_entry=entry,
            realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
                value=Decimal("0"), source="isolated_probe"
            ),
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
        "account_truth_unavailable",
        "preflight_not_clean",
        "writer_lease_lost",
        "own_order_journal_unreadable",
        "mutation_boundary_read_unavailable",
        "foreign_same_day_orders_present",
        "ordering_preflight_not_clean",
    )
    observed = {
        await _run_post_consumption_probe(
            mode=mode,
            root=tmp_path / mode,
            monkeypatch=monkeypatch,
            expiry_minutes=35,
        )
        for mode in modes
    }

    print("POST_CONSUMPTION_REJECT_REASONS_DYNAMIC=" + ",".join(sorted(observed)))
    assert observed == set(bounded_send.POST_CONSUMPTION_REJECT_REASONS)
    assert observed <= bounded_send.POST_CONSUMPTION_REJECT_REASONS


def test_unclassified_post_consumption_failure_is_not_allowlisted() -> None:
    reason = kiwoom_coordination._closed_post_consumption_reason(
        RuntimeError("new unclassified post-consumption refusal")
    )

    assert reason == bounded_send.UNCLASSIFIED_POST_CONSUMPTION
    assert reason not in bounded_send.POST_CONSUMPTION_REJECT_REASONS
    with pytest.raises(AssertionError):
        kiwoom_coordination.KiwoomPostConsumptionOwnerRejected(
            reason,
            lane_id=kiwoom_coordination.KIWOOM_KR_LANE_ID,
        )


@pytest.mark.asyncio
async def test_consumed_no_send_record_survives_notifier_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MINOR-1: the immediate durable row predates and survives notifier failure."""

    entry, seal, _, factory = _install_isolated_runtime(
        monkeypatch,
        root=tmp_path,
        expiry_minutes=35,
    )
    table_dir = tmp_path / "policy-table"
    out_dir = tmp_path / "observations"
    _write_table(table_dir)
    _arm_cycle_runtime(monkeypatch)

    class AccountTruthUnavailable(FakeAccount):
        async def read_cash(self) -> Decimal:
            raise RuntimeError("isolated account truth failure")

    async def notifier_fails(_incident: dict[str, object]) -> None:
        raise RuntimeError("isolated notifier failure")

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        account=AccountTruthUnavailable(),
        coordination_factory=factory,
        coordination_entry=entry,
        authority_risk_notifier=notifier_fails,
    )

    reason = bounded_send.PRESEND_RECHECK_ACCOUNT_TRUTH_UNAVAILABLE
    cycle_path = out_dir / kiwoom_cycle.LANE / "cycles.jsonl"
    rows = [
        json.loads(line) for line in cycle_path.read_text(encoding="utf-8").splitlines()
    ]
    assert bounded_send.bounded_send_consumption_marker_path(
        seal["seal_digest"]
    ).exists()
    assert any(
        row.get("cycle_record_phase") == "seal_consumed_no_send_immediate"
        and row.get("seal_consumed_no_send", {}).get("reason") == reason
        for row in rows
    )
    incident = outcome.record["seal_consumed_no_send"]
    assert outcome.exit_code == 2
    assert outcome.zero_order_reason == kiwoom_coordination.SEAL_CONSUMED_NO_SEND
    assert incident["reason"] == reason
    assert incident["operator_notification"] == "failed"
    assert incident["notification_error_type"] == "RuntimeError"


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


def test_registered_validator_rejects_unconsumed_seal_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1343: a registered validator still requires its exact marker."""

    entry, seal, _, _factory = _install_isolated_runtime(
        monkeypatch,
        root=tmp_path,
        expiry_minutes=35,
    )
    snapshotted = bounded_send.snapshot_bounded_send_seal(seal)
    authority = core_coordination._issue_canary_scope_authority(
        lane_id=snapshotted.lane_id,
        physical_account_id=snapshotted.physical_account_id,
        seal_digest=snapshotted.seal_digest,
        seal_binding_validator=lambda lane_id, physical_account_id, seal_digest: (
            bounded_send.assert_consumed_bounded_send_seal_binding(
                snapshotted,
                lane_id,
                physical_account_id,
                seal_digest,
            )
        ),
    )

    with pytest.raises(LaneGuardError) as captured:
        core_coordination.assert_canary_scope_authority_binding(entry, authority)

    assert captured.value.code == "canary_scope_authority_seal_unbound"
    assert not bounded_send.bounded_send_consumption_marker_path(
        snapshotted.seal_digest
    ).exists()
