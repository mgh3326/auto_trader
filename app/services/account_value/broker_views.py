"""Narrow broker views for read-only account observations."""

from typing import Any

ALLOWED_METHODS = {
    "kis": frozenset(
        {
            "fetch_my_stocks",
            "fetch_my_overseas_stocks",
            "inquire_integrated_margin",
            "inquire_overseas_margin",
        }
    ),
    "kis_close": frozenset({"close"}),
    "upbit": frozenset({"fetch_my_coins"}),
    "toss": frozenset(
        {"accounts", "holdings", "buying_power", "exchange_rate", "aclose"}
    ),
}


class BrokerMethodRefusedError(AttributeError):
    pass


def read_only_view(broker: str, target: Any) -> Any:
    allowed = ALLOWED_METHODS[broker]

    class View:
        __slots__ = ()

        def __getattr__(self, name: str) -> Any:
            if name not in allowed:
                raise BrokerMethodRefusedError(f"{broker}: {name!r} is not allowed")

            def call(*args: Any, **kwargs: Any) -> Any:
                return getattr(target, name)(*args, **kwargs)

            return call

        def __setattr__(self, name: str, value: Any) -> None:
            raise BrokerMethodRefusedError(f"{broker}: view is read-only")

    return View()
