"""Kiwoom B0-X ORDERING-only local writer lease and fidelity journal.

This module intentionally does not know how to call a broker.  It supplies two
small, auditable primitives to :mod:`scripts.b0x.kr.kiwoom_cycle`:

* an account-keyed, non-blocking local writer lease, checked again immediately
  before every mutation; and
* an append-only lifecycle event journal.  A cycle record is a useful summary,
  but it must not be the only place where an acknowledgement, fill readback, or
  cancellation reconciliation can be observed.

The lease is host-local ``flock`` authority.  The accompanying broker foreign
trace is still mandatory at every mutation boundary; neither mechanism is
presented as a substitute for the other.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

ORDERING_EVENT_JOURNAL_NAME: Final[str] = "ordering-events.jsonl"


class AccountWriterLeaseContended(RuntimeError):
    """Another local process holds this account's ORDERING writer lease."""


class AccountWriterLeaseLost(RuntimeError):
    """A caller reached a mutation boundary without its lease still held."""


class OrderingJournalUnreadable(RuntimeError):
    """A lifecycle journal exists but cannot be parsed as append-only evidence."""


class WriterLease(Protocol):
    """Injection seam used by tests; production uses :class:`AccountWriterLease`."""

    def acquire(self) -> None: ...

    def assert_held(self) -> None: ...

    def release(self) -> None: ...

    def canonical(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class AccountWriterLease:
    """A non-blocking local lease keyed by the redacted account fingerprint.

    The lock lives beneath the B0-X artifact root rather than an operator repo,
    so normal runtime activity cannot dirty a PR-only checkout.  The raw
    account identifier never reaches this class: the public fingerprint from
    ``account_identity_summary`` is already a one-way digest.
    """

    root: Path
    lane: str
    account_fingerprint: str
    _handle: int | None = None
    _token: str | None = None

    @property
    def lock_path(self) -> Path:
        digest = hashlib.sha256(self.account_fingerprint.encode()).hexdigest()[:16]
        return Path(self.root).expanduser() / self.lane / f".{digest}.ordering.lock"

    @property
    def acquired(self) -> bool:
        return self._handle is not None and self._token is not None

    def acquire(self) -> None:
        if self.acquired:
            raise RuntimeError("account writer lease is already acquired")
        path = self.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(handle)
            raise AccountWriterLeaseContended(
                "kiwoom_mock ORDERING account writer lease is held by another "
                f"local process ({path}); refusing all mutations"
            ) from exc

        token = uuid.uuid4().hex
        try:
            os.ftruncate(handle, 0)
            os.write(
                handle,
                (
                    f"pid={os.getpid()}\n"
                    f"lane={self.lane}\n"
                    f"account_fingerprint={self.account_fingerprint}\n"
                    f"lease_token_sha256={hashlib.sha256(token.encode()).hexdigest()[:16]}\n"
                ).encode(),
            )
        except BaseException:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)
            raise
        self._handle = handle
        self._token = token

    def assert_held(self) -> None:
        if not self.acquired:
            raise AccountWriterLeaseLost(
                "kiwoom_mock ORDERING account writer lease is no longer held; "
                "the mutation boundary is closed"
            )

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._token = None
        if handle is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

    def canonical(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "authority": "host_local_fcntl_account_keyed",
            "lock_path": str(self.lock_path),
            "account_fingerprint": self.account_fingerprint,
            "checked_before_each_mutation": True,
        }


@dataclass(frozen=True, slots=True)
class OrderingEventJournal:
    """Append-only per-event evidence for the ORDERING lifecycle."""

    path: Path

    @classmethod
    def for_lane(cls, *, root: Path, lane: str) -> OrderingEventJournal:
        return cls(path=Path(root).expanduser() / lane / ORDERING_EVENT_JOURNAL_NAME)

    def append(self, event: dict[str, Any]) -> None:
        if not isinstance(event.get("at"), str) or not event["at"].strip():
            raise ValueError("ordering lifecycle event requires a non-empty timestamp")
        if not isinstance(event.get("event"), str) or not event["event"].strip():
            raise ValueError("ordering lifecycle event requires a non-empty event name")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_all(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        events: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError("ordering lifecycle row is not an object")
                if not isinstance(payload.get("at"), str) or not payload["at"].strip():
                    raise ValueError("ordering lifecycle row has no timestamp")
                if (
                    not isinstance(payload.get("event"), str)
                    or not payload["event"].strip()
                ):
                    raise ValueError("ordering lifecycle row has no event name")
                events.append(payload)
        except Exception as exc:  # noqa: BLE001 — corrupt evidence is never empty
            raise OrderingJournalUnreadable(
                f"ordering lifecycle journal at {self.path} is unreadable "
                f"({type(exc).__name__})"
            ) from exc
        return tuple(events)


__all__ = [
    "ORDERING_EVENT_JOURNAL_NAME",
    "AccountWriterLease",
    "AccountWriterLeaseContended",
    "AccountWriterLeaseLost",
    "OrderingEventJournal",
    "OrderingJournalUnreadable",
    "WriterLease",
]
