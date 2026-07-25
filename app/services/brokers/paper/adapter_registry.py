"""Adapter-only registry for the canonical paper execution application."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import PaperBrokerPort


class DuplicatePaperAdapterError(ValueError):
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        super().__init__(f"paper adapter already registered: {broker.value}")


class PaperAdapterNotFound(LookupError):
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        super().__init__(f"paper adapter is not registered: {broker.value}")


class PaperAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[Broker, PaperBrokerPort] = {}

    @property
    def adapters(self) -> Mapping[Broker, PaperBrokerPort]:
        return MappingProxyType(self._adapters)

    def register(self, adapter: PaperBrokerPort) -> None:
        broker = adapter.broker
        if broker in self._adapters:
            raise DuplicatePaperAdapterError(broker)
        self._adapters[broker] = adapter

    def resolve(self, broker: Broker) -> PaperBrokerPort:
        try:
            return self._adapters[broker]
        except KeyError as exc:
            raise PaperAdapterNotFound(broker) from exc


__all__ = [
    "DuplicatePaperAdapterError",
    "PaperAdapterNotFound",
    "PaperAdapterRegistry",
]
