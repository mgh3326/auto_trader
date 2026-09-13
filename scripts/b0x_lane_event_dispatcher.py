"""Dispatch one durable B0X ingress item through the fixed runner registry."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.services.b0x_lane_event_dispatcher import (
    B0XDispatchError,
    Executor,
    dispatch_once,
    dispatch_readback,
    load_dispatch_binding,
)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("path must be absolute and canonical")
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=_absolute_path)
    parser.add_argument("--state-db", required=True, type=_absolute_path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--readback", action="store_true")
    parser.add_argument("--lane")
    parser.add_argument("--event-id")
    args = parser.parse_args(argv)
    if args.readback and (not args.lane or not args.event_id):
        parser.error("--readback requires --lane and --event-id")
    if args.once and (args.lane is not None or args.event_id is not None):
        parser.error("--lane/--event-id are readback-only")
    return args


def _emit(value: object) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    executor: Executor | None = None,
    git_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    attempt_id_factory: Callable[[], str] | None = None,
    fault: Callable[[str], None] | None = None,
) -> int:
    """Run the production CLI shape with hermetic injection seams for tests."""

    args = parse_args(argv)
    try:
        binding = load_dispatch_binding(
            args.binding, state_db=args.state_db, git_runner=git_runner
        )
        if args.readback:
            _emit(
                dispatch_readback(
                    binding, lane=str(args.lane), event_id=str(args.event_id)
                ).as_dict()
            )
            return 0
        kwargs: dict[str, object] = {}
        if executor is not None:
            kwargs["executor"] = executor
        if attempt_id_factory is not None:
            kwargs["attempt_id_factory"] = attempt_id_factory
        if fault is not None:
            kwargs["fault"] = fault
        receipt = dispatch_once(
            binding,
            args.state_db,
            clock or (lambda: datetime.now(UTC)),
            **kwargs,  # type: ignore[arg-type]
        )
        _emit(receipt.as_dict())
        return 0
    except B0XDispatchError as exc:
        _emit(
            {
                "children_started": 0,
                "cycle_starts": 0,
                "push_reapplications": 0,
                "reason": exc.code,
                "status": "contract_error",
                "terminal_verified": False,
            }
        )
        return 2
    except BaseException:
        _emit(
            {
                "children_started": 0,
                "cycle_starts": 0,
                "push_reapplications": 0,
                "reason": "internal_error",
                "status": "error",
                "terminal_verified": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
