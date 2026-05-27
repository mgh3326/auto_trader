# ROB-332 — validated_run_card operator ingest + report-item citation wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an operator CLI that ingests a `validated_run_card.v1` JSON artifact as an `InvestmentSnapshot`, and wire `auto_emit` so an emitted report item cites a `validated_run_card` snapshot already present in the bundle when their symbols match.

**Architecture:** Two independent seams over the ROB-329 (PR #979) contract. (A) A `scripts/ingest_validated_run_card.py` CLI that reuses `RunCardSnapshotIngestor`; testable core `run_ingest(db=...)` injected with a session, `main_async` wires `AsyncSessionLocal`. (B) A `validated_run_card` branch in `EvidenceAutoEmitter.propose()` that builds a per-symbol evidence map via `build_run_card_evidence`, then a single post-pass attaches it under `evidence_snapshot["run_card"]` to items whose `symbol` matches. Consume-when-present only — this PR does **not** link run-card snapshots into bundles.

**Tech Stack:** Python 3.13, `argparse`, SQLAlchemy async (`AsyncSessionLocal`), Pydantic v2, pytest (`pytest-asyncio`), `uv`, ruff.

**Spec:** `docs/superpowers/specs/2026-05-27-rob-332-design.md`

---

## File Structure

- **Create** `scripts/ingest_validated_run_card.py` — operator ingest CLI. Responsibility: parse args, read+validate run-card JSON, dry-run summary or `--commit --confirm` persist via `RunCardSnapshotIngestor`. Mirrors `scripts/ingest_research_reports.py`.
- **Modify** `app/services/action_report/snapshot_backed/auto_emit.py` — add `validated_run_card` capture in `propose()` + symbol-match post-pass attaching `evidence_snapshot["run_card"]`.
- **Create** `docs/runbooks/validated-run-card-ingest.md` — operator usage runbook (satisfies "documented local command").
- **Create** `tests/test_ingest_validated_run_card_cli.py` — CLI arg parsing, dry-run, commit persistence, idempotency, no-source-uri.
- **Create** `tests/test_auto_emit_run_card_citation.py` — symbol-match attach / no-overlap / absent / empty-bundle.

No new migration (PR #979 shipped the `validated_run_card` CHECK extension). No model/schema changes — `RunCardSnapshotIngestor`, `build_run_card_citation`, `build_run_card_evidence` are reused as-is.

---

## Task 1: Operator ingest CLI

**Files:**
- Create: `scripts/ingest_validated_run_card.py`
- Test: `tests/test_ingest_validated_run_card_cli.py`

Reference reused symbols (verified to exist):
- `app.services.investment_snapshots.run_card_ingest.RunCardSnapshotIngestor.ingest(*, run_card_payload, market, account_scope=None, as_of=None, ...) -> tuple[InvestmentSnapshot, RunCardCitation]`
- `app.schemas.validated_run_card.build_run_card_citation(payload) -> RunCardCitation` (fields: `verdict`, `framing`, `trade_count`, `is_pass_stamp`, `symbols`, `recognized`)
- `app.services.investment_snapshots.repository.InvestmentSnapshotsRepository(session)`
- `app.core.db.AsyncSessionLocal`, `app.core.cli.setup_logging_and_sentry(service_name)`, `app.monitoring.sentry.capture_exception`

- [ ] **Step 1: Write the failing tests for arg parsing + dry-run**

Create `tests/test_ingest_validated_run_card_cli.py`:

```python
"""ROB-332 — operator CLI for validated_run_card ingest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ingest_validated_run_card as cli

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "validated_run_card"
    / "run_card_insufficient_data.json"
)


def _load() -> dict:
    with _FIXTURE.open() as fh:
        return json.load(fh)  # default json.loads accepts bare Infinity tokens


def test_parse_args_requires_file_and_market():
    ns = cli.parse_args(["--file", "x.json", "--market", "crypto"])
    assert ns.file == Path("x.json")
    assert ns.market == "crypto"
    assert ns.account_scope is None
    assert ns.commit is False
    assert ns.confirm is False


def test_parse_args_rejects_unknown_market():
    with pytest.raises(SystemExit):
        cli.parse_args(["--file", "x.json", "--market", "forex"])


def test_parse_args_rejects_unknown_account_scope():
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--file", "x.json", "--market", "us", "--account-scope", "binance_demo"]
        )


@pytest.mark.asyncio
async def test_run_ingest_dry_run_returns_headline_no_db(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=False,
        confirm=False,
    )
    assert code == 0
    assert summary["dry_run"] is True
    assert summary["verdict"] == "insufficient_data"
    assert summary["is_pass_stamp"] is False
    assert summary["trade_count"] == 2
    assert summary["symbols"] == ["XRPUSDT"]
    assert "snapshot_uuid" not in summary
    # strict-JSON safe (no Infinity/NaN leaks into output)
    json.dumps(summary, allow_nan=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_validated_run_card_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.ingest_validated_run_card'`.

- [ ] **Step 3: Create the CLI module (parse_args + run_ingest dry-run + commit)**

Create `scripts/ingest_validated_run_card.py`:

```python
#!/usr/bin/env python3
"""Ingest a validated_run_card.v1 JSON artifact as an InvestmentSnapshot (ROB-332).

Usage:
    uv run python -m scripts.ingest_validated_run_card --file run_card.json --market crypto
    uv run python -m scripts.ingest_validated_run_card --file run_card.json --market crypto --commit --confirm

Defaults to dry-run (prints the JSON-safe citation headline, no DB write). Commit
mode also requires --confirm. The snapshot is append-only audit evidence with no
broker mutation, so --commit --confirm is the only operator gate (no env flag).

Boundary (ROB-332): reuses RunCardSnapshotIngestor from PR #979. The local
run-card file path is never recorded as a source_uri (the ingestor sets
source_kind="manual"). No broker/order/watch mutation, no scheduler.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception
from app.schemas.validated_run_card import RunCardCitation, build_run_card_citation
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.services.investment_snapshots.run_card_ingest import RunCardSnapshotIngestor

logger = logging.getLogger(__name__)

_MARKETS = ("kr", "us", "crypto")
_ACCOUNT_SCOPES = ("kis_live", "kis_mock", "alpaca_paper", "upbit_live")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest a validated_run_card.v1 JSON artifact (ROB-332)."
    )
    parser.add_argument("--file", required=True, type=Path, help="Run-card JSON path.")
    parser.add_argument("--market", required=True, choices=_MARKETS)
    parser.add_argument("--account-scope", choices=_ACCOUNT_SCOPES, default=None)
    parser.add_argument(
        "--as-of", default=None, help="ISO-8601 as_of; defaults to run-card generated_at."
    )
    parser.add_argument(
        "--commit", action="store_true", help="Persist; default is dry-run only."
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Required with --commit."
    )
    return parser.parse_args(argv)


def _headline(citation: RunCardCitation) -> dict[str, Any]:
    return {
        "recognized": citation.recognized,
        "verdict": citation.verdict,
        "framing": citation.framing,
        "trade_count": citation.trade_count,
        "is_pass_stamp": citation.is_pass_stamp,
        "symbols": citation.symbols,
    }


def _parse_as_of(raw: str | None) -> dt.datetime | None:
    if raw is None:
        return None
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


async def run_ingest(
    *,
    db: Any,
    raw_payload: dict[str, Any],
    market: str,
    account_scope: str | None,
    as_of: dt.datetime | None,
    commit: bool,
    confirm: bool,
) -> tuple[int, dict[str, Any]]:
    """Core ingest. Returns (exit_code, summary). Does not commit the session;
    the caller (main_async) commits on success so tests can introspect + roll back."""
    citation = build_run_card_citation(raw_payload)

    if not commit:
        return 0, {"dry_run": True, **_headline(citation)}

    if not confirm:
        return 4, {"error": "commit mode requires --confirm"}

    ingestor = RunCardSnapshotIngestor(InvestmentSnapshotsRepository(db))
    snapshot, citation = await ingestor.ingest(
        run_card_payload=raw_payload,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        as_of=as_of,
    )
    return 0, {
        "dry_run": False,
        "snapshot_uuid": str(snapshot.snapshot_uuid),
        **_headline(citation),
    }


async def main_async(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="ingest-validated-run-card")
    ns = parse_args(argv)

    if not ns.file.is_file():
        logger.error("file not found: %s", ns.file)
        return 1

    try:
        raw_payload = json.loads(ns.file.read_text(encoding="utf-8"))
        as_of = _parse_as_of(ns.as_of)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("payload/as-of parse failed: %s", exc)
        capture_exception(exc, process="ingest_validated_run_card")
        return 2

    if not ns.commit:
        _code, summary = await run_ingest(
            db=None,
            raw_payload=raw_payload,
            market=ns.market,
            account_scope=ns.account_scope,
            as_of=as_of,
            commit=False,
            confirm=False,
        )
        print(json.dumps(summary, allow_nan=False, ensure_ascii=False))
        return 0

    async with AsyncSessionLocal() as db:
        try:
            code, summary = await run_ingest(
                db=db,
                raw_payload=raw_payload,
                market=ns.market,
                account_scope=ns.account_scope,
                as_of=as_of,
                commit=True,
                confirm=ns.confirm,
            )
            if code == 0:
                await db.commit()
            else:
                await db.rollback()
        except Exception as exc:
            await db.rollback()
            logger.error("ingest failed: %s", exc, exc_info=True)
            capture_exception(exc, process="ingest_validated_run_card")
            return 3

    print(json.dumps(summary, allow_nan=False, ensure_ascii=False))
    return code


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the dry-run + parse tests to verify they pass**

Run: `uv run pytest tests/test_ingest_validated_run_card_cli.py -v -k "parse_args or dry_run"`
Expected: PASS (4 tests).

- [ ] **Step 5: Add commit + idempotency + commit-gate + no-source-uri tests**

Append to `tests/test_ingest_validated_run_card_cli.py`:

```python
@pytest.mark.asyncio
async def test_run_ingest_commit_persists_sanitized_snapshot(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=True,
    )
    assert code == 0
    assert summary["dry_run"] is False
    uuid_str = summary["snapshot_uuid"]
    assert uuid_str

    import uuid as _uuid

    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    repo = InvestmentSnapshotsRepository(db_session)
    snap = await repo.get_snapshot_by_uuid(_uuid.UUID(uuid_str))
    assert snap is not None
    assert snap.snapshot_kind == "validated_run_card"
    assert snap.source_kind == "manual"  # never the local file path
    # Non-finite metric sanitized to null -> strict-JSON safe payload.
    assert snap.payload_json["net_after_cost"]["profit_factor"] is None
    json.dumps(snap.payload_json, allow_nan=False)


@pytest.mark.asyncio
async def test_run_ingest_is_idempotent_reuses_snapshot(db_session):
    payload = _load()
    _c1, s1 = await cli.run_ingest(
        db=db_session, raw_payload=payload, market="crypto",
        account_scope=None, as_of=None, commit=True, confirm=True,
    )
    _c2, s2 = await cli.run_ingest(
        db=db_session, raw_payload=payload, market="crypto",
        account_scope=None, as_of=None, commit=True, confirm=True,
    )
    # Same canonical payload dedups to the same snapshot row.
    assert s1["snapshot_uuid"] == s2["snapshot_uuid"]


@pytest.mark.asyncio
async def test_run_ingest_commit_without_confirm_is_gated(db_session):
    code, summary = await cli.run_ingest(
        db=db_session,
        raw_payload=_load(),
        market="crypto",
        account_scope=None,
        as_of=None,
        commit=True,
        confirm=False,
    )
    assert code == 4
    assert "snapshot_uuid" not in summary
```

- [ ] **Step 6: Run the full CLI test file to verify all pass**

Run: `uv run pytest tests/test_ingest_validated_run_card_cli.py -v`
Expected: PASS (7 tests).

- [ ] **Step 7: Commit**

```bash
git add scripts/ingest_validated_run_card.py tests/test_ingest_validated_run_card_cli.py
git commit -m "feat(rob-332): operator CLI to ingest validated_run_card.v1 snapshots

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: auto_emit symbol-match citation wiring

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/test_auto_emit_run_card_citation.py`

Reference reused symbols:
- `app.schemas.validated_run_card.build_run_card_citation(payload) -> RunCardCitation` (has `.symbols`)
- `app.schemas.validated_run_card.build_run_card_evidence(*, snapshot_uuid, citation) -> dict` (headline-first: `verdict`/`framing`/`trade_count`/`is_pass_stamp`, stats nested under `validation`)
- Existing `_snapshot_uuid(snapshot)` helper in `auto_emit.py`.
- `EvidenceAutoEmitter.propose(*, snapshots, request_market, account_scope) -> list[IngestReportItem]`

- [ ] **Step 1: Write the failing wiring tests**

Create `tests/test_auto_emit_run_card_citation.py`:

```python
"""ROB-332 — auto_emit cites a validated_run_card snapshot when symbols match."""

import json

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


class _Snap:
    def __init__(self, kind, payload, symbol=None, uuid="rc-uuid-0001"):
        self.snapshot_kind = kind
        self.payload_json = payload
        self.symbol = symbol
        self.snapshot_uuid = uuid


_OK_QUOTE = {
    "status": "ok",
    "best_bid": 100,
    "best_ask": 101,
    "bid_depth": 5,
    "ask_depth": 5,
    "spread_bps": 10,
}


def _run_card(symbols, verdict="not_validated"):
    return {
        "schema_version": "validated_run_card.v1",
        "verdict": verdict,
        "framing": "audit evidence, not a pass stamp",
        "net_after_cost": {"trades": 12, "profit_factor": float("inf")},
        "validation": {"bootstrap": {"ci_lower": 0.1}, "monte_carlo": {"p_value": 0.4}},
        "gate_report": {"symbols": symbols, "trade_count": 12},
    }


def _buy_universe(symbol):
    return [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": symbol, "quote": _OK_QUOTE}, symbol=symbol),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [
                    {"symbol": symbol, "score": 8.0, "reasons": ["momentum"], "source": "kis"}
                ],
            },
        ),
    ]


