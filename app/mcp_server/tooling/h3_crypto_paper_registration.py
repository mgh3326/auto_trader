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
* ``get_holdings`` is the one listed read whose arguments can select a live
  broker account (KIS/Upbit/Toss holdings with the server's credentials). On
  this profile it is pinned to DB paper accounts: ``account`` must be a paper
  token (``paper`` / ``paper:<name>``), ``account_mode`` is forced to
  ``db_simulated``, and ``account_type`` / ``fresh_sellable`` /
  ``include_ledger_lots`` (KIS live evidence) are refused. The paper
  short-circuit in ``_collect_portfolio_positions`` then never reaches a
  broker client.

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


class H3CryptoPaperProfileError(RuntimeError):
    """The registered h3-crypto-paper surface is not exactly the allowlist."""


def _paper_pinned_get_holdings[F: Callable[..., Any]](function: F) -> F:
    """Refuse every get_holdings argument that could reach a live account."""
    signature = inspect.signature(function)

    @wraps(function)
    async def pinned(*call_args: Any, **call_kwargs: Any) -> Any:
        bound = signature.bind_partial(*call_args, **call_kwargs)
        arguments = bound.arguments
        account = arguments.get("account")
        if not isinstance(account, str) or not is_paper_account_token(account):
            raise ValueError(
                "h3-crypto-paper get_holdings is pinned to DB paper accounts: "
                "account must be 'paper' or 'paper:<name>'"
            )
        mode = arguments.get("account_mode")
        if mode is not None and mode != H3_CRYPTO_PAPER_ACCOUNT_MODE:
            raise ValueError(
                "h3-crypto-paper get_holdings is pinned to "
                f"account_mode='{H3_CRYPTO_PAPER_ACCOUNT_MODE}'"
            )
        for refused in ("account_type", "fresh_sellable", "include_ledger_lots"):
            if arguments.get(refused):
                raise ValueError(
                    f"h3-crypto-paper get_holdings does not accept {refused}"
                )
        arguments["account_mode"] = H3_CRYPTO_PAPER_ACCOUNT_MODE
        return await function(*bound.args, **bound.kwargs)

    pinned.__signature__ = signature.replace(
        parameters=[
            (
                parameter.replace(default=H3_CRYPTO_PAPER_ACCOUNT_MODE)
                if name == "account_mode"
                else parameter
            )
            for name, parameter in signature.parameters.items()
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
            if name == H3_CRYPTO_PAPER_PINNED_HOLDINGS_TOOL:
                return _paper_pinned_get_holdings(function)
            return function

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
