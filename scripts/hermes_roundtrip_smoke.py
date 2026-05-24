"""ROB-287 / ROB-309 — operator-runnable Hermes round-trip smoke CLI.

Drives the full current Hermes HTTP contract in sequence against a
target auto_trader instance and reports per-step status. Loads the same
Hermes-produced JSON fixtures the round-trip test uses, so the CLI
contract stays in lock-step with the test contract.

The chain (ROB-309 extends the ROB-287 baseline of
context → stage-artifacts → composition):

    1. POST /context           — frozen context packet
    2. POST /stage-artifacts   — append-only stage rows (5 artifacts)
    3. POST /symbol-reports    — per-symbol reductions (ROB-301)
    4. POST /dimension-reports — per-dimension analyst reports (ROB-306)
    5. POST /context (re-pull)  — assert dimension_reports +
                                  symbol_intermediate_reports now non-empty
    6. POST /composition       — final report; threads the captured
                                  symbol/dimension report UUIDs (ROB-308)
    7. GET  /runs/{run}/dimension-reports?dimension=market — read surface
    8. GET  /investment-reports/{report} — assert held-action vs
                                  new-candidate grouping is present

Usage::

    uv run python -m scripts.hermes_roundtrip_smoke \\
        --base-url https://<auto_trader-host> \\
        --token "<HERMES_INGEST_TOKEN value>" \\
        --bundle-uuid <existing-bundle-uuid>

Exit codes::

    0 — full chain succeeded.
    1 — at least one step failed; stderr carries the response body for
        diagnosis. Re-runnable: the stage-artifacts / symbol-reports /
        dimension-reports ingests are idempotent so a partial run can be
        resumed safely with the same ``--run-uuid``.

Hard invariants:

* No external LLM is called. Hermes payloads come from the JSON
  fixtures, not from any LLM.
* No broker / order / watch / order-intent mutation reachable.
* Default ``--bundle-uuid`` is required — the CLI refuses to invent
  bundle UUIDs to keep behaviour predictable for the operator.
* ``--token`` is never logged or printed; only its presence/absence
  is surfaced.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hermes"

logger = logging.getLogger("hermes_roundtrip_smoke")


def _load_fixture(name: str) -> dict[str, Any]:
    text = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
    parsed = json.loads(text)
    parsed.pop("_comment", None)
    return parsed


def _substitute_placeholders(
    payload: dict[str, Any],
    *,
    run_uuid: uuid.UUID,
    snapshot_bundle_uuid: uuid.UUID,
    symbol_report_uuid: uuid.UUID | str | None = None,
    dimension_report_uuid: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Substitute the placeholder UUIDs inside a fixture.

    ``run_uuid`` / ``snapshot_bundle_uuid`` are always available. The
    ``symbol_report_uuid`` / ``dimension_report_uuid`` placeholders are only
    known after the symbol-reports / dimension-reports POSTs return their
    server-assigned UUIDs (ROB-309), so they are substituted in a second pass
    on the composition payload.
    """
    raw = json.dumps(payload)
    raw = raw.replace("{{run_uuid}}", str(run_uuid))
    raw = raw.replace("{{snapshot_bundle_uuid}}", str(snapshot_bundle_uuid))
    if symbol_report_uuid is not None:
        raw = raw.replace("{{symbol_report_uuid}}", str(symbol_report_uuid))
    if dimension_report_uuid is not None:
        raw = raw.replace("{{dimension_report_uuid}}", str(dimension_report_uuid))
    return json.loads(raw)


