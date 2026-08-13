"""ORDERING-local lease and append-only evidence primitives."""

from __future__ import annotations

import pytest

from scripts.b0x.kr import kiwoom_ordering as ordering_support

pytestmark = pytest.mark.unit


def test_account_writer_lease_is_account_keyed_and_nonblocking(tmp_path) -> None:  # noqa: ANN001
    first = ordering_support.AccountWriterLease(
        root=tmp_path,
        lane="kiwoom_mock",
        account_fingerprint="sha256:account-a",
    )
    same_account = ordering_support.AccountWriterLease(
        root=tmp_path,
        lane="kiwoom_mock",
        account_fingerprint="sha256:account-a",
    )
    other_account = ordering_support.AccountWriterLease(
        root=tmp_path,
        lane="kiwoom_mock",
        account_fingerprint="sha256:account-b",
    )
    first.acquire()
    try:
        with pytest.raises(ordering_support.AccountWriterLeaseContended):
            same_account.acquire()
        other_account.acquire()
        other_account.assert_held()
    finally:
        other_account.release()
        first.release()


def test_ordering_event_journal_rejects_malformed_prior_evidence(tmp_path) -> None:  # noqa: ANN001
    journal = ordering_support.OrderingEventJournal.for_lane(
        root=tmp_path, lane="kiwoom_mock"
    )
    journal.path.parent.mkdir(parents=True)
    journal.path.write_text('{"at":"2026-08-12T03:00:00+00:00"}\n', encoding="utf-8")

    with pytest.raises(ordering_support.OrderingJournalUnreadable):
        journal.read_all()
