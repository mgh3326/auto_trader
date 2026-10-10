"""Context-scoped refusal of credential-backed live broker clients (#1257).

A closed-world MCP profile that must never use a live broker credential (the
h3-us-paper profile) runs each tool body inside :func:`broker_credentials_blocked`.
While that context is active, the KIS, Toss and authenticated Upbit clients
refuse before any token lookup or network send: constructing a KIS or Toss
client, ensuring a KIS token, dispatching a KIS request, sending a Toss request
or an authenticated Upbit request all raise :class:`BrokerCredentialsBlocked`.

Outside the context nothing changes: the check is one ContextVar read. Code
that already falls back on a broker failure (US quotes to Yahoo, US daily
candles to cached rows, the USD/KRW rate to open.er-api) degrades the same
way it does on a broker outage.

The context is a ContextVar, so it follows the awaiting task and tasks created
from it; a thread started with ``loop.run_in_executor`` does not inherit it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_BLOCKED_BY: ContextVar[str | None] = ContextVar(
    "broker_credentials_blocked_by", default=None
)


class BrokerCredentialsBlocked(RuntimeError):
    """A credential-backed broker client was reached inside a blocked context."""


@contextmanager
def broker_credentials_blocked(owner: str) -> Iterator[None]:
    """Refuse every credential-backed broker client for the enclosed calls."""
    token = _BLOCKED_BY.set(owner)
    try:
        yield
    finally:
        _BLOCKED_BY.reset(token)


def broker_credentials_blocked_by() -> str | None:
    """The owner that blocks broker credentials here, or ``None``."""
    return _BLOCKED_BY.get()


def assert_broker_credentials_allowed(client: str) -> None:
    """Raise when called inside :func:`broker_credentials_blocked`."""
    owner = _BLOCKED_BY.get()
    if owner is not None:
        raise BrokerCredentialsBlocked(
            f"{owner}: {client} uses live broker credentials and is blocked here"
        )


__all__ = [
    "BrokerCredentialsBlocked",
    "assert_broker_credentials_allowed",
    "broker_credentials_blocked",
    "broker_credentials_blocked_by",
]
