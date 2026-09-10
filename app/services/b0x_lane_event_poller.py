"""Bounded HTTP ingress for durable B0X lane events.

The handoffkeep HTTP row is data only. This module has no broker, proposal,
watch, order, approval, model, prompt, shell, or generic dispatch surface.
It polls one closed GET endpoint and commits ingress evidence, business
disposition, and sweep progress in one local SQLite transaction.
"""

from __future__ import annotations

import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.services.b0x_lane_consumer import (
    B0XConsumerContractError,
    ConsumerReceipt,
    _connect,
    _consume_lane_event_in_connection,
)

BINDING_SCHEMA = "b0x-ingress/v1"
ACTIVE_BINDING_STATUS = "INSTALLED"
DRAFT_BINDING_STATUS = "DRAFT_NOT_INSTALLED"
HTTP_TIMEOUT_SECONDS = 5.0
PAGE_LIMIT = 200
MAX_BINDING_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_ROW_BYTES = 65_536
MAX_TEXT_BYTES = 2_048
LANE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RECEIPT = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,126}$")
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,255}$")
_PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
_ROW_KEYS = frozenset(
    {
        "id",
        "kind",
        "job_id",
        "epoch",
        "owner_lane",
        "machine",
        "pane_id",
        "report_path",
        "report_last_line",
        "question",
        "pr",
        "head",
        "reason",
        "event_id",
        "text",
        "event_time",
        "received_at",
        "delivered_at",
        "delivered_to",
        "attempts",
    }
)
_ROW_KEYS_WITH_TRUNCATED = _ROW_KEYS | {"truncated"}
_UNUSED_RELAY_EVENT_STRING_KEYS = (
    "job_id",
    "machine",
    "pane_id",
    "report_path",
    "report_last_line",
    "question",
    "pr",
    "head",
    "reason",
)
_TOP_KEYS = frozenset(
    {
        "schema",
        "status",
        "lane",
        "http",
        "runtime",
        "gates",
        "activation",
        "route_readback",
    }
)


class B0XIngressError(ValueError):
    """A closed ingress contract failed without exposing untrusted values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class B0XIngressHTTPError(RuntimeError):
    """A bounded HTTP operation failed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CredentialHeader:
    header: str
    env_key: str


@dataclass(frozen=True)
class HTTPBinding:
    base_url: str
    credential_source: str
    credential_headers: tuple[CredentialHeader, CredentialHeader]
    timeout_seconds: float
    page_limit: int
    max_response_bytes: int
    max_row_bytes: int
    max_text_bytes: int


@dataclass(frozen=True)
class RuntimeBinding:
    state_db: Path
    lock_path: Path
    owner: str
    group: str
    binding_mode: int
    state_mode: int
    lock_mode: int
    poll_budget_seconds: float
    max_pages_per_run: int


@dataclass(frozen=True)
class GateBinding:
    ingress_enabled: bool
    dispatch_enabled: bool
    source_enabled: bool


@dataclass(frozen=True)
class ActivationBinding:
    start_after_id: int | None
    activation_at: datetime | None
    source_binding_epoch: int | None
    code_head: str | None
    binding_install_receipt: str | None
    source_quiescence_receipt: str | None
    backlog_count: int | None
    backlog_min_id: int | None
    backlog_max_id: int | None


@dataclass(frozen=True)
class RouteReadback:
    verified: bool
    lane: str
    sink: bool
    unique_owner: bool
    owner_machine: str
    sink_record: str
    receipt: str | None


@dataclass(frozen=True)
class B0XIngressBinding:
    schema: str
    status: str
    lane: str
    http: HTTPBinding
    runtime: RuntimeBinding
    gates: GateBinding
    activation: ActivationBinding
    route_readback: RouteReadback


