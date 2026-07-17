#!/usr/bin/env python3
"""ROB-960 empirical materializer CLI -- --plan/--run boundary.

Wires H4's frozen campaign plan (``run_rob944_campaign.build_plan``), H4+H6's
capture-wrapped empirical execution (``rob960_empirical_orchestrator``), and
H5's scorecard build/render (``rob945_scorecard``) into ONE operator
entrypoint: a single approved ``--run`` produces both H6's committed trial-
accounting rows AND H5's hash-pinned ``scorecard.json``/``scorecard.md``
files, as one atomically-published unit -- never one without the other.

``--plan`` is PURE (never imports ``app.*``): prints
``rob960_scorecard_writer.build_materializer_plan()``, which echoes H4's own
``build_plan()`` verbatim plus only additive operator metadata (output
filenames) -- no new schema/version key (captain plan-gate G5).

``--run`` fixed order (captain plan-gate G8 -- scorecard finished BEFORE H6
commit, never after):
  1. Preflight (hash/run-id/opt-in/bridge) -- fails closed before any DB
     session is even constructed (proven by a spy test).
  2. Open the real session, run H4/H6 orchestration (capture-wrapped) to an
     in-memory, UNCOMMITTED accounting report + (if corpus/walk-forward/PBO
     all genuinely succeeded) real per-strategy H5 evidence.
  3. If real per-strategy evidence was never produced (global corpus/gap/PBO
     failure): roll back -- "H6 accounting complete, no scorecard" never
     becomes a durable end state (captain plan-gate G9). Exit 6.
  4. Build the H5 scorecard envelope + markdown (pure, in-memory) and stage
     both files (fsync'd, not yet final) -- still pre-commit. Any failure
     here rolls back too. Exit 6.
  5. Commit the H6 transaction. A commit failure rolls back. Exit 7 (reuses
     H4's own commit-failure code exactly).
  6. Publish the staged pair (pair-atomic -- see ``rob960_scorecard_writer``).
     A publish failure here means the DB is ALREADY durable (nothing to roll
     back) -- staging is preserved, a sanitized message that never falsely
     claims a rollback is printed. Exit 6.
  7. Success: exit 0 if every primary attempt was empirically
     ``status="completed"``, else 5 -- reuses H4's own 0/5 empirical-success
     convention exactly.

No new exit code is ever introduced (captain plan-gate G5) -- every new
failure branch above reuses one of H4's existing 0/2/4/5/6/7.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__version__ = "rob940-empirical-materializer.v1"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_rob940_empirical_materializer",
        description=(
            "ROB-960 empirical materializer -- frozen H4/H6/H5 wiring, "
            "--plan/--run boundary"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan",
        action="store_true",
        help="pure, no I/O -- prints the materializer plan",
    )
    mode.add_argument(
        "--run", action="store_true", help="empirical, fail-closed gated execution"
    )
    parser.add_argument(
        "--expected-full-campaign-hash",
        default=None,
        help="[--run, required] operator-pinned expected full_campaign_hash",
    )
    parser.add_argument(
        "--campaign-run-id",
        default=None,
        help=(
            "[--run, required] MUST equal the value --plan reports as "
            "expected_campaign_run_id -- never an arbitrary UUID/timestamp"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="[--run, required] scorecard.json/scorecard.md output directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.plan:
        from rob960_scorecard_writer import build_materializer_plan

        print(json.dumps(build_materializer_plan(), indent=2, sort_keys=True))
        return 0
    # args.run
    if not args.expected_full_campaign_hash:
        print(
            "run preflight failed: --expected-full-campaign-hash is required for --run",
            file=sys.stderr,
        )
        return 2
    if not args.campaign_run_id:
        print(
            "run preflight failed: --campaign-run-id is required for --run",
            file=sys.stderr,
        )
        return 2
    if not args.output_dir:
        print(
            "run preflight failed: --output-dir is required for --run",
            file=sys.stderr,
        )
        return 2
    return _run_empirical(
        expected_full_campaign_hash=args.expected_full_campaign_hash,
        campaign_run_id=args.campaign_run_id,
        output_dir=args.output_dir,
    )


def _run_empirical(
    *, expected_full_campaign_hash: str, campaign_run_id: str, output_dir: str
) -> int:
    """Thin sanitized wrapper (mirrors run_rob944_campaign._run_empirical
    exactly): a final backstop for anything the impl's own narrower
    try/excepts don't already cover."""
    try:
        return _run_empirical_impl(
            expected_full_campaign_hash=expected_full_campaign_hash,
            campaign_run_id=campaign_run_id,
            output_dir=output_dir,
        )
    except Exception:  # noqa: BLE001 -- deliberate final sanitized backstop, never re-raise
        print(
            "run failed with an unexpected error before a documented exit code could be "
            "produced -- see server-side logs for diagnostics",
            file=sys.stderr,
        )
        return 6


def _run_empirical_impl(
    *, expected_full_campaign_hash: str, campaign_run_id: str, output_dir: str
) -> int:
    """Gate 1 (fresh hash + campaign_run_id derivation check, then bridge +
    opt-in) happens entirely BEFORE any DB session is constructed -- mirrors
    run_rob944_campaign._run_empirical_impl's own preflight exactly, reusing
    the SAME frozen functions (never re-derived independently)."""
    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob944_campaign import (
        RunPreflightError,
        _derive_primary_campaign_run_id,
        _run_precheck_bridge_and_opt_in,
    )

    envelope = build_production_frozen_campaign_envelope()
    actual_hash = envelope.full_campaign_hash()
    if actual_hash != expected_full_campaign_hash:
        print(
            "run preflight failed: full_campaign_hash mismatch -- refusing to run",
            file=sys.stderr,
        )
        return 4
    if campaign_run_id != _derive_primary_campaign_run_id(actual_hash):
        print(
            "run preflight failed: --campaign-run-id does not match the value "
            "canonically derived from the frozen full-campaign hash -- an arbitrary "
            "UUID/timestamp is refused",
            file=sys.stderr,
        )
        return 4

    try:
        _run_precheck_bridge_and_opt_in()
    except RunPreflightError as exc:
        print(f"run preflight failed: {exc}", file=sys.stderr)
        return 2

    import asyncio

    return asyncio.run(
        _do_run(
            expected_full_campaign_hash=actual_hash,
            campaign_run_id=campaign_run_id,
            output_dir=output_dir,
        )
    )


