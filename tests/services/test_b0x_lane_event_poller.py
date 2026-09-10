"""Offline production-entry tests for the B0X handoffkeep HTTP ingress."""

from __future__ import annotations

import ast
import grp
import hashlib
import json
import os
import pwd
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services.b0x_lane_consumer import (
    event_rows,
    record_terminal_disposition,
)
from app.services.b0x_lane_event_poller import (
    MAX_RESPONSE_BYTES,
    MAX_TEXT_BYTES,
    PAGE_LIMIT,
    B0XIngressError,
    ingress_readback,
    load_binding,
    stable_host_lock,
)
from scripts import b0x_lane_event_poller as cli

pytestmark = pytest.mark.unit
KST = ZoneInfo("Asia/Seoul")
HEAD = "a" * 40
ENV = {
    "FIXTURE_HANDOFFKEEP_KEY_ONE": "synthetic-sentinel-alpha",
    "FIXTURE_HANDOFFKEEP_KEY_TWO": "synthetic-sentinel-beta",
}
ROOT = Path(__file__).resolve().parents[2]


def _event_text(
    event_id: str,
    *,
    disposition: str = "cycle_kickoff",
    playbook: str | None = None,
) -> str:
    identity = event_id.removeprefix("kickoff-")
    if "-T" in identity:
        base, tick = identity.rsplit("-T", 1)
    else:
        base, tick = identity, None
    parts = base.rsplit("-", 3)
    slot = parts[0]
    date = "-".join(parts[1:])
    if playbook is None:
        playbook = {
            "b0x-nudge-kr": "docs/runbooks/b0x-kr-cycle.md",
            "b0x-nudge-us": "docs/runbooks/b0x-us-cycle.md",
            "b0x-nudge-crypto": "docs/runbooks/b0x-crypto-cycle.md",
            "b0x-table-kr": "docs/runbooks/b0x-policy-table-build.md",
            "b0x-table-us": "docs/runbooks/b0x-policy-table-build.md",
            "b0x-harvest": "docs/runbooks/b0x-harvest.md",
        }[slot]
    return json.dumps(
        {
            "date": date,
            "disposition": disposition,
            "playbook": playbook,
            "source": "b0x",
            "slot": slot,
            "tick": tick,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _row(
    delivery_id: int,
    event_id: str = "kickoff-b0x-nudge-kr-2026-09-10",
    *,
    received_at: str = "2026-09-10T09:05:10+09:00",
    delivered_at: str | None = None,
    delivered_to: str | None = None,
    text: str | None = None,
) -> dict[str, object]:
    return {
        "id": delivery_id,
        "kind": "lane.event",
        "owner_lane": "fixture-ingress-lane",
        "event_id": event_id,
        "text": text if text is not None else _event_text(event_id),
        "epoch": 7,
        "event_time": received_at,
        "received_at": received_at,
        "delivered_at": delivered_at,
        "delivered_to": delivered_to,
    }


def _binding_payload(
    tmp_path: Path,
    *,
    active: bool = True,
    max_pages: int = 8,
    backlog_count: int = 3,
) -> dict[str, object]:
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    rendered = (ROOT / "config/b0x_ingress_binding.json.in").read_text(encoding="utf-8")
    replacements = {
        "@B0X_INGRESS_LANE@": "fixture-ingress-lane",
        "@HANDOFFKEEP_HTTP_BASE_URL@": "https://handoffkeep-fixture.invalid",
        "@HANDOFFKEEP_CREDENTIAL_SOURCE@": "fixture-existing-credential-source",
        "@HANDOFFKEEP_CREDENTIAL_HEADER_ONE@": "X-Fixture-Key-One",
        "@HANDOFFKEEP_CREDENTIAL_ENV_KEY_ONE@": "FIXTURE_HANDOFFKEEP_KEY_ONE",
        "@HANDOFFKEEP_CREDENTIAL_HEADER_TWO@": "X-Fixture-Key-Two",
        "@HANDOFFKEEP_CREDENTIAL_ENV_KEY_TWO@": "FIXTURE_HANDOFFKEEP_KEY_TWO",
        "@B0X_INGRESS_STATE_DB@": str(tmp_path / "state" / "ingress.sqlite3"),
        "@B0X_INGRESS_LOCK_PATH@": str(tmp_path / "state" / "ingress.lock"),
        "@B0X_INGRESS_OS_OWNER@": owner,
        "@B0X_INGRESS_OS_GROUP@": group,
        "@B0X_INGRESS_BINDING_MODE@": "0600",
        "@B0X_INGRESS_STATE_MODE@": "0600",
        "@B0X_INGRESS_LOCK_MODE@": "0600",
        "@B0X_INGRESS_OWNER_MACHINE@": "fixture-owner-machine",
        "@B0X_INGRESS_SINK_RECORD@": "fixture-sink-record",
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    assert "@" not in rendered
    payload: dict[str, object] = json.loads(rendered)
    payload["status"] = "INSTALLED" if active else "DRAFT_NOT_INSTALLED"
    runtime = payload["runtime"]
    gates = payload["gates"]
    activation = payload["activation"]
    route = payload["route_readback"]
    assert isinstance(runtime, dict)
    assert isinstance(gates, dict)
    assert isinstance(activation, dict)
    assert isinstance(route, dict)
    runtime["max_pages_per_run"] = max_pages
    gates.update(
        ingress_enabled=active,
        dispatch_enabled=active,
        source_enabled=active,
    )
    activation.update(
        start_after_id=3 if active else None,
        activation_at="2026-09-10T08:00:00+09:00" if active else None,
        source_binding_epoch=7 if active else None,
        code_head=HEAD if active else None,
        binding_install_receipt="sha256:" + "b" * 64 if active else None,
        source_quiescence_receipt="sha256:" + "d" * 64 if active else None,
        backlog_count=backlog_count if active else None,
        backlog_min_id=1 if active and backlog_count else None,
        backlog_max_id=3 if active and backlog_count else None,
    )
    route.update(
        verified=active,
        unique_owner=active,
        receipt="sha256:" + "c" * 64 if active else None,
    )
    return payload


def _write_binding(tmp_path: Path, payload: dict[str, object]) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    binding.chmod(0o600)
    state_db = Path(str(payload["runtime"]["state_db"]))  # type: ignore[index]
    return binding, state_db


def _transport(
    responder: object,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(responder, BaseException):
            raise responder
        if callable(responder):
            rows = responder(request)
        else:
            rows = responder
        return httpx.Response(200, json={"events": rows})

    return httpx.MockTransport(handler), requests


def _run(
    capsys: pytest.CaptureFixture[str],
    binding: Path,
    state_db: Path,
    transport: httpx.BaseTransport,
    *,
    now: datetime,
    **kwargs: object,
) -> tuple[int, dict[str, object]]:
    result = cli.main(
        [
            "--binding",
            str(binding),
            "--state-db",
            str(state_db),
            "--once",
        ],
        http_transport=transport,
        processing_at=now,
        environ=ENV,
        runtime_code_head=HEAD,
        **kwargs,
    )
    output = json.loads(capsys.readouterr().out)
    return result, output


def test_real_cli_get_path_consumes_two_rows_once_and_readback_is_durable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _binding_payload(tmp_path)
    binding, state_db = _write_binding(tmp_path, payload)
    kr = _row(4)
    first_transport, first_requests = _transport([kr])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        first_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert first["business_consumed_count"] == 1
    assert first["cycle_created_count"] == 1
    assert first["dispatch_queued_count"] == 1
    assert first["dispatch_started_count"] == 0
    assert first["terminal_evidence_count"] == 0
    raw_text = str(kr["text"]).encode("utf-8")
    assert first["rows"][0]["text_bytes"] == len(raw_text)
    assert first["rows"][0]["text_sha256"] == hashlib.sha256(raw_text).hexdigest()
    assert first["backlog_report"] == {
        "count": 3,
        "min_id": 1,
        "max_id": 3,
        "start_after_id": 3,
    }

    record_terminal_disposition(
        state_db=state_db,
        lane="fixture-ingress-lane",
        event_id=str(kr["event_id"]),
        evidence="fixture-terminal-evidence",
        failed=False,
    )
    delivered = deepcopy(kr)
    delivered["delivered_at"] = "2026-09-10T09:05:40+09:00"
    crypto_id = "kickoff-b0x-nudge-crypto-2026-09-10-T1300"
    crypto = _row(
        5,
        crypto_id,
        received_at="2026-09-10T13:00:05+09:00",
        delivered_to="fixture-sink-record",
    )
    second_transport, second_requests = _transport([delivered, crypto])
    rc, second = _run(
        capsys,
        binding,
        state_db,
        second_transport,
        now=datetime(2026, 9, 10, 13, 0, 30, tzinfo=KST),
    )
    assert rc == 0
    assert second["business_consumed_count"] == 1
    assert second["cycle_created_count"] == 1
    assert second["rows"][0]["business_disposition"] == "duplicate"
    assert second["rows"][0]["duplicate"] is True
    assert second["rows"][0]["cycle_created"] is False
    assert second["rows"][1]["business_disposition"] == "queued_cycle"
    assert len(event_rows(state_db)) == 2

    assert [request.method for request in first_requests + second_requests] == [
        "GET",
        "GET",
    ]
    for request in first_requests + second_requests:
        params = parse_qs(request.url.query.decode())
        assert request.url.path == "/v1/relay/events"
        assert params["lane"] == ["fixture-ingress-lane"]
        assert params["kind"] == ["lane.event"]
        assert params["undelivered"] == ["false"]
        assert params["limit"] == ["200"]

    readback_rc = cli.main(
        [
            "--binding",
            str(binding),
            "--state-db",
            str(state_db),
            "--readback",
            "--lane",
            "fixture-ingress-lane",
            "--event-id",
            crypto_id,
        ]
    )
    readback = json.loads(capsys.readouterr().out)
    assert readback_rc == 0
    assert readback["ingress_observed"] is True
    assert readback["business_disposition"] == "queued_cycle"
    assert readback["cycle_created"] is True
    assert readback["dispatch_queued"] is True
    assert readback["dispatch_started"] is False
    assert readback["terminal_evidence_present"] is False
    assert readback["ingress"][0]["source_binding_epoch"] == 7
    assert readback["ingress"][0]["truncated_present"] is False
    assert readback["ingress"][0]["truncated_value"] is None
    assert len(readback["ingress"][0]["row_sha256"]) == 64
    assert readback["ingress"][0]["processing_at"].endswith("+00:00")

    terminal_rc = cli.main(
        [
            "--binding",
            str(binding),
            "--state-db",
            str(state_db),
            "--readback",
            "--lane",
            "fixture-ingress-lane",
            "--event-id",
            str(kr["event_id"]),
        ]
    )
    terminal = json.loads(capsys.readouterr().out)
    assert terminal_rc == 0
    assert terminal["ingress_observation_count"] == 2
    assert terminal["dispatch_started"] is False
    assert terminal["terminal_evidence_present"] is True
    assert (
        terminal["terminal_evidence_sha256"]
        == hashlib.sha256(b"fixture-terminal-evidence").hexdigest()
    )


def test_transport_is_get_only_and_secrets_never_reach_output_or_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    transport, requests = _transport([])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["poll_http_success"] is True
    assert [request.method for request in requests] == ["GET"]
    serialized = json.dumps(output, sort_keys=True)
    assert all(secret not in serialized for secret in ENV.values())
    source = (ROOT / "app/services/b0x_lane_event_poller.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "client.post(",
        "client.patch(",
        "client.delete(",
        '"/delivered"',
        "psycopg",
        "asyncpg",
        "DATABASE_URL",
        "subprocess",
    ):
        assert forbidden not in source
    assert "_consume_lane_event_in_connection(" in source
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported_roots.isdisjoint(
        {"broker", "orders", "proposal", "strategy", "watch"}
    )
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint({"compile", "eval", "exec"})


def test_duplicate_rejects_immutable_hub_metadata_tampering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    first_transport, _ = _transport([_row(4)])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        first_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0 and first["cycle_created_count"] == 1

    tampered = _row(4, received_at="2026-09-10T09:05:11+09:00")
    tampered_transport, _ = _transport([tampered])
    rc, second = _run(
        capsys,
        binding,
        state_db,
        tampered_transport,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert second["rows"][0]["business_disposition"] == ("rejected_tampered_duplicate")
    assert second["cycle_created_count"] == 0
    assert len(event_rows(state_db)) == 1


def test_response_row_text_and_closed_envelope_bounds_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    extra_row_field = _row(4)
    extra_row_field["unknown"] = "forbidden"
    oversize_text = _row(5, text="x" * (MAX_TEXT_BYTES + 1))
    unknown_event_id = "kickoff-b0x-nudge-us-2026-09-10"
    unknown_body = json.loads(str(_row(6, unknown_event_id)["text"]))
    unknown_body["unknown"] = "forbidden"
    unknown_body_row = _row(6, unknown_event_id, text=json.dumps(unknown_body))
    transport, _ = _transport([extra_row_field, oversize_text, unknown_body_row])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert [row["business_disposition"] for row in output["rows"]] == [
        "rejected_row_keys_invalid",
        "rejected_text_invalid_or_oversize",
        "rejected_event_contract_invalid",
    ]
    assert output["cycle_created_count"] == 0
    assert event_rows(state_db) == []

    response_root = tmp_path / "response"
    response_root.mkdir()
    response_binding, response_db = _write_binding(
        response_root, _binding_payload(response_root)
    )

    def oversize_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    response_transport = httpx.MockTransport(oversize_response)
    rc, response_output = _run(
        capsys,
        response_binding,
        response_db,
        response_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 1
    assert response_output["reason"] == "http_response_oversize"
    assert event_rows(response_db) == []


def test_floor_sweep_finds_late_lower_delivery_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    event = _row(5)
    transport, requests = _transport([event])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 20, tzinfo=KST),
    )
    assert rc == 0 and first["sweep_complete"] is True

    calls = 0

    def later(_request: httpx.Request) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [_row(4), event] if calls == 1 else []

    later_transport, later_requests = _transport(later)
    rc, second = _run(
        capsys,
        binding,
        state_db,
        later_transport,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert [row["delivery_id"] for row in second["rows"]] == [4, 5]
    assert all(row["business_disposition"] == "duplicate" for row in second["rows"])
    assert parse_qs(later_requests[0].url.query.decode())["after_id"] == ["3"]
    evidence = ingress_readback(
        load_binding(binding, state_db=state_db),
        lane="fixture-ingress-lane",
        event_id=str(event["event_id"]),
    )
    assert {row["delivery_id"] for row in evidence["ingress"]} == {4, 5}
    assert len(requests) == 1


def test_page_boundary_and_budget_continue_from_committed_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _binding_payload(tmp_path, max_pages=3)
    binding, state_db = _write_binding(tmp_path, payload)
    page = [_row(delivery_id) for delivery_id in range(4, 4 + PAGE_LIMIT)]
    first_transport, first_requests = _transport(page)
    ticks = iter((0.0, 100.0))
    rc, first = _run(
        capsys,
        binding,
        state_db,
        first_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
        monotonic=lambda: next(ticks),
    )
    assert rc == 0
    assert first["status"] == "partial"
    assert first["sweep_cursor"] == 203
    assert first["business_consumed_count"] == 1

    tail_transport, tail_requests = _transport([_row(204)])
    rc, tail = _run(
        capsys,
        binding,
        state_db,
        tail_transport,
        now=datetime(2026, 9, 10, 9, 5, 45, tzinfo=KST),
    )
    assert rc == 0
    assert tail["resumed_partial_sweep"] is True
    assert tail["sweep_complete"] is True
    assert tail["rows"][0]["business_disposition"] == "duplicate"
    assert parse_qs(first_requests[0].url.query.decode())["after_id"] == ["3"]
    assert parse_qs(tail_requests[0].url.query.decode())["after_id"] == ["203"]


def test_poison_row_is_rejected_and_later_row_is_consumed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    poison = _row(4, "bad-event-id", text="{")
    good = _row(5)
    transport, _ = _transport([poison, good])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["rows"][0]["business_disposition"] == (
        "rejected_event_contract_invalid"
    )
    assert output["rows"][0]["cycle_created"] is False
    assert output["rows"][1]["business_disposition"] == "queued_cycle"
    assert output["cycle_created_count"] == 1


def test_http_failure_is_redacted_and_retry_does_not_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    failed_transport, _ = _transport(
        httpx.ConnectError("synthetic-sentinel-alpha must never escape")
    )
    rc, failed = _run(
        capsys,
        binding,
        state_db,
        failed_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 1
    assert failed["reason"] == "http_get_failed"
    assert "synthetic-sentinel" not in json.dumps(failed)
    assert event_rows(state_db) == []

    retry_transport, retry_requests = _transport([_row(4)])
    rc, retry = _run(
        capsys,
        binding,
        state_db,
        retry_transport,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert retry["cycle_created_count"] == 1
    assert parse_qs(retry_requests[0].url.query.decode())["after_id"] == ["3"]


@pytest.mark.parametrize("phase", ("before_transaction", "before_commit"))
def test_crash_before_commit_rolls_back_business_receipt_and_cursor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    phase: str,
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    transport, _ = _transport([_row(4)])

    def crash(candidate: str, _delivery_id: int | None) -> None:
        if candidate == phase:
            raise RuntimeError("synthetic crash")

    rc, failed = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
        fault=crash,
    )
    assert rc == 1
    assert failed["reason"] == "internal_error"
    assert event_rows(state_db) == []

    retry_transport, retry_requests = _transport([_row(4)])
    rc, retry = _run(
        capsys,
        binding,
        state_db,
        retry_transport,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert retry["cycle_created_count"] == 1
    assert parse_qs(retry_requests[0].url.query.decode())["after_id"] == ["3"]


def test_crash_after_commit_is_durable_and_restart_adds_no_business_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))

    def crash(phase: str, _delivery_id: int | None) -> None:
        if phase == "after_commit":
            raise RuntimeError("synthetic crash")

    transport, _ = _transport([_row(4)])
    rc, failed = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
        fault=crash,
    )
    assert rc == 1
    assert failed["reason"] == "internal_error"
    assert len(event_rows(state_db)) == 1

    empty_transport, empty_requests = _transport([])
    rc, resumed = _run(
        capsys,
        binding,
        state_db,
        empty_transport,
        now=datetime(2026, 9, 10, 9, 5, 35, tzinfo=KST),
    )
    assert rc == 0
    assert resumed["resumed_partial_sweep"] is True
    assert parse_qs(empty_requests[0].url.query.decode())["after_id"] == ["4"]

    repeat_transport, _ = _transport([_row(4)])
    rc, repeat = _run(
        capsys,
        binding,
        state_db,
        repeat_transport,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert repeat["rows"][0]["business_disposition"] == "duplicate"
    assert repeat["cycle_created_count"] == 0
    assert len(event_rows(state_db)) == 1


def test_concurrent_entry_uses_one_stable_host_local_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding_path, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    binding = load_binding(binding_path, state_db=state_db)
    transport, requests = _transport([])
    with stable_host_lock(binding):
        rc, output = _run(
            capsys,
            binding_path,
            state_db,
            transport,
            now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
        )
    assert rc == 2
    assert output["reason"] == "host_local_lock_busy"
    assert requests == []
    assert binding.runtime.lock_path.is_file()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(status="DRAFT_NOT_INSTALLED"),
        lambda value: value["gates"].update(ingress_enabled=False),
        lambda value: value["gates"].update(dispatch_enabled=False),
        lambda value: value["gates"].update(source_enabled=False),
        lambda value: value["activation"].update(start_after_id=None),
        lambda value: value["activation"].update(activation_at=None),
        lambda value: value["activation"].update(source_binding_epoch=None),
        lambda value: value["activation"].update(code_head=None),
        lambda value: value["activation"].update(code_head="b" * 40),
        lambda value: value["activation"].update(binding_install_receipt=None),
        lambda value: value["activation"].update(source_quiescence_receipt=None),
        lambda value: value["activation"].update(backlog_count=None),
        lambda value: value["activation"].update(backlog_min_id=None),
        lambda value: value["activation"].update(backlog_max_id=None),
        lambda value: value["route_readback"].update(verified=False),
        lambda value: value["route_readback"].update(lane="fixture-other-lane"),
        lambda value: value["route_readback"].update(sink=False),
        lambda value: value["route_readback"].update(unique_owner=False),
        lambda value: value["route_readback"].update(receipt=None),
    ),
)
def test_draft_missing_activation_gate_or_route_readback_consumes_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: object,
) -> None:
    payload = _binding_payload(tmp_path)
    mutate(payload)
    binding, state_db = _write_binding(tmp_path, payload)
    transport, requests = _transport([_row(4)])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["status"] == "blocked"
    assert output["business_consumed_count"] == 0
    assert output["ingress_durably_recorded_count"] == 0
    assert requests == []
    assert not state_db.exists()


def test_false_gate_blocks_before_code_head_process_http_or_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _binding_payload(tmp_path)
    gates = payload["gates"]
    assert isinstance(gates, dict)
    gates["ingress_enabled"] = False
    binding, state_db = _write_binding(tmp_path, payload)
    transport, requests = _transport([_row(4)])

    def forbidden_runner(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("code-head subprocess must not run behind a false gate")

    rc = cli.main(
        [
            "--binding",
            str(binding),
            "--state-db",
            str(state_db),
            "--once",
        ],
        http_transport=transport,
        processing_at=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
        environ=ENV,
        code_head_runner=forbidden_runner,
    )
    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["status"] == "blocked"
    assert "ingress_enabled_false" in output["reasons"]
    assert requests == []
    assert not state_db.exists()


def test_pre_activation_backlog_is_reported_once_without_consumption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    empty, _ = _transport([])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        empty,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert first["backlog_report"] == {
        "count": 3,
        "min_id": 1,
        "max_id": 3,
        "start_after_id": 3,
    }
    assert first["business_consumed_count"] == 0

    again, _ = _transport([])
    rc, second = _run(
        capsys,
        binding,
        state_db,
        again,
        now=datetime(2026, 9, 10, 9, 5, 40, tzinfo=KST),
    )
    assert rc == 0
    assert second["backlog_report"] is None
    assert event_rows(state_db) == []


def test_hub_arrival_on_time_but_poll_late_is_held_without_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    transport, _ = _transport([_row(4)])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 6, 0, tzinfo=KST),
    )
    assert rc == 0
    assert output["rows"][0]["business_disposition"] == (
        "preserved_unconsumed_out_of_window"
    )
    assert output["rows"][0]["cycle_created"] is False
    assert output["cycle_created_count"] == 0
    assert output["dispatch_queued_count"] == 0


def test_prior_kst_date_and_prior_tick_are_held_and_boundary_duplicate_stays_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _binding_payload(tmp_path)
    activation = payload["activation"]
    assert isinstance(activation, dict)
    activation["activation_at"] = "2026-09-08T08:00:00+09:00"
    binding, state_db = _write_binding(tmp_path, payload)
    prior_date = _row(
        4,
        "kickoff-b0x-nudge-kr-2026-09-09",
        received_at="2026-09-09T09:05:10+09:00",
    )
    prior_tick = _row(
        5,
        "kickoff-b0x-nudge-crypto-2026-09-10-T0900",
        received_at="2026-09-10T09:00:10+09:00",
    )
    transport, _ = _transport([prior_date, prior_tick])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 13, 0, 30, tzinfo=KST),
    )
    assert rc == 0
    assert [row["business_disposition"] for row in output["rows"]] == [
        "preserved_unconsumed_out_of_window",
        "preserved_unconsumed_out_of_window",
    ]
    assert output["cycle_created_count"] == 0

    duplicate_transport, _ = _transport([prior_date])
    rc, duplicate = _run(
        capsys,
        binding,
        state_db,
        duplicate_transport,
        now=datetime(2026, 9, 11, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert duplicate["rows"][0]["business_disposition"] == "duplicate"
    assert duplicate["rows"][0]["cycle_created"] is False
    assert len(event_rows(state_db)) == 2


def test_active_slot_hold_is_never_promoted_after_terminal_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    first_event_id = "kickoff-b0x-nudge-kr-2026-09-10"
    first_transport, _ = _transport([_row(4, first_event_id)])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        first_transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0 and first["cycle_created_count"] == 1

    held_event_id = "kickoff-b0x-nudge-us-2026-09-10"
    held_row = _row(
        5,
        held_event_id,
        received_at="2026-09-10T22:35:10+09:00",
    )
    held_transport, _ = _transport([held_row])
    rc, held = _run(
        capsys,
        binding,
        state_db,
        held_transport,
        now=datetime(2026, 9, 10, 22, 35, 30, tzinfo=KST),
    )
    assert rc == 0
    assert held["rows"][0]["business_disposition"] == ("held_unconsumed_active_slot")
    assert held["cycle_created_count"] == 0

    record_terminal_disposition(
        state_db=state_db,
        lane="fixture-ingress-lane",
        event_id=first_event_id,
        evidence="fixture-terminal-release",
        failed=False,
    )
    duplicate_transport, _ = _transport([held_row])
    rc, duplicate = _run(
        capsys,
        binding,
        state_db,
        duplicate_transport,
        now=datetime(2026, 9, 11, 0, 0, 1, tzinfo=KST),
    )
    assert rc == 0
    assert duplicate["rows"][0]["business_disposition"] == "duplicate"
    assert duplicate["rows"][0]["cycle_created"] is False
    assert duplicate["cycle_created_count"] == 0
    rows = {row["event_id"]: row for row in event_rows(state_db)}
    assert rows[held_event_id]["disposition"] == "held_unconsumed_active_slot"
    assert rows[held_event_id]["cycle_created"] is False


@pytest.mark.parametrize(
    ("delivered_to", "expected", "esc"),
    (
        (None, "queued_cycle", False),
        ("fixture-sink-record", "queued_cycle", False),
        ("fixture-other-sink", "rejected_route_mismatch", True),
    ),
)
def test_delivered_null_and_sink_are_valid_but_route_mismatch_is_held_esc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    delivered_to: str | None,
    expected: str,
    esc: bool,
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    transport, _ = _transport([_row(4, delivered_to=delivered_to)])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["rows"][0]["business_disposition"] == expected
    assert output["rows"][0]["esc"] is esc
    assert output["rows"][0]["cycle_created"] is (not esc)


def test_truncated_absence_is_distinct_from_false_and_true_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    absent = _row(4)
    explicit_false = _row(5)
    explicit_false["truncated"] = False
    explicit_true = _row(6)
    explicit_true["truncated"] = True
    transport, _ = _transport([absent, explicit_false, explicit_true])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["rows"][2]["business_disposition"] == "rejected_truncated_true"
    connection = sqlite3.connect(state_db)
    try:
        values = connection.execute(
            "SELECT delivery_id, truncated_present, truncated_value "
            "FROM b0x_ingress_receipt ORDER BY delivery_id"
        ).fetchall()
    finally:
        connection.close()
    assert values == [(4, 0, None), (5, 1, 0), (6, 1, 1)]


def test_future_tampered_and_other_lane_rows_are_durable_nonexecuting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    first_transport, _ = _transport([_row(4)])
    rc, first = _run(
        capsys,
        binding,
        state_db,
        first_transport,
        now=datetime(2026, 9, 10, 9, 5, 20, tzinfo=KST),
    )
    assert rc == 0 and first["cycle_created_count"] == 1

    changed = _row(4)
    changed["text"] = str(changed["text"]) + " "
    future = _row(5, received_at="2026-09-10T09:06:00+09:00")
    other_lane = _row(6)
    other_lane["owner_lane"] = "fixture-other-lane"
    transport, _ = _transport([changed, future, other_lane])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert [row["business_disposition"] for row in output["rows"]] == [
        "rejected_tampered_duplicate",
        "rejected_future_timestamp",
        "rejected_owner_lane_mismatch",
    ]
    assert all(not row["cycle_created"] for row in output["rows"])
    assert len(event_rows(state_db)) == 1


def test_row_epoch_must_match_the_reviewed_source_binding_epoch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    wrong_epoch = _row(4)
    wrong_epoch["epoch"] = 8
    transport, _ = _transport([wrong_epoch])
    rc, output = _run(
        capsys,
        binding,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["rows"][0]["business_disposition"] == (
        "rejected_source_binding_epoch_mismatch"
    )
    assert output["cycle_created_count"] == 0
    assert event_rows(state_db) == []


def test_public_config_unit_timer_are_real_default_off_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = ROOT / "config/b0x_ingress_binding.json.in"
    service = (ROOT / "ops/ncp/systemd/job-b0x-lane-event-poller.service.in").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "ops/ncp/systemd/job-b0x-lane-event-poller.timer").read_text(
        encoding="utf-8"
    )
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["status"] == "DRAFT_NOT_INSTALLED"
    assert payload["gates"] == {
        "ingress_enabled": False,
        "dispatch_enabled": False,
        "source_enabled": False,
    }
    assert payload["activation"]["start_after_id"] is None
    assert payload["activation"]["code_head"] is None
    assert payload["route_readback"]["verified"] is False
    assert "scripts.b0x_lane_event_poller" in service
    assert "--binding @B0X_INGRESS_BINDING@" in service
    assert "--state-db @B0X_INGRESS_STATE_DB@" in service
    assert "--once" in service
    assert "Type=oneshot" in service
    assert "B0X_INGRESS_UNIT_TEMPLATE_RENDERED" not in service
    assert "TimeoutStartSec=@B0X_INGRESS_SERVICE_TIMEOUT_SECONDS@" in service
    assert "EnvironmentFile=@B0X_INGRESS_CREDENTIAL_ENV_FILE@" in service
    assert "OnBootSec=5s" in timer
    assert "OnUnitInactiveSec=5s" in timer
    assert "Persistent=false" in timer
    assert "AccuracySec=1s" in timer
    assert "Unit=job-b0x-lane-event-poller.service" in timer

    rc = cli.main(
        [
            "--binding",
            str(config),
            "--state-db",
            str(tmp_path / "state.sqlite3"),
            "--once",
        ],
        runtime_code_head=HEAD,
    )
    output = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert output["reason"] == "binding_retains_private_placeholder"
    public_added = config.read_text(encoding="utf-8") + service + timer
    for unresolved_private_input in (
        "@B0X_INGRESS_LANE@",
        "@HANDOFFKEEP_HTTP_BASE_URL@",
        "@HANDOFFKEEP_CREDENTIAL_SOURCE@",
        "@B0X_INGRESS_STATE_DB@",
        "@B0X_INGRESS_LOCK_PATH@",
        "@B0X_INGRESS_OS_OWNER@",
        "@B0X_INGRESS_BINDING@",
        "@B0X_INGRESS_CREDENTIAL_ENV_FILE@",
    ):
        assert unresolved_private_input in public_added
    assert "fixture-" not in public_added


def test_binding_owner_mode_contract_accepts_only_declared_safe_mode(
    tmp_path: Path,
) -> None:
    payload = _binding_payload(tmp_path)
    runtime = payload["runtime"]
    assert isinstance(runtime, dict)
    runtime["binding_mode"] = "0640"
    binding, state_db = _write_binding(tmp_path, payload)
    binding.chmod(0o640)
    assert load_binding(binding, state_db=state_db).runtime.binding_mode == 0o640

    binding.chmod(0o644)
    with pytest.raises(B0XIngressError, match="binding_mode_mismatch"):
        load_binding(binding, state_db=state_db)


def test_readback_never_claims_started_or_terminal_before_real_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding_path, state_db = _write_binding(tmp_path, _binding_payload(tmp_path))
    transport, _ = _transport([_row(4)])
    rc, output = _run(
        capsys,
        binding_path,
        state_db,
        transport,
        now=datetime(2026, 9, 10, 9, 5, 30, tzinfo=KST),
    )
    assert rc == 0
    assert output["cycle_created_count"] == 1
    assert output["dispatch_queued_count"] == 1
    assert output["dispatch_started_count"] == 0
    assert output["terminal_evidence_count"] == 0

    binding = load_binding(binding_path, state_db=state_db)
    readback = ingress_readback(
        binding,
        lane="fixture-ingress-lane",
        event_id="kickoff-b0x-nudge-kr-2026-09-10",
    )
    assert readback["ingress_observed"] is True
    assert readback["dispatch_queued"] is True
    assert readback["dispatch_started"] is False
    assert readback["terminal_evidence_present"] is False
    assert readback["terminal_evidence_sha256"] is None
