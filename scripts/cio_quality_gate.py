#!/usr/bin/env python3
"""CIO Scout Report Quality Gate CLI.

Thin wrapper around :mod:`app.services.cio_quality_gate_service`. Reads a
Scout Report markdown from a file, stdin, or a Paperclip issue comment thread,
runs the G1~G6 gate sweep, and prints either the CIO runbook-shaped markdown
summary or a JSON payload.

Usage:
    uv run python scripts/cio_quality_gate.py path/to/scout_report.md
    uv run python scripts/cio_quality_gate.py --stdin < scout.md
    uv run python scripts/cio_quality_gate.py --paperclip-issue ROB-158

Exit codes:
    0 = all gates pass (ACCEPT)
    1 = soft-gate only fail (ACCEPT-WITH-FLAG)
    2 = hard-gate fail (REOPEN)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def load_from_paperclip(issue_id: str) -> str:
    api_url = os.environ.get("PAPERCLIP_API_URL")
    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not (api_url and api_key):
        raise SystemExit(
            "PAPERCLIP_API_URL and PAPERCLIP_API_KEY must be set to use "
            "--paperclip-issue"
        )
    cmd = [
        "curl",
        "-sS",
        "-H",
        f"Authorization: Bearer {api_key}",
        f"{api_url}/api/issues/{issue_id}/comments",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    comments = json.loads(result.stdout)
    if not isinstance(comments, list) or not comments:
        raise SystemExit(f"No comments found on {issue_id}")
    largest = max(comments, key=lambda c: len(c.get("body", "")))
    return largest["body"]


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
    src.add_argument(
        "--paperclip-issue",
        dest="paperclip_issue",
        help="Fetch largest comment from a Paperclip issue (e.g. ROB-158)",
    )
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
    elif args.paperclip_issue:
        md = load_from_paperclip(args.paperclip_issue)
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
    "load_from_paperclip",
    "main",
    "render_report",
    "run_gates",
]


if __name__ == "__main__":
    sys.exit(main())