async def _do_run(
    *, expected_full_campaign_hash: str, campaign_run_id: str, output_dir: str
) -> int:
    try:
        return await _do_run_with_session(
            expected_full_campaign_hash=expected_full_campaign_hash,
            campaign_run_id=campaign_run_id,
            output_dir=output_dir,
        )
    except Exception:  # noqa: BLE001 -- session-boundary failure must exit sanitized, never re-raise
        print(
            "run orchestration failed to establish/close the database session -- see "
            "server-side logs for diagnostics",
            file=sys.stderr,
        )
        return 6


async def _do_run_with_session(
    *, expected_full_campaign_hash: str, campaign_run_id: str, output_dir: str
) -> int:
    from rob960_empirical_orchestrator import run_empirical_campaign_with_capture
    from run_rob944_campaign import _import_campaign_controller, _safe_rollback

    from app.core.db import AsyncSessionLocal

    controller = _import_campaign_controller()
    output_dir_path = Path(output_dir)

    async with AsyncSessionLocal() as session:
        try:
            outcome = await run_empirical_campaign_with_capture(
                session,
                controller,
                expected_full_campaign_hash=expected_full_campaign_hash,
                campaign_run_id=campaign_run_id,
            )
        except Exception:  # noqa: BLE001 -- an unknown failure must roll back and exit with a FIXED sanitized message, never re-raise
            await _safe_rollback(session)
            print(
                "run orchestration failed with an unexpected error (rolled back) -- "
                "see server-side logs for diagnostics",
                file=sys.stderr,
            )
            return 6

        # Captain plan-gate G9: a global corpus/gap/PBO failure means real
        # per-strategy H5 evidence was never produced -- "H6 accounting
        # complete, no scorecard" must never become a durable end state.
        if outcome.strategies_evidence is None:
            await _safe_rollback(session)
            print(
                "H6 accounting evidence could not be paired with a real, complete "
                "scorecard input set (global corpus/gap/PBO evidence unavailable) -- "
                "rolled back, no scorecard written",
                file=sys.stderr,
            )
            return 6

        # Captain plan-gate G8: build + stage the scorecard BEFORE the H6
        # commit, never after.
        try:
            envelope_dict = _build_scorecard_envelope(
                outcome,
                full_campaign_hash=expected_full_campaign_hash,
                campaign_run_id=campaign_run_id,
            )
            markdown = _render_markdown(envelope_dict)
            staging_dir = _stage(envelope_dict, markdown, output_dir_path)
        except Exception:  # noqa: BLE001 -- scorecard construction failure must roll back and exit sanitized
            await _safe_rollback(session)
            print(
                "scorecard construction failed before commit (rolled back) -- see "
                "server-side logs for diagnostics",
                file=sys.stderr,
            )
            return 6

        try:
            await session.commit()
        except Exception:  # noqa: BLE001 -- a commit failure must roll back and exit with a FIXED sanitized message, never re-raise
            await _safe_rollback(session)
            print(
                "run commit failed after orchestration succeeded (rolled back) -- "
                "see server-side logs for diagnostics",
                file=sys.stderr,
            )
            return 7

        try:
            json_path, md_path = _publish(staging_dir, output_dir_path)
        except Exception:  # noqa: BLE001 -- the DB is ALREADY durable here -- no rollback is attempted, never claim one
            print(
                "scorecard publish failed after a successful, durable H6 commit -- "
                "staging preserved for forensic recovery, see server-side logs for "
                "diagnostics",
                file=sys.stderr,
            )
            return 6

        print(
            json.dumps(
                {
                    "scorecard_artifact_hash": envelope_dict["scorecard_artifact_hash"],
                    "json_path": str(json_path),
                    "md_path": str(md_path),
                    "empirical_success": outcome.empirical_success,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if outcome.empirical_success else 5


def _build_scorecard_envelope(
    outcome, *, full_campaign_hash: str, campaign_run_id: str
) -> dict:
    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from rob945_scorecard import build_scorecard

    envelope = build_production_frozen_campaign_envelope()
    plain = envelope.to_dict()
    return build_scorecard(
        full_campaign_hash=full_campaign_hash,
        full_campaign_payload=plain,
        campaign_run_id=campaign_run_id,
        dataset_manifest_hash=plain["dataset_manifest_hash"],
        signal_manifest_hash=plain["signal_manifest_hash"],
        accounting_report=outcome.report.model_dump(),
        attempt_evidence=outcome.attempt_evidence,
        walkforward_results=outcome.walkforward_results,
        strategies=outcome.strategies_evidence,
    )


def _render_markdown(envelope: dict) -> str:
    from rob945_scorecard import render_markdown

    return render_markdown(envelope)


def _stage(envelope: dict, markdown: str, output_dir: Path) -> Path:
    from rob960_scorecard_writer import stage_scorecard_files

    return stage_scorecard_files(envelope, markdown, output_dir)


def _publish(staging_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    from rob960_scorecard_writer import publish_staged_scorecard

    return publish_staged_scorecard(staging_dir, output_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
