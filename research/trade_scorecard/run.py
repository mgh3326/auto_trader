"""Write a KR trade scorecard from local, verified inputs."""

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from research.trade_retro.export import SCHEMA_VERSIONS, load_export
from research.trade_retro.prices import load_snapshot
from research.trade_scorecard.score import score


def render_public(cards: list[dict]) -> str:
    kinds = Counter(card["kind"] for card in cards)
    return (
        "# 국내 실거래 3축 회고 점수표\n\n"
        "| 구분 | 건수 |\n|---|---:|\n"
        f"| 실현 FIFO 조각 | {kinds['exit']} |\n"
        f"| 미청산 FIFO 조각 | {kinds['open']} |\n\n"
        "거절 후보 데이터 결측 (`no_declined_candidates`).\n"
        "결정 근거 (`no_decision_evidence`), 업종 (`no_sector`), "
        "배당 (`no_dividend`), 실제 비용 (`no_cost`)은 현재 export에서 확인할 수 없다.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--account", choices=("kis_live", "toss_live"), required=True)
    parser.add_argument("--until", type=date.fromisoformat, required=True)
    parser.add_argument("--public-out", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    private = args.private_dir.expanduser().resolve()
    private_file = (private / "scorecard.csv").resolve()
    repo = Path(__file__).resolve().parents[2]
    if private.is_relative_to(repo) or private_file.is_relative_to(repo):
        parser.error(
            "--private-dir and its scorecard.csv must be outside the repository"
        )
    public = args.public_out.expanduser().resolve()
    if private_file == public:
        parser.error("public and private output paths must differ")

    export = load_export(
        args.export, schema_versions=(*SCHEMA_VERSIONS, "dots-history-export/real-v1")
    )
    book = load_snapshot(args.prices)
    cards = score(export, book, args.account, args.until)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", newline="", encoding="utf-8", dir=private, delete_on_close=False
    ) as handle:
        columns = (
            "symbol",
            "kind",
            "date",
            "exit_ref",
            "entry_ref",
            "exit_intent",
            "quantity",
            "lot",
            "selection",
            "entry",
            "entry_missing_reason",
            "exit_wait",
        )
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for card in cards:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, dict)
                    else value
                    for key, value in card.items()
                }
            )
        handle.flush()
        Path(handle.name).replace(private_file)
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text(render_public(cards), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
