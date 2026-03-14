import inspect

from fastapi.params import Depends as DependsParam
from fastapi.params import Query as QueryParam


def test_low_risk_router_batch_uses_annotated_fastapi_params() -> None:
    from app.routers import analysis_json, manual_holdings, stock_latest

    endpoints = (
        manual_holdings.list_broker_accounts,
        manual_holdings.create_broker_account,
        manual_holdings.update_broker_account,
        manual_holdings.delete_broker_account,
        manual_holdings.list_holdings,
        manual_holdings.create_holding,
        manual_holdings.create_holdings_bulk,
        manual_holdings.update_holding,
        manual_holdings.delete_holding,
        manual_holdings.search_stock_aliases,
        manual_holdings.create_stock_alias,
        manual_holdings.seed_toss_stock_aliases,
        stock_latest.get_latest_analysis_results,
        stock_latest.get_stock_analysis_history,
        stock_latest.get_filter_options,
        stock_latest.get_analysis_status,
        stock_latest.get_latest_analysis_statistics,
        stock_latest.trigger_new_analysis,
        analysis_json.get_analysis_results,
        analysis_json.get_analysis_detail,
        analysis_json.get_latest_analysis_by_symbol,
        analysis_json.get_filter_options,
        analysis_json.get_analysis_statistics,
    )

    fastapi_default_types = (DependsParam, QueryParam)

    offenders: list[str] = []
    for endpoint in endpoints:
        signature = inspect.signature(endpoint)
        for parameter in signature.parameters.values():
            if isinstance(parameter.default, fastapi_default_types):
                offenders.append(
                    f"{endpoint.__module__}.{endpoint.__name__}:{parameter.name}"
                )

    assert offenders == []
