"""#1112 — scope constants for the 16:30 / 07:00 KST stale-proposal night sweep.

The night sweep reuses ``OrderProposalsService.sweep_expired`` (ROB-897) with a
NARROWER scope than the manual ``order_proposal_expire_sweep`` lever:

* only groups still ``proposed`` (nobody approved them), and
* only when every rung is still waiting for a human (``draft`` /
  ``pending_approval`` / ``needs_reconfirm``). A rung mid-approval
  (``revalidating``/``approved``) or anywhere past submission is never touched;
  its group is skipped exactly as the ROB-897 sweep skips non-voidable rungs.

Each rung it expires records ``NIGHT_SWEEP_VOID_REASON`` so the 7-D blocking
report can say which sweep cleared it and when. The sweep is a DB write plus
the existing Telegram card clean-up — it never reaches a broker and never
creates, modifies or cancels an order.
"""

from __future__ import annotations

NIGHT_SWEEP_GROUP_STATES: frozenset[str] = frozenset({"proposed"})
NIGHT_SWEEP_RUNG_STATES: frozenset[str] = frozenset(
    {"draft", "pending_approval", "needs_reconfirm"}
)
# ``expired_`` prefix keeps the closed void-reason group ``cancelled_or_expired``.
NIGHT_SWEEP_VOID_REASON = "expired_valid_until_night_sweep"

# Declared cron (Asia/Seoul) — registered ONLY when
# ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED is true (default false).
NIGHT_SWEEP_CRONS: tuple[str, ...] = ("30 16 * * 1-5", "0 7 * * 1-5")

__all__ = [
    "NIGHT_SWEEP_CRONS",
    "NIGHT_SWEEP_GROUP_STATES",
    "NIGHT_SWEEP_RUNG_STATES",
    "NIGHT_SWEEP_VOID_REASON",
]
