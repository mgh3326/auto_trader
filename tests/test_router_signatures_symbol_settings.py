import inspect
from typing import get_args, get_origin, get_type_hints

from fastapi.params import Depends as DependsParam
from fastapi.params import Query as QueryParam


def test_symbol_settings_router_uses_annotated_fastapi_params() -> None:
    from app.routers import symbol_settings

    endpoints = (
        symbol_settings.get_user_from_request,
        symbol_settings.get_user_defaults,
        symbol_settings.update_user_defaults,
        symbol_settings.get_all_settings,
        symbol_settings.get_domestic_estimated_costs,
        symbol_settings.get_overseas_estimated_costs,
        symbol_settings.get_all_estimated_costs,
        symbol_settings.get_crypto_estimated_costs,
        symbol_settings.get_settings_by_symbol,
        symbol_settings.create_settings,
        symbol_settings.update_settings,
        symbol_settings.delete_settings,
        symbol_settings.get_estimated_cost,
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

    hints = get_type_hints(symbol_settings.get_all_settings, include_extras=True)
    query_metadata: list[QueryParam] = []
    for parameter_name in ("active_only", "instrument_type"):
        annotation = hints[parameter_name]
        if get_origin(annotation) is not None:
            query_metadata.extend(
                meta
                for meta in get_args(annotation)[1:]
                if isinstance(meta, QueryParam)
            )

    assert offenders == []
    assert len(query_metadata) == 2
