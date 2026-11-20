"""The account of the request a pre-send hook is about to release (hk 3795 T4c, I15).

A pre-send hook takes no arguments, so a broker transport that knows which account
its prepared request carries publishes it here for the duration of the hook call.
The mandate send boundary derives its account reference from this value when it is
set; when it is not (a transport that has not been taught), it falls back to the
account the settings name. Dependency-free: the transport imports it, and the
mandate boundary reads it.
"""

from __future__ import annotations

from contextvars import ContextVar

#: The account identifier of the request about to be sent, or ``None`` if unknown.
PRE_SEND_ACCOUNT: ContextVar[str | None] = ContextVar("pre_send_account", default=None)