def test_buy_item_for_matching_symbol_cites_run_card():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["005930"]), symbol="005930")
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys, "expected a buy candidate"
    rc = buys[0].evidence_snapshot["run_card"]
    assert rc["verdict"] == "not_validated"
    assert rc["is_pass_stamp"] is False
    assert rc["trade_count"] == 12
    assert rc["snapshot_uuid"] == "rc-uuid-0001"
    # Non-finite sanitized; bootstrap/MC nested under validation, not standalone.
    assert rc["net_after_cost"]["profit_factor"] is None
    assert "bootstrap" in rc["validation"]
    json.dumps(buys[0].evidence_snapshot, allow_nan=False)


def test_validated_verdict_flows_is_pass_stamp_true():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["005930"], verdict="validated"))
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys[0].evidence_snapshot["run_card"]["is_pass_stamp"] is True


def test_run_card_present_but_symbol_not_overlapping_is_not_cited():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["XRPUSDT"]))
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    assert all("run_card" not in i.evidence_snapshot for i in items)


def test_no_run_card_in_bundle_leaves_items_unchanged():
    items = EvidenceAutoEmitter().propose(
        snapshots=_buy_universe("005930"), request_market="kr", account_scope=None
    )
    assert items
    assert all("run_card" not in i.evidence_snapshot for i in items)


