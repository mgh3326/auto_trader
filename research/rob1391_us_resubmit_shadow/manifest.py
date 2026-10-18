"""T0 manifest: what is sealed, and how every later run is checked against it.

Sealed (prereg §2 실행 전 봉인 조건): operator approval reference, prereg
sha256 (the file itself is never copied), code git SHA, deployed policy
``{version, content_hash}`` (re-checked against the local policy service),
fee schedule (content + sha256, or absent -> net tests pending), market-data
provider/host/feed, fill models, exit policy, E0, caps, the official session
calendar for the whole horizon, seed and analysis constants.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow.costs import FeeSchedule
from research.rob1391_us_resubmit_shadow.ledger import canonical_json, sha256_bytes
from research.rob1391_us_resubmit_shadow.market_data import DATA_HOST, FEEDS
from research.rob1391_us_resubmit_shadow.us_calendar import (
    USCalendar,
    calendar_for,
    exchange_calendars_version,
)

# Every rule that departs from the pre-registration text is a named
# deviation. The prereg itself says such changes are the operator's decision,
# so nothing here is applied silently:
#   * MANDATORY ones are in effect by construction (no prereg-literal
#     implementation exists) -> ``seal`` refuses until each is approved;
#   * OPTIONAL ones relax a strict prereg-literal default -> off unless
#     approved at seal.
DEVIATIONS: dict[str, tuple[bool, str]] = {
    "D1_exit_hold_to_d20": (
        True,
        "the live exit policy is session judgment and cannot be replayed; both "
        "arms hold to D+20 and are marked net of liquidation cost",
    ),
    "D2_resubmit_gate_subset": (
        True,
        "D1-D3 gate re-validation = sealed policy hash unchanged + halt/stale + "
        "split/merge review + first-quote spread <= 30bp + remaining/duplicate/"
        "virtual cash; screening/support/deep-band gates are not re-run",
    ),
    "D3_budget_fill_notional_only": (
        False,
        "budget cap counts fill notional only (prereg: fill + cost <= B_i); "
        "costs are still charged in net results",
    ),
    "D4_policy_epoch_invariant_basis": (
        False,
        "accept parents without an order-time policy stamp, relying on the "
        "per-run sealed-policy re-check (prereg: policy hash complete per parent)",
    ),
    "D5_accept_outside_regular_session": (
        False,
        "accept DAY orders accepted outside regular hours that work in the next "
        "regular session (prereg: regular-session DAY acceptance)",
    ),
    "D6_ledger_budget_basis": (
        False,
        "B_i = ledger price x quantity when no approved proposal rung exists "
        "(prereg: approved budget B_i)",
    ),
}
MANDATORY_DEVIATIONS = frozenset(k for k, (m, _) in DEVIATIONS.items() if m)

# Sessions tracked after the window: D0 of the last window session + 23
# (D3 fill + 20 for the auxiliary metric).
HORIZON_AFTER_WINDOW = K.RESUBMIT_SESSIONS + K.AUX_FILL_OFFSET
LOOKBACK_BEFORE_WINDOW = K.LIQUIDITY_LOOKBACK_SESSIONS + K.DEDUPE_MAX_SESSION_GAP + 1


class ManifestError(RuntimeError):
    pass


def calendar_block(cal: USCalendar, start_day: Any) -> dict[str, Any]:
    window = cal.window(start_day, K.WINDOW_SESSIONS)
    first = cal.index(window[0]) - LOOKBACK_BEFORE_WINDOW
    last = cal.index(window[-1]) + HORIZON_AFTER_WINDOW
    span = [
        cal.offset(window[0], i - cal.index(window[0])) for i in range(first, last + 1)
    ]
    return {
        "name": "XNYS",
        "library": "exchange_calendars",
        "library_version": exchange_calendars_version(),
        "sessions": [cal.session(d).as_record() for d in span],
    }


def build_manifest(
    *,
    seal_at: datetime,
    approval_ref: str,
    prereg_path: Path,
    code_sha: str,
    policy_stamp: dict[str, str],
    deployed_policy: dict[str, str],
    feed: str,
    fee_schedule_path: Path | None,
    approved_deviations: frozenset[str] | set[str] = frozenset(),
    deviation_ref: str | None = None,
) -> dict[str, Any]:
    if seal_at.tzinfo is None:
        raise ManifestError("seal time must be timezone-aware")
    if not approval_ref.strip():
        raise ManifestError("--approval-ref is required (operator approval evidence)")
    if feed not in FEEDS:
        raise ManifestError(f"feed must be one of {FEEDS}")
    approved = frozenset(approved_deviations)
    unknown = approved - set(DEVIATIONS)
    if unknown:
        raise ManifestError(f"unknown deviation id(s): {sorted(unknown)}")
    missing = MANDATORY_DEVIATIONS - approved
    if missing:
        raise ManifestError(
            "seal refused: prereg-incompatible rule(s) in effect without operator "
            f"approval: {sorted(missing)} -- pass --approve-deviation for each and "
            "--deviation-ref <operator-desk tag or hk:doc>"
        )
    if approved and not (deviation_ref or "").strip():
        raise ManifestError(
            "approved deviations need --deviation-ref (operator approval)"
        )
    if deployed_policy != policy_stamp:
        raise ManifestError(
            "deployed get_trading_policy {version, content_hash} differs from this "
            "checkout's policy file; seal from the deployed revision"
        )
    prereg_bytes = prereg_path.read_bytes()
    fee_block: dict[str, Any] | None = None
    if fee_schedule_path is not None:
        fee_bytes = fee_schedule_path.read_bytes()
        fee_raw = json.loads(fee_bytes)
        FeeSchedule.parse(fee_raw)  # validates; raises on any malformed field
        fee_block = {"sha256": sha256_bytes(fee_bytes), "content": fee_raw}

    cal = calendar_for(seal_at.astimezone(UTC).date())
    start = cal.first_session_opening_after(seal_at)
    window = cal.window(start, K.WINDOW_SESSIONS)
    return {
        "experiment_id": K.EXPERIMENT_ID,
        "epoch_id": f"{K.EXPERIMENT_ID}:epoch-{start.isoformat()}",
        "sealed_at_utc": seal_at.astimezone(UTC).isoformat(),
        "operator_approval_ref": approval_ref.strip(),
        "prereg_sha256": sha256_bytes(prereg_bytes),
        "code_git_sha": code_sha,
        "policy": dict(policy_stamp),
        "deployed_policy_echo": dict(deployed_policy),
        "fee_schedule": fee_block,
        "net_metrics_status": "SEALED" if fee_block else "PENDING_FEE_SCHEDULE",
        "deviations": {
            "approved": sorted(approved),
            "ref": (deviation_ref or "").strip() or None,
            "catalog": {
                k: {"mandatory": m, "rule": t} for k, (m, t) in DEVIATIONS.items()
            },
        },
        "market_data": {
            "provider": "alpaca_market_data",
            "host": DATA_HOST,
            "feed": feed,
            "adjustment": "raw",
            "feed_limitation": (
                "IEX is a single venue: quotes are not the NBBO and minute volume "
                "is IEX-only"
                if feed == "iex"
                else "SIP consolidated tape"
            ),
        },
        "constants": K.sealed_constants(),
        "window": {
            "start_session": window[0].isoformat(),
            "end_session": window[-1].isoformat(),
            "sessions": [d.isoformat() for d in window],
        },
        "calendar": calendar_block(cal, start),
    }


def verify_against_runtime(manifest: dict[str, Any]) -> list[str]:
    """Problems that must stop the epoch (empty list == consistent)."""

    problems: list[str] = []
    if manifest.get("constants") != json.loads(canonical_json(K.sealed_constants())):
        problems.append("constants_changed_since_seal")
    sealed_cal = manifest["calendar"]
    days = tuple(s["day"] for s in sealed_cal["sessions"])
    live = _live_calendar_records(days)
    if live is None or live != sealed_cal["sessions"]:
        problems.append("calendar_drift_since_seal")
    return problems


@lru_cache(maxsize=4)
def _live_calendar_records(days: tuple[str, ...]) -> list[dict[str, Any]] | None:
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    try:
        cal = USCalendar(first, last)
        records = [cal.session(date.fromisoformat(d)).as_record() for d in days]
        if (
            len(cal.window(first, len(days))) != len(days)
            or cal.window(first, len(days))[-1] != last
        ):
            return None
    except Exception:
        return None
    return records
