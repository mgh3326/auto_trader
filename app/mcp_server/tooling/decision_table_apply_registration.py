"""MCP registration for idempotent decision-table application.

The registration imports no broker client.  Existing persistence writers are
resolved only at their individual call boundaries; the coordinator itself does
not open a broker connection or expose a direct order operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.decision_table_apply import (
    DecisionTableApplyDependencies,
    apply_decision_table,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

DECISION_TABLE_APPLY_TOOL_NAMES: set[str] = {"decision_table_apply"}

_TOOL_DESCRIPTION = (
    "Apply one exact, validated kr-nxt decision-table artifact through existing "
    "proposal/watch/forecast persistence writers. This tool never performs a "
    "direct broker order operation. Real application is confirm-gated and "
    "resumes idempotently after partial writer failures."
)


def _default_dependencies() -> DecisionTableApplyDependencies:
    """Build lazy adapters so dry-run validation does not import order writers."""

    async def artifact_get(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.analysis_artifact_tools import analysis_artifact_get

        return await analysis_artifact_get(**kwargs)

    async def artifact_list(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.analysis_artifact_tools import (
            analysis_artifact_list,
        )

        return await analysis_artifact_list(**kwargs)

    async def artifact_save(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.analysis_artifact_tools import (
            analysis_artifact_save,
        )

        return await analysis_artifact_save(**kwargs)

    async def proposal_create(**kwargs: Any) -> dict[str, Any]:
        # The established proposal writer owns its own commit and any
        # post-commit Telegram behavior. It is intentionally loaded only when
        # a validated proposal row reaches this explicit writer boundary.
        from app.mcp_server.tooling.order_proposal_tools import order_proposal_create

        return await order_proposal_create(**kwargs)

    async def watch_create(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.investment_reports_handlers import (
            investment_watch_create_impl,
        )

        return await investment_watch_create_impl(**kwargs)

    async def forecast_save(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.forecast_tools import forecast_save

        return await forecast_save(**kwargs)

    async def context_append(**kwargs: Any) -> dict[str, Any]:
        from app.mcp_server.tooling.session_context_tools import session_context_append

        return await session_context_append(**kwargs)

    return DecisionTableApplyDependencies(
        artifact_get=artifact_get,
        artifact_list=artifact_list,
        artifact_save=artifact_save,
        proposal_create=proposal_create,
        watch_create=watch_create,
        forecast_save=forecast_save,
        context_append=context_append,
    )


def register_decision_table_apply_tools(
    mcp: FastMCP,
    *,
    dependencies: DecisionTableApplyDependencies | None = None,
) -> None:
    """Register the confirm-gated, resumable apply tool on an execution lane."""

    resolved_dependencies = dependencies or _default_dependencies()

    @mcp.tool(name="decision_table_apply", description=_TOOL_DESCRIPTION)
    async def decision_table_apply(
        artifact_id: int | str,
        table_hash: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await apply_decision_table(
            artifact_id,
            table_hash,
            dry_run=dry_run,
            confirm=confirm,
            dependencies=resolved_dependencies,
        )


__all__ = [
    "DECISION_TABLE_APPLY_TOOL_NAMES",
    "register_decision_table_apply_tools",
]
