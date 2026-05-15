from __future__ import annotations

import importlib
import sys

_FORBIDDEN = {"app.services.brokers.upbit.orders", "app.services.upbit_websocket"}


def test_read_model_does_not_import_broker_mutations():
    for forbidden in _FORBIDDEN:
        sys.modules.pop(forbidden, None)
    importlib.import_module("app.services.upbit_public_read_model")
    importlib.import_module("app.services.upbit_public_read_model.read_model")
    for forbidden in _FORBIDDEN:
        assert forbidden not in sys.modules
