"""Run one scheduleless fill/watch handoff pull.

The command is default-disabled by ``FILL_EVENT_HANDOFF_ENABLED``.  It does
not register a scheduler and never calls a broker or order surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.services.fill_event_handoff.broker_risk import TradeNotifierRiskPush
from app.services.fill_event_handoff.bundle import (
    BUNDLE_SHADOW_STATE_DIR,
    FILL_HANDOFF_BUNDLE_SHADOW_STATE_ENV,
    BundleConfig,
    FillHandoffBundleRunner,
    NullLaneEventSink,
    PanewireLaneEventSink,
    handoff_enabled,
    shadow_enabled,
)
from app.services.fill_event_handoff.shadow import (
    ShadowKickConfig,
    shadow_kick_config_from_env,
)
from app.services.lane_events import (
    LANE_PATTERN,
    lane_event_config_from_env,
)


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("watermark must be non-negative")
    return parsed


def _lanes(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(market, str) and isinstance(lane, str)
        for market, lane in parsed.items()
    ):
        raise ValueError("FILL_HANDOFF_LANES must be a string map")
    if not set(parsed).issubset({"crypto", "kr", "us"}) or not all(
        LANE_PATTERN.fullmatch(lane) for lane in parsed.values()
    ):
        raise ValueError("FILL_HANDOFF_LANES must map crypto, kr, or us to valid lanes")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-fill-id", type=_nonnegative)
    parser.add_argument("--since-watch-id", type=_nonnegative)
    parser.add_argument("--state-dir", type=Path, default=None)
    return parser.parse_args(argv)


def _default_state_dir(*, shadow: bool) -> Path:
    if shadow:
        # Shadow keeps its own cursor/state world; production watermarks must
        # never be advanced by a shadow run.
        return Path(
            os.getenv(FILL_HANDOFF_BUNDLE_SHADOW_STATE_ENV, BUNDLE_SHADOW_STATE_DIR)
        )
    return Path(
        os.getenv("FILL_HANDOFF_BUNDLE_STATE_DIR", "/var/lib/fill-handoff-bundle")
    )


def _delivery_runtime(
    enabled: bool,
    *,
    shadow: bool = False,
) -> tuple[dict[str, str], Any, int, int, float]:
    if shadow:
        # Shadow evaluates the same outbound shape (lane map and batch knobs
        # parse exactly like the enabled path) but the transport is forced to
        # the Null implementation — nothing leaves the process.
        lanes = _lanes(os.getenv("FILL_HANDOFF_LANES"))
        return (
            lanes,
            NullLaneEventSink(),
            int(os.getenv("FILL_HANDOFF_BATCH_LIMIT", "500")),
            int(os.getenv("FILL_HANDOFF_LOOKBACK_IDS", "256")),
            float(os.getenv("FILL_HANDOFF_SINK_TIMEOUT_S", "3")),
        )
    if not enabled:
        # The master gate owns the whole outbound configuration surface. This
        # lets a disabled catch-up run advance durable cursors even if stale
        # delivery variables are malformed.
        return {}, NullLaneEventSink(), 500, 256, 3.0
    lanes = _lanes(os.getenv("FILL_HANDOFF_LANES"))
    sink = (
        PanewireLaneEventSink(
            lane_event_config_from_env(
                prefix_fallbacks=("FILL_HANDOFF_EMIT", "LANE_EVENT_EMIT")
            )
        )
        if lanes
        else NullLaneEventSink()
    )
    return (
        lanes,
        sink,
        int(os.getenv("FILL_HANDOFF_BATCH_LIMIT", "500")),
        int(os.getenv("FILL_HANDOFF_LOOKBACK_IDS", "256")),
        float(os.getenv("FILL_HANDOFF_SINK_TIMEOUT_S", "3")),
    )


async def main_async(
    *,
    state_dir: Path | None = None,
    since_fill_id: int | None = None,
    since_watch_id: int | None = None,
) -> dict[str, Any]:
    from app.core.db import AsyncSessionLocal

    enabled = handoff_enabled()
    shadow = shadow_enabled()
    lanes, sink, batch_limit, lookback_ids, sink_timeout_s = _delivery_runtime(
        enabled, shadow=shadow
    )
    runner = FillHandoffBundleRunner(
        BundleConfig(
            state_dir=state_dir or _default_state_dir(shadow=shadow),
            lanes=lanes,
            batch_limit=batch_limit,
            lookback_ids=lookback_ids,
            sink_timeout_s=sink_timeout_s,
            since_fill_id=since_fill_id,
            since_watch_id=since_watch_id,
            shadow=shadow,
            # Kick envs are only read under shadow so a malformed kick knob
            # can never break the production bundle path.
            shadow_kick=shadow_kick_config_from_env() if shadow else ShadowKickConfig(),
        ),
        # Under shadow the runner builds its own Null transports; passing
        # nothing keeps injected transports out of the observation path.
        sink=None if shadow else sink,
        # The Telegram-backed notifier is never even constructed under shadow.
        notifier=None if shadow else TradeNotifierRiskPush(),
    )
    async with AsyncSessionLocal() as db:
        result = await runner.run(db)
    result["transport"] = (
        "shadow" if shadow else ("panewire_local_inbox" if lanes else "disabled")
    )
    result["configured_markets"] = sorted(lanes)
    if result.get("shadow") is not None:
        # The runner also logs this line; stderr is the channel journalctl and
        # docker logs actually capture for this script.
        print(
            "fill_handoff_bundle_shadow "
            + json.dumps(result["shadow"], ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(
            main_async(
                state_dir=args.state_dir,
                since_fill_id=args.since_fill_id,
                since_watch_id=args.since_watch_id,
            )
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - safe type-only operator signal
        print(f"fill-handoff-bundle: {type(exc).__name__}", file=sys.stderr)
        return 1
    if handoff_enabled() and result["transport"] == "disabled":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
