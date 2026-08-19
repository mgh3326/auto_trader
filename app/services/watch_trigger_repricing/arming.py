"""ROB-1286 §101차 ③ / r2 NEW BLOCKER 3 — what must be true before a tick runs.

r2's finding
------------
The r2 gate read ``getattr(spawner, "is_dry", False)``. A live-capable
stand-in that answered ``True`` walked straight past the durable-store
requirement:

    SELF_ATTESTED_DRY status=ok spawned=1 calls=1

So "nobody can arm a live spawner against a volatile store" was not a code
fact. The gate trusted a boolean the untrusted side supplied.

The fix: dryness is a *type*, not an answer
-------------------------------------------
:data:`DRY_SPAWNER_TYPES` is a closed set of classes this package itself
defines. An object is dry because it is one of those, not because it says
so. Everything else is live and must satisfy the full contract:

* it inherits :class:`~.live_contract.LiveSessionSpawner`, whose constructor
  already checked that its declared grant is exactly the proposal-only set;
* it can reconcile, so an ambiguous start becomes decided rather than a
  permanently unjudged fire;
* the claim store is durable, so dedup survives the flow run.

Membership is by **exact type**, not ``isinstance`` (ROB-1290)
-------------------------------------------------------------
r2's fix said a ``DrySessionSpawner`` subclass was harmless because it
inherits a ``spawn`` that starts nothing "unless the subclass overrides
it, at which point the subclass is a live spawner ... and is refused". The
second half was not true: ``isinstance`` accepted the subclass, so an
override went straight past the live contract and the durability rule.
Comparing ``type(spawner)`` against a closed set of classes this package
defines closes it -- and the closed set is what makes that safe, since the
package's own dry spawners are enumerated rather than inherited into.

What arming does not decide (r3)
--------------------------------
Everything here is about *which object* runs. None of it constrains what
that object's judge does once it runs, because the judge is in-process
Python. :mod:`.chain_spawner` states that limit exactly; do not read a
passing arming check as an approval boundary.
"""

from __future__ import annotations

from app.services.watch_trigger_repricing.live_contract import (
    LiveSessionSpawner,
    LiveSpawnerContractViolation,
    assert_live_spawner_contract,
)
from app.services.watch_trigger_repricing.spawn import (
    DrySessionSpawner,
    ScriptedDrySessionSpawner,
)

__all__ = [
    "DRY_SPAWNER_TYPES",
    "live_spawner_types",
    "ArmingRefused",
    "assert_arming_contract",
    "is_dry_spawner",
]

# Closed set. Membership is decided here, by exact type, never by the
# object and never by inheritance.
DRY_SPAWNER_TYPES: tuple[type, ...] = (DrySessionSpawner, ScriptedDrySessionSpawner)


def live_spawner_types() -> tuple[type, ...]:
    """The concrete live spawners this package will arm. Closed, by type.

    r3: satisfying :class:`~.live_contract.LiveSessionSpawner` was not enough.
    An external subclass could take a callable in *its own* constructor,
    override ``spawn``, declare a clean grant, and arm -- the base class no
    longer has a ``tool=`` argument, but a subclass can add one back. So
    arming names the exact classes it will run, the same way dryness does,
    and a subclass of an allowed type is *not* an allowed type.

    Imported lazily: ``chain_spawner`` imports the MCP registration surface,
    and importing that at module scope here would drag it into every
    consumer of :mod:`.arming`.
    """
    from app.services.watch_trigger_repricing.chain_spawner import ProposalChainSpawner

    return (ProposalChainSpawner,)


class ArmingRefused(RuntimeError):
    """The tick may not run in this configuration."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def is_dry_spawner(spawner: object) -> bool:
    """Dryness by exact type. Self-reporting and inheritance are both ignored.

    A subclass of a dry spawner is not dry: overriding ``spawn`` is all it
    takes to start doing real work, and inheriting a name must not buy an
    exemption from the live contract.
    """
    return type(spawner) in DRY_SPAWNER_TYPES


def assert_arming_contract(*, spawner: object, store: object) -> None:
    """Refuse to run unless the live path has proven what it must.

    Read the module docstring for what this does **not** buy: the judge a
    live spawner runs is in-process code, and no check here constrains it.
    """
    if is_dry_spawner(spawner):
        return

    # Contract first, so a duck-typed or grant-less object still gets the
    # specific diagnostic that names what it is missing.
    try:
        assert_live_spawner_contract(spawner)
    except LiveSpawnerContractViolation as exc:
        raise ArmingRefused(
            "live_spawner_contract_unmet",
            f"refusing to run {type(spawner).__name__}: {exc}",
        ) from exc

    if not isinstance(spawner, LiveSessionSpawner):  # pragma: no cover - defensive
        raise ArmingRefused(
            "live_spawner_contract_unmet",
            f"{type(spawner).__name__} is not a LiveSessionSpawner",
        )

    # Then the closed set. Satisfying the contract is not the same as being
    # a spawner this package wrote: a well-formed external subclass passes
    # every check above and can still put anything it likes in ``spawn``.
    if type(spawner) not in live_spawner_types():
        raise ArmingRefused(
            "unlisted_live_spawner",
            f"refusing to run {type(spawner).__name__}: arming accepts only the "
            "concrete live spawners this package defines, by exact type. A "
            "subclass is not one of them -- it can reintroduce in its own "
            "constructor the injected callable the base class removed",
        )

    if not getattr(store, "is_durable", False):
        raise ArmingRefused(
            "non_durable_claim_store",
            f"refusing to run live spawner {type(spawner).__name__} against a "
            "non-durable claim store -- dedup would not survive the flow run, "
            "so one fire could become two sessions",
        )
