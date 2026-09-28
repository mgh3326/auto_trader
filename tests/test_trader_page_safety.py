"""Safety: the /trader read-only surface must not reach mutation paths (889).

Two pins:
1. Subprocess import guard — importing the new router/service modules must not
   pull in order/approval/proposal/watch-mutation *services*. Broker *client*
   modules (e.g. ``app.services.brokers.toss.client``) are excluded on purpose:
   they are the mandated Stage-1 read clients; their mutation methods are
   pinned unreachable by the call scan below instead.
2. AST scan — no new module may *call* an order/approval/watch mutation name.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

NEW_MODULES = [
    "app.routers.trader_page",
    "app.routers.trader_spa",
    "app.services.trader_page.service",
    "app.services.trader_page.open_orders_cache",
    "app.schemas.trader_page",
]

# Mutation orchestration surfaces the read-only page must never import.
FORBIDDEN_IMPORT_PREFIXES = [
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.order_service",
    "app.services.orders",
    "app.services.order_send_intent_service",
    "app.services.order_preview_session_service",
    "app.services.pending_orders_service",
    "app.services.pending_order_sync_service",
    "app.services.execution_event",
    "app.services.investment_reports.watch_lifecycle",
    "app.services.alpaca_paper_order_application",
    "app.services.toss_live_order_ledger_service",
    "app.services.kis_live_order_ledger_service",
    "app.services.live_order_ledger_service",
    "app.services.paper_limit_order_service",
    "app.services.preopen_approval_bridge_common",
    "app.services.kis_mock_preopen_approval_bridge",
    "app.services.preopen_paper_approval_bridge",
    # fill-write pipeline: the page must stay downstream-read only
    "app.services.execution_ledger.fill_ingest",
    "app.services.fill_enrichment",
    "app.services.fill_notification",
    "app.services.fill_event_handoff",
    "app.monitoring.trade_notifier",
    "app.services.support_reserve_net_consumer",
    "app.services.toss_manual_activity",
    "app.services.trading_policy_service",
    "app.services.decision_table_apply",
    "app.services.market_close_digest",
    "app.tasks",
]

# ``app.models.__init__`` eagerly imports the order_proposals *model*, which
# pulls these enum/contract modules transitively; the package ``__init__`` is
# deliberately lazy so the real mutation services stay out.  Every module under
# the prefix that is NOT in this set is a mutation surface and must stay
# unimported.
ALLOWED_ORDER_PROPOSALS_ENUM_SURFACE = {
    "app.services.order_proposals",
    "app.services.order_proposals.errors",
    "app.services.order_proposals.state_machine",
    "app.services.order_proposals.rung_reason",
    "app.services.order_proposals.callback_inbox",
    "app.services.order_proposals.callback_inbox.contracts",
    "app.services.order_proposals.callback_inbox.result_boundary",
}

# Method/function names that mutate orders, approvals, watches, or ledgers.
# An attribute call or bare name call to any of these inside the new modules
# means a mutation path became reachable. The scan checks the call target's
# final attribute, so `svc.place_order()` is caught regardless of the
# receiver's name; bound aliases assigned to a local still evade it — the
# subprocess import guard above is the other half of the pin.
FORBIDDEN_CALL_NAMES = {
    "place_order",
    "cancel_order",
    "cancel_orders",
    "modify_order",
    "amend_order",
    "submit_order",
    "send_order",
    "approve",
    "approve_proposal",
    "reject_proposal",
    "dispatch",
    "record_fill_evidence",
    "upsert_fill",
    "commit_fill",
    "update_alert_status",
    "update_alert_lifecycle",
    "activate_alert",
    "cancel_alert",
    "expire_alerts",
    "create_alert",
    "insert_report",
    "execute",
}


@pytest.mark.unit
def test_trader_page_modules_import_no_mutation_services() -> None:
    project_root = Path(__file__).resolve().parent.parent
    script = (
        "import importlib, json, sys\n"
        f"for name in {json.dumps(NEW_MODULES)}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    forbidden_hits = {
        module
        for module in loaded
        for prefix in FORBIDDEN_IMPORT_PREFIXES
        if module == prefix or module.startswith(f"{prefix}.")
    }
    order_proposal_hits = {
        module
        for module in loaded
        if module == "app.services.order_proposals"
        or module.startswith("app.services.order_proposals.")
    } - ALLOWED_ORDER_PROPOSALS_ENUM_SURFACE
    offenders = sorted(forbidden_hits | order_proposal_hits)
    assert offenders == [], f"/trader modules pulled mutation services: {offenders}"


@pytest.mark.unit
def test_trader_page_modules_call_no_mutation_names() -> None:
    project_root = Path(__file__).resolve().parent.parent
    targets = [
        project_root / "app/routers/trader_page.py",
        project_root / "app/routers/trader_spa.py",
        project_root / "app/services/trader_page/service.py",
        project_root / "app/services/trader_page/open_orders_cache.py",
        project_root / "app/schemas/trader_page.py",
    ]
    offenders: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in FORBIDDEN_CALL_NAMES:
                offenders.append(f"{path.name}:{node.lineno}:{name}")
    assert offenders == [], f"mutation calls reachable from /trader: {offenders}"
