"""Only allow the futures read methods of the order-capable KIS client."""

from __future__ import annotations

import os
from typing import Any

ALLOWED_METHODS = frozenset(
    {
        "futures_balance",
        "futures_deposit",
        "futures_orderable",
        "futures_master",
        "close",
    }
)


def read_only_view(target: Any) -> Any:
    class ReadOnlyView:
        __slots__ = ()

        def __getattr__(self, name: str) -> Any:
            if name not in ALLOWED_METHODS:
                raise AttributeError(name)

            def call(*args: Any, **kwargs: Any) -> Any:
                return getattr(target, name)(*args, **kwargs)

            return call

        def __setattr__(self, name: str, value: Any) -> None:
            raise AttributeError(name)

    return ReadOnlyView()


def open_futures_view(*, is_mock: bool = True) -> Any:
    if os.environ.get("KIS_FUTURES_READ_ENABLED") != "true":
        raise RuntimeError("KIS_FUTURES_READ_ENABLED이 true가 아닙니다")
    from app.services.brokers.kis.client import KISClient

    return read_only_view(KISClient(is_mock=is_mock))
