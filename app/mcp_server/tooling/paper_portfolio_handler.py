"""Paper trading portfolio handler for MCP tools.

Keeps paper-specific collection/translation logic isolated so that the live
broker tooling files (portfolio_holdings.py, portfolio_cash.py) only need a
single delegation point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaperAccountSelector:
    """Resolved selector for paper account queries.

    account_name is None when the caller passed the bare "paper" token, which
    means "all active paper accounts".
    """

    account_name: str | None


def _strip(value: str | None) -> str:
    return (value or "").strip()


def _split_paper_token(account: str | None) -> tuple[str, str] | None:
    """Return (head_lower, raw_name) if account is a paper token, else None.

    Tolerates whitespace around the ":" delimiter.
    """
    token = _strip(account)
    if not token:
        return None
    head, sep, raw_name = token.partition(":")
    head_lower = head.strip().lower()
    if head_lower != "paper":
        return None
    if not sep:
        return ("paper", "")
    return ("paper", raw_name)


def is_paper_account_token(account: str | None) -> bool:
    return _split_paper_token(account) is not None


def parse_paper_account_token(account: str | None) -> PaperAccountSelector:
    parts = _split_paper_token(account)
    if parts is None:
        raise ValueError(f"not a paper account token: {account!r}")

    _, raw_name = parts
    name = raw_name.strip()
    return PaperAccountSelector(account_name=name or None)


__all__ = [
    "PaperAccountSelector",
    "is_paper_account_token",
    "parse_paper_account_token",
]
