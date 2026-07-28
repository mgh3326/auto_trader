"""ROB-1115 typed strategy-learning-event recorder (#1700 follow-up, N6).

Minimal-path operator CLI around ``app.services.strategy_learning_event_service``.
There is no service/router/scheduler wiring here — this only gives operators a
typed, enum-validated way to append rows without hand-written SQL.

Dry-run is the default: the CLI always builds and validates a
``StrategyLearningEventRequest`` (enum + shape checks included) and prints the
canonical payload plus derived ``request_hash`` / ``memory_event_id``. Only
``--commit`` opens a DB session and calls ``record_learning_event``.

Payload input is JSON, either inline (``--payload-json``) or from a file
(``--payload-file``), so the two-part JSONB fields (``failure_fingerprint``,
``learning_payload``) are expressed the same way the API/tests express them.
Field-by-field flags are intentionally omitted — cramming ``learning_payload``'s
9-field contract into argparse flags would just duplicate the Pydantic schema
with a worse error surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from pydantic import ValidationError

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append one typed research.strategy_learning_events row (ROB-1115). "
            "Dry-run by default; pass --commit to actually write."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--payload-json",
        help="Inline JSON object matching StrategyLearningEventRequest.",
    )
    source.add_argument(
        "--payload-file",
        help="Path to a JSON file matching StrategyLearningEventRequest.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the row. Without this flag the CLI only validates "
        "and prints the payload (default behavior).",
    )
    return parser


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json is not None:
        raw = args.payload_json
    else:
        with open(args.payload_file, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON payload: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("payload must be a JSON object")
    return data


def _build_request(payload: dict[str, Any]):
    from app.schemas.strategy_learning_event import StrategyLearningEventRequest

    try:
        return StrategyLearningEventRequest.model_validate(payload)
    except ValidationError as exc:
        # Enum / shape rejection surfaces here — this is the "record refusal"
        # path: invalid failure_class, verdict, stage, or malformed
        # reason_codes/evidence_refs/failure_fingerprint/learning_payload all
        # raise before any DB call is attempted.
        raise SystemExit(f"payload rejected by typed contract:\n{exc}") from exc


def _print_preview(request: Any) -> None:
    from app.schemas.strategy_learning_event import canonical_event_request_payload
    from app.services.strategy_learning_event_service import (
        compute_learning_event_request_hash,
        derive_memory_event_id,
    )

    request_hash = compute_learning_event_request_hash(request)
    memory_event_id = derive_memory_event_id(
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
    )
    preview = {
        "mode": "dry_run",
        "request_hash": request_hash,
        "memory_event_id": memory_event_id,
        "request": canonical_event_request_payload(request),
        "idempotency_key": request.idempotency_key,
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))


async def _commit(request: Any) -> dict[str, Any]:
    from app.core.db import AsyncSessionLocal
    from app.services.strategy_learning_event_service import (
        LearningEventExperimentNotFound,
        LearningEventIdempotencyConflict,
        StoredLearningEventInvalid,
        record_learning_event,
        to_learning_event_record,
    )

    async with AsyncSessionLocal() as db:
        try:
            row = await record_learning_event(db, request)
            await db.commit()
        except (
            LearningEventExperimentNotFound,
            LearningEventIdempotencyConflict,
            StoredLearningEventInvalid,
        ) as exc:
            await db.rollback()
            raise SystemExit(f"record refused: {exc}") from exc
        record = to_learning_event_record(row)
    return {
        "mode": "committed",
        "memory_event_id": record.memory_event_id,
        "request_hash": record.request_hash,
        "created_at": record.created_at.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args(argv)
    payload = _load_payload(args)
    request = _build_request(payload)

    if not args.commit:
        _print_preview(request)
        return 0

    result = asyncio.run(_commit(request))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
