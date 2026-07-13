from __future__ import annotations

from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.adapter_registry import (
    DuplicatePaperAdapterError,
    PaperAdapterNotFound,
    PaperAdapterRegistry,
)


class _Adapter:
    broker = Broker.BINANCE


def test_registry_registers_and_resolves_by_broker() -> None:
    adapter = _Adapter()
    registry = PaperAdapterRegistry()

    registry.register(adapter)  # type: ignore[arg-type]

    assert registry.resolve(Broker.BINANCE) is adapter


def test_registry_rejects_duplicate_broker() -> None:
    registry = PaperAdapterRegistry()
    registry.register(_Adapter())  # type: ignore[arg-type]

    try:
        registry.register(_Adapter())  # type: ignore[arg-type]
    except DuplicatePaperAdapterError as exc:
        assert exc.broker is Broker.BINANCE
    else:
        raise AssertionError("duplicate adapter registration must fail")


def test_registry_missing_broker_has_typed_error() -> None:
    registry = PaperAdapterRegistry()

    try:
        registry.resolve(Broker.ALPACA)
    except PaperAdapterNotFound as exc:
        assert exc.broker is Broker.ALPACA
    else:
        raise AssertionError("missing adapter resolution must fail")
