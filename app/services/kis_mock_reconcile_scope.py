"""KIS mock reconciliation scope selector — validate/normalize/shape (ROB-1018/ROB-1007).

Single source of truth for the ``market``/``symbol``/``ledger_ids`` selector
set accepted by ``kis_mock_reconciliation_run``. Lives in its own module (not
``app.mcp_server.tooling.kis_mock_ledger``) so both the MCP-tool layer
(``kis_mock_ledger``) and the job layer (``app.jobs.kis_mock_reconciliation_job``)
can import the *same* function without a circular import — ``kis_mock_ledger``
already imports ``run_kis_mock_reconciliation`` from the job module, so the job
module cannot import back from ``kis_mock_ledger``.
"""

from __future__ import annotations

from typing import Any

_MARKET_ALIASES = {"kr": "equity_kr", "us": "equity_us"}
_ALLOWED_RECONCILE_MARKETS = frozenset({"equity_kr", "equity_us"})
_ALLOWED_RECONCILE_MARKET_VALUES = sorted(_MARKET_ALIASES) + sorted(
    _ALLOWED_RECONCILE_MARKETS
)


def normalize_kis_mock_reconcile_market(market: str | None) -> str | None:
    if market is None:
        return None
    return _MARKET_ALIASES.get(market, market)


def _validate_ledger_ids(ledger_ids: Any) -> str | None:
    """Return an error message when ``ledger_ids`` is structurally invalid.

    Only structural validation (type/shape) happens here — this module has
    no DB access, so "does this id actually exist" is checked downstream by
    the job layer once a DB session is available. Returns ``None`` when the
    value is a well-formed non-empty list of positive integers.
    """
    if not isinstance(ledger_ids, list | tuple):
        return (
            "ledger_ids must be a non-empty list of positive integers, got "
            f"{type(ledger_ids).__name__}"
        )
    if len(ledger_ids) == 0:
        return "ledger_ids must not be empty"
    for value in ledger_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"ledger_ids must contain only positive integers, got {value!r}"
    return None


def resolve_kis_mock_reconcile_scope(
    *,
    market: str | None,
    symbol: str | None,
    ledger_ids: list[int] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Single front-layer point: validate, normalize, and shape the
    reconciliation scope selector set (ROB-1018 fix #3, ROB-1007 fix #2/#3).

    This is the ONE place that (a) checks ``market`` against the allowlist,
    (b) structurally validates ``ledger_ids``, and (c) decides how the scope
    gets echoed back. Both the MCP tool registration (front layer) and
    :func:`app.mcp_server.tooling.kis_mock_ledger.kis_mock_reconciliation_run_impl`
    (and, as of ROB-1007, the job layer itself,
    :func:`app.jobs.kis_mock_reconciliation_job.run_kis_mock_reconciliation`)
    call this *same* function so no layer can shape the response differently
    or disagree on what counts as a valid selector — a job-layer bypass of
    this exact function (building ``scope = {"market": market, ...}``
    directly) was the ROB-1018 R4 follow-up defect this module closes.
    Callers must invoke this *before* any other gate (config error,
    confirm-required) so an invalid selector always short-circuits with the
    same rejection shape regardless of what other conditions also hold.

    Adding a new selector means adding a parameter here and including it in
    both dicts below — no restructuring of call sites or gating order.

    Returns ``(scope, error)`` — exactly one is not ``None``:

    - ``scope``: the effective/canonical selectors (alias-normalized
      ``market``, e.g. ``"us"`` -> ``"equity_us"``; ``ledger_ids`` as a
      ``list[int]``), safe to use for the rest of the request. ``ledger_ids``
      is present in the dict only when the caller supplied it — omitting it
      preserves the exact pre-ROB-1007 two-key ``scope``/``requested_scope``
      shape so existing exact-dict-equality assertions keep passing.
    - ``error``: a ready-to-return rejection dict. It always carries
      ``selector`` (``"market"`` or ``"ledger_ids"``) so callers can tell
      structurally which selector was rejected and why, plus the verbatim,
      unnormalized request under ``requested_scope`` (never ``scope``, since
      no valid scope was ever established). A silent pass-through would
      otherwise yield an ``orders_processed=0`` false-success indistinguishable
      from "scope matched but nothing was open" — this is exactly the
      ROB-1018 defect class, now generalized to a second selector.
    """
    requested_scope: dict[str, Any] = {"market": market, "symbol": symbol}
    if ledger_ids is not None:
        requested_scope["ledger_ids"] = ledger_ids

    normalized_market = normalize_kis_mock_reconcile_market(market)
    if (
        normalized_market is not None
        and normalized_market not in _ALLOWED_RECONCILE_MARKETS
    ):
        return None, {
            "success": False,
            "error": (
                f"unknown market '{market}' — allowed values: "
                f"{_ALLOWED_RECONCILE_MARKET_VALUES}"
            ),
            "selector": "market",
            "allowed_markets": _ALLOWED_RECONCILE_MARKET_VALUES,
            "account_mode": "kis_mock",
            "requested_scope": requested_scope,
        }

    if ledger_ids is not None:
        ledger_ids_error = _validate_ledger_ids(ledger_ids)
        if ledger_ids_error:
            return None, {
                "success": False,
                "error": ledger_ids_error,
                "selector": "ledger_ids",
                "account_mode": "kis_mock",
                "requested_scope": requested_scope,
            }

    scope: dict[str, Any] = {"market": normalized_market, "symbol": symbol}
    if ledger_ids is not None:
        scope["ledger_ids"] = [int(value) for value in ledger_ids]
    return scope, None
