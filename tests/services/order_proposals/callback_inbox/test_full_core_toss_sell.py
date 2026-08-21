"""W5 B14 — the durable default path through the *whole* order core.

Adversarial reviews R15, R16 and R17. Earlier attempts faked
``revalidate_and_submit`` (skipping approval hash, fresh preview, nonce, lease
and every rung transition) and then ``order_execution._place_order_impl``
(which is not transport at all: it owns the env/confirm gates, approval-hash
verification, the execution branch, the pre-send intent boundary and the
accepted-only ledger write). Both were false green.

This module takes the Toss seam, where ``revalidation._default_place_order_fn``
lazily imports and calls the **real** ``toss_preview_order`` /
``toss_place_order``, so no production default has to move.

REAL, end to end
    ``run_telegram_callback_job`` -> ``process_callback_job`` (advisory lock,
    attempt + entry markers) -> ``handle_normalized_callback`` (published
    binding preflight, approval-window boundary, nonce consumption, commit
    lease, ``record_approval``) -> ``revalidate_and_submit`` (fresh preview,
    buying-power claim, ``_classify_submit``, rung transitions) ->
    ``_default_place_order_fn`` -> ``toss_preview_order`` (approval-hash mint)
    -> ``toss_place_order`` (live approval-hash verification, **fresh direct
    broker sellable**, the POST, and the real ledger service write) ->
    ``record_toss_place_order`` -> ``review.toss_live_order_ledger``.

FAKED — exactly and only the external I/O boundary
    * the Toss API client itself (``TossReadClient.from_settings``), whose
      methods are the actual network calls: ``warnings``, ``holdings``,
      ``prices``, ``list_orders``, ``sellable_quantity``, ``place_order``,
      ``aclose``;
    * ``validate_toss_api_config`` (reads deployment secrets);
    * the KR session / NXT tradability calendar reads;
    * the sellable Redis cache (fakeredis, deliberately empty);
    * ``publish_place_time_forecast`` (an outbound publish, not order core);
    * the Telegram notifier.

Nothing between the task and the client is faked. In particular
``_place_order_impl``, ``toss_preview_order``, ``toss_place_order`` and
``record_toss_place_order`` all run for real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.dispatch_contract import ApprovalCardKind
from app.services.order_proposals.service import RungInput

from .conftest import (
    CHAT_ID,
    FakeNotifier,
    _publish_fixture_card,
    load_job,
    make_update,
    proposal_callback_data,
)

pytestmark = pytest.mark.integration

SYMBOL = "005930"
SELL_QUANTITY = Decimal("3")
SELL_PRICE = Decimal("71000")
BROKER_ORDER_ID = "w5-toss-order-1"


class TimelineTossClient:
    """The Toss API client, and only the client.

    Every method here is a real network call in production. Each records into
    a shared timeline so the *order* of broker interactions can be asserted --
    "was the fresh sellable read before the POST" is the question W2's direct
    preflight exists to answer, and only a timeline can answer it.
    """

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.placed_payloads: list[dict[str, Any]] = []
        self.sellable_calls: list[str] = []
        self.sellable_quantity_value = Decimal("10")

    async def aclose(self) -> None:
        return None

    async def warnings(self, symbol: str):
        self.timeline.append("warnings")
        return []

    async def holdings(self, *, symbol: str | None = None):
        self.timeline.append("holdings")
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    symbol=SYMBOL,
                    quantity=Decimal("10"),
                    average_purchase_price=Decimal("70000"),
                    last_price=SELL_PRICE,
                    name="Samsung",
                    market_country="KR",
                    currency="KRW",
                    market_value={},
                    profit_loss={},
                    daily_profit_loss={},
                    cost={},
                )
            ],
            raw_overview={},
        )

    async def prices(self, symbols):
        self.timeline.append("prices")
        return [
            SimpleNamespace(
                symbol=SYMBOL,
                last=SELL_PRICE,
                close=SELL_PRICE,
                base=SELL_PRICE,
                currency="KRW",
            )
        ]

    async def list_orders(self, *, status, symbol=None, **kwargs):
        self.timeline.append("list_orders")
        return SimpleNamespace(orders=[], next_cursor=None, has_next=False)

    async def sellable_quantity(self, *, symbol: str):
        # W2's direct, fresh, pre-mutation broker read.
        self.timeline.append("fresh_sellable")
        self.sellable_calls.append(symbol)
        return SimpleNamespace(sellable_quantity=self.sellable_quantity_value)

    async def place_order(self, payload):
        self.timeline.append("broker_mutation")
        self.placed_payloads.append(payload)
        return SimpleNamespace(
            order_id=BROKER_ORDER_ID,
            client_order_id=payload.get("clientOrderId"),
        )


@pytest.fixture
def toss_core(monkeypatch: pytest.MonkeyPatch) -> TimelineTossClient:
    """Arm the real Toss order core with a fake client and calendar reads."""
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    from app.mcp_server.tooling import toss_live_ledger
    from app.services.kr_symbol_universe_service import NxtTradability
    from app.services.toss_sellable_cache import TossSellableCache

    timeline: list[str] = []
    client = TimelineTossClient(timeline)

    # -- gates: enabled for this test only; the shipped defaults stay false --
    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    # -- the client itself is the only order-path fake ----------------------
    monkeypatch.setattr(
        otv.TossReadClient, "from_settings", classmethod(lambda cls, *a, **k: client)
    )

    # -- calendar / universe reads (broker-read-only helpers) ---------------
    async def _regular_session(_moment):
        return "regular"

    async def _nxt(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=True, nxt_trading_suspended=False, asof=None
            )
        }

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", _regular_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", _nxt)

    # -- a deliberately EMPTY warm cache: if the general/warm sellable path
    #    were consulted it would find nothing, and the timeline would show it.
    empty_cache = TossSellableCache(
        ttl_seconds=600,
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    monkeypatch.setattr(otv, "get_shared_sellable_cache", lambda: empty_cache)

    # -- an outbound publish, not order core --------------------------------
    async def _no_publish(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        toss_live_ledger, "publish_place_time_forecast", _no_publish, raising=False
    )
    return client


async def _seed_toss_sell(session, *, nonce: str):
    """A published, approvable KR Toss SELL with an explicit finite quantity."""
    service = OrderProposalsService(session)
    group = await service.create_proposal(
        symbol=SYMBOL,
        market="equity_kr",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="w5-full-core",
        rungs=[RungInput(0, "sell", SELL_QUANTITY, SELL_PRICE, None)],
    )
    dispatched_at = datetime.now(UTC)
    from tests.services.order_proposals.window_fakes import allow_known_session

    window = await allow_known_session(group, now=dispatched_at)
    await service.record_approval_dispatch(
        group.proposal_id,
        message_id=555,
        chat_id=str(CHAT_ID),
        now=dispatched_at,
        approval_window_policy_stamp=window.policy_stamp,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service, group, nonce=nonce, card_kind=ApprovalCardKind.MANUAL
    )
    await session.commit()
    return group


async def _queue(inbox_cleanup: list[uuid.UUID], data: str) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 670_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(data=data, update_id=update_id, callback_id=f"cbq-{update_id}"),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.accepted is True, result
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _ledger_rows(correlation_id: str | None = None) -> list[Any]:
    from app.models.review import TossLiveOrderLedger

    async with AsyncSessionLocal() as session:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.broker_order_id == BROKER_ORDER_ID
        )
        rows = list((await session.execute(stmt)).scalars().all())
        session.expunge_all()
        return rows


@pytest.mark.asyncio
async def test_the_durable_default_path_runs_the_whole_toss_sell_order_core(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    toss_core: TimelineTossClient,
) -> None:
    """B14 / R17 — no ``handler=``, no ``revalidate_fn=``, no core faked."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    group = await _seed_toss_sell(db_session, nonce="tosssell123")
    data = proposal_callback_data(group, action="op")
    job_id = await _queue(inbox_cleanup, data)

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    result = await task_module.run_telegram_callback_job(str(job_id))
    assert result["status"] == "succeeded", (result, toss_core.timeline)

    # -- the broker timeline: fresh sellable, then exactly one mutation -----
    assert toss_core.timeline.count("fresh_sellable") == 1, toss_core.timeline
    assert toss_core.timeline.count("broker_mutation") == 1, toss_core.timeline
    assert toss_core.timeline.index("fresh_sellable") < toss_core.timeline.index(
        "broker_mutation"
    ), toss_core.timeline
    assert toss_core.sellable_calls == [SYMBOL]

    # -- the mutation payload: an explicit, finite, positive SELL quantity --
    assert len(toss_core.placed_payloads) == 1
    payload = toss_core.placed_payloads[0]
    quantity = Decimal(str(payload.get("quantity")))
    assert quantity == SELL_QUANTITY and quantity > 0, payload
    assert str(payload.get("side")).lower() in {"sell", "ask"}, payload
    assert payload.get("clientOrderId"), "no idempotency key reached the broker"

    # -- the approval really was spent, once, by the real gates -------------
    async with AsyncSessionLocal() as session:
        refreshed, rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert refreshed.approval_nonce_used_at is not None
    assert refreshed.approved_at is not None
    assert refreshed.commit_lease_until is not None
    assert [rung.state for rung in rungs] in (["acked"], ["resting"]), [
        rung.state for rung in rungs
    ]
    rung = rungs[0]
    assert rung.broker_order_id == BROKER_ORDER_ID
    assert rung.correlation_id
    assert rung.approval_hash_digest, "no approval-hash digest was recorded"
    assert rung.idempotency_key, "no idempotency key was recorded"

    # -- the production ledger service wrote exactly one accepted row ------
    ledger_rows = await _ledger_rows()
    assert len(ledger_rows) == 1, ledger_rows
    assert ledger_rows[0].broker_order_id == BROKER_ORDER_ID

    # -- the inbox row is terminal and scrubbed ----------------------------
    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.nonce is None and row.chat_id is None

    # -- redelivery and recovery add nothing -------------------------------
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    replay_job = await _queue(inbox_cleanup, data)
    replay = await task_module.run_telegram_callback_job(str(replay_job))
    assert replay["status"] == "discarded", replay
    await recover_callback_jobs()

    assert toss_core.timeline.count("fresh_sellable") == 1, toss_core.timeline
    assert toss_core.timeline.count("broker_mutation") == 1, toss_core.timeline
    assert len(await _ledger_rows()) == 1


@pytest.mark.asyncio
async def test_the_full_core_test_fakes_nothing_above_the_client(
    _bootstrap_test_schema,
) -> None:
    """Structural: the order/ledger core must not be monkeypatched here.

    The two false-green attempts this file replaces both patched a core
    function and claimed it was transport. This guard names them.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    patched: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
        ):
            patched.append(str(node.args[1].value))

    for core in (
        "_place_order_impl",
        "toss_place_order",
        "toss_preview_order",
        "record_toss_place_order",
        "revalidate_and_submit",
        "handle_normalized_callback",
        "_default_place_order_fn",
        "consume_published_proposal_callback",
        "acquire_commit_lease",
    ):
        assert core not in patched, f"{core} is order core, not transport"
