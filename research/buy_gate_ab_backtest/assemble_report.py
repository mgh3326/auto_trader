"""Assemble the operator-facing markdown report from result + bootstrap files.

    uv run python -m research.buy_gate_ab_backtest.assemble_report DIR PROSE_DIR

Numbers are rendered by report.py / render_bootstrap.py from the run outputs;
this module only stitches them to the fixed prose sections.
"""

from __future__ import annotations

import argparse
import io
import os
from contextlib import redirect_stdout

from research.buy_gate_ab_backtest import render_bootstrap, render_censoring, report


def _capture(fn, directory: str) -> str:
    buffer = io.StringIO()
    argv_backup = os.sys.argv
    os.sys.argv = ["x", directory]
    try:
        with redirect_stdout(buffer):
            fn()
    finally:
        os.sys.argv = argv_backup
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir")
    parser.add_argument("prose_dir")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    def prose(name: str) -> str:
        with open(os.path.join(args.prose_dir, name), encoding="utf-8") as handle:
            return handle.read()

    tables = _capture(report.main, args.results_dir)
    bootstrap = _capture(render_bootstrap.main, args.results_dir)
    censoring = _capture(render_censoring.main, args.results_dir)

    document = "\n".join(
        [
            prose("report-head.md"),
            prose("report-mid.md"),
            "\n---\n\n## 4. 시장별 결과 (풀링 — §5.1 먼저 읽을 것)\n",
            tables,
            prose("report-tail.md"),
            bootstrap,
            "\n---\n\n## 6.2 B2 — D+20 검열 분해와 유계 민감도\n",
            censoring,
            prose("report-close.md"),
        ]
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(document)
    print(f"wrote {args.out} ({len(document):,} chars)")


if __name__ == "__main__":
    main()