def test_empty_bundle_returns_no_items():
    items = EvidenceAutoEmitter().propose(
        snapshots=[], request_market="kr", account_scope=None
    )
    assert items == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auto_emit_run_card_citation.py -v`
Expected: the four `run_card`-citing assertions FAIL with `KeyError: 'run_card'` (the absent/empty cases already pass).

- [ ] **Step 3: Add the run-card import to auto_emit.py**

In `app/services/action_report/snapshot_backed/auto_emit.py`, after the existing import block (`from app.schemas.investment_reports import IngestReportItem`), add:

```python
from app.schemas.validated_run_card import (
    build_run_card_citation,
    build_run_card_evidence,
)
```

- [ ] **Step 4: Capture the run-card snapshot in the propose() loop**

In `EvidenceAutoEmitter.propose`, alongside the other accumulator initializers (near `news_snapshot: Any | None = None`), add:

```python
        run_card_evidence_by_symbol: dict[str, dict[str, Any]] = {}
```

Then in the `for snapshot in snapshots:` dispatch chain, after the `elif kind == "news":` block, add a new branch:

```python
            elif kind == "validated_run_card":
                snap_uuid = _snapshot_uuid(snapshot)
                citation = build_run_card_citation(payload)
                if snap_uuid is not None and citation.symbols:
                    evidence = build_run_card_evidence(
                        snapshot_uuid=snap_uuid, citation=citation
                    )
                    for sym in citation.symbols:
                        run_card_evidence_by_symbol.setdefault(sym, evidence)
