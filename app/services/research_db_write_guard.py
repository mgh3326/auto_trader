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
(env var, live session bind) and pass the resolved, typed values in.

R1 remediation (Critical 1, ``strategy-verify-rob946-r1-20260717-170647.md``):
a bare database NAME cannot distinguish a disposable local fixture from a
production cluster that happens to share the same database name (``test_db``
is an extremely common name on staging/prod replicas). ``ResearchDbTarget`` now
binds ``host`` AND ``database_name`` together — both resolved from the real
session bind, never a caller-asserted label — and authorization is a positive
EXACT match of that (host, database) pair against a ``ResearchDbPolicy``: a
closed, validated, immutable set of KNOWN disposable research targets, not a
bag of raw strings a caller can self-issue at the call site. A single string
(a database name, an ``ENVIRONMENT`` value, a URL substring) can never satisfy
this by itself — the caller must additionally supply the correct real host,
which is not something a careless or malicious caller controls (it comes from
the actual bound engine, not their assertion). This is not a cryptographic
capability boundary (Python cannot enforce that), but it removes the specific
reproduced failure: a production host is rejected even when its database
happens to be named identically to the disposable local one.

Boundary: this module must never import a broker/order/fill ledger — see
``tests/services/research/test_no_broker_import_guard.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ResearchDbPolicy",
    "ResearchDbTarget",
    "ResearchDbTargetRejected",
    "ResearchWriteDisabled",
    "ResearchWriteGuardError",
    "assert_research_write_authorized",
    "default_research_db_policy",
    "research_write_opt_in_enabled",
    "resolve_research_db_target",
]

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})

# The one known-disposable local research target this repo's test suite runs
# against (see tests/conftest.py `_ensure_test_env` — DATABASE_URL is
# force-overwritten to this exact host+database for every test run). This is
# NOT consulted automatically by the guard; it exists only as an explicit,
# reviewed convenience for real (non-test) call sites via
# `default_research_db_policy()`. Every caller — test or real — must still
# pass a policy object explicitly; nothing here is read from ambient state.
_LOCAL_TEST_DB_HOST = "localhost"
_LOCAL_TEST_DB_NAME = "test_db"


class ResearchWriteGuardError(Exception):
    """Base error for the ROB-946 research-DB write guard."""


class ResearchWriteDisabled(ResearchWriteGuardError):
    """The explicit ROB-940 research-write opt-in is false or absent."""


class ResearchDbTargetRejected(ResearchWriteGuardError):
    """The resolved (host, database) target is malformed or not a positive
    match against the authority-owned policy — never derived from
    '!= production', a substring check, or a bare database name alone."""


@dataclass(frozen=True)
class ResearchDbTarget:
    """A resolved (not caller-asserted) (host, database) identity.

    Both fields are required and normalized (stripped, lowercased) so that
    casing/whitespace cannot create a false mismatch OR a false match. Holds
    no port/driver/credentials — never a full DSN — so no error message built
    from it can leak a secret. A target with either field empty is malformed
    and can never authorize (see ``__post_init__``).
    """

    host: str
    database_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", self.host.strip().lower())
        object.__setattr__(self, "database_name", self.database_name.strip())


@dataclass(frozen=True)
class ResearchDbPolicy:
    """An authority-owned, closed set of positively-identified disposable
    research DB targets.

    Deliberately NOT a bag of raw strings (e.g. ``frozenset({"test_db"})``):
    every entry is a full ``ResearchDbTarget`` (host AND database bound
    together), and the ONLY way to build one is :meth:`of`, which rejects an
    empty policy and any malformed (empty-field) target. A caller who only
    knows a database name cannot construct an authorizing policy without also
    naming a specific host — and the guard compares that policy against the
    REAL resolved bind, which the caller does not control.
    """

    allowed_targets: frozenset[ResearchDbTarget]

    @classmethod
    def of(cls, *targets: ResearchDbTarget) -> ResearchDbPolicy:
        if not targets:
            raise ValueError(
                "a ResearchDbPolicy must name at least one disposable research target"
            )
        for target in targets:
            if not target.host or not target.database_name:
                raise ValueError(
                    f"malformed policy target (host={target.host!r}, "
                    f"database_name={target.database_name!r}) — both fields "
                    "are required"
                )
        return cls(allowed_targets=frozenset(targets))

    def authorizes(self, target: ResearchDbTarget) -> bool:
        return (
            bool(target.host)
            and bool(target.database_name)
            and target in self.allowed_targets
        )


def default_research_db_policy() -> ResearchDbPolicy:
    """The one reviewed, known-disposable local research target.

    A convenience for real (non-test) call sites — NOT read automatically by
    the guard and NOT a fallback; every caller still passes a policy
    explicitly.
    """
    return ResearchDbPolicy.of(
        ResearchDbTarget(host=_LOCAL_TEST_DB_HOST, database_name=_LOCAL_TEST_DB_NAME)
    )


def research_write_opt_in_enabled(raw_value: str | None) -> bool:
    """Pure truthy check for the ROB-940 research-write opt-in env var.

    Takes the raw string value directly (e.g. ``os.environ.get(...)``) rather
    than reading the environment itself, so this stays a pure function.
    """
    if raw_value is None:
        return False
    return raw_value.strip().lower() in _TRUTHY_VALUES


def resolve_research_db_target(session: object) -> ResearchDbTarget:
    """Resolve the ACTUAL bound (host, database) identity from a live session.

    Pure metadata inspection (``session.get_bind().url``) — no query, no I/O.
    These are real facts taken from the engine's own bind, not a label a
    caller could assert falsely.
    """
    bind = session.get_bind()  # type: ignore[attr-defined]
    url = bind.url
    return ResearchDbTarget(host=url.host or "", database_name=url.database or "")


def assert_research_write_authorized(
    *,
    opt_in_enabled: bool,
    target: ResearchDbTarget,
    policy: ResearchDbPolicy,
) -> None:
    """Fail closed unless BOTH conditions hold; raises before any DB write.

    * ``opt_in_enabled`` must be explicitly True.
    * ``target`` (host AND database, both non-empty) must be a positive EXACT
      match against ``policy`` — never a substring/prefix match, never a bare
      database-name check, never "not production".
    """
    if not opt_in_enabled:
        raise ResearchWriteDisabled(
            "ROB-940 research-write opt-in is disabled; refusing to write"
        )
    if not policy.authorizes(target):
        raise ResearchDbTargetRejected(
            "research DB target (host, database) is not a positive match "
            "against the authority-owned disposable-target policy"
        )
