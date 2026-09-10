"""Poll one closed handoffkeep GET feed into the durable B0X consumer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.services.b0x_lane_event_poller import (
    B0XIngressError,
    B0XIngressHTTPError,
    activation_blockers,
    ingress_readback,
    load_binding,
    poll_once,
    stable_host_lock,
)

ROOT = Path(__file__).resolve().parents[1]


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("path must be absolute and canonical")
    return path


def _lane(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 128:
        raise argparse.ArgumentTypeError("lane is invalid")
    return value


def _event_id(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 512:
        raise argparse.ArgumentTypeError("event_id is invalid")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=_absolute_path)
    parser.add_argument("--state-db", required=True, type=_absolute_path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--readback", action="store_true")
    parser.add_argument("--lane", type=_lane)
    parser.add_argument("--event-id", type=_event_id)
    args = parser.parse_args(argv)
    if args.readback and (args.lane is None or args.event_id is None):
        parser.error("--readback requires --lane and --event-id")
    if args.once and (args.lane is not None or args.event_id is not None):
        parser.error("--lane/--event-id are readback-only")
    return args


def _git_head(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    result = runner(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise B0XIngressError("runtime_code_head_unavailable")
    head = result.stdout.strip()
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise B0XIngressError("runtime_code_head_invalid")
    return head


def _emit(value: Mapping[str, object]) -> None:
    print(json.dumps(dict(value), separators=(",", ":"), sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    http_transport: httpx.BaseTransport | None = None,
    processing_at: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    runtime_code_head: str | None = None,
    code_head_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    monotonic: Callable[[], float] | None = None,
    fault: Callable[[str, int | None], None] | None = None,
) -> int:
    """Run the real CLI path with injectable I/O boundaries for offline tests."""

    args = parse_args(argv)
    instant = processing_at or datetime.now(UTC)
    try:
        binding = load_binding(args.binding, state_db=args.state_db)
        with stable_host_lock(binding):
            if args.readback:
                _emit(
                    ingress_readback(
                        binding,
                        lane=args.lane,
                        event_id=args.event_id,
                    )
                )
                return 0
            provisional_head = (
                runtime_code_head or binding.activation.code_head or ("0" * 40)
            )
            blockers = activation_blockers(
                binding,
                now=instant,
                runtime_code_head=provisional_head,
            )
            if blockers:
                _emit(
                    {
                        "business_consumed_count": 0,
                        "cycle_created_count": 0,
                        "dispatch_queued_count": 0,
                        "dispatch_started_count": 0,
                        "ingress_durably_recorded_count": 0,
                        "poll_http_success": False,
                        "reasons": list(blockers),
                        "status": "blocked",
                        "terminal_evidence_count": 0,
                    }
                )
                return 0
            effective_head = runtime_code_head or _git_head(code_head_runner)
            blockers = activation_blockers(
                binding,
                now=instant,
                runtime_code_head=effective_head,
            )
            if blockers:
                _emit(
                    {
                        "business_consumed_count": 0,
                        "cycle_created_count": 0,
                        "dispatch_queued_count": 0,
                        "dispatch_started_count": 0,
                        "ingress_durably_recorded_count": 0,
                        "poll_http_success": False,
                        "reasons": list(blockers),
                        "status": "blocked",
                        "terminal_evidence_count": 0,
                    }
                )
                return 0
            kwargs: dict[str, object] = {}
            if monotonic is not None:
                kwargs["monotonic"] = monotonic
            if fault is not None:
                kwargs["fault"] = fault
            result = poll_once(
                binding,
                processing_at=instant,
                http_transport=http_transport,
                environ=os.environ if environ is None else environ,
                **kwargs,
            )
            output = result.as_dict()
            output["business_consumed_count"] = sum(
                row.business_disposition
                in {
                    "queued_cycle",
                    "queued_policy_table_build",
                    "observed_harvest_no_cycle",
                }
                for row in result.rows
            )
            _emit(output)
            return 0
    except B0XIngressHTTPError as exc:
        _emit(
            {
                "business_consumed_count": 0,
                "cycle_created_count": 0,
                "dispatch_started_count": 0,
                "poll_http_success": False,
                "reason": exc.code,
                "status": "http_error",
                "terminal_evidence_count": 0,
            }
        )
        return 1
    except B0XIngressError as exc:
        _emit(
            {
                "business_consumed_count": 0,
                "cycle_created_count": 0,
                "dispatch_started_count": 0,
                "poll_http_success": False,
                "reason": exc.code,
                "status": "contract_error",
                "terminal_evidence_count": 0,
            }
        )
        return 2
    except BaseException:
        _emit(
            {
                "business_consumed_count": 0,
                "cycle_created_count": 0,
                "dispatch_started_count": 0,
                "poll_http_success": False,
                "reason": "internal_error",
                "status": "error",
                "terminal_evidence_count": 0,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
