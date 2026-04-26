import ast
import inspect

import pytest

from app.services.order_intent_discord_brief import build_decision_desk_url
from app.services import order_intent_discord_brief as brief_module


@pytest.mark.unit
def test_build_decision_desk_url_strips_trailing_slash() -> None:
    url = build_decision_desk_url("https://trader.robinco.dev/", "decision-r1")
    assert url == "https://trader.robinco.dev/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_local_origin() -> None:
    url = build_decision_desk_url("http://localhost:8000", "decision-r1")
    assert url == "http://localhost:8000/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_percent_encodes_run_id() -> None:
    url = build_decision_desk_url(
        "https://trader.robinco.dev/", "decision-abc/with slash"
    )
    assert url == (
        "https://trader.robinco.dev/portfolio/decision"
        "?run_id=decision-abc%2Fwith%20slash"
    )


@pytest.mark.unit
def test_module_does_not_import_forbidden_modules() -> None:
    """AST-level guard so the module stays import-side-effect free.

    Substring checks would catch forbidden tokens in docstrings; an AST
    walk only inspects actual `import` and `from ... import ...` nodes.
    """
    source = inspect.getsource(brief_module)
    tree = ast.parse(source)

    forbidden_prefixes = (
        "sqlalchemy",
        "redis",
        "httpx",
        "app.core.config",
        "app.tasks",
        "app.services.kis",
        "app.services.upbit",
        "app.services.redis_token_manager",
    )

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.append(node.module)

    for name in imported:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"forbidden import '{name}' in order_intent_discord_brief.py"
            )
