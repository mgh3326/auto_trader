"""Profile-specific D deregistration from Complete classification (2026-09-03).

The audit is frozen evidence, not a runtime config dependency. This proxy only
removes reviewed names; existing feature gates and profile allowlists still run.
Audited D exceptions remain for the promoted lanes and mixed regression contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fastmcp import FastMCP

_SHARED_DEAD_TOOLS = frozenset(
    {
        "analysis_bundle_create",
        "analysis_bundle_get",
    }
)

PROFILE_DEAD_TOOLS: dict[str, frozenset[str]] = {
    "us-paper": _SHARED_DEAD_TOOLS,
    "db-paper": _SHARED_DEAD_TOOLS
    | frozenset(
        {
            "compare_paper_accounts",
            "compare_strategies",
            "get_paper_performance",
            "get_paper_trade_log",
            "recommend_go_live",
        }
    ),
    "crypto": _SHARED_DEAD_TOOLS,
    "kiwoom": _SHARED_DEAD_TOOLS,
    "kiwoom_kr": _SHARED_DEAD_TOOLS,
    "hermes-paper-kis": _SHARED_DEAD_TOOLS,
    "default": _SHARED_DEAD_TOOLS,
}


class _DeadToolFilteredMCP:
    def __init__(self, inner: Any, removed: frozenset[str]) -> None:
        self._inner = inner
        self._removed = removed

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if name not in self._removed:
            return self._inner.tool(*args, **kwargs)
        if direct is not None:
            return direct
        return lambda function: function

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()


def without_dead_tools(mcp: FastMCP, profile: str) -> FastMCP:
    removed = PROFILE_DEAD_TOOLS.get(profile, frozenset())
    if not removed:
        return mcp
    return cast("FastMCP", _DeadToolFilteredMCP(mcp, removed))
