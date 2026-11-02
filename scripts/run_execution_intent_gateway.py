"""ROB-1461 — run the intent gateway route on loopback, in the foreground.

An operator starts this by hand for a mock run and stops it afterwards. No
scheduler, unit or restart policy starts it. See
docs/runbooks/execution-intent-gateway.md.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

DEFAULT_PORT = 8782
REPO_ROOT = Path(__file__).resolve().parents[1]


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1", help="A loopback address. Anything else exits 2."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not is_loopback(args.host):
        print(f"refused: {args.host} is not a loopback address", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        "app.execution_intent_gateway_server:create_app",
        factory=True,
        # Run as a script, sys.path holds scripts/ and not the repository root.
        app_dir=str(REPO_ROOT),
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