def _redact_token(token: str | None) -> str:
    if not token:
        return "(empty)"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…(len={len(token)})"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hermes ↔ auto_trader round-trip smoke (ROB-287 Phase C)."
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help=(
            "Target auto_trader base URL, e.g. https://<host> "
            "(no trailing slash; the CLI prefixes /trading/api/...)."
        ),
    )
    parser.add_argument(
        "--bundle-uuid",
        required=True,
        type=uuid.UUID,
        help="Existing snapshot_bundle_uuid the smoke will operate on.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HERMES_INGEST_TOKEN", ""),
        help=(
            "HERMES_INGEST_TOKEN value. Defaults to the env var with "
            "the same name. Never logged."
        ),
    )
    parser.add_argument(
        "--token-header",
        default=os.environ.get("HERMES_INGEST_TOKEN_HEADER", "X-Hermes-Ingest-Token"),
        help="Header name the auto_trader server expects the token on.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request HTTP timeout in seconds. Default 30.",
    )
    parser.add_argument(
        "--run-uuid",
        type=uuid.UUID,
        default=None,
        help=(
            "Optional Hermes-side run_uuid. If omitted, the CLI generates "
            "a fresh UUID4 for this run (recommended)."
        ),
    )
    parser.add_argument(
        "--fixture-set",
        choices=("kr", "us"),
        default="kr",
        help=(
            "Which Hermes fixture pair to send. ``kr`` uses the original "
            "KR/KIS-paper payloads (default); ``us`` uses the US narrow "
            "smoke payloads pinned to market='us', account_scope='alpaca_paper', "
            "status='draft'. The operator chooses the set to match the bundle "
            "they passed via --bundle-uuid."
        ),
    )
    parser.add_argument(
        "--session-cookie",
        default=os.environ.get("HERMES_SMOKE_SESSION_COOKIE", ""),
        help=(
            "Operator session cookie used for the read-surface GETs "
            "(dimension-reports + final report bundle). Those endpoints are "
            "session-authed (NOT the Hermes ingest token). When omitted the "
            "CLI skips the two GET assertions and logs a warning — the ingest "
            "chain (steps 1-6) still runs and gates exit code. Never logged."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full response bodies for each step.",
    )
    return parser.parse_args(argv)


_FIXTURE_BY_SET: dict[str, dict[str, str]] = {
    "kr": {
        "stage_artifacts": "stage_artifacts_request.json",
        "symbol_reports": "symbol_reports_request.json",
        "dimension_reports": "dimension_reports_request.json",
        "composition": "composition_request.json",
    },
    "us": {
        "stage_artifacts": "stage_artifacts_request_us.json",
        "symbol_reports": "symbol_reports_request_us.json",
        "dimension_reports": "dimension_reports_request_us.json",
        "composition": "composition_request_us.json",
    },
}


async def _post(
    client: httpx.AsyncClient,
    path: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    verbose: bool,
) -> dict[str, Any]:
    resp = await client.post(path, json=body, headers=headers)
    if resp.status_code >= 400:
        logger.error(
            "POST %s → %s\n  body: %s",
            path,
            resp.status_code,
            resp.text[:1000],
        )
        raise SystemExit(1)
    parsed = resp.json()
    if verbose:
        logger.info(
            "POST %s → 200\n  body: %s", path, json.dumps(parsed, indent=2)[:1500]
        )
    else:
        keys = ", ".join(sorted(parsed.keys())) if isinstance(parsed, dict) else "?"
        logger.info("POST %s → 200  (keys: %s)", path, keys)
    return parsed


async def _get(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    verbose: bool,
) -> dict[str, Any]:
    resp = await client.get(path, headers=headers)
    if resp.status_code >= 400:
        logger.error(
            "GET %s → %s\n  body: %s",
            path,
            resp.status_code,
            resp.text[:1000],
        )
        raise SystemExit(1)
    parsed = resp.json()
    if verbose:
        logger.info(
            "GET %s → 200\n  body: %s", path, json.dumps(parsed, indent=2)[:1500]
        )
    else:
        keys = ", ".join(sorted(parsed.keys())) if isinstance(parsed, dict) else "?"
        logger.info("GET %s → 200  (keys: %s)", path, keys)
    return parsed


_HERMES_PREFIX = "/trading/api/investment-reports/hermes"
_READ_PREFIX = "/trading/api/investment-reports"


async def _run(args: argparse.Namespace) -> int:
    if not args.token:
        logger.error("HERMES_INGEST_TOKEN is empty; provide via --token or env var.")
        return 1
    logger.info("token: %s  header: %s", _redact_token(args.token), args.token_header)

    run_uuid = args.run_uuid or uuid.uuid4()
    headers = {args.token_header: args.token, "Content-Type": "application/json"}
    # The read-surface GETs are session-authed (NOT the Hermes ingest token).
    read_cookie: str = args.session_cookie or ""
    read_headers: dict[str, str] = {"Cookie": read_cookie} if read_cookie else {}

    base_url = args.base_url.rstrip("/")
    fixtures = _FIXTURE_BY_SET[args.fixture_set]
    logger.info("base_url: %s", base_url)
    logger.info("bundle_uuid: %s", args.bundle_uuid)
    logger.info("run_uuid: %s", run_uuid)
    logger.info(
        "fixture_set: %s (stage=%s symbol=%s dimension=%s composition=%s)",
        args.fixture_set,
        fixtures["stage_artifacts"],
        fixtures["symbol_reports"],
        fixtures["dimension_reports"],
        fixtures["composition"],
    )
    logger.info(
        "read-surface GETs: %s",
        "enabled (session cookie supplied)"
        if read_cookie
        else "SKIPPED (no --session-cookie / HERMES_SMOKE_SESSION_COOKIE)",
    )

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        # 1. context export
        logger.info("--- step 1/8: context export ---")
        await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/context",
            body={"snapshot_bundle_uuid": str(args.bundle_uuid)},
            headers=headers,
            verbose=args.verbose,
        )

        # 2. stage-artifacts ingest
        logger.info("--- step 2/8: stage-artifacts ingest ---")
        stage_payload = _substitute_placeholders(
            _load_fixture(fixtures["stage_artifacts"]),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
        )
        stage_resp = await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/stage-artifacts",
            body=stage_payload,
            headers=headers,
            verbose=args.verbose,
        )
        n_artifacts = len(stage_resp.get("artifacts", []))
        n_idempotent = sum(
            1 for a in stage_resp.get("artifacts", []) if a.get("idempotent_existing")
        )
        logger.info(
            "  artifacts: %d (idempotent reuse: %d) run_status: %s",
            n_artifacts,
            n_idempotent,
            stage_resp.get("run_status"),
        )

        # 3. symbol-reports ingest (ROB-301) — capture a symbol_report_uuid.
        logger.info("--- step 3/8: symbol-reports ingest ---")
        symbol_payload = _substitute_placeholders(
            _load_fixture(fixtures["symbol_reports"]),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
        )
        symbol_resp = await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/symbol-reports",
            body=symbol_payload,
            headers=headers,
            verbose=args.verbose,
        )
        symbol_reports = symbol_resp.get("symbol_reports", [])
        if not symbol_reports:
            logger.error("symbol-reports ingest returned no rows; aborting.")
            raise SystemExit(1)
        symbol_report_uuid = symbol_reports[0]["symbol_report_uuid"]
        logger.info(
            "  symbol_reports: %d  first symbol_report_uuid: %s",
            len(symbol_reports),
            symbol_report_uuid,
        )

        # 4. dimension-reports ingest (ROB-306) — capture a dimension_report_uuid.
        logger.info("--- step 4/8: dimension-reports ingest ---")
        dimension_payload = _substitute_placeholders(
            _load_fixture(fixtures["dimension_reports"]),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
        )
        dimension_resp = await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/dimension-reports",
            body=dimension_payload,
            headers=headers,
            verbose=args.verbose,
        )
        dimension_reports = dimension_resp.get("dimension_reports", [])
        if not dimension_reports:
            logger.error("dimension-reports ingest returned no rows; aborting.")
            raise SystemExit(1)
        # Prefer the market-dimension row (it is what the read-surface GET filters
        # on); fall back to the first row if absent.
        market_dim = next(
            (d for d in dimension_reports if d.get("dimension") == "market"),
            dimension_reports[0],
        )
        dimension_report_uuid = market_dim["dimension_report_uuid"]
        logger.info(
            "  dimension_reports: %d  market dimension_report_uuid: %s",
            len(dimension_reports),
            dimension_report_uuid,
        )

        # 5. context re-pull — the freshly-ingested reports must now surface.
        logger.info("--- step 5/8: context re-pull (carries reports) ---")
        ctx2 = await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/context",
            body={"snapshot_bundle_uuid": str(args.bundle_uuid)},
            headers=headers,
            verbose=args.verbose,
        )
        ctx_dim_reports = ctx2.get("dimension_reports", [])
        ctx_symbol_reports = ctx2.get("symbol_intermediate_reports", [])
        if not ctx_dim_reports or not ctx_symbol_reports:
            logger.error(
                "context re-pull did NOT carry the ingested reports "
                "(dimension_reports=%d, symbol_intermediate_reports=%d); aborting.",
                len(ctx_dim_reports),
                len(ctx_symbol_reports),
            )
            raise SystemExit(1)
        logger.info(
            "  context carries dimension_reports: %d  symbol_intermediate_reports: %d",
            len(ctx_dim_reports),
            len(ctx_symbol_reports),
        )

        # 6. composition ingest — thread the captured report UUIDs into the payload.
        logger.info("--- step 6/8: composition ingest ---")
        composition_payload = _substitute_placeholders(
            _load_fixture(fixtures["composition"]),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
            symbol_report_uuid=symbol_report_uuid,
            dimension_report_uuid=dimension_report_uuid,
        )
        comp_resp = await _post(
            client,
            f"{base_url}{_HERMES_PREFIX}/composition",
            body=composition_payload,
            headers=headers,
            verbose=args.verbose,
        )
        report_uuid = comp_resp.get("report_uuid")
        logger.info(
            "  report_uuid: %s  items: %s  status: %s",
            report_uuid,
            comp_resp.get("items_count"),
            comp_resp.get("status"),
        )

        # 7 + 8 — read surfaces (session-authed). Skipped when no cookie supplied.
        if not read_cookie:
            logger.warning(
                "Skipping read-surface assertions (steps 7-8): no session cookie. "
                "Re-run with --session-cookie to verify the dimension-reports "
                "read surface + the held-action/new-candidate bundle grouping."
            )
            logger.info("--- ingest chain OK (read-surface GETs skipped) ---")
            return 0

        # 7. dimension-reports read surface (filtered to the market dimension).
        logger.info("--- step 7/8: dimension-reports read surface ---")
        dim_view = await _get(
            client,
            f"{base_url}{_READ_PREFIX}/runs/{run_uuid}/dimension-reports"
            "?dimension=market",
            headers=read_headers,
            verbose=args.verbose,
        )
        view_reports = dim_view.get("reports", [])
        if not view_reports:
            logger.error(
                "dimension-reports read surface returned no market reports; aborting."
            )
            raise SystemExit(1)
        logger.info(
            "  read-surface market reports: %d  stance: %s",
            len(view_reports),
            view_reports[0].get("stance"),
        )

        # 8. final report bundle — assert held-action vs new-candidate grouping.
        logger.info("--- step 8/8: final report bundle grouping ---")
        bundle = await _get(
            client,
            f"{base_url}{_READ_PREFIX}/{report_uuid}",
            headers=read_headers,
            verbose=args.verbose,
        )
        rollup = bundle.get("decision_rollup", {})
        item_groups = bundle.get("item_groups", {})
        new_candidate = rollup.get("new_candidate", [])
        held_action = rollup.get("held_action", [])
        if not new_candidate:
            logger.error(
                "final bundle decision_rollup.new_candidate is empty — the "
                "new_buy_candidate item did not classify; aborting."
            )
            raise SystemExit(1)
        logger.info(
            "  decision_rollup: new_candidate=%d held_action=%d  groups: %s",
            len(new_candidate),
            len(held_action),
            ", ".join(sorted(item_groups.keys())),
        )

    logger.info("--- round-trip OK ---")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