```

- [ ] **Step 5: Attach via a single symbol-match post-pass before returning**

Immediately before `return items` at the end of `propose`, add:

```python
        # ROB-332 — cite a bundle-resident validated_run_card on items whose
        # symbol matches the run card's symbols (consume-when-present; no-op
        # when no run card is in the bundle or no symbol overlaps).
        if run_card_evidence_by_symbol:
            for item in items:
                if item.symbol and item.symbol in run_card_evidence_by_symbol:
                    item.evidence_snapshot["run_card"] = run_card_evidence_by_symbol[
                        item.symbol
                    ]

        return items
```

(Replace the existing bare `return items` with the block above.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_auto_emit_run_card_citation.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Verify the snapshot_backed import guard still passes**

Run: `uv run pytest tests/ -v -k "import_guard or snapshot_backed_guard or no_inprocess_llm"`
Expected: PASS (no in-process LLM provider introduced — the new imports are pure schema helpers). If no such test is collected by that filter, run the broader guard: `uv run pytest tests/ -k "guard" -v` and confirm green.

- [ ] **Step 8: Commit**

```bash
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_run_card_citation.py
git commit -m "feat(rob-332): cite bundle-resident validated_run_card on symbol-matched report items

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Operator runbook + final lint/test sweep

**Files:**
- Create: `docs/runbooks/validated-run-card-ingest.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/validated-run-card-ingest.md`:

```markdown
# Validated Run-Card Ingest (ROB-332)

Ingest a `validated_run_card.v1` JSON artifact (produced by
`research/nautilus_scalping`) as an immutable `InvestmentSnapshot`
(`snapshot_kind="validated_run_card"`, `source_kind="manual"`). Audit-only:
no broker/order/watch mutation, no scheduler.

## Dry-run (default — no DB write)

    uv run python -m scripts.ingest_validated_run_card \
      --file path/to/run_card.json --market crypto

Prints the JSON-safe citation headline (`verdict`, `framing`, `trade_count`,
`is_pass_stamp`, `symbols`). `insufficient_data` / `not_validated` is NOT a
pass stamp.

## Commit (operator-gated)

    uv run python -m scripts.ingest_validated_run_card \
      --file path/to/run_card.json --market crypto --commit --confirm

`--commit` requires `--confirm`. Prints the created (or, on re-ingest of an
identical payload, the reused) `snapshot_uuid`. Re-ingesting the same payload
is idempotent (dedup on canonical payload hash).

## Arguments

- `--file` (required): run-card JSON path. The path is never recorded as a
  source URI; only the sanitized payload is persisted.
- `--market` (required): `kr` | `us` | `crypto`. Binance-demo run cards use
  `crypto` (there is no `binance_demo` account scope).
- `--account-scope` (optional): `kis_live` | `kis_mock` | `alpaca_paper` |
  `upbit_live`.
- `--as-of` (optional): ISO-8601; defaults to the run card's `generated_at`.

## Citation in /invest/reports

Once a `validated_run_card` snapshot is a member of a report bundle, a
report item whose symbol matches the run card's `gate_report.symbols` cites it
under `evidence_snapshot["run_card"]` (verdict-first; bootstrap/Monte-Carlo
stats stay nested under `validation`). Linking a run-card snapshot into a
bundle is out of scope for this CLI (operator/Hermes/future work).

## Exit codes

`0` ok · `1` file not found · `2` payload/as-of parse error · `3` ingest
failure · `4` `--commit` without `--confirm`.
```

