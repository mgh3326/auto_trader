"""ROB-287 Phase C — operator-runnable Hermes round-trip smoke CLI.

Drives the four Hermes HTTP endpoints in sequence against a target
auto_trader instance and reports per-step status. Loads the same
Hermes-produced JSON fixtures the round-trip test uses, so the CLI
contract stays in lock-step with the test contract.

Usage::

    uv run python -m scripts.hermes_roundtrip_smoke \\
        --base-url https://<auto_trader-host> \\
        --token "<HERMES_INGEST_TOKEN value>" \\
        --bundle-uuid <existing-bundle-uuid>

Exit codes::

    0 — full chain succeeded (context + stage-artifacts + composition).
    1 — at least one step failed; stderr carries the response body for
        diagnosis. Re-runnable: the stage-artifacts ingest is
        idempotent so a partial run can be resumed safely.

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
    payload: dict[str, Any], *, run_uuid: uuid.UUID, snapshot_bundle_uuid: uuid.UUID
) -> dict[str, Any]:
    raw = json.dumps(payload)
    raw = raw.replace("{{run_uuid}}", str(run_uuid))
    raw = raw.replace("{{snapshot_bundle_uuid}}", str(snapshot_bundle_uuid))
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
        "--verbose",
        action="store_true",
        help="Print full response bodies for each step.",
    )
    return parser.parse_args(argv)


_FIXTURE_BY_SET: dict[str, tuple[str, str]] = {
    "kr": ("stage_artifacts_request.json", "composition_request.json"),
    "us": ("stage_artifacts_request_us.json", "composition_request_us.json"),
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


async def _run(args: argparse.Namespace) -> int:
    if not args.token:
        logger.error("HERMES_INGEST_TOKEN is empty; provide via --token or env var.")
        return 1
    logger.info("token: %s  header: %s", _redact_token(args.token), args.token_header)

    run_uuid = args.run_uuid or uuid.uuid4()
    headers = {args.token_header: args.token, "Content-Type": "application/json"}

    base_url = args.base_url.rstrip("/")
    stage_fixture, composition_fixture = _FIXTURE_BY_SET[args.fixture_set]
    logger.info("base_url: %s", base_url)
    logger.info("bundle_uuid: %s", args.bundle_uuid)
    logger.info("run_uuid: %s", run_uuid)
    logger.info(
        "fixture_set: %s (stage=%s composition=%s)",
        args.fixture_set,
        stage_fixture,
        composition_fixture,
    )

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        # 1. context export
        logger.info("--- step 1/3: context export ---")
        await _post(
            client,
            f"{base_url}/trading/api/investment-reports/hermes/context",
            body={"snapshot_bundle_uuid": str(args.bundle_uuid)},
            headers=headers,
            verbose=args.verbose,
        )

        # 2. stage-artifacts ingest
        logger.info("--- step 2/3: stage-artifacts ingest ---")
        stage_payload = _substitute_placeholders(
            _load_fixture(stage_fixture),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
        )
        stage_resp = await _post(
            client,
            f"{base_url}/trading/api/investment-reports/hermes/stage-artifacts",
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

        # 3. composition ingest
        logger.info("--- step 3/3: composition ingest ---")
        composition_payload = _substitute_placeholders(
            _load_fixture(composition_fixture),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=args.bundle_uuid,
        )
        comp_resp = await _post(
            client,
            f"{base_url}/trading/api/investment-reports/hermes/composition",
            body=composition_payload,
            headers=headers,
            verbose=args.verbose,
        )
        logger.info(
            "  report_uuid: %s  items: %s  status: %s",
            comp_resp.get("report_uuid"),
            comp_resp.get("items_count"),
            comp_resp.get("status"),
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
