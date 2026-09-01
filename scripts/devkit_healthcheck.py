"""Bounded loopback-only health probe used by ``make dev-verify``."""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from urllib.error import URLError
from urllib.request import urlopen

LOOPBACK_HOST = "127.0.0.1"
HEALTH_PATH = "/healthz"


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _parse_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 1.0 <= timeout <= 60.0:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 60 seconds")
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=_parse_port)
    parser.add_argument(
        "--timeout-seconds", required=True, type=_parse_timeout, metavar="SECONDS"
    )
    return parser


def _health_url(port: int) -> str:
    return f"http://{LOOPBACK_HOST}:{port}{HEALTH_PATH}"


def wait_for_health(*, port: int, timeout_seconds: float) -> None:
    url = _health_url(port)
    deadline = time.monotonic() + timeout_seconds
    last_error = "server did not answer"

    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:  # noqa: S310 - loopback-only
                if response.status == 200:
                    print(f"dev health check passed: {url}")
                    return
                last_error = f"received HTTP {response.status}"
        except (OSError, URLError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.25)

    raise RuntimeError(
        f"dev health check timed out after {timeout_seconds:g}s ({last_error})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wait_for_health(port=args.port, timeout_seconds=args.timeout_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
