from __future__ import annotations

from pathlib import Path

import pytest

from app.services.kis_mock_runner.singleton import (
    MUTATION_WRITER_SURFACES,
    KISMockWriterLease,
    PidFileLock,
    WriterSingletonContended,
    WriterSingletonUnavailable,
    account_mode_advisory_key,
    enforce_kis_mock_mutation_writer,
    has_active_writer_lease,
)


class SharedAdvisoryLock:
    def __init__(self) -> None:
        self.held = False
        self.keys: list[int] = []

    async def try_acquire(self, key: int) -> bool:
        self.keys.append(key)
        if self.held:
            return False
        self.held = True
        return True

    async def release(self, key: int) -> None:
        self.keys.append(key)
        self.held = False


class UnavailableAdvisoryLock:
    async def try_acquire(self, key: int) -> bool:
        del key
        raise RuntimeError("database unavailable")

    async def release(self, key: int) -> None:
        del key


class FakeLease:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeLease:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.exited += 1


@pytest.mark.asyncio
async def test_db_advisory_lock_contention_fails_closed(tmp_path: Path) -> None:
    advisory = SharedAdvisoryLock()
    first = KISMockWriterLease(
        file_lock=PidFileLock(tmp_path / "first.lock"), advisory_lock=advisory
    )
    second = KISMockWriterLease(
        file_lock=PidFileLock(tmp_path / "second.lock"), advisory_lock=advisory
    )
    await first.acquire()
    with pytest.raises(WriterSingletonContended, match="PostgreSQL advisory"):
        await second.acquire()
    assert second.acquired is False
    await first.release()
    await second.acquire()
    await second.release()
    assert all(key == account_mode_advisory_key("kis_mock") for key in advisory.keys)


@pytest.mark.asyncio
async def test_local_pid_file_lock_contention_fails_closed(tmp_path: Path) -> None:
    shared_path = tmp_path / "kis-mock.lock"
    first = KISMockWriterLease(
        file_lock=PidFileLock(shared_path), advisory_lock=SharedAdvisoryLock()
    )
    second = KISMockWriterLease(
        file_lock=PidFileLock(shared_path), advisory_lock=SharedAdvisoryLock()
    )
    await first.acquire()
    with pytest.raises(WriterSingletonContended, match="local PID lock"):
        await second.acquire()
    await first.release()


@pytest.mark.asyncio
async def test_db_lock_failure_releases_local_file_lock(tmp_path: Path) -> None:
    path = tmp_path / "recoverable.lock"
    unavailable = KISMockWriterLease(
        file_lock=PidFileLock(path), advisory_lock=UnavailableAdvisoryLock()
    )
    with pytest.raises(WriterSingletonUnavailable, match="advisory lock unavailable"):
        await unavailable.acquire()
    recovered = KISMockWriterLease(
        file_lock=PidFileLock(path), advisory_lock=SharedAdvisoryLock()
    )
    await recovered.acquire()
    await recovered.release()


def test_account_mode_key_is_stable_and_signed_bigint() -> None:
    key = account_mode_advisory_key("kis_mock")
    assert key == account_mode_advisory_key("kis_mock")
    assert -(2**63) <= key < 2**63


@pytest.mark.asyncio
async def test_armed_mutation_scope_acquires_once_and_marks_reentrant_context() -> None:
    lease = FakeLease()
    async with enforce_kis_mock_mutation_writer(
        enabled=True,
        lease_factory=lambda: lease,  # type: ignore[arg-type]
    ):
        assert has_active_writer_lease() is True
        async with enforce_kis_mock_mutation_writer(
            enabled=True, lease_factory=lambda: (_ for _ in ()).throw(AssertionError())
        ):
            assert has_active_writer_lease() is True
    assert has_active_writer_lease() is False
    assert lease.entered == lease.exited == 1


def test_writer_catalog_enumerates_all_approved_kis_mock_surfaces() -> None:
    assert MUTATION_WRITER_SURFACES == {
        "runner",
        "watch_auto_execute",
        "smoke_cli",
        "manual_mcp_mutation",
    }
