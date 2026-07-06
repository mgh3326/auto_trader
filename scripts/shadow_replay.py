# scripts/shadow_replay.py
"""Headless `claude -p` A' shadow-replay driver + markdown report (ROB-697, M1).

Replays each corpus item (`select_replay_corpus`, Task 0) through a headless
`claude -p` subprocess call `k` times, scores the replayed decisions against
what was actually decided (`extract_decision` / `summarize`, Task 1), and
writes a markdown report.

ROB-501 guard: this is a `scripts/` CLI, not `app/` runtime code. It shells
out to the external `claude` binary via `subprocess.run` (that's an
out-of-process CLI call, not an in-process LLM SDK import) and imports no
LLM provider. M1 is read-only: no orders, no watch mutation, no report
persistence — the replayed agent returns JSON on stdout; this driver only
scores and reports it.

Usage (operator, requires a live DB + the `claude` CLI on PATH):
    # Dry plan only (no `claude -p` spawned) — always safe to run:
    uv run python -m scripts.shadow_replay --k 5

    # Real batch (spawns `k` x corpus-size `claude -p` subprocesses):
    uv run python -m scripts.shadow_replay --k 5 --model claude-opus-4-8 --confirm

See docs/runbooks/shadow-replay.md for the full procedure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.shadow_replay.corpus import CorpusItem, CorpusSelection
from app.services.shadow_replay.scoring import extract_decision, summarize

_MCP_CONFIG = str(Path(__file__).with_name("shadow_replay_mcp.json"))

# MCP-session-churn markers: if the CLI's stderr mentions any of these, the
# MCP connection reset mid-run (e.g. the stdio server bounced) rather than
# the agent making a real decision. Treated as a discarded sample, not a
# "no action" data point — counting it as data would understate fidelity.
_RESET_MARKERS = ("get_hermes_context", "connection error", "tool not found")

# Pinned to an EXACT model id, never a "-latest" alias: A' replay measures
# longitudinal self-consistency / fidelity across repeated runs (this batch
# today vs. a re-run next month). A "-latest" alias silently repoints to a
# newer model over time, which would confound "did the decision drift"
# with "did the model change" — the model id is recorded in every result
# row and the report header specifically so results stay comparable.
_DEFAULT_MODEL = "claude-opus-4-8"

_PROMPT = (
    "You are replaying a FROZEN trading-decision context. Call "
    "investment_report_get_hermes_context with snapshot_bundle_uuid={uuid}. "
    "Base your decision ONLY on its stage_inputs and cited_snapshots (the frozen "
    "evidence) plus get_trading_policy thresholds and the route_request lane. Do NOT "
    "call any other tool. Output ONLY a JSON object: "
    '{{"side": "buy"|"sell"|null, "max_action": {{"notional": <num|null>, '
    '"limit_price": <num|null>}}, "trade_setup": {{"stop": <num|null>, '
    '"target": <num|null>, "headline": {{"entry": <num|null>}}}}, '
    '"trigger_checklist": [<str>...]}}'
)


def _to_item_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Adapt a raw `claude -p` JSON reply into the shape `extract_decision` expects.

    `_PROMPT` instructs the replayed agent to output `trade_setup` at the TOP
    level (a flat, easy-to-follow contract for the agent). `extract_decision`
    (Task 1, already committed) instead reads a *nested*
    `evidence_snapshot.trade_setup`, matching the shape of a persisted
    `InvestmentReportItem`. Calling `extract_decision(raw)` directly on the
    raw claude reply would silently read `ev.get("trade_setup")` off an empty
    `{}` and drop entry/stop/target every time — a shape mismatch, not a
    genuine "the model didn't propose a setup" result. This adapter is the
    fix: it re-nests `trade_setup` under `evidence_snapshot` before scoring.

    Pure function — dict in, dict out, no I/O.
    """
    return {
        "side": raw.get("side"),
        "max_action": raw.get("max_action") or {},
        "evidence_snapshot": {"trade_setup": raw.get("trade_setup") or {}},
        "trigger_checklist": raw.get("trigger_checklist") or [],
    }


