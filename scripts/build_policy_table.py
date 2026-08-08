"""ROB-1230 P-1 — build a policy_table.v1 artifact (crypto/Upbit adapter).

Scheduleless, read-only, advisory. Fetches Upbit market data + this repo's
holdings/watch-alert tables, runs the shared indicator core (a direct reuse
of research/kr_corpus/d3_engine's fib/BB/RSI/tick code — see
scripts/policy_table/core/signal_math.py), and writes a policy_table.v1 JSON
artifact plus a human-readable Markdown summary.

No broker mutation. No order-tool imports (verify: `grep -rn "orders import"
scripts/policy_table` should be empty — only `client.py`'s read functions
are used). Never merges, never places or cancels an order.

Usage
-----
    uv run python -m scripts.build_policy_table --market crypto

    # Reproducibility check (ROB-1230 acceptance #3): dump raw inputs once,
    # then replay the same inputs through the pure compute+serialize path
    # twice and diff the bytes.
    uv run python -m scripts.build_policy_table --market crypto \\
        --dump-raw /tmp/raw.json --out-dir /tmp/pt-a
    uv run python -m scripts.build_policy_table --market crypto \\
        --replay-raw /tmp/raw.json --out-dir /tmp/pt-b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.policy_table.adapters import crypto as crypto_adapter
from scripts.policy_table.core.schema import (
    canonical_json_bytes,
    compute_policy_table_hash,
    sha256_of_bytes,
)

DEFAULT_OUT_DIR = Path.home() / "services" / "auto_trader-operator" / "policy-tables"

# The exact D3 engine module files this job reuses (not reimplements). Their
# content hashes are stamped into every artifact so a reviewer can confirm
# no drift between generation time and verification time.
D3_ENGINE_MODULES = (
    Path("research/kr_corpus/d3_engine/indicators.py"),
    Path("research/kr_corpus/d3_engine/signals.py"),
    Path("research/kr_corpus/d3_engine/tick.py"),
    Path("research/kr_corpus/d3_engine/policies.py"),
    Path("research/kr_corpus/d3_engine/models.py"),
    Path("research/kr_corpus/d3_engine/constants.py"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_repo_root(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _engine_module_hashes() -> dict[str, str]:
    root = _repo_root()
    hashes: dict[str, str] = {}
    for relative_path in D3_ENGINE_MODULES:
        data = (root / relative_path).read_bytes()
        hashes[str(relative_path)] = sha256_of_bytes(data)
    return hashes


def _build_stamps(payload_without_stamps: dict[str, Any]) -> dict[str, Any]:
    head = _git_head()
    return {
        "policy_table_hash": compute_policy_table_hash(payload_without_stamps),
        "auto_trader_head": head,
        "indicator_code_commit": head,
        "engine_module_sha256": _engine_module_hashes(),
        "input_as_of": payload_without_stamps["generated_at"],
    }


def _render_summary_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Policy Table — {payload['market']} — {payload['generated_at']}")
    lines.append("")
    for label in payload["trust_labels"]:
        lines.append(f"> {label}")
    lines.append("")
    lines.append(
        f"universe: {payload['universe']['total_symbols']} symbols "
        f"(holdings={len(payload['universe']['holdings'])}, "
        f"watch={len(payload['universe']['watch'])}, "
        f"top_n={len(payload['universe']['top_n'])})"
    )
    if payload["universe"]["symbols_with_insufficient_history"]:
        lines.append(
            "insufficient history (skipped signal): "
            + ", ".join(payload["universe"]["symbols_with_insufficient_history"])
        )
    lines.append("")
    breadth = payload["market_context"]["alt_breadth"]
    lines.append(
        f"alt_breadth: {breadth['positive_24h_count']}/{breadth['swept_market_count']} "
        f"positive 24h "
        f"({(breadth['positive_pct'] * 100) if breadth['positive_pct'] is not None else 'n/a'}%)"
    )
    lines.append("")

    # Simple blend rank per §2 of the design doc: RSI oversold + nearest
    # qualifying support + 24h trade value — display ordering only, no
    # effect on the JSON row content.
    def blend_key(row: dict[str, Any]) -> tuple[float, float]:
        if row.get("insufficient_history"):
            return (float("inf"), float("inf"))
        rsi = row.get("rsi")
        rank = row.get("D_context", {}).get("alt_breadth_rank")
        rsi_val = float(rsi) if rsi is not None else 100.0
        rank_val = float(rank) if rank is not None else 10_000.0
        return (rsi_val, rank_val)

    ranked = sorted(payload["rows"], key=blend_key)
    lines.append("| symbol | held | RSI | buy_l1 | buy_l2 | sell_r1 | resistance_mismatch |")
    lines.append("|---|---|---:|---:|---:|---:|---|")
    for row in ranked[:30]:
        if row.get("insufficient_history"):
            lines.append(f"| {row['symbol']} | - | insufficient history | | | | |")
            continue
        buy = row["A_buy_side"]
        sell = row["B_sell_side"]
        buy_l2 = buy["buy_l2"]["price"] if buy["buy_l2"] else "-"
        lines.append(
            f"| {row['symbol']} | {'Y' if row['held'] else ''} | {row['rsi']} | "
            f"{buy['buy_l1']['price']} | {buy_l2} | {sell['sell_r1'] or '-'} | "
            f"{sell['label']} |"
        )
    lines.append("")
    lines.append(f"policy_table_hash: `{payload['stamps']['policy_table_hash']}`")
    lines.append(f"auto_trader_head: `{payload['stamps']['auto_trader_head']}`")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> int:
    if args.market != "crypto":
        raise NotImplementedError(
            "only market=crypto is implemented in P-1 (KR adapter is P-2)"
        )

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stale_marker = out_dir / f"latest-{args.market}.STALE"
    latest_link = out_dir / f"latest-{args.market}.json"

    try:
        if args.replay_raw:
            raw_payload = json.loads(Path(args.replay_raw).expanduser().read_text())
            raw = crypto_adapter.RawInputs.from_jsonable(raw_payload)
        else:
            raw = await crypto_adapter.fetch_raw_inputs(top_n=args.top_n)

        if args.dump_raw:
            Path(args.dump_raw).expanduser().write_text(
                json.dumps(raw.to_jsonable(), sort_keys=True, indent=2) + "\n"
            )

        payload = crypto_adapter.compute_policy_table(raw, top_n=args.top_n)
        payload["stamps"] = _build_stamps(payload)

        artifact_bytes = canonical_json_bytes(payload)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") if not args.fixed_ts else args.fixed_ts
        json_path = out_dir / f"{ts}-{args.market}.json"
        md_path = out_dir / f"{ts}-{args.market}-summary.md"

        json_path.write_bytes(artifact_bytes)
        md_path.write_text(_render_summary_md(payload))

        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(json_path.name)
        if stale_marker.exists():
            stale_marker.unlink()

        print(f"OK json={json_path} md={md_path}")
        print(f"policy_table_hash={payload['stamps']['policy_table_hash']}")
        print(f"json_sha256={sha256_of_bytes(artifact_bytes)}")
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level CLI failure boundary
        stale_marker.write_text(
            json.dumps(
                {
                    "stale_since": datetime.now(UTC).isoformat(),
                    "last_good_artifact": (
                        str(latest_link.resolve()) if latest_link.exists() else None
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
            )
            + "\n"
        )
        print(f"FAILED — see {stale_marker}: {exc}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="crypto", choices=["crypto"])
    parser.add_argument("--top-n", type=int, default=crypto_adapter.DEFAULT_TOP_N)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument(
        "--dump-raw", default=None, help="write fetched raw inputs to this path"
    )
    parser.add_argument(
        "--replay-raw",
        default=None,
        help="skip network/DB fetch; replay raw inputs from this path",
    )
    parser.add_argument(
        "--fixed-ts",
        default=None,
        help="use this filename timestamp instead of now() (testing only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
