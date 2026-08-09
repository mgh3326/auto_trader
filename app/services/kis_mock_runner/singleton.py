"""Cross-process writer singleton for the KIS mock account mode.

The lock is intentionally not a ``ps`` scan: a process list cannot reliably
identify containers, stale PIDs, or another host.  A non-blocking PostgreSQL
advisory lock keyed by the account mode is the authority.  A PID-bearing file
lock gives local operator visibility and catches same-host contention before a
database connection is opened.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ACCOUNT_MODE = "kis_mock"
MUTATION_WRITER_SURFACES = frozenset(
    {
        "runner",
        "watch_auto_execute",
        "smoke_cli",
        "manual_mcp_mutation",
        # B0-X KR is a separately approved, manual-only adapter path.  It
        # acquires this same account-wide lease before its confirm preflight so
        # it cannot overlap any catalogued kis_mock writer.
        "b0x_adapter",
    }
)
_ACTIVE_WRITER_LEASE: ContextVar[bool] = ContextVar(
    "kis_mock_runner_active_writer_lease", default=False
)


class WriterSingletonContended(RuntimeError):
    """Another writer is active; the caller must fail closed immediately."""


class WriterSingletonUnavailable(RuntimeError):
    """The durable advisory-lock authority could not be reached."""


class WriterSurfaceUnknown(ValueError):
    """Prevent a new mutation path from bypassing the explicit writer catalog."""


def account_mode_advisory_key(account_mode: str = ACCOUNT_MODE) -> int:
    """Stable signed bigint key accepted by PostgreSQL advisory-lock functions."""
    if not account_mode.strip():
        raise ValueError("account_mode must be non-blank")
    digest = hashlib.sha256(account_mode.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def assert_known_writer_surface(writer_surface: str) -> None:
    if writer_surface not in MUTATION_WRITER_SURFACES:
        raise WriterSurfaceUnknown(
            f"KIS mock mutation writer is not catalogued: {writer_surface!r}"
        )


class AdvisoryLock(Protocol):
    async def try_acquire(self, key: int) -> bool: ...

    async def release(self, key: int) -> None: ...


class FileLock(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


class PidFileLock:
    """Non-blocking local lock carrying a diagnostic PID, never a lock authority."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def try_acquire(self) -> bool:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - Linux/macOS CI supports it
            raise WriterSingletonUnavailable(
                "fcntl file locking is unavailable"
            ) from exc
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class PostgresAdvisoryLock:
    """Hold a dedicated PostgreSQL connection for the lifetime of the writer."""

    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None

    async def try_acquire(self, key: int) -> bool:
        if self._connection is not None:
            raise RuntimeError("advisory lock instance is already acquired")
        from app.core import db

        connection = await db.engine.connect()
        try:
            result = await connection.execute(
                text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                {"key": key},
            )
            acquired = bool(result.scalar_one())
            if not acquired:
                await connection.close()
                return False
            self._connection = connection
            return True
        except BaseException:
            await connection.close()
            raise

    async def release(self, key: int) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                {"key": key},
            )
        finally:
            await connection.close()


class KISMockWriterLease:
    """Acquire both local visibility and authoritative DB singleton locks."""

    def __init__(
        self,
        *,
        writer_surface: str = "runner",
        account_mode: str = ACCOUNT_MODE,
        file_lock: FileLock | None = None,
        advisory_lock: AdvisoryLock | None = None,
    ) -> None:
        assert_known_writer_surface(writer_surface)
        self._writer_surface = writer_surface
        self._key = account_mode_advisory_key(account_mode)
        default_path = Path(tempfile.gettempdir()) / "auto_trader_kis_mock_writer.lock"
        self._file_lock = file_lock or PidFileLock(default_path)
        self._advisory_lock = advisory_lock or PostgresAdvisoryLock()
        self._acquired = False
        self._context_token: Token[bool] | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def acquire(self) -> None:
        """Acquire non-blocking locks; contention and DB failure both close the path."""
        if self._acquired:
            raise RuntimeError("writer lease is already acquired")
        try:
            file_acquired = self._file_lock.try_acquire()
        except WriterSingletonUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - file lock failure must not open writes
            raise WriterSingletonUnavailable("KIS mock PID lock unavailable") from exc
        if not file_acquired:
            raise WriterSingletonContended(
                "KIS mock writer singleton contended at local PID lock"
            )
        try:
            advisory_acquired = await self._advisory_lock.try_acquire(self._key)
        except Exception as exc:  # noqa: BLE001 - cross-process authority unavailable
            self._file_lock.release()
            raise WriterSingletonUnavailable(
                "KIS mock PostgreSQL advisory lock unavailable"
            ) from exc
        if not advisory_acquired:
            self._file_lock.release()
            raise WriterSingletonContended(
                "KIS mock writer singleton contended at PostgreSQL advisory lock"
            )
        self._acquired = True

    async def release(self) -> None:
        """Release in reverse order; an unlock error never leaves the file lock held."""
        if not self._acquired:
            return
        self._acquired = False
        try:
            await self._advisory_lock.release(self._key)
        finally:
            self._file_lock.release()

    async def __aenter__(self) -> KISMockWriterLease:
        await self.acquire()
        self._context_token = _ACTIVE_WRITER_LEASE.set(True)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        try:
            await self.release()
        finally:
            if self._context_token is not None:
                _ACTIVE_WRITER_LEASE.reset(self._context_token)
                self._context_token = None


def has_active_writer_lease() -> bool:
    """Whether this task already owns the advisory lease (retry-safe/reentrant)."""
    return _ACTIVE_WRITER_LEASE.get()


@asynccontextmanager
async def enforce_kis_mock_mutation_writer(
    *,
    enabled: bool,
    lease_factory: Callable[[], KISMockWriterLease] = KISMockWriterLease,
) -> AsyncIterator[None]:
    """Enforce writer cardinality for every KRX mock mutation boundary.

    The runner gate makes this dormant until KR-B2 explicitly arms the shell.
    Once armed, unscoped legacy/manual callers obtain the same one-call lease;
    runner-owned retries see the context and reuse their long-lived lease.
    """
    if not enabled or has_active_writer_lease():
        yield
        return
    lease = lease_factory()
    async with lease:
        token = _ACTIVE_WRITER_LEASE.set(True)
        try:
            yield
        finally:
            _ACTIVE_WRITER_LEASE.reset(token)


AdvisoryAcquire = Awaitable[bool]
