"""CLI: ``uv run python -m research.rob1391_us_resubmit_shadow <command>``.

Commands: seal | step | register | advance | evaluate | report | status.
Exit codes: 0 ok / idempotent no-op, 2 refused (order, integrity, checkout),
3 D0 candidates pending broker reconcile (rerun later), 4 a stop latched
during this command (operator review; nothing resumes automatically).

No secret is printed: the database URL is read from the environment variable
named by ``--database-url-env``; market-data keys are loaded by name only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from research.rob1391_us_resubmit_shadow import engine, guards
from research.rob1391_us_resubmit_shadow.db_source import (
    DbParentSource,
    FileParentSource,
)
from research.rob1391_us_resubmit_shadow.ledger import (
    Ledger,
    LedgerIntegrityError,
    canonical_json,
)
from research.rob1391_us_resubmit_shadow.manifest import (
    DEVIATIONS,
    ManifestError,
    build_manifest,
)
from research.rob1391_us_resubmit_shadow.report import (
    build_report,
    evaluate,
    render_markdown,
)

EXIT_OK, EXIT_REFUSED, EXIT_PENDING, EXIT_LATCHED = 0, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rob1391_us_resubmit_shadow")
    sub = parser.add_subparsers(dest="command", required=True)

    seal = sub.add_parser("seal", help="write the T0 manifest (once)")
    seal.add_argument("--ledger-dir", required=True, type=Path)
    seal.add_argument(
        "--prereg",
        required=True,
        type=Path,
        help="private prereg copy (hashed, never copied)",
    )
    seal.add_argument(
        "--approval-ref",
        required=True,
        help="operator approval evidence, e.g. hk doc/comment id",
    )
    seal.add_argument("--deployed-policy-version", required=True)
    seal.add_argument("--deployed-policy-hash", required=True)
    seal.add_argument("--feed", choices=("sip", "iex"), default="sip")
    seal.add_argument("--fee-schedule", type=Path, default=None)
    seal.add_argument(
        "--approve-deviation",
        action="append",
        default=[],
        choices=sorted(DEVIATIONS),
        help="operator-approved departure from the prereg text (repeatable); "
        "D1/D2 are mandatory -- seal refuses without them",
    )
    seal.add_argument(
        "--deviation-ref",
        default=None,
        help="operator-desk tag or hk:doc that approved the listed deviations",
    )
    seal.add_argument("--seal-at", default=None, help="ISO instant (default: now)")

    for name in ("step", "register", "advance"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--ledger-dir", required=True, type=Path)
        src = cmd.add_mutually_exclusive_group(required=True)
        src.add_argument(
            "--database-url-env", help="name of the env var holding the read-only DSN"
        )
        src.add_argument(
            "--parents-file", type=Path, help="offline JSON snapshot (replay/test)"
        )
        if name == "advance":
            cmd.add_argument(
                "--through", required=True, help="last session (YYYY-MM-DD) to process"
            )
        else:
            cmd.add_argument(
                "--session", required=True, help="US session date YYYY-MM-DD"
            )

    for name in ("evaluate", "report", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--ledger-dir", required=True, type=Path)
        if name != "status":
            cmd.add_argument(
                "--out", required=True, type=Path, help="private output directory"
            )
    return parser


def _context(args: argparse.Namespace, ledger: Ledger) -> engine.Context:
    from research.rob1391_us_resubmit_shadow.market_data import AlpacaMarketData

    manifest, _ = ledger.read_manifest()
    source = (
        DbParentSource(args.database_url_env)
        if args.database_url_env
        else FileParentSource(args.parents_file)
    )

    def sink(endpoint: str, params: dict[str, Any], payload: dict[str, Any]) -> str:
        return ledger.store_market_data(
            endpoint, {"endpoint": endpoint, "params": params, "payload": payload}
        )

    market = AlpacaMarketData(feed=manifest["market_data"]["feed"], sink=sink)
    return engine.Context(
        ledger=ledger, market=market, source=source, now=engine.now_utc
    )


def _seal(args: argparse.Namespace) -> int:
    ledger = Ledger(args.ledger_dir)
    seal_at = (
        datetime.fromisoformat(args.seal_at) if args.seal_at else datetime.now(UTC)
    )
    with ledger.locked():
        manifest = build_manifest(
            seal_at=seal_at,
            approval_ref=args.approval_ref,
            prereg_path=args.prereg,
            code_sha=guards.git_code_sha(),
            policy_stamp=guards.current_policy_stamp(),
            deployed_policy={
                "version": args.deployed_policy_version,
                "content_hash": args.deployed_policy_hash,
            },
            feed=args.feed,
            fee_schedule_path=args.fee_schedule,
            approved_deviations=frozenset(args.approve_deviation),
            deviation_ref=args.deviation_ref,
        )
        digest = ledger.write_manifest(manifest)
        ledger.append("sealed", {"manifest_sha256": digest}, now=datetime.now(UTC))
    print(
        f"sealed {manifest['epoch_id']}: window {manifest['window']['start_session']}.."
        f"{manifest['window']['end_session']} · feed {manifest['market_data']['feed']} · "
        f"net {manifest['net_metrics_status']} · manifest sha256 {digest}"
    )
    return EXIT_OK


def _run_session(args: argparse.Namespace) -> int:
    ledger = Ledger(args.ledger_dir)
    with ledger.locked():
        ctx = _context(args, ledger)
        state = ctx.load()
        latches_before = len(state.latches)
        if args.command == "advance":
            done = engine.advance(ctx, date.fromisoformat(args.through))
            print(f"advanced: {done or 'nothing due'}")
        elif args.command == "step":
            out = engine.step(ctx, date.fromisoformat(args.session))
            print(
                "no-op (already recorded)"
                if out is None
                else f"step {args.session}: {len(out['parents'])} tracked"
            )
        else:
            out = engine.register(ctx, date.fromisoformat(args.session))
            print(
                "no-op (already recorded)"
                if out is None
                else f"register {args.session}: assigned {len(out['assigned'])}, excluded {len(out['exclusions'])}"
            )
        if len(state.latches) > latches_before:
            for latch in state.latches[latches_before:]:
                print(
                    f"STOP latched: {latch['latch_type']} / {latch['reason']} (operator review; no auto-resume)"
                )
            return EXIT_LATCHED
    return EXIT_OK


def _load_state(ledger: Ledger):
    from research.rob1391_us_resubmit_shadow.state import fold

    manifest, digest = ledger.read_manifest()
    return fold(manifest, digest, ledger.load())


def _write_private(out: Path, name: str, text: str) -> Path:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = out / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seal":
            return _seal(args)
        if args.command in {"step", "register", "advance"}:
            return _run_session(args)
        ledger = Ledger(args.ledger_dir)
        state = _load_state(ledger)
        if args.command == "status":
            due = engine._expected_next(state)
            print(
                json.dumps(
                    {
                        "epoch_id": state.manifest["epoch_id"],
                        "window": [state.window[0], state.window[-1]],
                        "registered": len(state.registered),
                        "stepped": len(state.stepped),
                        "parents": len(state.parents),
                        "latches": [
                            (x["latch_type"], x["reason"]) for x in state.latches
                        ],
                        "next_due": due,
                    },
                    ensure_ascii=False,
                )
            )
            return EXIT_OK
        if args.command == "evaluate":
            path = _write_private(
                args.out, "evaluation.json", canonical_json(evaluate(state)) + "\n"
            )
            print(f"wrote {path}")
            return EXIT_OK
        report = build_report(state)
        _write_private(args.out, "report.json", canonical_json(report) + "\n")
        path = _write_private(args.out, "report.md", render_markdown(report))
        print(f"verdict: {report['verdict']} · wrote {path}")
        return EXIT_OK
    except engine.PendingReconcile as exc:
        print(f"pending: {exc}", file=sys.stderr)
        return EXIT_PENDING
    except (
        engine.OrderingError,
        LedgerIntegrityError,
        guards.CodeShaError,
        ManifestError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
