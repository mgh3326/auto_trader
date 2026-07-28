#!/usr/bin/env python3
"""ROB-1060 H2 — ROB-846 registry registration CLI: ``plan`` / ``register``.

Mirrors ``research/nautilus_scalping/run_rob944_campaign.py``'s ``--plan`` /
``--run`` boundary exactly:

* ``plan`` is PURE — no network, no DB connection, no environment mutation.
  It builds the full 16-config identity plan (using ``artifact``/``configs``/
  ``identity``/``params``, all pure) and prints it as stable JSON. It never
  imports ``app.*`` at module scope OR inside its own function body.
* ``register`` is the empirical write path, gated in this order, fail-closed
  on the first unmet condition:

    1. the default-off ``ALPACA_TRACK_SEAL_REGISTER_WRITE_OPT_IN`` env var
       must be explicitly truthy;
    2. ``--confirm`` must be passed (one explicit operator execution, AC4);
    3. the ROB-946 research-DB write guard
       (``app.services.research_db_write_guard``) must positively authorize
       the resolved (host, database) target — never "not production", never
       a bare database-name check;
    4. only then: fetch each H2 parent's stored typed-AST manifest, decode and
       integrity-check it, assert every sealed component except ``code`` is
       preserved, and register all 16 H3-code supersessions via
       ``app.services.strategy_experiment_registry.register_experiment``
       (idempotent by canonical identity — re-running ``register`` is safe).

Every ``app.*``/DB import is DEFERRED inside ``_cmd_register``'s body, never
at module scope — this is the boundary
``test_registry_cli_import_guard.py`` enforces (module-scope-only, mirroring
``research/nautilus_scalping/tests/test_rob944_cli_import_guard.py``). This
is also the answer to the ROB-1060 "is the ROB-846 registry reachable"
design question: yes, via a CLI-boundary module that lives OUTSIDE H1's
``research/alpaca_track/`` package (whose own import guard forbids ``app``
anywhere in that tree) — see ``conftest.py``'s module docstring.

No broker mutation, no scheduler/TaskIQ registration anywhere in this file.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any

import artifact as art
import identity as ident

from research_contracts.canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)

__all__ = [
    "PlanComponentDivergenceError",
    "SemanticHashDriftError",
    "SupersessionGuardUnwiredError",
    "build_registration_plan",
    "main",
]

ENV_WRITE_OPT_IN = "ALPACA_TRACK_SEAL_REGISTER_WRITE_OPT_IN"


class SemanticHashDriftError(RuntimeError):
    """``build_registration_plan()``'s freshly-built artifact's semantic hash
    does not match the pinned ``artifact.SEALED_ARTIFACT_SEMANTIC_HASH``
    (ROB-1060 H2-lock adversarial-verification Finding 4, 2026-07-26).

    Before this check existed, the seal was a TEST-TIME lock only:
    ``build_registration_plan()`` computed and emitted ``semantic_hash`` in
    its output but never compared it to the pinned constant, so a drifted
    ``artifact.py``/``identity.py``/``configs.py``/``params.py`` (e.g. an
    edit that passed a code review but silently moved the digest) would
    register without complaint at RUNTIME -- only the test suite would
    notice, and the test suite is not wired into CI (Finding 1). This is the
    runtime half of the lock: fail closed here too, before any identity
    reaches ``register_experiment``."""


class PlanComponentDivergenceError(RuntimeError):
    """A spec's ``components`` in the plan returned by
    ``build_registration_plan()`` diverge from the sealed artifact's own
    ``identity_components`` for the same ``config_id`` (ROB-1060 H2-lock
    adversarial-verification NEW-1, 2026-07-26).

    Finding 4's ``SemanticHashDriftError`` proves the pinned digest binds the
    ARTIFACT (``sealed.semantic_hash()``). It does NOT prove the digest binds
    the PLAN this function derives from that artifact and returns -- an
    independent adversarial pass demonstrated that a uniform mutation applied
    to every spec's ``components`` AFTER the digest check (e.g. forcing
    ``cost.primary`` to a relaxed scenario across all 16 configs) passes the
    Finding-4 gate untouched, keeps
    ``validate_same_family_components_are_identical`` satisfied (a uniform
    mutation stays internally consistent within each family), and the fully
    green test suite: the correct pinned digest ships attached to relaxed
    components, and that exact dict is what ``_cmd_register`` feeds into
    ``StrategyExperimentIdentity``. This check closes that gap: immediately
    before returning, every spec's ``components`` must equal
    ``sealed.to_dict()["identity_components"][config_id]`` -- a fresh,
    independent recomputation from the same (unmutated) sealed artifact --
    or registration is refused."""


def build_registration_plan() -> dict[str, Any]:
    """Build the pure H3-code supersession plan for all 16 H2 identities.

    The pinned H2 artifact is reconstructed and verified first.  Its exact
    canonical identity derives each immutable parent experiment ID.  A child
    copy then replaces only ``code`` with the byte-derived H3 implementation
    component and is checked by the same preservation authority used later
    against the parent's stored DB manifest.

    Never touches DB/network — safe to call from ``plan`` or from tests.

    Fails closed (``SemanticHashDriftError``) if the freshly-built artifact's
    semantic hash does not match the pinned
    ``artifact.SEALED_ARTIFACT_SEMANTIC_HASH`` -- this is the ONLY runtime
    entry point in this package that consumes the sealed artifact
    (``_cmd_plan``/``_cmd_register`` both call it), so gating here covers
    both the read-only ``plan`` output and the ``register`` write path."""
    sealed = art.build_sealed_artifact()
    semantic_hash = sealed.semantic_hash()
    if semantic_hash != art.SEALED_ARTIFACT_SEMANTIC_HASH:
        raise SemanticHashDriftError(
            f"sealed artifact semantic hash {semantic_hash!r} does not match "
            f"the pinned {art.SEALED_ARTIFACT_SEMANTIC_HASH!r} -- refusing to "
            "build a registration plan (or register) from a drifted artifact"
        )
    seal = sealed.params
    per_config_components = []
    specs = []
    for config in sealed.configs:
        components = ident.build_components_for_config(config, seal)
        per_config_components.append((config, components))
        specs.append(
            {
                "config_id": config.config_id,
                "family": config.family,
                "strategy_key": components["strategy"]["strategy_key"],
                "strategy_version": components["strategy"]["strategy_version"],
                "components": components,
            }
        )
    ident.validate_same_family_components_are_identical(per_config_components)

    # ROB-1060 H2-lock adversarial-verification NEW-1 (2026-07-26): bind the
    # digest to the PLAN, not merely to the artifact it was derived from.
    # `sealed.to_dict()["identity_components"]` is a fresh, independent
    # recomputation of every config's 11-component identity from the same
    # (unmutated) `sealed` object built above -- if anything mutated `specs`
    # in between (the exact adversarial scenario this closes), this
    # recomputation will not reflect that mutation and the comparison below
    # will diverge.
    fresh_identity_components = sealed.to_dict()["identity_components"]
    for spec in specs:
        config_id = spec["config_id"]
        expected_components = fresh_identity_components[config_id]
        actual_components = spec["components"]
        if actual_components == expected_components:
            continue
        diverged_names = [
            name
            for name in expected_components
            if actual_components.get(name) != expected_components[name]
        ]
        raise PlanComponentDivergenceError(
            f"config {config_id!r}: component(s) {diverged_names!r} in the "
            "returned registration plan diverge from the sealed artifact's "
            "own identity_components -- the pinned semantic hash binds the "
            "ARTIFACT, not any plan derived from it; refusing to return (or "
            "register) a plan whose components were mutated after the "
            "digest check"
        )

    h3_code_by_family = {
        family: ident.build_h3_implementation_code_component(family)
        for family in ("AP-A1", "AP-A2")
    }
    supersession_specs = []
    for spec in specs:
        parent_components = spec["components"]
        child_components = copy.deepcopy(parent_components)
        child_components["code"] = copy.deepcopy(h3_code_by_family[spec["family"]])
        ident.assert_supersession_preserves_sealed_components(
            child_components=child_components,
            parent_components=parent_components,
        )
        parent_experiment_id = derive_experiment_id(
            spec["strategy_key"],
            spec["strategy_version"],
            compute_identity_hashes(parent_components),
        )
        supersession_specs.append(
            {
                **spec,
                "supersedes_experiment_id": parent_experiment_id,
                "components": child_components,
            }
        )

    return {
        "semantic_hash": semantic_hash,
        "config_count": len(sealed.configs),
        "plan_kind": "h3_real_implementation_supersession",
        "h3_source_commit": "5c09c2e7a",
        "specs": supersession_specs,
    }


def _cmd_plan(_args: argparse.Namespace) -> int:
    plan = build_registration_plan()
    print(json.dumps(plan, indent=2, sort_keys=True, default=str))
    return 0


class SupersessionGuardUnwiredError(RuntimeError):
    """A supersession was requested without both comparable component sets.

    ROB-1069 wires the guard for the H3 path. This error remains the fail-closed
    boundary for any future call site that supplies a parent ID without also
    fetching and decoding that parent's stored manifest.
    """


def _assert_supersession_guard_wired_before_use(
    supersedes_experiment_id: str | None,
    *,
    child_components: dict[str, Any] | None = None,
    parent_components: dict[str, Any] | None = None,
) -> None:
    """Call the preservation authority, or fail closed when inputs are unwired.

    ``None`` remains a no-op for legitimate fresh-registration consumers.
    """
    if supersedes_experiment_id is None:
        return
    if child_components is None or parent_components is None:
        raise SupersessionGuardUnwiredError(
            f"supersedes_experiment_id={supersedes_experiment_id!r} was about "
            "to be registered, but "
            "identity.assert_supersession_preserves_sealed_components has "
            "not been wired into _cmd_register to check it against its "
            "parent's sealed components -- wiring that component-"
            "preservation guard is a HARD PREREQUISITE for any supersession "
            "(see the Finding-5 note in _cmd_register and that function's "
            "own docstring); refusing to register a superseding experiment "
            "without it"
        )
    ident.assert_supersession_preserves_sealed_components(
        child_components=child_components,
        parent_components=parent_components,
    )


async def _register_supersession_plan(
    *,
    session: Any,
    plan: dict[str, Any],
    registry: Any,
    identity_type: Any,
) -> list[dict[str, str]]:
    """Execute the guarded per-spec portion of ``_cmd_register``.

    Dependencies stay injected so this exact production subpath can be tested
    against both in-memory no-write ports and the disposable integration DB.
    """
    registered = []
    for spec in plan["specs"]:
        supersedes_experiment_id = spec["supersedes_experiment_id"]
        parent_components = await registry.get_experiment_identity_components(
            session, supersedes_experiment_id
        )
        _assert_supersession_guard_wired_before_use(
            supersedes_experiment_id,
            child_components=spec["components"],
            parent_components=parent_components,
        )
        identity = identity_type(
            strategy_key=spec["strategy_key"],
            strategy_version=spec["strategy_version"],
            hypothesis=(
                f"ROB-1069 H3 implementation supersession: {spec['config_id']}"
            ),
            supersedes_experiment_id=supersedes_experiment_id,
            **spec["components"],
        )
        row = await registry.register_experiment(session, identity)
        registered.append(
            {"config_id": spec["config_id"], "experiment_id": row.experiment_id}
        )
    return registered


def _cmd_register(args: argparse.Namespace) -> int:
    # Deliberate two-stage deferred import (stricter than a single deferred
    # block): `app.services.research_db_write_guard` imports nothing but the
    # stdlib itself, so importing ONLY it first is safe even with no .env
    # configured. `app.core.db` (below), by contrast, eagerly constructs and
    # validates the full pydantic Settings object (KIS/Upbit/DB/secret-key
    # env vars) — in a bare environment that import alone raises an
    # unrelated ValidationError. A default-off gate must fail closed
    # *cleanly* in exactly that bare environment, so the opt-in + --confirm
    # checks run first, using only the lightweight import, before the
    # heavier `app.core.db`/registry imports are attempted at all.
    from app.services.research_db_write_guard import research_write_opt_in_enabled

    opt_in_enabled = research_write_opt_in_enabled(os.environ.get(ENV_WRITE_OPT_IN))
    if not opt_in_enabled:
        print(
            f"registration refused: {ENV_WRITE_OPT_IN} is not enabled (default-off)",
            file=sys.stderr,
        )
        return 2
    if not args.confirm:
        print("registration refused: --confirm was not passed", file=sys.stderr)
        return 2

    import asyncio

    from app.core.db import AsyncSessionLocal
    from app.schemas.research_backtest import StrategyExperimentIdentity
    from app.services import strategy_experiment_registry as registry
    from app.services.research_db_write_guard import (
        assert_research_write_authorized,
        default_research_db_policy,
        resolve_research_db_target,
    )

    plan = build_registration_plan()

    async def _do_register() -> int:
        async with AsyncSessionLocal() as session:
            target = resolve_research_db_target(session)
            policy = default_research_db_policy()
            try:
                assert_research_write_authorized(
                    opt_in_enabled=opt_in_enabled, target=target, policy=policy
                )
            except Exception as exc:  # noqa: BLE001 -- fail-closed, report, exit
                print(f"registration refused: {exc}", file=sys.stderr)
                return 3

            registered = await _register_supersession_plan(
                session=session,
                plan=plan,
                registry=registry,
                identity_type=StrategyExperimentIdentity,
            )
            await session.commit()
            print(json.dumps({"registered": registered}, indent=2, sort_keys=True))
            return 0

    return asyncio.run(_do_register())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ROB-1060 H2 — ROB-846 registry seal plan/registration"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="pure, no DB/network")
    plan_parser.set_defaults(func=_cmd_plan)

    register_parser = sub.add_parser(
        "register", help="default-off, requires env opt-in + --confirm"
    )
    register_parser.add_argument("--confirm", action="store_true")
    register_parser.set_defaults(func=_cmd_register)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
