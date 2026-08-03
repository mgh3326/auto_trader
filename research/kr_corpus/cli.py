"""Command-line entry point for the fixed kr-corpus-v1 job."""

from __future__ import annotations

import json
import sys

from .collector import CorpusCollector
from .config import FROZEN_CONFIG
from .pacing import RequestPacer
from .source import PykrxSource
from .state import StateStore
from .storage import build_snapshot_paths


def main() -> int:
    config = FROZEN_CONFIG
    paths = build_snapshot_paths(config)
    main_state = StateStore(paths.main_state)
    holdout_state = StateStore(paths.holdout_state)
    try:
        pacer = RequestPacer(
            min_interval_sec=config.min_request_interval_sec,
            max_requests=config.max_requests,
        )
        source = PykrxSource(config, pacer)
        result = CorpusCollector(
            config,
            source,
            pacer,
            main_state,
            holdout_state,
        ).run()
        print(
            json.dumps(
                {
                    "terminal_verdict": result.terminal_verdict,
                    "request_budget_projected": result.request_budget_projected,
                    "requests_actual": result.requests_actual,
                    "artifact_root": str(result.artifact_root)
                    if result.artifact_root
                    else None,
                    "holdout_root": str(result.holdout_root)
                    if result.holdout_root
                    else None,
                    "stop_reason": result.stop_reason,
                    "crosscheck_verified": result.crosscheck_verified,
                },
                sort_keys=True,
            )
        )
        return 0 if result.terminal_verdict != "BLOCKED_PRECONDITION" else 2
    finally:
        main_state.close()
        holdout_state.close()


if __name__ == "__main__":  # pragma: no cover - direct entry point
    sys.exit(main())
