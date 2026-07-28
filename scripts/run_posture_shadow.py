"""Run one posture-v1 shadow rep from a read-only captured JSON snapshot.

Examples:
    # Shipped policy is default-off: prints disabled and reads/writes nothing.
    uv run python -m scripts.run_posture_shadow \
      --input snapshot.json --output posture-coverage.jsonl

    # After an operator-approved policy PR sets posture.enabled=true, the same
    # command appends one coverage-only JSONL record.

There is no proposal, notification, scheduler, or broker client in this module.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from app.schemas.trading_policy import TradingPolicyDocument
from app.services.posture_generator import PostureGeneratorInput
from app.services.posture_shadow_service import (
    append_shadow_jsonl,
    run_posture_shadow,
)

_DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "trading_policy.yaml"
)


def _load_policy(path: Path) -> tuple[TradingPolicyDocument, str]:
    import hashlib

    raw = path.read_bytes()
    policy = TradingPolicyDocument.model_validate(yaml.safe_load(raw))
    return policy, hashlib.sha256(raw).hexdigest()[:12]


def _load_snapshot(path: Path) -> PostureGeneratorInput:
    return PostureGeneratorInput.model_validate_json(path.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-1106 manual posture-v1 shadow coverage recorder"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Captured holdings/quotes/policy_contexts JSON (read-only).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Append-only local JSONL coverage artifact.",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=_DEFAULT_POLICY_PATH,
        help="Operator-approved trading policy YAML.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    policy, content_hash = _load_policy(args.policy)
    outcome = run_posture_shadow(
        posture_policy=policy.posture,
        policy_version=policy.version,
        policy_content_hash=content_hash,
        snapshot_loader=lambda: _load_snapshot(args.input),
        recorder=lambda result: append_shadow_jsonl(args.output, result),
    )

    summary: dict[str, object] = {
        "status": outcome.status,
        "reason": outcome.reason,
        "policy_version": policy.version,
        "policy_content_hash": content_hash,
    }
    if outcome.result is not None:
        summary["coverage"] = outcome.result.coverage.model_dump(mode="json")
        summary["unmapped_holdings"] = [
            row.model_dump(mode="json") for row in outcome.result.unmapped_holdings
        ]
        summary["safety"] = outcome.result.safety.model_dump(mode="json")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
