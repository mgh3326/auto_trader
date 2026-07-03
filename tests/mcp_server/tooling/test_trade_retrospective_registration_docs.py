from __future__ import annotations

import pytest

from app.mcp_server.tooling.trade_retrospective_registration import (
    register_trade_retrospective_tools,
)
from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
)

pytestmark = pytest.mark.unit


class _FakeMCP:
    def __init__(self) -> None:
        self.descriptions: dict[str, str] = {}

    def tool(self, *, name: str, description: str):
        def _decorator(fn):
            self.descriptions[name] = description
            return fn

        return _decorator


def _register() -> str:
    mcp = _FakeMCP()
    register_trade_retrospective_tools(mcp)
    return mcp.descriptions["save_trade_retrospective"]


def test_description_enumerates_root_cause_class_values():
    desc = _register()
    for value in VALID_ROOT_CAUSE_CLASSES:
        assert value in desc, (
            f"root_cause_class value {value!r} missing from description"
        )


def test_description_enumerates_trigger_type_values():
    desc = _register()
    for value in VALID_TRIGGER_TYPES:
        assert value in desc, f"trigger_type value {value!r} missing from description"


def test_description_states_next_actions_required_with_trigger_type():
    desc = _register().lower()
    assert "next_actions" in desc
    assert "trigger_type" in desc
    assert "required" in desc