# Greedy ``\{.*\}`` (first "{" to last "}") so NESTED objects (max_action /
# trade_setup) are captured whole; the agent's reply contains a single JSON
# object, so greedy is correct here (non-greedy would truncate at the first
# nested "}").
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_decision_json(text: str) -> dict[str, Any] | None:
    """Pull the decision JSON object out of the replayed agent's reply text.

    The `claude --output-format json` envelope's ``result`` field is the
    agent's full assistant text, NOT guaranteed to be raw JSON: despite the
    "Output ONLY a JSON object" instruction, the model frequently prepends a
    reasoning paragraph and wraps the object in a ```json ... ``` fence
    (verified at operator run-time — the smoke happened to get a raw object,
    the batch got prose+fence). Try, in order: (1) the whole text as JSON,
    (2) a ```json fenced object, (3) the last bare ``{...}`` object. Returns
    None if nothing parses (a discarded sample). Pure function.
    """
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    for pattern in (_FENCE_RE, _BARE_OBJ_RE):
        m = pattern.search(text)
        if m:
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _one_run(uuid: str, model: str) -> dict[str, Any] | None:
    """Invoke one headless `claude -p` replay call for `uuid`.

    Best-effort + defensive by design: the exact `claude -p
    --output-format json` envelope shape is only verified at operator
    run-time (docs/runbooks/shadow-replay.md, Step 6 — requires a live DB,
    the running MCP server, and the `claude` CLI). ANY subprocess failure
    (missing binary, timeout), non-zero exit, an MCP-reset stderr marker, or
    a JSON-parse failure returns None — a DISCARDED sample, NOT a data
    point. `run_batch` counts discards separately and a single bad replay
    call never crashes the batch.

    NOTE: this is the ONLY place `claude` is invoked. Tests must always
    monkeypatch `_one_run` — never let a real subprocess run in CI.
    """
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--mcp-config",
                _MCP_CONFIG,
                "--allowedTools",
                "mcp__shadow-replay__investment_report_get_hermes_context,"
                "mcp__shadow-replay__get_trading_policy,"
                "mcp__shadow-replay__route_request",
                "--max-turns",
                "8",
                "--output-format",
                "json",
            ],
            input=_PROMPT.format(uuid=uuid),
            text=True,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0 or any(m in proc.stderr.lower() for m in _RESET_MARKERS):
        return None  # discarded sample (MCP reset / error) — NOT a data point

    # `claude --output-format json` wraps the assistant text in an envelope;
    # the decision contract lives in the `result` field, which is FREE TEXT
    # (may be prose + a ```json fence, not raw JSON) — see
    # `_extract_decision_json`. Fall back to the whole stdout if the CLI ever
    # emits the contract un-enveloped.
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _extract_decision_json(proc.stdout)
    text = outer.get("result") if isinstance(outer, dict) else proc.stdout
    return _extract_decision_json(text) if isinstance(text, str) else None


def _run_one_item(
    item: CorpusItem, *, k: int, model: str, tick: Decimal, source: str
) -> dict[str, Any]:
    raw = [_one_run(item.snapshot_bundle_uuid, model) for _ in range(k)]
    decisions = [extract_decision(_to_item_shape(r)) for r in raw if r is not None]
    return {
        "item_uuid": item.item_uuid,
        "item_kind": item.item_kind,
        "source": source,
        "model": model,
        "discarded": sum(1 for r in raw if r is None),
        "summary": summarize(decisions, item.reference_decision, tick=tick),
    }


def run_batch(
    corpus: CorpusSelection, *, k: int, model: str, tick: Decimal
) -> list[dict[str, Any]]:
    """Replay every corpus item `k` times and score the results.

    One result row per `CorpusItem`: `{item_uuid, item_kind, source, model,
    discarded, summary}`. `discarded` counts `_one_run` calls that returned
    None (MCP reset / parse failure) out of `k`; `summary` is
    `summarize(...)` over only the successfully-parsed decisions, so
    discards never silently count as "no action" data points.
    """
    return [
        _run_one_item(item, k=k, model=model, tick=tick, source=corpus.source)
        for item in corpus.items
    ]


def all_samples_discarded(results: list[dict[str, Any]]) -> bool:
    """True iff at least one sample was attempted and EVERY attempted sample
    was discarded (`_one_run` returned `None` for every call, across every
    corpus item).

    Pure function: only reads the per-item result dicts `run_batch` returns
    (`discarded` + `summary["k"]`) — no I/O, no subprocess calls.

    Why this exists: `_one_run` is best-effort + defensive by design (ANY
    subprocess failure, non-zero exit, MCP-reset marker, or JSON-parse
    failure returns `None` rather than raising). That is the right behavior
    for a single flaky sample. But if the `claude --output-format json`
    envelope assumption is simply WRONG (verified only at operator
    run-time), `_one_run` returns `None` for every single call, `run_batch`
    still returns a normal-looking list of result rows (`discarded == k`,
    `summary["k"] == 0` for every item), and the batch would otherwise exit
    0 and print `"step": "done"` — an operator could easily read that as
    "ran clean, fidelity is just bad" instead of "the harness itself is
    broken." This predicate is the trigger `_amain` uses to turn that
    silent-looking success into a loud, non-zero-exit warning.
    """
    total_attempted = 0
    total_discarded = 0
    for row in results:
        summary = row.get("summary") or {}
        discarded = row.get("discarded", 0)
        attempted = discarded + summary.get("k", 0)
        total_attempted += attempted
        total_discarded += discarded
    return total_attempted > 0 and total_discarded == total_attempted