- [ ] **Step 2: Commit the runbook**

```bash
git add docs/runbooks/validated-run-card-ingest.md
git commit -m "docs(rob-332): runbook for validated_run_card operator ingest

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 3: Lint + format check (report exact output in PR)**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run ruff format --check scripts/ingest_validated_run_card.py tests/test_ingest_validated_run_card_cli.py tests/test_auto_emit_run_card_citation.py app/services/action_report/snapshot_backed/auto_emit.py
```
Expected: `All checks passed!` and format check clean. If format check reports diffs, run `uv run ruff format <those files>` and re-commit.

- [ ] **Step 4: Run the full ROB-332 test surface + the pre-existing run-card tests**

Run:
```bash
uv run pytest tests/test_ingest_validated_run_card_cli.py tests/test_auto_emit_run_card_citation.py tests/test_auto_emit_candidate_citation.py tests/test_validated_run_card_citation.py tests/test_run_card_snapshot_ingest.py -v
```
Expected: PASS (new files + the two ROB-329 files + the existing auto_emit citation file all green — confirms no regression in the wired path).

- [ ] **Step 5: Final commit if any format fixups were needed**

```bash
git add -A
git commit -m "chore(rob-332): ruff format fixups" || echo "nothing to commit"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- Spec §"Component A — Operator ingest CLI" → Task 1 (parse_args, dry-run, commit, idempotency, no-source-uri, exit codes). ✓
- Spec §"Component B — auto_emit symbol-match wiring" → Task 2 (capture branch + post-pass attach + import-guard check). ✓
- Spec §"Testing (net-new only)" → Task 1 Steps 1/5 (CLI), Task 2 Step 1 (wiring), Task 3 Step 4 (regression sweep). Citation-builder internals NOT re-tested. ✓
- Spec §"Known limitations" → exercised: `test_run_card_present_but_symbol_not_overlapping_is_not_cited` (symbol-format/exact-match), `--market crypto` + no scope for binance-demo (CLI tests), idempotency/orphan-run (idempotency test). ✓
- Spec acceptance "documented local command" → Task 3 runbook. ✓
- Spec acceptance "ruff + pytest run and reported" → Task 3 Steps 3–4. ✓

**Deviation from spec (intentional):** The spec described detecting created-vs-reused via `snapshot.run_id` comparison, but `RunCardSnapshotIngestor.ingest` does not return its run. To avoid changing the ROB-329 API, the CLI prints the `snapshot_uuid` (correctly the reused row on re-ingest) and idempotency is proven by `test_run_ingest_is_idempotent_reuses_snapshot` (two commits → same uuid). No explicit reused-label is emitted. This satisfies the acceptance criterion ("print the created or reused snapshot_uuid").

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `run_ingest(db, raw_payload, market, account_scope, as_of, commit, confirm) -> (int, dict)` used identically across Task 1 tests and implementation. `build_run_card_evidence(*, snapshot_uuid, citation)` keyword-only call matches the real signature. `_snapshot_uuid` reused from auto_emit. `evidence_snapshot["run_card"]` key consistent between Task 2 implementation and tests. ✓
