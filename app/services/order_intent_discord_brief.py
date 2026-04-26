"""Pure formatter for Decision Desk → Discord handoff brief.

Contract:
- No DB / Redis / httpx / settings / env imports.
- No I/O, no logging side effects, no global state.
- Inputs in → string out. Deterministic for fixed inputs.
"""

from __future__ import annotations

from urllib.parse import quote


def build_decision_desk_url(base_url: str, run_id: str) -> str:
    """Compose `<origin>/portfolio/decision?run_id=<quoted-id>`.

    Pure string operation. Strips trailing slashes from the origin and
    percent-encodes the run id with no safe characters reserved.
    """
    base = base_url.rstrip("/")
    return f"{base}/portfolio/decision?run_id={quote(run_id, safe='')}"