def _fmt_rate(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def write_report(results: list[dict[str, Any]], path: Path) -> str:
    """Render `run_batch` results as a markdown table and write it to `path`.

    Header records the corpus `source` and the pinned `model` (both stamped
    onto every result row by `run_batch`, pulled here from the first row)
    so the report file is self-describing for longitudinal comparison even
    once separated from the invocation that produced it. Returns the
    rendered markdown text (also used for stdout echo by `main`).
    """
    source = results[0]["source"] if results else "n/a"
    model = results[0]["model"] if results else "n/a"
    lines = [
        "# A' Shadow Replay Report (ROB-697, M1)",
        "",
        f"- **Corpus source:** {source}",
        f"- **Model:** {model}",
        f"- **Items:** {len(results)}",
        "",
        "| item_uuid | item_kind | side_rate | size_band_rate | limit_rate | "
        "same_decision_rate | no_action_rate | discarded |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in results:
        summary = row["summary"]
        fidelity = summary.get("fidelity") or {}
        lines.append(
            "| {uuid} | {kind} | {side} | {size} | {limit} | {same} | {noact} | {disc} |".format(
                uuid=row["item_uuid"],
                kind=row["item_kind"],
                side=_fmt_rate(fidelity.get("side_rate")),
                size=_fmt_rate(fidelity.get("size_band_rate")),
                limit=_fmt_rate(fidelity.get("limit_rate")),
                same=_fmt_rate(fidelity.get("same_decision_rate")),
                noact=_fmt_rate(summary.get("no_action_rate")),
                disc=row["discarded"],
            )
        )
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A' shadow replay: headless `claude -p` batch driver (ROB-697, M1)"
    )
    parser.add_argument(
        "--k", type=int, default=5, help="Replay count per corpus item (default 5)."
    )
    parser.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help=(
            "Pinned EXACT Claude model id (never a '-latest' alias) so replay "
            f"results stay comparable across runs. Default: {_DEFAULT_MODEL}."
        ),
    )
    parser.add_argument(
        "--tick",
        type=str,
        default="0.01",
        help="Price tick size (Decimal string) used for limit-price tolerance.",
    )
    parser.add_argument(
        "--min-per-kind",
        type=int,
        default=1,
        help="Forwarded to select_replay_corpus's action/watch coverage gate.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("shadow_replay_report.md"),
        help="Markdown report output path.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Required to actually spawn any `claude -p` subprocess. Without "
            "it, prints a dry plan (corpus size + would-run call count) and "
            "exits 0 — nothing is spawned."
        ),
    )
    return parser


def _dry_plan_payload(corpus: CorpusSelection, *, k: int, model: str) -> dict[str, Any]:
    return {
        "step": "dry_plan",
        "confirm": False,
        "source": corpus.source,
        "corpus_size": len(corpus.items),
        "would_run_claude_p_calls": len(corpus.items) * k,
        "model": model,
    }


async def _amain(args: argparse.Namespace) -> int:
    # Deferred import: building the async engine needs DATABASE_URL, which a
    # unit-test import of this module should never be coupled to.
    from app.core.db import AsyncSessionLocal
    from app.services.shadow_replay.corpus import (
        CorpusUnavailable,
        select_replay_corpus,
    )

    async with AsyncSessionLocal() as session:
        try:
            corpus = await select_replay_corpus(session, min_per_kind=args.min_per_kind)
        except CorpusUnavailable as exc:
            print(
                json.dumps(
                    {"step": "corpus_unavailable", "error": str(exc)},
                    ensure_ascii=False,
                )
            )
            return 2

    if not args.confirm:
        print(
            json.dumps(
                _dry_plan_payload(corpus, k=args.k, model=args.model),
                ensure_ascii=False,
            )
        )
        return 0

    results = run_batch(corpus, k=args.k, model=args.model, tick=Decimal(args.tick))
    report_text = write_report(results, args.report)
    print(report_text)
    print(
        json.dumps(
            {"step": "done", "report_path": str(args.report)}, ensure_ascii=False
        )
    )

    if all_samples_discarded(results):
        total_attempted = sum(
            row.get("discarded", 0) + (row.get("summary") or {}).get("k", 0)
            for row in results
        )
        print(
            f"WARNING: all {total_attempted} replay samples were discarded — "
            "this usually means the `claude -p` invocation or --output-format "
            "parsing is broken, not that the model kept resetting. Check the "
            "harness before trusting this report.",
            file=sys.stderr,
        )
        return 3

    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
