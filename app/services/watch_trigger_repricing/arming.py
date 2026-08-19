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
    "ArmingRefused",
    "assert_arming_contract",
    "is_dry_spawner",
]

# Closed set. Membership is decided here, by exact type, never by the
# object and never by inheritance.
DRY_SPAWNER_TYPES: tuple[type, ...] = (DrySessionSpawner, ScriptedDrySessionSpawner)


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
    """Refuse to run unless the live path has proven what it must."""
    if is_dry_spawner(spawner):
        return

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

    if not getattr(store, "is_durable", False):
        raise ArmingRefused(
            "non_durable_claim_store",
            f"refusing to run live spawner {type(spawner).__name__} against a "
            "non-durable claim store -- dedup would not survive the flow run, "
            "so one fire could become two sessions",
        )
