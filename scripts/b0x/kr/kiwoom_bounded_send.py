"""Registered, expiring, one-shot authority for the KR Kiwoom owner path.

The production registry ships empty.  A future operator-approved PR may add an
exact four-field seal to that data file; a caller-supplied look-alike is never
authority by itself.  Consumption writes an exclusive, fsync-backed marker
*before* a send-capable owner can be constructed.  The marker is both evidence
and the durable latch, so any write/verification uncertainty fails closed.

This module does not construct an owner and cannot call a broker.  The sole
``grant_only=False`` production call remains in ``kiwoom_coordination``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import regular_session_bounds

KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA: Final[str] = "kiwoom-bounded-send-seal-registry.v1"
KIWOOM_BOUNDED_SEND_MARKER_SCHEMA: Final[str] = "kiwoom-bounded-send-consumption.v1"
KIWOOM_BOUNDED_SEND_SEAL_REGISTRY_PATH: Final[Path] = Path(__file__).with_name(
    "kiwoom_bounded_send_seals.toml"
)
KIWOOM_BOUNDED_SEND_CONSUMPTION_ROOT: Final[Path] = (
    Path.home() / ".local" / "state" / "auto-trader" / "b0x-kr-kiwoom-bounded-send"
)

KIWOOM_BOUNDED_SEND_INVALID_SEAL: Final[str] = "bounded_send_invalid_seal"
KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE: Final[str] = (
    "bounded_send_registry_unavailable"
)
KIWOOM_BOUNDED_SEND_UNREGISTERED: Final[str] = "bounded_send_seal_unregistered"
KIWOOM_BOUNDED_SEND_EXPIRED: Final[str] = "bounded_send_seal_expired"
KIWOOM_BOUNDED_SEND_ALREADY_CONSUMED: Final[str] = "bounded_send_seal_already_consumed"
KIWOOM_BOUNDED_SEND_MARKER_WRITE_FAILED: Final[str] = "bounded_send_marker_write_failed"
KIWOOM_BOUNDED_SEND_MARKER_INVALID: Final[str] = "bounded_send_marker_invalid"

_SEAL_KEYS: Final[frozenset[str]] = frozenset(
    {"lane_id", "physical_account_id", "expires_at", "seal_digest"}
)
_REGISTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "registered_seals"}
)
_MARKER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "lane_id",
        "physical_account_id",
        "expires_at",
        "seal_digest",
        "consumed_at",
    }
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_KST: Final[ZoneInfo] = ZoneInfo("Asia/Seoul")
_STATE_README_NAME: Final[str] = "README.md"
_STATE_README_BYTES: Final[bytes] = (
    b"# Kiwoom bounded-send one-shot state\n\n"
    b"This directory is durable authorization state, not a cleanup target.\n"
    b"Deleting a consumption marker here releases that seal's one-shot latch "
    b"after a process restart. Do not remove or edit these files.\n"
)
_PROCESS_CONSUMPTION_LOCK = threading.Lock()
_PROCESS_CONSUMED_SEAL_DIGESTS: set[str] = set()


class KiwoomBoundedSendSealRejected(RuntimeError):
    """Closed, report-safe refusal before owner construction."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> Never:
    raise KiwoomBoundedSendSealRejected(code)


def _parse_canonical_utc(raw: object, *, code: str) -> dt.datetime:
    if type(raw) is not str or not raw.endswith("Z"):
        _reject(code)
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError:
        _reject(code)
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        _reject(code)
    canonical = parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if canonical != raw:
        _reject(code)
    return parsed.astimezone(dt.UTC)


