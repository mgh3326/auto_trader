"""#1257 (#1245) — ``MCP_PROFILE=h3-us-paper``.

Least-privilege surface for the H3-US managed-envelope pilot session
(auto_trader-operator ``runners/h3_pilot_runner.py --market us`` with
``prompts/h3-managed-envelope-pilot-us.md``, drafted at
``mock/contracts/h3-managed-envelope-pilot-v1.prompt.md``). Before this
profile the Alpaca paper order tools existed only on ``us-paper`` (which also
carries the automated submit tool, the reconcile writer and every other paper
account) and, gated, on DEFAULT (which carries every live order tool).

Closed world, same shape as ``h3_crypto_paper_registration`` (#1171):

* ``register_all_tools`` returns before the broad "Always" block, so no
  shared registrar runs except the ones named below.
* Every one of those registrars runs through ``_H3UsPaperMCP``, an exact-set
  proxy: a tool whose name is not in ``H3_US_PAPER_TOOL_NAMES`` is dropped at
  registration time, and ``assert_complete`` fails the boot if a listed name
  was not produced (the registered set equals the allowlist).
* The allowlist is a literal reviewed here, independent of registrar-owned
  name sets, so a tool added to a shared registrar later cannot widen this
  profile. The only mutations on it are the two Alpaca paper order tools
  (paper endpoint only, confirm-gated, server-derived idempotency), the quote
  snapshot builder that the submit requires, and the two record writes.
* Argument pins keep every listed tool on the H3-US account and market:
  every tool in ``H3_US_PAPER_MARKET_PINNED_TOOLS`` is pinned to
  ``market="us"`` (omitted -> us, anything else refused before the body
  runs); every tool in ``H3_US_PAPER_ACCOUNT_PINNED_TOOLS`` is pinned to
  ``account_mode="alpaca_paper"`` (the lab and crypto paper accounts are
  refused); ``alpaca_paper_submit_order`` is pinned to
  ``asset_class="us_equity"``; ``get_operating_briefing`` is pinned to
  ``account_scope="db_simulated"`` (no KIS/Toss account or broker pending-
  order read; the runner reads the Alpaca paper account through the three
  listed reads).

What this module does not do: it chooses what is registered and pins
arguments. The Alpaca paper caps, the quote-snapshot trust check, the
runner's per-call guard (exact runner intents) and every tool body are reused
unchanged. Deployment is in ``scripts/deploy-ncp-pull.sh`` and
``docs/runbooks/h3-us-paper-mcp.md``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.mcp_server.tooling.alpaca_paper import register_alpaca_paper_tools
from app.mcp_server.tooling.alpaca_paper_orders import (
    register_alpaca_paper_orders_tools,
)
from app.mcp_server.tooling.analysis_artifact_registration import (
    register_analysis_artifact_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    register_market_quote_snapshot_tools,
)
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.session_context_registration import (
    register_session_context_tools,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
from app.services.brokers.credential_firewall import broker_credentials_blocked

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])

# The complete surface of MCP_PROFILE=h3-us-paper. Each name is traced to the
# auto_trader-operator line that needs it (runner = runners/h3_pilot_runner.py,
# prompt = mock/contracts/h3-managed-envelope-pilot-v1.prompt.md); the
# runner's registered_tools("us") is this same set of 20.
H3_US_PAPER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # plan phase bootstrap (prompt PHASE=plan step 1; runner
        # bootstrap_sequence) — read-only.
        "get_operating_briefing",
        "route_request",
        # plan research (runner RESEARCH_TOOLS; prompt steps 2-3, runner
        # discovery_text names discover_buy_candidates_fanout(market="us") and
        # analyze_stock; candles get_ohlcv; Q-89 liveness get_quote SPY).
        "get_trading_policy",
        "get_quote",
        "get_ohlcv",
        "get_top_stocks",
        "screen_stocks",
        "screen_stocks_snapshot",
        "discover_buy_candidates_fanout",
        "analyze_stock",
        "analyze_stock_batch",
        "session_context_get_recent",
        # plan account reads (runner ACCOUNT_READ_TOOLS["us"]; the runner
        # injects each with account_mode="alpaca_paper").
        "alpaca_paper_list_orders",
        "alpaca_paper_get_order",
        "alpaca_paper_list_positions",
        # execute phase (runner MUTATION_MCP_TOOLS["us"]; prompt PHASE=execute
        # "first call market_quote_snapshot_ensure ... pass the returned id as
        # quote_snapshot_id").
        "market_quote_snapshot_ensure",
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
        # record phase (prompt PHASE=record; runner RECORD_MCP_TOOLS).
        "analysis_artifact_save",
        "session_context_append",
    }
)

H3_US_PAPER_PROFILE = "h3-us-paper"
H3_US_PAPER_MARKET = "us"
H3_US_PAPER_ACCOUNT_MODE = "alpaca_paper"
H3_US_PAPER_ASSET_CLASS = "us_equity"
H3_US_PAPER_BRIEFING_SCOPE = "db_simulated"

# Every listed tool that takes a market and whose body can reach a broker
# client or a live account for another market (KIS domestic quotes/candles,
# the KR discovery fan-out, crypto/Upbit paths, the briefing's KR/crypto live
# account scopes, the KR/crypto quote-snapshot builders). Each one is pinned
# to market="us": an omitted market is filled in, any other value is refused
# before the tool body runs.
H3_US_PAPER_MARKET_PINNED_TOOLS: frozenset[str] = frozenset(
    {
        "get_operating_briefing",
        "get_quote",
        "get_ohlcv",
        "get_top_stocks",
        "screen_stocks",
        "screen_stocks_snapshot",
        "discover_buy_candidates_fanout",
        "analyze_stock",
        "analyze_stock_batch",
        "market_quote_snapshot_ensure",
    }
)
# Listed tools that take a market argument but only read or write the DB or
# the policy file, never a broker client; left unpinned on purpose.
H3_US_PAPER_MARKET_UNPINNED_DB_ONLY: frozenset[str] = frozenset(
    {
        "route_request",
        "get_trading_policy",
        "session_context_get_recent",
        "analysis_artifact_save",
    }
)
# Every listed Alpaca paper tool. account_mode selects the paper account and
# its credentials (alpaca_paper / alpaca_paper_lab / alpaca_paper_crypto);
# H3-US is the alpaca_paper account only.
H3_US_PAPER_ACCOUNT_PINNED_TOOLS: frozenset[str] = frozenset(
    {
        "alpaca_paper_list_orders",
        "alpaca_paper_get_order",
        "alpaca_paper_list_positions",
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
    }
)


class H3UsPaperProfileError(RuntimeError):
    """The registered h3-us-paper surface is not exactly the allowlist."""


def _refuse(tool: str, detail: str) -> ValueError:
    return ValueError(f"h3-us-paper {tool} {detail}")


def _check_submit(arguments: dict[str, Any]) -> None:
    asset_class = arguments.get("asset_class")
    if asset_class is not None and asset_class != H3_US_PAPER_ASSET_CLASS:
        raise _refuse(
            "alpaca_paper_submit_order",
            f"is pinned to asset_class='{H3_US_PAPER_ASSET_CLASS}'",
        )
    arguments["asset_class"] = H3_US_PAPER_ASSET_CLASS


def _check_briefing(arguments: dict[str, Any]) -> None:
    # The default US scope reads a live broker account (KIS/Toss holdings and
    # pending orders); db_simulated reads DB paper holdings and skips the
    # broker pending-order collector.
    scope = arguments.get("account_scope")
    if scope is not None and scope != H3_US_PAPER_BRIEFING_SCOPE:
        raise _refuse(
            "get_operating_briefing",
            f"is pinned to account_scope='{H3_US_PAPER_BRIEFING_SCOPE}'",
        )
    arguments["account_scope"] = H3_US_PAPER_BRIEFING_SCOPE


_ARGUMENT_CHECKS: dict[str, Callable[[dict[str, Any]], None]] = {
    "alpaca_paper_submit_order": _check_submit,
    "get_operating_briefing": _check_briefing,
}
_PINNED_DEFAULTS: dict[str, dict[str, str]] = {
    "alpaca_paper_submit_order": {"asset_class": H3_US_PAPER_ASSET_CLASS},
    "get_operating_briefing": {"account_scope": H3_US_PAPER_BRIEFING_SCOPE},
}


def _signature_with_defaults(
    signature: inspect.Signature, defaults: dict[str, str]
) -> inspect.Signature:
    """Show each pinned value as the schema default where Python allows it.

    A parameter followed by a required positional parameter keeps no default
    (``market_quote_snapshot_ensure(market, symbol)``); its value is still
    pinned at call time.
    """
    parameters = list(signature.parameters.values())
    shown: list[inspect.Parameter] = []
    for index, parameter in enumerate(parameters):
        later_required = any(
            later.default is inspect.Parameter.empty
            and later.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            for later in parameters[index + 1 :]
        )
        if parameter.name in defaults and not later_required:
            parameter = parameter.replace(default=defaults[parameter.name])
        shown.append(parameter)
    return signature.replace(parameters=shown)


def _pinned_tool[F: Callable[..., Any]](name: str, function: F) -> F:
    """Wrap one listed tool: argument pins, then the credential firewall.

    Every listed tool body runs inside ``broker_credentials_blocked``, so a
    KIS, Toss or authenticated Upbit client reached from it refuses before
    any token lookup or send (US quotes then fall back to Yahoo, US daily
    candles to cached rows, the USD/KRW rate to open.er-api).
    """
    if not inspect.iscoroutinefunction(function):
        raise H3UsPaperProfileError(f"{name} is not an async tool")
    market_pinned = name in H3_US_PAPER_MARKET_PINNED_TOOLS
    account_pinned = name in H3_US_PAPER_ACCOUNT_PINNED_TOOLS
    check = _ARGUMENT_CHECKS.get(name)
    signature = inspect.signature(function)
    if market_pinned and "market" not in signature.parameters:
        raise H3UsPaperProfileError(f"{name} has no market parameter to pin")
    if account_pinned and "account_mode" not in signature.parameters:
        raise H3UsPaperProfileError(f"{name} has no account_mode parameter to pin")
    defaults = dict(_PINNED_DEFAULTS.get(name, {}))
    if market_pinned:
        defaults["market"] = H3_US_PAPER_MARKET
    if account_pinned:
        defaults["account_mode"] = H3_US_PAPER_ACCOUNT_MODE

    @wraps(function)
    async def pinned(*call_args: Any, **call_kwargs: Any) -> Any:
        bound = signature.bind_partial(*call_args, **call_kwargs)
        arguments = bound.arguments
        if market_pinned:
            market = arguments.get("market")
            if market is not None and market != H3_US_PAPER_MARKET:
                raise _refuse(name, f"is pinned to market='{H3_US_PAPER_MARKET}'")
            arguments["market"] = H3_US_PAPER_MARKET
        if account_pinned:
            mode = arguments.get("account_mode")
            if mode is not None and mode != H3_US_PAPER_ACCOUNT_MODE:
                raise _refuse(
                    name, f"is pinned to account_mode='{H3_US_PAPER_ACCOUNT_MODE}'"
                )
            arguments["account_mode"] = H3_US_PAPER_ACCOUNT_MODE
        if check is not None:
            check(arguments)
        with broker_credentials_blocked(H3_US_PAPER_PROFILE):
            return await function(*bound.args, **bound.kwargs)

    pinned.__signature__ = _signature_with_defaults(signature, defaults)
    return cast(F, pinned)


class _H3UsPaperMCP:
    """Exact-set recording proxy for the h3-us-paper profile."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.registered: set[str] = set()

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if not isinstance(name, str) or name not in H3_US_PAPER_TOOL_NAMES:
            if direct is not None:
                return direct

            def drop(function: _F) -> _F:
                return function

            return drop

        def pin(function: _F) -> _F:
            return _pinned_tool(name, function)

        if direct is not None:
            self.registered.add(name)
            return self._inner.tool(pin(direct), *args[1:], **kwargs)

        register = self._inner.tool(*args, **kwargs)

        def record(function: _F) -> _F:
            self.registered.add(name)
            return register(pin(function))

        return record

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()

    def assert_complete(self) -> None:
        if self.registered != H3_US_PAPER_TOOL_NAMES:
            missing = sorted(H3_US_PAPER_TOOL_NAMES - self.registered)
            raise H3UsPaperProfileError(
                f"h3-us-paper registered surface is incomplete: missing={missing}"
            )


def register_h3_us_paper_tools(mcp: FastMCP) -> None:
    """Register exactly ``H3_US_PAPER_TOOL_NAMES`` (closed world)."""
    proxy = _H3UsPaperMCP(mcp)
    filtered = cast("FastMCP", proxy)
    register_operating_briefing_tools(filtered)
    register_route_request_tools(filtered)
    register_trading_policy_tools(filtered)
    register_market_data_tools(filtered)
    register_analysis_tools(filtered)
    register_session_context_tools(filtered)
    register_analysis_artifact_tools(filtered)
    register_alpaca_paper_tools(filtered)
    register_alpaca_paper_orders_tools(filtered)
    register_market_quote_snapshot_tools(filtered)
    proxy.assert_complete()


__all__ = [
    "H3_US_PAPER_TOOL_NAMES",
    "H3UsPaperProfileError",
    "register_h3_us_paper_tools",
]
