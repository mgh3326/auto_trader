"""Optional-kind fail-open stub collectors.

Each collector here is registered for the bundle policy but produces an
explicit ``unavailable`` result on every call. That keeps the bundle's
status honest (it shows up under ``unavailable_sources`` and the bundle
degrades to ``partial`` if anything else fails) without blocking the
report — optional kinds, by policy, never gate executable language.

These stubs exist so the registry can stay symmetric with the policy
without inventing literal extensions for collectors that aren't wired
yet (e.g. ``upbit_remote_debug`` is intentionally absent from the
``SnapshotKind`` literal; we don't add it here).
"""

from __future__ import annotations

from app.services.action_report.snapshot_backed.collectors._base import (
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class _FailOpenStubBase:
    """Base for read-not-yet-wired optional collectors.

    Subclasses set ``snapshot_kind``, ``origin``, and an explanatory
    ``unavailable_reason``. ``collect`` always returns a single
    ``unavailable`` result — the bundle service treats it as "attempted but
    no data", which surfaces in ``unavailable_sources`` for transparency.
    """

    snapshot_kind: str
    origin: str
    unavailable_reason: str

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        return [
            unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                origin=self.origin,
                reason=self.unavailable_reason,
                as_of=utcnow(),
            )
        ]


class SymbolStubCollector(_FailOpenStubBase):
    snapshot_kind = "symbol"
    origin = "auto_trader_db"
    unavailable_reason = "symbol collector not wired yet (ROB-273 follow-up)"


class CandidateUniverseStubCollector(_FailOpenStubBase):
    snapshot_kind = "candidate_universe"
    origin = "auto_trader_db"
    unavailable_reason = (
        "candidate_universe collector not wired yet (ROB-273 follow-up)"
    )


class InvestPageStubCollector(_FailOpenStubBase):
    snapshot_kind = "invest_page"
    origin = "invest_http"
    unavailable_reason = "invest_page collector not wired yet (ROB-273 follow-up)"


class NaverRemoteDebugStubCollector(_FailOpenStubBase):
    snapshot_kind = "naver_remote_debug"
    origin = "naver_remote_debug"
    unavailable_reason = (
        "naver_remote_debug probe is operator-driven only; "
        "automated probe is intentionally not wired"
    )


class TossRemoteDebugStubCollector(_FailOpenStubBase):
    snapshot_kind = "toss_remote_debug"
    origin = "toss_remote_debug"
    unavailable_reason = (
        "toss_remote_debug probe is operator-driven only; "
        "automated probe is intentionally not wired"
    )


class BrowserProbeStubCollector(_FailOpenStubBase):
    """Holds the seat for the Upbit/public-page cross-check.

    The Linear ticket asks for an ``upbit_remote_debug``-style optional
    source but the snapshot enum doesn't include that literal yet (adding
    it requires an alembic + Pydantic literal extension and was explicitly
    scoped out of this PR). For now we register the policy's
    ``browser_probe`` kind as fail-open so the registry is symmetric with
    the policy.
    """

    snapshot_kind = "browser_probe"
    origin = "manual"
    unavailable_reason = (
        "browser_probe cross-check is operator-driven only; "
        "automated probe is intentionally not wired"
    )