def _require_exact_nonempty_string(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    return value


def compute_bounded_send_seal_digest(
    *, lane_id: str, physical_account_id: str, expires_at: str
) -> str:
    """Hash the exact canonical JSON serialization of the three bound fields."""

    lane = _require_exact_nonempty_string(lane_id)
    account = _require_exact_nonempty_string(physical_account_id)
    expiry = _require_exact_nonempty_string(expires_at)
    _parse_canonical_utc(expiry, code=KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    canonical = json.dumps(
        {
            "expires_at": expiry,
            "lane_id": lane,
            "physical_account_id": account,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundedSendSeal:
    """Immutable snapshot of the four-field authority supplied by a caller."""

    lane_id: str
    physical_account_id: str
    expires_at: str
    seal_digest: str

    @property
    def expiry(self) -> dt.datetime:
        return _parse_canonical_utc(
            self.expires_at, code=KIWOOM_BOUNDED_SEND_INVALID_SEAL
        )

    def canonical(self) -> dict[str, str]:
        return {
            "lane_id": self.lane_id,
            "physical_account_id": self.physical_account_id,
            "expires_at": self.expires_at,
            "seal_digest": self.seal_digest,
        }


def snapshot_bounded_send_seal(raw: object) -> BoundedSendSeal:
    """Copy one exact built-in dict once, then validate only the frozen copy."""

    if type(raw) is not dict:
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    snapshot = dict.copy(raw)
    if set(snapshot) != _SEAL_KEYS:
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    lane_id = _require_exact_nonempty_string(snapshot["lane_id"])
    physical_account_id = _require_exact_nonempty_string(
        snapshot["physical_account_id"]
    )
    expires_at = _require_exact_nonempty_string(snapshot["expires_at"])
    seal_digest = _require_exact_nonempty_string(snapshot["seal_digest"])
    if _SHA256_HEX.fullmatch(seal_digest) is None:
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    expected = compute_bounded_send_seal_digest(
        lane_id=lane_id,
        physical_account_id=physical_account_id,
        expires_at=expires_at,
    )
    if seal_digest != expected:
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    return BoundedSendSeal(
        lane_id=lane_id,
        physical_account_id=physical_account_id,
        expires_at=expires_at,
        seal_digest=seal_digest,
    )


def _registered_seals() -> dict[str, BoundedSendSeal]:
    path = Path(KIWOOM_BOUNDED_SEND_SEAL_REGISTRY_PATH)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    try:
        payload = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    if type(payload) is not dict or set(payload) != _REGISTRY_KEYS:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    if payload["schema_version"] != KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    entries = payload["registered_seals"]
    if type(entries) is not list:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    registered: dict[str, BoundedSendSeal] = {}
    try:
        registry_now = _checked_wall_clock_now() if entries else None
        for raw_entry in entries:
            seal = snapshot_bounded_send_seal(raw_entry)
            if registry_now is None:
                _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
            _assert_registry_expiry_within_current_krx_session(
                seal,
                now=registry_now,
            )
            if seal.seal_digest in registered:
                _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
            registered[seal.seal_digest] = seal
    except KiwoomBoundedSendSealRejected as exc:
        if exc.code == KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE:
            raise
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    return registered


def _wall_clock_now() -> dt.datetime:
    """Return the real UTC clock; no public constructor accepts a clock hook."""

    return dt.datetime.now(dt.UTC)


def _checked_wall_clock_now() -> dt.datetime:
    now = _wall_clock_now()
    if (
        type(now) is not dt.datetime
        or now.tzinfo is None
        or now.utcoffset() != dt.timedelta(0)
    ):
        _reject(KIWOOM_BOUNDED_SEND_EXPIRED)
    return now.astimezone(dt.UTC)


def _assert_unexpired(seal: BoundedSendSeal, *, now: dt.datetime) -> None:
    if seal.expiry <= now:
        _reject(KIWOOM_BOUNDED_SEND_EXPIRED)


def _assert_registry_expiry_within_current_krx_session(
    seal: BoundedSendSeal,
    *,
    now: dt.datetime,
) -> None:
    """Require effective issuance and expiry within today's confirmed KRX day."""

    session_day = now.astimezone(_KST).date()
    bounds = regular_session_bounds("kr", session_day)
    if bounds is None:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)
    _, session_close = bounds
    expiry = seal.expiry
    if expiry.astimezone(_KST).date() != session_day or expiry > session_close:
        _reject(KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE)


def assert_bounded_send_seal_registered_and_current(seal: BoundedSendSeal) -> None:
    """Preflight registration/account-independent expiry before port creation."""

    registered = _registered_seals().get(seal.seal_digest)
    if registered is None or registered != seal:
        _reject(KIWOOM_BOUNDED_SEND_UNREGISTERED)
    _assert_unexpired(seal, now=_checked_wall_clock_now())


def bounded_send_consumption_marker_path(seal_digest: str) -> Path:
    if type(seal_digest) is not str or _SHA256_HEX.fullmatch(seal_digest) is None:
        _reject(KIWOOM_BOUNDED_SEND_INVALID_SEAL)
    return Path(KIWOOM_BOUNDED_SEND_CONSUMPTION_ROOT) / f"{seal_digest}.json"


def _canonical_marker_bytes(
    seal: BoundedSendSeal, *, consumed_at: dt.datetime
) -> bytes:
    payload = {
        "schema_version": KIWOOM_BOUNDED_SEND_MARKER_SCHEMA,
        **seal.canonical(),
        "consumed_at": consumed_at.isoformat().replace("+00:00", "Z"),
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise OSError("bounded send marker write made no progress")
        written += count


def _ensure_consumption_directory(path: Path) -> None:
    """Create the state directory and its operator warning before any marker."""

    path.mkdir(parents=True, exist_ok=True)
    readme_path = path / _STATE_README_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(readme_path, flags, 0o644)
    except FileExistsError:
        return
    try:
        _write_all(fd, _STATE_README_BYTES)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_consumption_marker(path: Path, payload: bytes) -> None:
    """Exclusively create and fsync a marker; never remove it on uncertainty."""

    _ensure_consumption_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _load_exact_marker(seal: BoundedSendSeal) -> dict[str, str]:
    path = bounded_send_consumption_marker_path(seal.seal_digest)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)
    if type(payload) is not dict or set(payload) != _MARKER_KEYS:
        _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)
    expected = {
        "schema_version": KIWOOM_BOUNDED_SEND_MARKER_SCHEMA,
        **seal.canonical(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)
    consumed_at = _parse_canonical_utc(
        payload.get("consumed_at"), code=KIWOOM_BOUNDED_SEND_MARKER_INVALID
    )
    if consumed_at >= seal.expiry:
        _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)
    return payload


def consume_registered_bounded_send_seal(seal: BoundedSendSeal) -> Path:
    """Atomically spend the process and durable one-shot authority."""

    with _PROCESS_CONSUMPTION_LOCK:
        assert_bounded_send_seal_registered_and_current(seal)
        if seal.seal_digest in _PROCESS_CONSUMED_SEAL_DIGESTS:
            _reject(KIWOOM_BOUNDED_SEND_ALREADY_CONSUMED)

        consumed_at = _checked_wall_clock_now()
        _assert_unexpired(seal, now=consumed_at)
        marker_path = bounded_send_consumption_marker_path(seal.seal_digest)
        marker_bytes = _canonical_marker_bytes(seal, consumed_at=consumed_at)
        try:
            _write_consumption_marker(marker_path, marker_bytes)
        except FileExistsError:
            _reject(KIWOOM_BOUNDED_SEND_ALREADY_CONSUMED)
        except OSError:
            _reject(KIWOOM_BOUNDED_SEND_MARKER_WRITE_FAILED)

        try:
            if marker_path.read_bytes() != marker_bytes:
                _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)
        except OSError:
            _reject(KIWOOM_BOUNDED_SEND_MARKER_INVALID)

        # A seal that expires while the durable write is being committed stays
        # consumed but never creates an owner.
        _assert_unexpired(seal, now=_checked_wall_clock_now())
        _PROCESS_CONSUMED_SEAL_DIGESTS.add(seal.seal_digest)
        return marker_path


def assert_consumed_bounded_send_seal_current(seal: BoundedSendSeal) -> None:
    """Re-attest registration, expiry, marker bytes, and process consumption."""

    assert_bounded_send_seal_registered_and_current(seal)
    with _PROCESS_CONSUMPTION_LOCK:
        if seal.seal_digest not in _PROCESS_CONSUMED_SEAL_DIGESTS:
            _reject(KIWOOM_BOUNDED_SEND_ALREADY_CONSUMED)
        _load_exact_marker(seal)
        _assert_unexpired(seal, now=_checked_wall_clock_now())


__all__ = [
    "BoundedSendSeal",
    "KIWOOM_BOUNDED_SEND_ALREADY_CONSUMED",
    "KIWOOM_BOUNDED_SEND_CONSUMPTION_ROOT",
    "KIWOOM_BOUNDED_SEND_EXPIRED",
    "KIWOOM_BOUNDED_SEND_INVALID_SEAL",
    "KIWOOM_BOUNDED_SEND_MARKER_INVALID",
    "KIWOOM_BOUNDED_SEND_MARKER_SCHEMA",
    "KIWOOM_BOUNDED_SEND_MARKER_WRITE_FAILED",
    "KIWOOM_BOUNDED_SEND_REGISTRY_SCHEMA",
    "KIWOOM_BOUNDED_SEND_REGISTRY_UNAVAILABLE",
    "KIWOOM_BOUNDED_SEND_SEAL_REGISTRY_PATH",
    "KIWOOM_BOUNDED_SEND_UNREGISTERED",
    "KiwoomBoundedSendSealRejected",
    "assert_bounded_send_seal_registered_and_current",
    "assert_consumed_bounded_send_seal_current",
    "bounded_send_consumption_marker_path",
    "compute_bounded_send_seal_digest",
    "consume_registered_bounded_send_seal",
    "snapshot_bounded_send_seal",
]
