from datetime import timedelta

from app.services.order_proposals.approval_window import (
    SubmissionSessionEvidence,
    evaluate_approval_window,
)


async def allow_known_session(group, *, now):
    """Keep legacy unit suites focused while retaining validity enforcement."""

    async def resolver(group, *, now):
        return SubmissionSessionEvidence(
            known=True,
            source="test:known-session",
            current_session="regular",
            allowed_sessions=("regular",),
            allowed_now=True,
            allowed_until=now + timedelta(hours=8),
            next_allowed_at=now + timedelta(days=1),
        )

    return await evaluate_approval_window(group, now=now, session_resolver=resolver)
