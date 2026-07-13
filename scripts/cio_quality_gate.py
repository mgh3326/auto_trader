#!/usr/bin/env python3
"""CIO Scout Report Quality Gate CLI.

Thin wrapper around :mod:`app.services.cio_quality_gate_service`. Reads a
Scout Report markdown from a file or stdin, runs the G1~G6 gate sweep, and
prints either the CIO runbook-shaped markdown summary or a JSON payload.

Usage:
    uv run python scripts/cio_quality_gate.py path/to/scout_report.md
    uv run python scripts/cio_quality_gate.py --stdin < scout.md

Exit codes:
    0 = all gates pass (ACCEPT)
    1 = soft-gate only fail (ACCEPT-WITH-FLAG)
    2 = hard-gate fail (REOPEN)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is importable when the script is invoked directly
# (e.g. `python scripts/cio_quality_gate.py ...`). Keep this narrow so the
# service module stays the single source of truth.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.cio_quality_gate_service import (  # noqa: E402
    Candidate,
    GateResult,
    QualityGateReport,
    build_reopen_comment,
    evaluate_scout_report,
    extract_candidates,
    render_report,
    run_gates,
)

def _json_payload(report: QualityGateReport) -> dict:
    return {
        "candidates": [
            {
                "name": c.name,
                "code": c.code,
                "is_new": c.is_new,
                "execution_cell": c.execution_cell,
                "items": {str(k): bool(c.items.get(k)) for k in range(1, 9)},
                "verdict": c.verdict,
            }
            for c in report.candidates
        ],
        "gates": [
            {
                "key": r.key,
                "label": r.label,
                "severity": r.severity,
                "passed": r.passed,
                "detail": r.detail,
            }
            for r in report.gates
        ],
        "overall_status": report.overall_status,
        "violations": [
            {"gate_id": v.gate_id, "severity": v.severity, "detail": v.detail}
            for v in report.violations
        ],
        "reopen_comment": report.reopen_comment,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("path", nargs="?", help="Path to Scout Report markdown file")
    src.add_argument("--stdin", action="store_true", help="Read markdown from stdin")
    p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON")
    p.add_argument(
        "--cash",
        type=float,
        default=None,
        help="Override actual cash balance (raw KRW) for G6 ratio computation",
    )
    p.add_argument(
        "--tool-failure",
        dest="tool_failures",
        action="append",
        default=None,
        help="Extra tool failure signal for G3 (repeatable)",
    )
    args = p.parse_args(argv)

    if args.stdin:
        md = sys.stdin.read()
    else:
        md = Path(args.path).read_text(encoding="utf-8")

    report = evaluate_scout_report(
        markdown=md,
        cash_balance=args.cash,
        tool_failures=args.tool_failures,
    )

    if args.as_json:
        print(json.dumps(_json_payload(report), ensure_ascii=False, indent=2))
    else:
        print(render_report(report.candidates, report.gates))

    if report.overall_status == "FAIL":
        return 2
    if report.overall_status == "PARTIAL":
        return 1
    return 0


# Re-export for legacy importers that relied on the script as a module
__all__ = [
    "Candidate",
    "GateResult",
    "QualityGateReport",
    "build_reopen_comment",
    "evaluate_scout_report",
    "extract_candidates",
    "main",
    "render_report",
    "run_gates",
]


if __name__ == "__main__":
    sys.exit(main())
