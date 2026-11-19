"""hk 3795 T3a: the ``mpb`` path stays apart from the order path, statically.

No database. The package-wide closure test of ``test_static_boundary.py`` already
covers every module under ``app/services/mandate``; these tests add what is specific to
the morning-batch card: it imports nothing from the order-proposal services at all,
it writes plan events only through the store, and the existing callback and the W5
inbox do not know it exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
BATCH_MODULES = (
    "app/services/mandate/batch_callback.py",
    "app/services/mandate/batches.py",
    "app/services/mandate/batch_rules.py",
    "app/services/mandate/batch_cards.py",
    "scripts/mandate_batch.py",
)
#: Everything that can place, route or approve an order, or reach a broker.
ORDER_SIDE = (
    "app.services.order_proposals",
    "app.services.brokers",
    "app.mcp_server",
    "app.services.mechanical_loss_cut",
    "app.services.execution_intent_gateway",
    "app.services.kis_",
    "app.services.toss_",
    "app.tasks",
)


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", BATCH_MODULES)
def test_a_batch_module_imports_nothing_from_the_order_side(module: str) -> None:
    offenders = [n for n in _imports(ROOT / module) if n.startswith(ORDER_SIDE)]
    assert offenders == []


def test_the_callback_writes_plan_events_only_through_the_store() -> None:
    """No ``session.add`` and no SQL insert in the callback module: the only write is
    ``append_plan_event`` (the T1 writer that the fold and the DB trigger both check)."""
    tree = ast.parse((ROOT / "app/services/mandate/batch_callback.py").read_text())
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "add" not in calls and "add_all" not in calls and "execute" not in calls
    assert calls.count("append_plan_event") == 1
    text = (ROOT / "app/services/mandate/batch_callback.py").read_text().lower()
    assert "insert into" not in text and "update review." not in text


def test_the_batch_writer_is_the_only_one_of_the_two_tables() -> None:
    """Only ``batches.py`` constructs batch or card rows; nothing else in ``app`` does."""
    hits: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("MandatePlanBatch", "MandatePlanBatchMember")
            ):
                hits.append(str(path.relative_to(ROOT)))
    assert sorted(set(hits)) == ["app/services/mandate/batches.py"]


@pytest.mark.parametrize(
    "path",
    [
        "app/services/order_proposals/telegram_callback.py",
        "app/services/order_proposals/approval_message.py",
        "app/services/order_proposals/callback_inbox/ingress.py",
        "app/services/order_proposals/callback_inbox/worker.py",
        "app/services/order_proposals/callback_inbox/contracts.py",
    ],
)
def test_the_existing_callback_and_the_w5_inbox_do_not_know_the_mpb_path(
    path: str,
) -> None:
    text = (ROOT / path).read_text()
    assert re.search(r"\bmpb\b", text) is None
    assert "mandate_plan_batch" not in text
    assert "mandate.batch_callback" not in text


def test_the_router_routes_by_the_prefix_and_leaves_the_rest_in_place() -> None:
    text = (ROOT / "app/routers/telegram_callback.py").read_text()
    assert "is_mandate_update(body)" in text
    first = text.index("is_mandate_update(body)")
    # the mandate branch comes before the durable-inbox branch and the inline call
    assert first < text.index("ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED:")
    assert first < text.index("await handle_callback_update(")
