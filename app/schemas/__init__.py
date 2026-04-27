"""Lazy schema package exports.

Avoid importing all schema modules when a single submodule is requested. Some
legacy schemas import pricing services at module import time; eager package
exports would pull broker/Redis execution paths into unrelated routers.
"""

_EXPORT_MODULES = {
    # Broker Account
    "BrokerAccountCreate": "app.schemas.manual_holdings",
    "BrokerAccountUpdate": "app.schemas.manual_holdings",
    "BrokerAccountResponse": "app.schemas.manual_holdings",
    # Manual Holding
    "ManualHoldingCreate": "app.schemas.manual_holdings",
    "ManualHoldingUpdate": "app.schemas.manual_holdings",
    "ManualHoldingResponse": "app.schemas.manual_holdings",
    "ManualHoldingBulkCreate": "app.schemas.manual_holdings",
    # Stock Alias
    "StockAliasCreate": "app.schemas.manual_holdings",
    "StockAliasResponse": "app.schemas.manual_holdings",
    "StockAliasSearchResult": "app.schemas.manual_holdings",
    # n8n pending orders
    "N8nPendingOrderItem": "app.schemas.n8n.pending_orders",
    "N8nPendingOrderSummary": "app.schemas.n8n.pending_orders",
    "N8nPendingOrdersResponse": "app.schemas.n8n.pending_orders",
    # Portfolio
    "HoldingInfoResponse": "app.schemas.manual_holdings",
    "ReferencePricesResponse": "app.schemas.manual_holdings",
    "MergedHoldingResponse": "app.schemas.manual_holdings",
    "MergedPortfolioResponse": "app.schemas.manual_holdings",
    # Portfolio Decision
    "PortfolioDecisionSlateResponse": "app.schemas.portfolio_decision",
    # Portfolio Position Detail
    "PositionDetailComponentResponse": "app.schemas.portfolio_position_detail",
    "PositionDetailSummaryResponse": "app.schemas.portfolio_position_detail",
    "PositionDetailPageResponse": "app.schemas.portfolio_position_detail",
    "PositionIndicatorsResponse": "app.schemas.portfolio_position_detail",
    "PositionNewsResponse": "app.schemas.portfolio_position_detail",
    "PositionOpinionsResponse": "app.schemas.portfolio_position_detail",
    # Trading
    "BuyOrderRequest": "app.schemas.manual_holdings",
    "SellOrderRequest": "app.schemas.manual_holdings",
    "OrderSimulationResponse": "app.schemas.manual_holdings",
    "ExpectedProfitResponse": "app.schemas.manual_holdings",
    # Backtest
    "BacktestRunSummary": "app.schemas.research_backtest",
    "BacktestPairSummary": "app.schemas.research_backtest",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
