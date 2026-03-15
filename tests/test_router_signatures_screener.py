import inspect

from fastapi.params import Depends as DependsParam
from fastapi.params import Query as QueryParam


def test_screener_router_uses_annotated_fastapi_params() -> None:
    from app.routers import screener

    endpoints = (
        screener.screener_list,
        screener.screener_refresh,
        screener.screener_request_report,
        screener.screener_report_status,
        screener.screener_callback,
        screener.screener_order,
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
