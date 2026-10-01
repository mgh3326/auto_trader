"""#1171 (operator hk 1135 = A) — ``MCP_PROFILE=h3-crypto-paper``.

Least-privilege surface for the H3-CRYPTO managed-envelope pilot session
(auto_trader-operator ``runners/h3_pilot_runner.py --market crypto`` with
``prompts/h3-managed-envelope-pilot-crypto.md``). Before this profile the
crypto paper tools existed only on DEFAULT, which also carries every live
order tool; pointing the H3 session there would have left the live surface
one allowedTools entry away.

Closed world, same shape as the other allowlist profiles:

* ``register_all_tools`` returns before the broad "Always" block, so no
  shared registrar runs except the ten named below.
* Every one of those registrars runs through ``_H3CryptoPaperMCP``, an
  exact-set proxy: a tool whose name is not in ``H3_CRYPTO_PAPER_TOOL_NAMES``
  is dropped at registration time, and ``assert_complete`` fails the boot if
  a listed name was not produced (the registered set equals the allowlist).
* The allowlist is a literal reviewed here, independent of registrar-owned
  name sets, so a tool added to a shared registrar later cannot widen this
  profile. The only mutations on it are the four ROB-703 paper simulator
  tools (``paper.*`` tables, no broker call) plus the two record writes the
  runner's record phase makes.
* Argument pins (round 2 of #1171 verification): a listed tool can still
  read through broker credentials for the wrong arguments. Every tool in
  ``H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS`` is pinned to ``market="crypto"``
  (omitted -> crypto, anything else refused before the body runs), so KIS
  quote/candle/indicator/analysis/screen paths and paper equity valuation are
  unreachable. ``get_operating_briefing`` is pinned to
  ``account_scope="db_simulated"`` (DB paper holdings, no live Upbit account
  read, no broker pending-order collector). ``get_holdings`` is pinned to DB
  paper accounts: ``account`` must be a paper token (``paper`` /
  ``paper:<name>``), ``account_mode`` is forced to ``db_simulated``, and
  ``account_type`` / ``fresh_sellable`` / ``include_ledger_lots`` are refused.

What this module does not do: it chooses what is registered. The paper
simulator semantics, the runner's per-call guard (account_id=2, exact runner
intents) and every tool body are reused unchanged. Deployment is declared in
``docs/runbooks/h3-crypto-paper-mcp.md`` and is not wired into any deploy
script.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.mcp_server.tooling.analysis_artifact_registration import (
    register_analysis_artifact_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.paper_limit_order_handler import (
    register_paper_limit_order_tools,
)
from app.mcp_server.tooling.paper_portfolio_handler import is_paper_account_token
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.session_context_registration import (
    register_session_context_tools,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])

# The complete surface of MCP_PROFILE=h3-crypto-paper. Each name is traced to
# the auto_trader-operator line that needs it (runner = runners/
# h3_pilot_runner.py, prompt = prompts/h3-managed-envelope-pilot-crypto.md);
# the runner's registered_tools("crypto") and the operator_contract.yaml
# registration allowed_tools are this same set of 20.
H3_CRYPTO_PAPER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # plan phase bootstrap (prompt PHASE=plan step 1; runner
        # bootstrap_sequence) — read-only.
        "get_operating_briefing",
        "route_request",
        # plan research (runner CRYPTO_RESEARCH_TOOLS; prompt step 3 names the
        # crypto screens and the support sources) — read-only.
        "get_trading_policy",
        "get_quote",
        "get_ohlcv",
        "get_support_resistance",
        "get_indicators",
        "get_momentum_candidates",
        "screen_stocks",
        "screen_stocks_snapshot",
        "analyze_stock",
        "analyze_stock_batch",
        "session_context_get_recent",
        # plan account reads (prompt step 2; runner ACCOUNT_READ_TOOLS
        # ["crypto"], order load-bearing). paper_reconcile_orders writes only
        # paper.* tables (fill sync); get_holdings is paper-pinned below.
        "paper_reconcile_orders",
        "paper_list_pending_orders",
        "get_holdings",
        # execute phase (runner MUTATION_MCP_TOOLS["crypto"]; operator
        # exception markets.crypto.allowed_mutation_tools) — paper simulator.
        "paper_place_limit_order",
        "paper_cancel_pending_order",
        # record phase (prompt PHASE=record; runner RECORD_MCP_TOOLS).
        "analysis_artifact_save",
        "session_context_append",
    }
)

H3_CRYPTO_PAPER_PINNED_HOLDINGS_TOOL = "get_holdings"
H3_CRYPTO_PAPER_ACCOUNT_MODE = "db_simulated"
H3_CRYPTO_PAPER_MARKET = "crypto"
H3_CRYPTO_PAPER_BRIEFING_SCOPE = "db_simulated"

# Every listed tool whose body can reach a broker client for a non-crypto
# market (KIS quotes/candles/indicators, equity screen enrichment, the equity
# analysis pipeline, paper equity valuation) or a live account (the briefing's
# holdings summary and pending-order collector). On this profile each one is
# pinned to market="crypto": an omitted market is filled in, any other value
# is refused before the tool body runs. The crypto paths read public Upbit
# market data and the DB only.
H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS: frozenset[str] = frozenset(
    {
        "get_operating_briefing",
        "get_quote",
        "get_ohlcv",
        "get_indicators",
        "get_support_resistance",
        "get_momentum_candidates",
        "screen_stocks",
        "screen_stocks_snapshot",
        "analyze_stock",
        "analyze_stock_batch",
        "get_holdings",
    }
)
# Listed tools that take a market argument but only read or write the DB or
# the policy file, never a broker client; left unpinned on purpose.
H3_CRYPTO_PAPER_MARKET_UNPINNED_DB_ONLY: frozenset[str] = frozenset(
    {
        "route_request",
        "get_trading_policy",
        "session_context_get_recent",
        "analysis_artifact_save",
    }
)


class H3CryptoPaperProfileError(RuntimeError):
    """The registered h3-crypto-paper surface is not exactly the allowlist."""


def _refuse(tool: str, detail: str) -> ValueError:
    return ValueError(f"h3-crypto-paper {tool} {detail}")


def _check_holdings(arguments: dict[str, Any]) -> None:
    account = arguments.get("account")
    if not isinstance(account, str) or not is_paper_account_token(account):
        raise _refuse(
            "get_holdings",
            "is pinned to DB paper accounts: account must be 'paper' or 'paper:<name>'",
        )
    mode = arguments.get("account_mode")
    if mode is not None and mode != H3_CRYPTO_PAPER_ACCOUNT_MODE:
        raise _refuse(
            "get_holdings",
            f"is pinned to account_mode='{H3_CRYPTO_PAPER_ACCOUNT_MODE}'",
        )
    for refused in ("account_type", "fresh_sellable", "include_ledger_lots"):
        if arguments.get(refused):
            raise _refuse("get_holdings", f"does not accept {refused}")
    arguments["account_mode"] = H3_CRYPTO_PAPER_ACCOUNT_MODE


def _check_briefing(arguments: dict[str, Any]) -> None:
    # The default crypto scope (upbit_live) reads the live Upbit account and
    # its open orders; db_simulated reads DB paper holdings and skips the
    # broker pending-order collector.
    scope = arguments.get("account_scope")
    if scope is not None and scope != H3_CRYPTO_PAPER_BRIEFING_SCOPE:
        raise _refuse(
            "get_operating_briefing",
            f"is pinned to account_scope='{H3_CRYPTO_PAPER_BRIEFING_SCOPE}'",
        )
    arguments["account_scope"] = H3_CRYPTO_PAPER_BRIEFING_SCOPE


_ARGUMENT_CHECKS: dict[str, Callable[[dict[str, Any]], None]] = {
    H3_CRYPTO_PAPER_PINNED_HOLDINGS_TOOL: _check_holdings,
    "get_operating_briefing": _check_briefing,
}
_PINNED_DEFAULTS: dict[str, dict[str, str]] = {
    H3_CRYPTO_PAPER_PINNED_HOLDINGS_TOOL: {
        "account_mode": H3_CRYPTO_PAPER_ACCOUNT_MODE,
    },
    "get_operating_briefing": {"account_scope": H3_CRYPTO_PAPER_BRIEFING_SCOPE},
}


def _pinned_tool[F: Callable[..., Any]](name: str, function: F) -> F:
    """Wrap one listed tool so it cannot select a live account or market."""
    market_pinned = name in H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS
    check = _ARGUMENT_CHECKS.get(name)
    if not market_pinned and check is None:
        return function
    signature = inspect.signature(function)
    if market_pinned and "market" not in signature.parameters:
        raise H3CryptoPaperProfileError(f"{name} has no market parameter to pin")
    defaults = dict(_PINNED_DEFAULTS.get(name, {}))
    if market_pinned:
        defaults["market"] = H3_CRYPTO_PAPER_MARKET

    @wraps(function)
    async def pinned(*call_args: Any, **call_kwargs: Any) -> Any:
        bound = signature.bind_partial(*call_args, **call_kwargs)
        arguments = bound.arguments
        if market_pinned:
            market = arguments.get("market")
            if market is not None and market != H3_CRYPTO_PAPER_MARKET:
                raise _refuse(name, f"is pinned to market='{H3_CRYPTO_PAPER_MARKET}'")
            arguments["market"] = H3_CRYPTO_PAPER_MARKET
        if check is not None:
            check(arguments)
        return await function(*bound.args, **bound.kwargs)

    pinned.__signature__ = signature.replace(
        parameters=[
            (
                parameter.replace(default=defaults[parameter_name])
                if parameter_name in defaults
                else parameter
            )
            for parameter_name, parameter in signature.parameters.items()
        ]
    )
    return cast(F, pinned)


class _H3CryptoPaperMCP:
    """Exact-set recording proxy for the h3-crypto-paper profile."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.registered: set[str] = set()

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if not isinstance(name, str) or name not in H3_CRYPTO_PAPER_TOOL_NAMES:
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
        if self.registered != H3_CRYPTO_PAPER_TOOL_NAMES:
            missing = sorted(H3_CRYPTO_PAPER_TOOL_NAMES - self.registered)
            raise H3CryptoPaperProfileError(
                f"h3-crypto-paper registered surface is incomplete: missing={missing}"
            )


def register_h3_crypto_paper_tools(mcp: FastMCP) -> None:
    """Register exactly ``H3_CRYPTO_PAPER_TOOL_NAMES`` (closed world)."""
    proxy = _H3CryptoPaperMCP(mcp)
    filtered = cast("FastMCP", proxy)
    register_operating_briefing_tools(filtered)
    register_route_request_tools(filtered)
    register_trading_policy_tools(filtered)
    register_market_data_tools(filtered)
    register_fundamentals_tools(filtered)
    register_analysis_tools(filtered)
    register_session_context_tools(filtered)
    register_analysis_artifact_tools(filtered)
    register_portfolio_tools(filtered)
    register_paper_limit_order_tools(filtered)
    proxy.assert_complete()


__all__ = [
    "H3_CRYPTO_PAPER_TOOL_NAMES",
    "H3CryptoPaperProfileError",
    "register_h3_crypto_paper_tools",
]