@dataclass(frozen=True)
class PollRowResult:
    delivery_id: int | None
    event_id: str | None
    text_sha256: str
    text_bytes: int
    ingress_recorded: bool
    ingress_disposition: str
    business_disposition: str
    duplicate: bool
    cycle_created: bool
    dispatch_queued: bool
    dispatch_started: bool
    terminal_evidence_present: bool
    esc: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PollResult:
    status: str
    poll_http_success: bool
    pages: int
    rows_observed: int
    rows_durably_recorded: int
    cycle_created_count: int
    dispatch_queued_count: int
    dispatch_started_count: int
    terminal_evidence_count: int
    fixed_floor: int
    sweep_cursor: int
    sweep_complete: bool
    resumed_partial_sweep: bool
    backlog_report: Mapping[str, int] | None
    rows: tuple[PollRowResult, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["rows"] = [row.as_dict() for row in self.rows]
        return value


class FaultHook(Protocol):
    def __call__(self, phase: str, delivery_id: int | None) -> None: ...


def _noop_fault(_phase: str, _delivery_id: int | None) -> None:
    return None


def _mapping(value: object, *, keys: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise B0XIngressError(code)
    return value


def _strict_bool(value: object, code: str) -> bool:
    if type(value) is not bool:
        raise B0XIngressError(code)
    return value


def _strict_int(
    value: object,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise B0XIngressError(code)
    return value


def _optional_int(value: object, code: str) -> int | None:
    return None if value is None else _strict_int(value, code)


def _bounded_text(
    value: object,
    code: str,
    *,
    maximum: int = 256,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8", errors="replace")) > maximum
        or _PLACEHOLDER.search(value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise B0XIngressError(code)
    return value


def _optional_text(value: object, code: str) -> str | None:
    return None if value is None else _bounded_text(value, code)


def _parse_timestamp(value: object, code: str) -> datetime:
    text = _bounded_text(value, code, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise B0XIngressError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise B0XIngressError(code)
    return parsed.astimezone(UTC)


def _optional_timestamp(value: object, code: str) -> datetime | None:
    return None if value is None else _parse_timestamp(value, code)


def _parse_mode(value: object, code: str) -> int:
    if value not in {"0600", "0640"}:
        raise B0XIngressError(code)
    return int(str(value), 8)


def _absolute_path(value: object, code: str) -> Path:
    text = _bounded_text(value, code, maximum=1024)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or str(path) != text:
        raise B0XIngressError(code)
    return path


def _base_url(value: object) -> str:
    text = _bounded_text(value, "invalid_http_base_url", maximum=2048)
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise B0XIngressError("invalid_http_base_url")
    return text.rstrip("/")


def _load_credentials(value: object) -> tuple[CredentialHeader, CredentialHeader]:
    if not isinstance(value, list) or len(value) != 2:
        raise B0XIngressError("credential_headers_must_have_two_entries")
    result: list[CredentialHeader] = []
    for item in value:
        mapping = _mapping(
            item,
            keys=frozenset({"header", "env_key"}),
            code="invalid_credential_header",
        )
        result.append(
            CredentialHeader(
                _bounded_text(
                    mapping["header"],
                    "invalid_credential_header_name",
                    pattern=_HEADER_NAME,
                ),
                _bounded_text(
                    mapping["env_key"],
                    "invalid_credential_env_key",
                    pattern=_ENV_KEY,
                ),
            )
        )
    if (
        len({item.header.lower() for item in result}) != 2
        or len({item.env_key for item in result}) != 2
    ):
        raise B0XIngressError("credential_headers_must_be_unique")
    return result[0], result[1]


def _load_http(value: object) -> HTTPBinding:
    mapping = _mapping(
        value,
        keys=frozenset(
            {
                "base_url",
                "credential_source",
                "credential_headers",
                "timeout_seconds",
                "page_limit",
                "max_response_bytes",
                "max_row_bytes",
                "max_text_bytes",
            }
        ),
        code="invalid_http_binding",
    )
    timeout = mapping["timeout_seconds"]
    if type(timeout) not in {int, float} or float(timeout) != HTTP_TIMEOUT_SECONDS:
        raise B0XIngressError("http_timeout_must_be_five_seconds")
    if mapping["page_limit"] != PAGE_LIMIT or type(mapping["page_limit"]) is not int:
        raise B0XIngressError("page_limit_must_be_200")
    fixed_limits = {
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "max_row_bytes": MAX_ROW_BYTES,
        "max_text_bytes": MAX_TEXT_BYTES,
    }
    for field, expected in fixed_limits.items():
        if type(mapping[field]) is not int or mapping[field] != expected:
            raise B0XIngressError(f"{field}_differs_from_closed_limit")
    return HTTPBinding(
        base_url=_base_url(mapping["base_url"]),
        credential_source=_bounded_text(
            mapping["credential_source"],
            "invalid_credential_source",
            pattern=_SAFE_IDENTITY,
        ),
        credential_headers=_load_credentials(mapping["credential_headers"]),
        timeout_seconds=float(timeout),
        page_limit=int(mapping["page_limit"]),
        max_response_bytes=int(mapping["max_response_bytes"]),
        max_row_bytes=int(mapping["max_row_bytes"]),
        max_text_bytes=int(mapping["max_text_bytes"]),
    )


def _load_runtime(value: object) -> RuntimeBinding:
    mapping = _mapping(
        value,
        keys=frozenset(
            {
                "state_db",
                "lock_path",
                "owner",
                "group",
                "binding_mode",
                "state_mode",
                "lock_mode",
                "poll_budget_seconds",
                "max_pages_per_run",
            }
        ),
        code="invalid_runtime_binding",
    )
    budget = mapping["poll_budget_seconds"]
    if type(budget) not in {int, float} or not 0.1 <= float(budget) <= 240:
        raise B0XIngressError("invalid_poll_budget")
    max_pages = _strict_int(
        mapping["max_pages_per_run"],
        "invalid_max_pages_per_run",
        minimum=1,
        maximum=100,
    )
    state_db = _absolute_path(mapping["state_db"], "invalid_state_db")
    lock_path = _absolute_path(mapping["lock_path"], "invalid_lock_path")
    if state_db == lock_path:
        raise B0XIngressError("state_db_and_lock_path_must_differ")
    return RuntimeBinding(
        state_db=state_db,
        lock_path=lock_path,
        owner=_bounded_text(mapping["owner"], "invalid_runtime_owner", maximum=64),
        group=_bounded_text(mapping["group"], "invalid_runtime_group", maximum=64),
        binding_mode=_parse_mode(mapping["binding_mode"], "invalid_binding_mode"),
        state_mode=_parse_mode(mapping["state_mode"], "invalid_state_mode"),
        lock_mode=_parse_mode(mapping["lock_mode"], "invalid_lock_mode"),
        poll_budget_seconds=float(budget),
        max_pages_per_run=max_pages,
    )


def _load_gates(value: object) -> GateBinding:
    mapping = _mapping(
        value,
        keys=frozenset({"ingress_enabled", "dispatch_enabled", "source_enabled"}),
        code="invalid_gate_binding",
    )
    return GateBinding(
        ingress_enabled=_strict_bool(
            mapping["ingress_enabled"], "invalid_ingress_gate"
        ),
        dispatch_enabled=_strict_bool(
            mapping["dispatch_enabled"], "invalid_dispatch_gate"
        ),
        source_enabled=_strict_bool(mapping["source_enabled"], "invalid_source_gate"),
    )


def _load_activation(value: object) -> ActivationBinding:
    mapping = _mapping(
        value,
        keys=frozenset(
            {
                "start_after_id",
                "activation_at",
                "source_binding_epoch",
                "code_head",
                "binding_install_receipt",
                "source_quiescence_receipt",
                "backlog_count",
                "backlog_min_id",
                "backlog_max_id",
            }
        ),
        code="invalid_activation_binding",
    )
    return ActivationBinding(
        start_after_id=_optional_int(
            mapping["start_after_id"], "invalid_start_after_id"
        ),
        activation_at=_optional_timestamp(
            mapping["activation_at"], "invalid_activation_at"
        ),
        source_binding_epoch=_optional_int(
            mapping["source_binding_epoch"], "invalid_source_binding_epoch"
        ),
        code_head=(
            None
            if mapping["code_head"] is None
            else _bounded_text(
                mapping["code_head"],
                "invalid_code_head",
                maximum=40,
                pattern=_SHA40,
            )
        ),
        binding_install_receipt=(
            None
            if mapping["binding_install_receipt"] is None
            else _bounded_text(
                mapping["binding_install_receipt"],
                "invalid_binding_install_receipt",
                maximum=71,
                pattern=_SHA256_RECEIPT,
            )
        ),
        source_quiescence_receipt=(
            None
            if mapping["source_quiescence_receipt"] is None
            else _bounded_text(
                mapping["source_quiescence_receipt"],
                "invalid_source_quiescence_receipt",
                maximum=71,
                pattern=_SHA256_RECEIPT,
            )
        ),
        backlog_count=_optional_int(mapping["backlog_count"], "invalid_backlog_count"),
        backlog_min_id=_optional_int(
            mapping["backlog_min_id"], "invalid_backlog_min_id"
        ),
        backlog_max_id=_optional_int(
            mapping["backlog_max_id"], "invalid_backlog_max_id"
        ),
    )


def _load_route(value: object) -> RouteReadback:
    mapping = _mapping(
        value,
        keys=frozenset(
            {
                "verified",
                "lane",
                "sink",
                "unique_owner",
                "owner_machine",
                "sink_record",
                "receipt",
            }
        ),
        code="invalid_route_readback",
    )
    return RouteReadback(
        verified=_strict_bool(mapping["verified"], "invalid_route_verified"),
        lane=_bounded_text(mapping["lane"], "invalid_route_lane", pattern=LANE_PATTERN),
        sink=_strict_bool(mapping["sink"], "invalid_route_sink"),
        unique_owner=_strict_bool(
            mapping["unique_owner"], "invalid_route_unique_owner"
        ),
        owner_machine=_bounded_text(
            mapping["owner_machine"],
            "invalid_route_owner_machine",
            pattern=_SAFE_IDENTITY,
        ),
        sink_record=_bounded_text(
            mapping["sink_record"],
            "invalid_route_sink_record",
            pattern=_SAFE_IDENTITY,
        ),
        receipt=(
            None
            if mapping["receipt"] is None
            else _bounded_text(
                mapping["receipt"],
                "invalid_route_receipt",
                maximum=71,
                pattern=_SHA256_RECEIPT,
            )
        ),
    )


def _validate_file_identity(path: Path, *, mode: int, owner: str, group: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise B0XIngressError("binding_path_wrong_kind")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise B0XIngressError("binding_mode_mismatch")
    try:
        actual_owner = pwd.getpwuid(metadata.st_uid).pw_name
        actual_group = grp.getgrgid(metadata.st_gid).gr_name
    except KeyError as exc:
        raise B0XIngressError("binding_owner_lookup_failed") from exc
    if actual_owner != owner or actual_group != group:
        raise B0XIngressError("binding_owner_mismatch")


def load_binding(path: Path, *, state_db: Path) -> B0XIngressBinding:
    """Load one strict binding without resolving credentials or contacting HTTP."""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise B0XIngressError("binding_path_must_be_absolute_regular_file")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise B0XIngressError("binding_path_must_be_canonical")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BINDING_BYTES:
        raise B0XIngressError("binding_size_invalid")
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise B0XIngressError("binding_json_invalid") from exc
    if _PLACEHOLDER.search(decoded):
        raise B0XIngressError("binding_retains_private_placeholder")
    mapping = _mapping(payload, keys=_TOP_KEYS, code="binding_keys_invalid")
    schema = _bounded_text(mapping["schema"], "binding_schema_invalid")
    if schema != BINDING_SCHEMA:
        raise B0XIngressError("binding_schema_invalid")
    status_value = _bounded_text(mapping["status"], "binding_status_invalid")
    if status_value not in {DRAFT_BINDING_STATUS, ACTIVE_BINDING_STATUS}:
        raise B0XIngressError("binding_status_invalid")
    lane = _bounded_text(mapping["lane"], "binding_lane_invalid", pattern=LANE_PATTERN)
    runtime = _load_runtime(mapping["runtime"])
    requested_state = _absolute_path(str(state_db), "state_db_argument_invalid")
    if requested_state != runtime.state_db:
        raise B0XIngressError("state_db_argument_differs_from_binding")
    binding = B0XIngressBinding(
        schema=schema,
        status=status_value,
        lane=lane,
        http=_load_http(mapping["http"]),
        runtime=runtime,
        gates=_load_gates(mapping["gates"]),
        activation=_load_activation(mapping["activation"]),
        route_readback=_load_route(mapping["route_readback"]),
    )
    _validate_file_identity(
        path,
        mode=runtime.binding_mode,
        owner=runtime.owner,
        group=runtime.group,
    )
    return binding


def activation_blockers(
    binding: B0XIngressBinding,
    *,
    now: datetime,
    runtime_code_head: str,
) -> tuple[str, ...]:
    """Return closed blocker codes; an empty tuple permits one poll sweep."""

    blockers: list[str] = []
    if now.tzinfo is None or now.utcoffset() is None:
        return ("processing_clock_not_timezone_aware",)
    if binding.status != ACTIVE_BINDING_STATUS:
        blockers.append("binding_not_installed")
    for field in ("ingress_enabled", "dispatch_enabled", "source_enabled"):
        if not getattr(binding.gates, field):
            blockers.append(f"{field}_false")
    activation = binding.activation
    required = {
        "start_after_id": activation.start_after_id,
        "activation_at": activation.activation_at,
        "source_binding_epoch": activation.source_binding_epoch,
        "code_head": activation.code_head,
        "binding_install_receipt": activation.binding_install_receipt,
        "source_quiescence_receipt": activation.source_quiescence_receipt,
        "backlog_count": activation.backlog_count,
    }
    blockers.extend(
        f"missing_{field}" for field, value in required.items() if value is None
    )
    if activation.code_head is not None and activation.code_head != runtime_code_head:
        blockers.append("runtime_code_head_mismatch")
    if (
        activation.activation_at is not None
        and activation.activation_at > now.astimezone(UTC)
    ):
        blockers.append("activation_at_in_future")
    if activation.start_after_id is not None and activation.backlog_count is not None:
        if activation.backlog_count == 0:
            if (
                activation.backlog_min_id is not None
                or activation.backlog_max_id is not None
            ):
                blockers.append("empty_backlog_must_not_have_range")
        elif (
            activation.backlog_min_id is None
            or activation.backlog_max_id is None
            or activation.backlog_min_id > activation.backlog_max_id
            or activation.backlog_max_id > activation.start_after_id
        ):
            blockers.append("invalid_pre_activation_backlog_range")
    route = binding.route_readback
    if not route.verified:
        blockers.append("route_readback_unverified")
    if route.lane != binding.lane:
        blockers.append("route_lane_mismatch")
    if not route.sink:
        blockers.append("route_is_not_sink")
    if not route.unique_owner:
        blockers.append("route_owner_not_unique")
    if route.receipt is None:
        blockers.append("missing_route_receipt")
    return tuple(blockers)


def _secure_runtime_file(
    path: Path, *, mode: int, owner: str, group: str, create: bool
) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise B0XIngressError("runtime_path_invalid")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or path.is_symlink():
        raise B0XIngressError("runtime_path_not_canonical")
    if not path.exists():
        if not create:
            raise B0XIngressError("runtime_file_missing")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, mode)
        try:
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
    if path.is_symlink() or not path.is_file():
        raise B0XIngressError("runtime_path_wrong_kind")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise B0XIngressError("runtime_file_mode_mismatch")
    try:
        actual_owner = pwd.getpwuid(metadata.st_uid).pw_name
        actual_group = grp.getgrgid(metadata.st_gid).gr_name
    except KeyError as exc:
        raise B0XIngressError("runtime_owner_lookup_failed") from exc
    if actual_owner != owner or actual_group != group:
        raise B0XIngressError("runtime_owner_mismatch")


@contextmanager
def stable_host_lock(binding: B0XIngressBinding) -> Any:
    """Take one stable host-local advisory lock; never delete the lock file."""

    runtime = binding.runtime
    _secure_runtime_file(
        runtime.lock_path,
        mode=runtime.lock_mode,
        owner=runtime.owner,
        group=runtime.group,
        create=True,
    )
    handle = runtime.lock_path.open("r+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise B0XIngressError("host_local_lock_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _credential_headers(
    binding: B0XIngressBinding, environ: Mapping[str, str]
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in binding.http.credential_headers:
        value = environ.get(item.env_key)
        if value is None or not value or len(value.encode()) > 4096:
            raise B0XIngressError("credential_value_missing_or_invalid")
        headers[item.header] = value
    return headers


def _bounded_response(response: httpx.Response, *, maximum: int) -> bytes:
    if response.status_code != 200:
        raise B0XIngressHTTPError("http_status_error")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise B0XIngressHTTPError("http_response_oversize")
        except ValueError as exc:
            raise B0XIngressHTTPError("http_content_length_invalid") from exc
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise B0XIngressHTTPError("http_response_oversize")
    return bytes(body)


def _fetch_page(
    client: httpx.Client,
    binding: B0XIngressBinding,
    *,
    cursor: int,
) -> list[object]:
    try:
        with client.stream(
            "GET",
            "/v1/relay/events",
            params={
                "lane": binding.lane,
                "kind": "lane.event",
                "undelivered": "false",
                "after_id": str(cursor),
                "limit": str(binding.http.page_limit),
            },
        ) as response:
            raw = _bounded_response(response, maximum=binding.http.max_response_bytes)
    except B0XIngressHTTPError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise B0XIngressHTTPError("http_get_failed") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise B0XIngressHTTPError("http_response_json_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"events"}:
        raise B0XIngressHTTPError("http_response_envelope_invalid")
    events = payload["events"]
    if not isinstance(events, list) or len(events) > binding.http.page_limit:
        raise B0XIngressHTTPError("http_response_events_invalid")
    return events


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise B0XIngressError("row_not_json_value") from exc


def _safe_row_identity(
    row: object,
) -> tuple[int | None, str | None, str, int, str, bytes]:
    encoded = _canonical_bytes(row)
    row_sha = hashlib.sha256(encoded).hexdigest()
    if not isinstance(row, dict):
        return None, None, hashlib.sha256(b"").hexdigest(), 0, row_sha, encoded
    delivery_id = row.get("id")
    safe_delivery_id = (
        delivery_id if type(delivery_id) is int and delivery_id > 0 else None
    )
    event_id = row.get("event_id")
    safe_event_id = (
        event_id
        if isinstance(event_id, str) and len(event_id.encode("utf-8")) <= 512
        else None
    )
    text = row.get("text")
    if isinstance(text, str):
        text_raw = text.encode("utf-8")
    else:
        text_raw = _canonical_bytes(text)
    return (
        safe_delivery_id,
        safe_event_id,
        hashlib.sha256(text_raw).hexdigest(),
        len(text_raw),
        row_sha,
        encoded,
    )


def _ensure_ingress_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS b0x_ingress_receipt (
            observation_key TEXT PRIMARY KEY,
            delivery_id INTEGER,
            lane TEXT,
            event_id TEXT,
            row_sha256 TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            text_bytes INTEGER NOT NULL,
            epoch INTEGER,
            event_time TEXT,
            hub_received_at TEXT,
            hub_delivered_at TEXT,
            delivered_to TEXT,
            truncated_present INTEGER NOT NULL
                CHECK (truncated_present IN (0, 1)),
            truncated_value INTEGER
                CHECK (truncated_value IS NULL OR truncated_value IN (0, 1)),
            processing_at TEXT NOT NULL,
            ingress_disposition TEXT NOT NULL,
            rejection_reason TEXT,
            business_disposition TEXT NOT NULL,
            business_duplicate INTEGER NOT NULL
                CHECK (business_duplicate IN (0, 1)),
            cycle_created INTEGER NOT NULL CHECK (cycle_created IN (0, 1)),
            dispatch_queued INTEGER NOT NULL CHECK (dispatch_queued IN (0, 1)),
            dispatch_started INTEGER NOT NULL CHECK (dispatch_started IN (0, 1)),
            terminal_evidence_present INTEGER NOT NULL
                CHECK (terminal_evidence_present IN (0, 1)),
            esc INTEGER NOT NULL CHECK (esc IN (0, 1))
        );
        CREATE INDEX IF NOT EXISTS idx_b0x_ingress_delivery
            ON b0x_ingress_receipt(delivery_id);
        CREATE INDEX IF NOT EXISTS idx_b0x_ingress_identity
            ON b0x_ingress_receipt(lane, event_id);
        CREATE TABLE IF NOT EXISTS b0x_ingress_sweep (
            lane TEXT PRIMARY KEY,
            fixed_floor INTEGER NOT NULL,
            resume_after_id INTEGER NOT NULL,
            partial INTEGER NOT NULL CHECK (partial IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS b0x_ingress_backlog_report (
            lane TEXT PRIMARY KEY,
            start_after_id INTEGER NOT NULL,
            backlog_count INTEGER NOT NULL,
            backlog_min_id INTEGER,
            backlog_max_id INTEGER,
            recorded_at TEXT NOT NULL
        );
        """
    )


def _existing_sweep(
    connection: sqlite3.Connection, *, lane: str, floor: int
) -> tuple[int, bool]:
    row = connection.execute(
        "SELECT fixed_floor, resume_after_id, partial "
        "FROM b0x_ingress_sweep WHERE lane = ?",
        (lane,),
    ).fetchone()
    if row is None:
        return floor, False
    if row[0] != floor:
        raise B0XIngressError("activation_floor_changed_for_existing_state")
    return (int(row[1]), bool(row[2])) if row[2] else (floor, False)


def _write_sweep_progress(
    connection: sqlite3.Connection,
    *,
    lane: str,
    floor: int,
    cursor: int,
    partial: bool,
    now_text: str,
) -> None:
    connection.execute(
        "INSERT INTO b0x_ingress_sweep"
        "(lane, fixed_floor, resume_after_id, partial, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(lane) DO UPDATE SET "
        "fixed_floor=excluded.fixed_floor, "
        "resume_after_id=excluded.resume_after_id, "
        "partial=excluded.partial, updated_at=excluded.updated_at",
        (lane, floor, cursor, int(partial), now_text),
    )


def _record_backlog_once(
    connection: sqlite3.Connection,
    binding: B0XIngressBinding,
    *,
    now_text: str,
) -> Mapping[str, int] | None:
    activation = binding.activation
    assert activation.start_after_id is not None
    assert activation.backlog_count is not None
    connection.execute("BEGIN IMMEDIATE")
    try:
        inserted = connection.execute(
            "INSERT OR IGNORE INTO b0x_ingress_backlog_report "
            "(lane, start_after_id, backlog_count, backlog_min_id, backlog_max_id, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                binding.lane,
                activation.start_after_id,
                activation.backlog_count,
                activation.backlog_min_id,
                activation.backlog_max_id,
                now_text,
            ),
        ).rowcount
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    if inserted != 1:
        return None
    result = {
        "count": activation.backlog_count,
        "start_after_id": activation.start_after_id,
    }
    if activation.backlog_min_id is not None:
        result["min_id"] = activation.backlog_min_id
    if activation.backlog_max_id is not None:
        result["max_id"] = activation.backlog_max_id
    return result


def _rejection(
    *,
    delivery_id: int | None,
    event_id: str | None,
    text_sha: str,
    text_bytes: int,
    reason: str,
    esc: bool = False,
) -> PollRowResult:
    return PollRowResult(
        delivery_id=delivery_id,
        event_id=event_id,
        text_sha256=text_sha,
        text_bytes=text_bytes,
        ingress_recorded=True,
        ingress_disposition="held" if esc else "rejected",
        business_disposition=f"rejected_{reason}",
        duplicate=False,
        cycle_created=False,
        dispatch_queued=False,
        dispatch_started=False,
        terminal_evidence_present=False,
        esc=esc,
    )


def _normalized_row(
    row: object,
    *,
    binding: B0XIngressBinding,
    processing_at: datetime,
    cursor: int,
    previous_id: int | None,
) -> tuple[dict[str, object] | None, PollRowResult | None, dict[str, object]]:
    (
        delivery_id,
        event_id,
        text_sha,
        text_bytes,
        row_sha,
        encoded,
    ) = _safe_row_identity(row)
    common: dict[str, object] = {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "text_sha256": text_sha,
        "text_bytes": text_bytes,
        "row_sha256": row_sha,
        "row_encoded_bytes": len(encoded),
        "epoch": None,
        "event_time": None,
        "hub_received_at": None,
        "hub_delivered_at": None,
        "delivered_to": None,
        "truncated_present": False,
        "truncated_value": None,
    }

    def rejected(
        reason: str, *, esc: bool = False
    ) -> tuple[None, PollRowResult, dict[str, object]]:
        return (
            None,
            _rejection(
                delivery_id=delivery_id,
                event_id=event_id,
                text_sha=text_sha,
                text_bytes=text_bytes,
                reason=reason,
                esc=esc,
            ),
            common,
        )

    if len(encoded) > binding.http.max_row_bytes:
        return rejected("row_oversize")
    if not isinstance(row, dict):
        return rejected("row_not_object")
    if set(row) not in {_ROW_KEYS, _ROW_KEYS_WITH_TRUNCATED}:
        return rejected("row_keys_invalid")
    truncated_present = "truncated" in row
    common["truncated_present"] = truncated_present
    if truncated_present:
        if type(row["truncated"]) is not bool:
            return rejected("truncated_not_boolean")
        common["truncated_value"] = row["truncated"]
        if row["truncated"] is True:
            return rejected("truncated_true")
    if delivery_id is None or delivery_id > 2**63 - 1:
        return rejected("delivery_id_invalid")
    if delivery_id <= cursor or (
        previous_id is not None and delivery_id <= previous_id
    ):
        return rejected("delivery_id_not_ascending")
    if row["kind"] != "lane.event":
        return rejected("kind_invalid")
    if row["owner_lane"] != binding.lane:
        return rejected("owner_lane_mismatch")
    if event_id is None:
        return rejected("event_id_invalid")
    if type(row["epoch"]) is not int or row["epoch"] < 0 or row["epoch"] > 2**63 - 1:
        return rejected("epoch_invalid")
    if row["epoch"] != binding.activation.source_binding_epoch:
        return rejected("source_binding_epoch_mismatch")
    common["epoch"] = row["epoch"]
    for key in _UNUSED_RELAY_EVENT_STRING_KEYS:
        value = row[key]
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8", errors="replace")) > binding.http.max_row_bytes
        ):
            return rejected(f"{key}_invalid")
    attempts = row["attempts"]
    if type(attempts) is not int or attempts < 0 or attempts > 2**63 - 1:
        return rejected("attempts_invalid")
    try:
        event_time = _parse_timestamp(row["event_time"], "event_time_invalid")
        hub_received = _parse_timestamp(row["received_at"], "hub_received_at_invalid")
        delivered_at = (
            None
            if row["delivered_at"] is None
            else _parse_timestamp(row["delivered_at"], "hub_delivered_at_invalid")
        )
    except B0XIngressError as exc:
        return rejected(exc.code)
    processing_utc = processing_at.astimezone(UTC)
    if event_time > processing_utc or hub_received > processing_utc:
        return rejected("future_timestamp")
    if delivered_at is not None and delivered_at > processing_utc:
        return rejected("future_timestamp")
    common["event_time"] = event_time.isoformat()
    common["hub_received_at"] = hub_received.isoformat()
    common["hub_delivered_at"] = (
        delivered_at.isoformat() if delivered_at is not None else None
    )
    delivered_to = row["delivered_to"]
    if delivered_to is not None and not isinstance(delivered_to, str):
        return rejected("delivered_to_invalid")
    common["delivered_to"] = delivered_to
    if delivered_to not in (None, "") and (
        delivered_to != binding.route_readback.sink_record
    ):
        return rejected("route_mismatch", esc=True)
    if text_bytes > binding.http.max_text_bytes or not isinstance(row["text"], str):
        return rejected("text_invalid_or_oversize")
    activation_at = binding.activation.activation_at
    assert activation_at is not None
    if hub_received < activation_at:
        return rejected("pre_activation_received_at")
    event = {
        "type": row["kind"],
        "owner_lane": row["owner_lane"],
        "event_id": row["event_id"],
        "text": row["text"],
    }
    return (
        {
            "event": event,
            **common,
            "hub_received_clock": hub_received,
        },
        None,
        common,
    )


def _insert_ingress_receipt(
    connection: sqlite3.Connection,
    *,
    binding: B0XIngressBinding,
    processing_at: datetime,
    common: Mapping[str, object],
    result: PollRowResult,
) -> None:
    observation_key = hashlib.sha256(
        (
            f"{common['delivery_id']}:{common['row_sha256']}:{common['text_sha256']}"
        ).encode()
    ).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO b0x_ingress_receipt "
        "(observation_key, delivery_id, lane, event_id, row_sha256, text_sha256, "
        "text_bytes, epoch, event_time, hub_received_at, hub_delivered_at, "
        "delivered_to, truncated_present, truncated_value, processing_at, "
        "ingress_disposition, rejection_reason, business_disposition, "
        "business_duplicate, cycle_created, dispatch_queued, dispatch_started, "
        "terminal_evidence_present, esc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (
            observation_key,
            common["delivery_id"],
            binding.lane,
            common["event_id"],
            common["row_sha256"],
            common["text_sha256"],
            common["text_bytes"],
            common["epoch"],
            common["event_time"],
            common["hub_received_at"],
            common["hub_delivered_at"],
            common["delivered_to"],
            int(bool(common["truncated_present"])),
            (
                None
                if common["truncated_value"] is None
                else int(bool(common["truncated_value"]))
            ),
            processing_at.astimezone(UTC).isoformat(),
            result.ingress_disposition,
            (
                result.business_disposition.removeprefix("rejected_")
                if result.business_disposition.startswith("rejected_")
                else None
            ),
            result.business_disposition,
            int(result.duplicate),
            int(result.cycle_created),
            int(result.dispatch_queued),
            int(result.dispatch_started),
            int(result.terminal_evidence_present),
            int(result.esc),
        ),
    )


def _already_observed(
    connection: sqlite3.Connection,
    *,
    delivery_id: int | None,
    row_sha: str,
) -> PollRowResult | None:
    if delivery_id is None:
        return None
    row = connection.execute(
        "SELECT event_id, text_sha256, text_bytes, ingress_disposition, "
        "business_disposition, cycle_created, dispatch_queued, dispatch_started, "
        "terminal_evidence_present, esc "
        "FROM b0x_ingress_receipt WHERE delivery_id = ? AND row_sha256 = ?",
        (delivery_id, row_sha),
    ).fetchone()
    if row is None:
        return None
    return PollRowResult(
        delivery_id,
        row[0],
        row[1],
        int(row[2]),
        True,
        str(row[3]),
        "duplicate",
        True,
        False,
        False,
        bool(row[7]),
        bool(row[8]),
        bool(row[9]),
    )


def _tampered_identity(
    connection: sqlite3.Connection,
    *,
    binding: B0XIngressBinding,
    delivery_id: int,
    event_id: str,
    text_sha: str,
    common: Mapping[str, object],
) -> bool:
    prior_delivery = connection.execute(
        "SELECT event_id, text_sha256, epoch, event_time, hub_received_at "
        "FROM b0x_ingress_receipt WHERE delivery_id = ? "
        "ORDER BY processing_at, observation_key LIMIT 1",
        (delivery_id,),
    ).fetchone()
    immutable_delivery_values = (
        event_id,
        text_sha,
        common["epoch"],
        common["event_time"],
        common["hub_received_at"],
    )
    by_identity = connection.execute(
        "SELECT 1 FROM b0x_ingress_receipt "
        "WHERE lane = ? AND event_id = ? AND text_sha256 != ? LIMIT 1",
        (binding.lane, event_id, text_sha),
    ).fetchone()
    return (
        prior_delivery is not None
        and tuple(prior_delivery) != immutable_delivery_values
    ) or by_identity is not None


def _consume_one(
    connection: sqlite3.Connection,
    *,
    binding: B0XIngressBinding,
    row: object,
    processing_at: datetime,
    cursor: int,
    previous_id: int | None,
    fault: FaultHook,
) -> PollRowResult:
    normalized, rejection, common = _normalized_row(
        row,
        binding=binding,
        processing_at=processing_at,
        cursor=cursor,
        previous_id=previous_id,
    )
    delivery_id = common["delivery_id"]
    assert delivery_id is None or isinstance(delivery_id, int)
    fault("before_transaction", delivery_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = _already_observed(
            connection,
            delivery_id=delivery_id,
            row_sha=str(common["row_sha256"]),
        )
        if existing is not None:
            result = existing
        elif rejection is not None:
            result = rejection
            _insert_ingress_receipt(
                connection,
                binding=binding,
                processing_at=processing_at,
                common=common,
                result=result,
            )
        else:
            assert normalized is not None
            assert isinstance(delivery_id, int)
            event_id = normalized["event_id"]
            assert isinstance(event_id, str)
            if _tampered_identity(
                connection,
                binding=binding,
                delivery_id=delivery_id,
                event_id=event_id,
                text_sha=str(common["text_sha256"]),
                common=common,
            ):
                result = _rejection(
                    delivery_id=delivery_id,
                    event_id=event_id,
                    text_sha=str(common["text_sha256"]),
                    text_bytes=int(common["text_bytes"]),
                    reason="tampered_duplicate",
                    esc=True,
                )
            else:
                try:
                    business = _consume_lane_event_in_connection(
                        normalized["event"],
                        connection=connection,
                        received_at=processing_at,
                        hub_received_at=normalized["hub_received_clock"],
                    )
                except B0XConsumerContractError:
                    result = _rejection(
                        delivery_id=delivery_id,
                        event_id=event_id,
                        text_sha=str(common["text_sha256"]),
                        text_bytes=int(common["text_bytes"]),
                        reason="event_contract_invalid",
                    )
                else:
                    assert isinstance(business, ConsumerReceipt)
                    result = PollRowResult(
                        delivery_id=delivery_id,
                        event_id=event_id,
                        text_sha256=str(common["text_sha256"]),
                        text_bytes=int(common["text_bytes"]),
                        ingress_recorded=True,
                        ingress_disposition="recorded",
                        business_disposition=(
                            "duplicate" if business.duplicate else business.disposition
                        ),
                        duplicate=business.duplicate,
                        cycle_created=business.cycle_created and not business.duplicate,
                        dispatch_queued=business.cycle_created,
                        dispatch_started=False,
                        terminal_evidence_present=False,
                        esc=False,
                    )
            _insert_ingress_receipt(
                connection,
                binding=binding,
                processing_at=processing_at,
                common=common,
                result=result,
            )
        floor = binding.activation.start_after_id
        assert floor is not None
        next_cursor = max(cursor, delivery_id) if delivery_id is not None else cursor
        _write_sweep_progress(
            connection,
            lane=binding.lane,
            floor=floor,
            cursor=next_cursor,
            partial=True,
            now_text=processing_at.astimezone(UTC).isoformat(),
        )
        fault("before_commit", delivery_id)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    fault("after_commit", delivery_id)
    return result


def _open_active_state(binding: B0XIngressBinding) -> sqlite3.Connection:
    runtime = binding.runtime
    _secure_runtime_file(
        runtime.state_db,
        mode=runtime.state_mode,
        owner=runtime.owner,
        group=runtime.group,
        create=True,
    )
    connection = _connect(runtime.state_db)
    _ensure_ingress_schema(connection)
    return connection


def poll_once(
    binding: B0XIngressBinding,
    *,
    processing_at: datetime,
    http_transport: httpx.BaseTransport | None = None,
    environ: Mapping[str, str] = os.environ,
    monotonic: Callable[[], float] = time.monotonic,
    fault: FaultHook = _noop_fault,
) -> PollResult:
    """Run one finite sweep or partial continuation under the caller's lock."""

    if processing_at.tzinfo is None or processing_at.utcoffset() is None:
        raise B0XIngressError("processing_clock_not_timezone_aware")
    headers = _credential_headers(binding, environ)
    connection = _open_active_state(binding)
    try:
        floor = binding.activation.start_after_id
        assert floor is not None
        cursor, resumed = _existing_sweep(connection, lane=binding.lane, floor=floor)
        now_text = processing_at.astimezone(UTC).isoformat()
        backlog = _record_backlog_once(connection, binding, now_text=now_text)
        deadline = monotonic() + binding.runtime.poll_budget_seconds
        results: list[PollRowResult] = []
        pages = 0
        complete = False
        with httpx.Client(
            base_url=binding.http.base_url,
            headers=headers,
            timeout=binding.http.timeout_seconds,
            follow_redirects=False,
            transport=http_transport,
        ) as client:
            while pages < binding.runtime.max_pages_per_run:
                if pages and monotonic() >= deadline:
                    break
                page = _fetch_page(client, binding, cursor=cursor)
                pages += 1
                previous_id: int | None = None
                for row in page:
                    result = _consume_one(
                        connection,
                        binding=binding,
                        row=row,
                        processing_at=processing_at,
                        cursor=cursor,
                        previous_id=previous_id,
                        fault=fault,
                    )
                    results.append(result)
                    if result.delivery_id is not None and result.delivery_id > cursor:
                        cursor = result.delivery_id
                        previous_id = result.delivery_id
                if len(page) < binding.http.page_limit:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _write_sweep_progress(
                            connection,
                            lane=binding.lane,
                            floor=floor,
                            cursor=floor,
                            partial=False,
                            now_text=now_text,
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    complete = True
                    cursor = floor
                    break
        return PollResult(
            status="complete" if complete else "partial",
            poll_http_success=True,
            pages=pages,
            rows_observed=len(results),
            rows_durably_recorded=sum(row.ingress_recorded for row in results),
            cycle_created_count=sum(row.cycle_created for row in results),
            dispatch_queued_count=sum(row.dispatch_queued for row in results),
            dispatch_started_count=sum(row.dispatch_started for row in results),
            terminal_evidence_count=sum(
                row.terminal_evidence_present for row in results
            ),
            fixed_floor=floor,
            sweep_cursor=cursor,
            sweep_complete=complete,
            resumed_partial_sweep=resumed,
            backlog_report=backlog,
            rows=tuple(results),
        )
    finally:
        connection.close()


def ingress_readback(
    binding: B0XIngressBinding,
    *,
    lane: str,
    event_id: str,
) -> dict[str, object]:
    """Return safe local evidence; never expose raw text or credential values."""

    if lane != binding.lane or LANE_PATTERN.fullmatch(lane) is None:
        raise B0XIngressError("readback_lane_mismatch")
    if not event_id or len(event_id.encode("utf-8")) > 512:
        raise B0XIngressError("readback_event_id_invalid")
    if not binding.runtime.state_db.exists():
        return {
            "status": "readback",
            "lane": lane,
            "event_id": event_id,
            "poll_http_success": None,
            "ingress_observed": False,
            "ingress_observation_count": 0,
            "business_disposition": None,
            "cycle_created": False,
            "dispatch_queued": False,
            "dispatch_started": False,
            "dispatch_attempt_id": None,
            "dispatch_claimed_at": None,
            "dispatch_process_started_at": None,
            "cycle_observed": False,
            "cycle_observed_at": None,
            "dispatch_terminal_type": None,
            "dispatch_terminal_verified": False,
            "dispatch_cycle_starts": 0,
            "dispatch_push_reapplications": 0,
            "terminal_evidence_present": False,
        }
    _secure_runtime_file(
        binding.runtime.state_db,
        mode=binding.runtime.state_mode,
        owner=binding.runtime.owner,
        group=binding.runtime.group,
        create=False,
    )
    connection = sqlite3.connect(
        f"file:{binding.runtime.state_db}?mode=ro", uri=True, timeout=10
    )
    try:
        observations = connection.execute(
            "SELECT delivery_id, row_sha256, text_sha256, text_bytes, epoch, "
            "event_time, hub_received_at, hub_delivered_at, delivered_to, "
            "truncated_present, truncated_value, processing_at, "
            "ingress_disposition, business_disposition, esc "
            "FROM b0x_ingress_receipt "
            "WHERE lane = ? AND event_id = ? ORDER BY processing_at, observation_key",
            (lane, event_id),
        ).fetchall()
        business = connection.execute(
            "SELECT disposition, cycle_created, terminal_evidence "
            "FROM b0x_lane_event WHERE lane = ? AND event_id = ?",
            (lane, event_id),
        ).fetchone()
        dispatch_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='b0x_dispatch_attempt'"
        ).fetchone()
        dispatch = (
            connection.execute(
                "SELECT attempt_id,claimed_at,process_started_at,cycle_observed_at,"
                "terminal_type,terminal_verified,cycle_starts,push_reapplications "
                "FROM b0x_dispatch_attempt WHERE lane=? AND event_id=?",
                (lane, event_id),
            ).fetchone()
            if dispatch_table is not None
            else None
        )
    finally:
        connection.close()
    disposition = business[0] if business is not None else None
    cycle_created = bool(business[1]) if business is not None else False
    terminal = business[2] if business is not None else None
    return {
        "status": "readback",
        "lane": lane,
        "event_id": event_id,
        "poll_http_success": None,
        "ingress_observed": bool(observations),
        "ingress_observation_count": len(observations),
        "ingress": [
            {
                "delivery_id": row[0],
                "row_sha256": row[1],
                "text_sha256": row[2],
                "text_bytes": row[3],
                "source_binding_epoch": row[4],
                "event_time": row[5],
                "hub_received_at": row[6],
                "hub_delivered_at": row[7],
                "delivered_to": row[8],
                "truncated_present": bool(row[9]),
                "truncated_value": None if row[10] is None else bool(row[10]),
                "processing_at": row[11],
                "disposition": row[12],
                "business_disposition": row[13],
                "esc": bool(row[14]),
            }
            for row in observations
        ],
        "business_disposition": disposition,
        "cycle_created": cycle_created,
        "dispatch_queued": cycle_created
        and disposition
        in {
            "queued_cycle",
            "dispatch_claimed",
            "success_observed",
            "zero_order_observed",
            "failed_preserved",
            "unknown_preserved",
        },
        "dispatch_attempt_id": None if dispatch is None else dispatch[0],
        "dispatch_claimed_at": None if dispatch is None else dispatch[1],
        "dispatch_started": dispatch is not None and dispatch[2] is not None,
        "dispatch_process_started_at": None if dispatch is None else dispatch[2],
        "cycle_observed": dispatch is not None and dispatch[3] is not None,
        "cycle_observed_at": None if dispatch is None else dispatch[3],
        "dispatch_terminal_type": None if dispatch is None else dispatch[4],
        "dispatch_terminal_verified": bool(dispatch[5])
        if dispatch is not None
        else False,
        "dispatch_cycle_starts": int(dispatch[6]) if dispatch is not None else 0,
        "dispatch_push_reapplications": int(dispatch[7]) if dispatch is not None else 0,
        "terminal_evidence_present": (
            bool(dispatch[5]) if dispatch is not None else terminal is not None
        ),
        "terminal_evidence_sha256": (
            hashlib.sha256(str(terminal).encode()).hexdigest()
            if terminal is not None
            else None
        ),
    }


__all__ = [
    "ACTIVE_BINDING_STATUS",
    "BINDING_SCHEMA",
    "B0XIngressBinding",
    "B0XIngressError",
    "B0XIngressHTTPError",
    "DRAFT_BINDING_STATUS",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_BINDING_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_ROW_BYTES",
    "MAX_TEXT_BYTES",
    "PAGE_LIMIT",
    "PollResult",
    "activation_blockers",
    "ingress_readback",
    "load_binding",
    "poll_once",
    "stable_host_lock",
]
