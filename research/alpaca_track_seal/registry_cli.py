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
    4. only then: register all 16 configs via
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
import json
import os
import sys
from typing import Any

import artifact as art
import identity as ident

__all__ = ["SemanticHashDriftError", "build_registration_plan", "main"]

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


def build_registration_plan() -> dict[str, Any]:
    """The full, PURE registration plan: semantic hash + per-config identity
    components, ready to feed an app-side ``StrategyExperimentIdentity``.
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
    return {
        "semantic_hash": semantic_hash,
        "config_count": len(sealed.configs),
        "specs": specs,
    }


def _cmd_plan(_args: argparse.Namespace) -> int:
    plan = build_registration_plan()
    print(json.dumps(plan, indent=2, sort_keys=True, default=str))
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    # ROB-1060 H2-lock adversarial-verification Finding 5 (2026-07-26,
    # DETERMINATION -- genuinely blocked, left UNWIRED): every identity built
    # below has `supersedes_experiment_id` unset -- this command registers
    # 16 FRESH identities, never a supersession, so
    # `identity.assert_supersession_preserves_sealed_components` has no
    # in-process parent to compare against here. Wiring it for a REAL future
    # supersession (H3's real-implementation `code` superseding one of these
    # rows) would require fetching the parent's stored `manifest` from
    # `research.strategy_experiments` (an `app.*`/DB read) and reconstructing
    # comparable raw components from its AST encoding -- both out of scope
    # for this pure H2 package under the ROB-1060 remediation constraints.
    # See `identity.assert_supersession_preserves_sealed_components`'s
    # docstring for the full determination; this is a recorded HARD
    # PREREQUISITE for H3, not an oversight.
    #
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

            registered = []
            for spec in plan["specs"]:
                identity = StrategyExperimentIdentity(
                    strategy_key=spec["strategy_key"],
                    strategy_version=spec["strategy_version"],
                    hypothesis=f"ROB-1060 H2 seal: {spec['config_id']}",
                    **spec["components"],
                )
                row = await registry.register_experiment(session, identity)
                registered.append(
                    {"config_id": spec["config_id"], "experiment_id": row.experiment_id}
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
