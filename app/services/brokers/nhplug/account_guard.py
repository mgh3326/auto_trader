"""Fail-closed mock-account allowlist built from the broker account response."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockResponseError,
)

# The vendor's account discriminator is a second, independent safety boundary.
ALLOWED_MOCK_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset({"03"})
DENIED_LIVE_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset({"01", "02"})
_ACCOUNT_LIST_KEY: Final[str] = "Output_0"


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """Minimal account identity needed to establish the mock allowlist."""

    act_no: str = field(repr=False)
    acct_type: str


@dataclass(frozen=True, slots=True)
class MockAccountAllowlist:
    """Verified account numbers, never rendered by normal diagnostics."""

    configured_account_no: str = field(repr=False)
    allowed_account_numbers: frozenset[str] = field(repr=False)
    account_type_counts: tuple[tuple[str, int], ...]

    @property
    def allowed_count(self) -> int:
        return len(self.allowed_account_numbers)

    @classmethod
    def from_acctinfo_response(
        cls, *, payload: dict[str, Any], configured_account_no: str
    ) -> MockAccountAllowlist:
        """Parse the documented account envelope and bind the configured account.

        Unknown response shapes, blank account identifiers, and absent `03`
        accounts are all failures.  An environment-supplied account is not
        trusted until it appears in the broker response with the exact allowed
        account type.
        """

        if (
            not isinstance(configured_account_no, str)
            or not configured_account_no.strip()
        ):
            raise NHPlugMockAccountRejected(
                "NHPLUG_MOCK_ACCOUNT_NO must be non-empty and broker-verified"
            )
        rows = payload.get(_ACCOUNT_LIST_KEY)
        if not isinstance(rows, list):
            raise NHPlugMockResponseError(
                "NHPLUG account response is missing the documented account list"
            )

        records: list[AccountRecord] = []
        counts: dict[str, int] = {}
        account_types_by_number: dict[str, set[str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise NHPlugMockResponseError(
                    "NHPLUG account list contains a non-object row"
                )
            raw_account = row.get("acct_no")
            raw_type = row.get("acct_type")
            if not isinstance(raw_account, str) or not raw_account.strip():
                raise NHPlugMockResponseError("NHPLUG account row has no valid acct_no")
            if not isinstance(raw_type, str) or not raw_type.strip():
                raise NHPlugMockResponseError(
                    "NHPLUG account row has no valid acct_type"
                )
            account_number = raw_account.strip()
            account_type = raw_type.strip()
            counts[account_type] = counts.get(account_type, 0) + 1
            account_types_by_number.setdefault(account_number, set()).add(account_type)
            records.append(AccountRecord(act_no=account_number, acct_type=account_type))

        # A broker response that labels one account with more than one type is
        # ambiguous.  In particular, accepting a number appearing as both 01
        # and 03 would let a live account inherit the mock allowlist.  Reject
        # the whole response before deriving any sendable account set.
        if any(
            len(account_types) > 1 for account_types in account_types_by_number.values()
        ):
            raise NHPlugMockAccountRejected(
                "NHPLUG account response assigns conflicting account types"
            )

        allowed: set[str] = set()
        for record in records:
            if record.acct_type in DENIED_LIVE_ACCOUNT_TYPES:
                continue
            if record.acct_type in ALLOWED_MOCK_ACCOUNT_TYPES:
                allowed.add(record.act_no)
        result = cls(
            configured_account_no=configured_account_no.strip(),
            allowed_account_numbers=frozenset(allowed),
            account_type_counts=tuple(sorted(counts.items())),
        )
        result.assert_allowed(result.configured_account_no)
        return result

    def assert_allowed(self, act_no: str) -> None:
        """Refuse every account except one broker-verified `acct_type=03` value."""

        if (
            not isinstance(act_no, str)
            or act_no.strip() not in self.allowed_account_numbers
        ):
            raise NHPlugMockAccountRejected(
                "configured account is not in the broker-verified acct_type=03 allowlist"
            )
