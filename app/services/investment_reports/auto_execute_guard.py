"""ROB-402 — auto-execute account guard. live is permanently blocked."""

from __future__ import annotations

_LIVE_ACCOUNT_MODES = frozenset({"kis_live", "upbit_live"})
_AUTO_EXECUTE_ALLOWED = frozenset({"kis_mock"})  # kiwoom_mock = ROB-399 follow-up


class AutoExecuteLiveBlocked(Exception):
    """auto_execute_mock attempted against a live account — never allowed."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is permanently blocked for live account "
            f"'{account_mode}'"
        )
        self.account_mode = account_mode


class AutoExecuteUnsupported(Exception):
    """auto_execute_mock against a non-live, non-kis_mock account (not yet wired)."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is not supported for account '{account_mode}'"
        )
        self.account_mode = account_mode


def assert_auto_execute_account_allowed(action_mode: str, account_mode: str) -> None:
    if action_mode != "auto_execute_mock":
        return
    if account_mode in _LIVE_ACCOUNT_MODES:
        raise AutoExecuteLiveBlocked(account_mode)
    if account_mode not in _AUTO_EXECUTE_ALLOWED:
        raise AutoExecuteUnsupported(account_mode)
