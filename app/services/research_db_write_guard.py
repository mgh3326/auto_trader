"""ROB-946 (H6) — research DB write guard.

ROB-946 §7 requires TWO independent conditions before any ROB-846 registry
write primitive (``register_experiment``/``record_trial``) fires:

  1. an explicit, default-off ROB-940 research-write opt-in;
  2. a typed/allowlisted non-production research DB target.

Neither may be satisfied by ``ENVIRONMENT != "production"``, a URL substring
check, or an empty/unknown/malformed/deceptive target — all of those are
explicitly banned justifications. This module is therefore pure and
dependency-injected: it never reads ``os.environ`` or opens a DB connection
itself. Callers resolve the opt-in flag and DB target from real ambient state
(env var, live session bind) and pass the resolved, typed values in; the guard
only judges those already-resolved facts against an explicit allowlist.

Boundary: this module must never import a broker/order/fill ledger — see
``tests/services/research/test_no_broker_import_guard.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ResearchDbTarget",
    "ResearchDbTargetRejected",
    "ResearchWriteDisabled",
    "ResearchWriteGuardError",
    "assert_research_write_authorized",
    "research_write_opt_in_enabled",
    "resolve_research_db_target",
]

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


class ResearchWriteGuardError(Exception):
    """Base error for the ROB-946 research-DB write guard."""


class ResearchWriteDisabled(ResearchWriteGuardError):
    """The explicit ROB-940 research-write opt-in is false or absent."""


class ResearchDbTargetRejected(ResearchWriteGuardError):
    """The resolved DB target is empty, unknown, or not on the explicit
    allowlist — never derived from '!= production' or a substring check."""


@dataclass(frozen=True)
class ResearchDbTarget:
    """A resolved (not caller-asserted) database identity.

    Deliberately holds only the bare database name — never a full DSN, host,
    or credentials — so no error message built from it can leak a secret.
    """

    database_name: str


def research_write_opt_in_enabled(raw_value: str | None) -> bool:
    """Pure truthy check for the ROB-940 research-write opt-in env var.

    Takes the raw string value directly (e.g. ``os.environ.get(...)``) rather
    than reading the environment itself, so this stays a pure function.
    """
    if raw_value is None:
        return False
    return raw_value.strip().lower() in _TRUTHY_VALUES


def resolve_research_db_target(session: object) -> ResearchDbTarget:
    """Resolve the ACTUAL bound database identity from a live session.

    Pure metadata inspection (``session.get_bind().url.database``) — no query,
    no I/O. This is a real fact taken from the engine's own bind, not a label
    a caller could assert falsely.
    """
    bind = session.get_bind()  # type: ignore[attr-defined]
    return ResearchDbTarget(database_name=bind.url.database or "")


def assert_research_write_authorized(
    *,
    opt_in_enabled: bool,
    target: ResearchDbTarget,
    allowlist: frozenset[str],
) -> None:
    """Fail closed unless BOTH conditions hold; raises before any DB write.

    * ``opt_in_enabled`` must be explicitly True.
    * ``target.database_name`` must be a non-empty EXACT match against
      ``allowlist`` — never a substring/prefix match, never "not production".
    """
    if not opt_in_enabled:
        raise ResearchWriteDisabled(
            "ROB-940 research-write opt-in is disabled; refusing to write"
        )
    name = target.database_name
    if not name or name not in allowlist:
        raise ResearchDbTargetRejected(
            "research DB target is not on the explicit non-production allowlist"
        )
