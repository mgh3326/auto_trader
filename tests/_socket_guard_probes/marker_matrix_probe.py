"""Marker/option truth-table probe driven by ``tests/test_rob1296_live_only_socket_guard.py``.

Deliberately *not* named ``test_*.py``: the outer suite must never collect these
items, because each one's expected verdict flips depending on whether the nested
pytest run received ``--run-live``. The outer test invokes this file by path and
compares the recorded verdicts against the expected truth table.

The probe never opens a socket. It asks the guard for its verdict through the
side-effect-free ``is_socket_address_permitted`` predicate, so running the
matrix — including the armed-live cell — emits zero packets and leaves the
blocked-attempt evidence counters untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import _socket_guard as socket_guard

# RFC 5737 TEST-NET-3. Used only as an *argument* to the pure policy predicate;
# no connection is ever attempted against it from this module.
EXTERNAL_ADDRESS = ("203.0.113.1", 443)
LOOPBACK_ADDRESS = ("127.0.0.1", 5432)

RECORD_PATH_ENV = "ROB1296_PROBE_RECORD"


def _record(case: str) -> None:
    payload = {
        "case": case,
        "exempt": socket_guard.is_current_test_exempt(),
        "external_permitted": socket_guard.is_socket_address_permitted(
            EXTERNAL_ADDRESS
        ),
        "loopback_permitted": socket_guard.is_socket_address_permitted(
            LOOPBACK_ADDRESS
        ),
    }
    destination = Path(os.environ[RECORD_PATH_ENV])
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def test_unmarked() -> None:
    _record("unmarked")


@pytest.mark.integration
def test_integration_only() -> None:
    _record("integration_only")


@pytest.mark.live
def test_live_only() -> None:
    _record("live_only")


@pytest.mark.integration
@pytest.mark.live
def test_integration_and_live() -> None:
    _record("integration_and_live")
