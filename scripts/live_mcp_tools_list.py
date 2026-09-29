#!/usr/bin/env python3
"""Read-only smoke for the dedicated live-* MCP units (task 975).

For each of at-mcp-live-kr / -us / -crypto it calls exactly three MCP
methods through the HAProxy tailnet frontend: ``initialize``,
``notifications/initialized`` and ``tools/list``. No tool is ever called.
It prints each endpoint's tool count and names and compares them with
config/mcp_profiles/live.yaml:

* ``MATCH``          the served set equals the manifest selection
* ``MATCH_GATES_OFF`` it equals the selection minus the flag-gated entries
  (ORDER_PROPOSALS_ENABLED is off in that unit's env)
* ``MISMATCH``       anything else; extra and missing names are printed

Bearer tokens come from the process environment (MCP_LIVE_KR_AUTH_TOKEN,
MCP_LIVE_US_AUTH_TOKEN, MCP_LIVE_CRYPTO_AUTH_TOKEN), for example via the
deploy's own ``docker run --env-file`` pair. Token values never reach
output: errors name the endpoint and the failed step only.

Exit 0 when every checked endpoint is MATCH or MATCH_GATES_OFF, 1 otherwise,
2 on usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "config" / "mcp_profiles" / "live.yaml"
TAILNET_HOST = "100.122.100.56"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class LiveUnit:
    profile: str
    port: int
    token_env: str


UNITS = (
    LiveUnit("live-kr", 8773, "MCP_LIVE_KR_AUTH_TOKEN"),
    LiveUnit("live-us", 8774, "MCP_LIVE_US_AUTH_TOKEN"),
    LiveUnit("live-crypto", 8775, "MCP_LIVE_CRYPTO_AUTH_TOKEN"),
)


class SmokeError(RuntimeError):
    """A failure whose message never contains a token or a header."""


def manifest_selection(
    profile: str, path: Path = MANIFEST
) -> tuple[set[str], set[str]]:
    """(every selected name, the flag-gated subset) for ``profile``."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    groups = document["profiles"][profile]["groups"]
    selected: set[str] = set()
    gated: set[str] = set()
    for entries in groups.values():
        for entry in entries or ():
            selected.add(entry["name"])
            if entry.get("gate"):
                gated.add(entry["name"])
    return selected, gated


def _decode(body: bytes, content_type: str) -> dict:
    text = body.decode("utf-8")
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                if isinstance(payload, dict) and "id" in payload:
                    return payload
        raise SmokeError("no JSON-RPC response in the event stream")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise SmokeError("response is not a JSON object")
    return payload


def _post(
    url: str, token: str, message: dict, session_id: str | None, step: str
) -> tuple[dict | None, str | None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            new_session = response.headers.get("Mcp-Session-Id") or session_id
    except urllib.error.HTTPError as error:
        raise SmokeError(f"{step}: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise SmokeError(f"{step}: {type(reason).__name__}") from None
    if "id" not in message:
        return None, new_session
    try:
        payload = _decode(body, content_type)
    except (ValueError, UnicodeDecodeError):
        raise SmokeError(f"{step}: unreadable response") from None
    if "error" in payload:
        code = (
            payload["error"].get("code") if isinstance(payload["error"], dict) else None
        )
        raise SmokeError(f"{step}: JSON-RPC error {code}")
    return payload, new_session


def list_tools(url: str, token: str) -> list[str]:
    """initialize, notifications/initialized, tools/list (all pages)."""
    _, session = _post(
        url,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "live-mcp-tools-list", "version": "1"},
            },
        },
        None,
        "initialize",
    )
    _post(
        url,
        token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session,
        "initialized",
    )
    names: list[str] = []
    cursor: str | None = None
    for request_id in range(2, 52):
        params = {"cursor": cursor} if cursor else {}
        payload, session = _post(
            url,
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": params,
            },
            session,
            "tools/list",
        )
        result = (payload or {}).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise SmokeError("tools/list: malformed result")
        names.extend(str(tool.get("name")) for tool in result["tools"])
        cursor = result.get("nextCursor")
        if not cursor:
            return names
    raise SmokeError("tools/list: too many pages")


def verdict(served: set[str], selected: set[str], gated: set[str]) -> str:
    if served == selected:
        return "MATCH"
    if served == selected - gated:
        return "MATCH_GATES_OFF"
    return "MISMATCH"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=TAILNET_HOST, help="HAProxy tailnet address")
    parser.add_argument(
        "--only",
        choices=[unit.profile for unit in UNITS],
        action="append",
        help="check only this profile (repeatable)",
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        default=[],
        metavar="PROFILE=URL",
        help="override one endpoint URL (tests)",
    )
    args = parser.parse_args(argv)
    overrides: dict[str, str] = {}
    for item in args.endpoint:
        profile, sep, url = item.partition("=")
        if not sep or profile not in {unit.profile for unit in UNITS}:
            parser.error(f"--endpoint must be PROFILE=URL, got {item!r}")
        overrides[profile] = url

    failed = False
    for unit in UNITS:
        if args.only and unit.profile not in args.only:
            continue
        url = overrides.get(unit.profile, f"http://{args.host}:{unit.port}/mcp")
        token = os.environ.get(unit.token_env, "").strip()
        if not token:
            print(f"{unit.profile}\t{url}\tERROR\t{unit.token_env} is not set")
            failed = True
            continue
        try:
            served = list_tools(url, token)
        except SmokeError as error:
            print(f"{unit.profile}\t{url}\tERROR\t{error}")
            failed = True
            continue
        selected, gated = manifest_selection(unit.profile)
        served_set = set(served)
        result = verdict(served_set, selected, gated)
        failed = failed or result == "MISMATCH" or len(served) != len(served_set)
        print(
            f"{unit.profile}\t{url}\t{result}\ttools={len(served)} manifest={len(selected)} gated={len(gated)}"
        )
        for name in sorted(served_set):
            print(f"  {name}")
        if result == "MISMATCH":
            print(f"  extra (served, not in manifest): {sorted(served_set - selected)}")
            print(f"  missing (manifest, not served): {sorted(selected - served_set)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
