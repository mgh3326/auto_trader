"""ROB-1270 J6B — Binance Spot Demo LIMIT composition.

Offline only: no broker, network, database, scheduler, or credential I/O.  The
``httpx_mock`` fixture is the ground truth for "zero transport" claims — every
such assertion below reads ``httpx_mock.get_requests()`` rather than inferring
from a return value.

The adversarial mutant set from the signed inputs (§F, twelve items) is covered
by the ``test_mutant_XX_*`` tests.  Each one is written so that inverting the
specific guard it names is what turns it red — not an import error, not a
missing fixture, and not a collapsed precondition.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import pathlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo import mock_auto_limit as mal
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.brokers.capabilities import (
    PAPER_BROKER_CAPABILITIES,
    Broker,
)
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.mock_integration.coordination import (
    DurableClaim,
    TerminalClaimEvidence,
)
from app.services.mock_integration.lineage import (
    DecisionIntentDraft,
    ExecutionPlanDraft,
    LineageEnvelope,
    MockLineageFactory,
    OrderAttemptDraft,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    ActivationStatus,
    LaneGuardError,
    get_lane_registry_entry,
)

_SPOT_DEMO_BASE = "https://demo-api.binance.com"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_MODULE_PATH = _REPO_ROOT / "app/services/brokers/binance/spot_demo/mock_auto_limit.py"


# --------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _SPOT_DEMO_BASE)


@pytest.fixture
def client(enabled_env: None) -> BinanceSpotDemoExecutionClient:
    return BinanceSpotDemoExecutionClient.from_env()


class _RecordingLaneEvidence:
    """Captures every lane-native evidence write in order."""

    def __init__(self) -> None:
        self.records: list[tuple[str, Mapping[str, Any]]] = []

    async def record_lane_evidence(
        self, kind: mal.SpotDemoLaneEvidenceKind, payload: Mapping[str, Any], /
    ) -> None:
        self.records.append((str(kind), dict(payload)))

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.records]


class _NeverCalledIntents:
    """Reservation port that fails loudly if coordination is ever reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reserve(self, **kwargs: Any) -> int:
        self.calls.append("reserve")
        raise AssertionError("coordination must not be reached in these tests")

    async def list_reservations(self, **kwargs: Any) -> list[Any]:
        self.calls.append("list_reservations")
        return []

    async def release_if_matches(self, **kwargs: Any) -> int:
        self.calls.append("release_if_matches")
        return 1


def _claims() -> Any:
    from app.services.mock_integration.coordination import DurableSendClaimAdapter

    return DurableSendClaimAdapter(_NeverCalledIntents())  # type: ignore[arg-type]


def _composition(
    client: BinanceSpotDemoExecutionClient,
    *,
    lane_evidence: Any = None,
    reservations: Any = None,
) -> mal.BinanceSpotDemoLimitComposition:
    return mal.BinanceSpotDemoLimitComposition(
        client=client,
        claims=_claims(),
        connection_factory=lambda: None,
        persistence=None,
        dispatch_evidence=None,
        uncertainty_gate=None,
        lane_evidence=lane_evidence or _RecordingLaneEvidence(),
        reservations=reservations,
    )


class _Reservation:
    """The row shape ``OrderSendIntentService.list_reservations`` returns."""

    def __init__(self, *, row_id: int, idempotency_key: str, side: str | None) -> None:
        self.row_id = row_id
        self.idempotency_key = idempotency_key
        self.side = side


class _RecordedReservations:
    """A reservation read-side port that records the scope it was asked about."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.scopes: list[str] = []

    async def reserve(self, **kwargs: Any) -> int:  # pragma: no cover - unused here
        raise AssertionError("the restart trigger never reserves")

    async def list_reservations(self, *, account_scope: str) -> list[Any]:
        self.scopes.append(account_scope)
        return list(self._rows)

    async def release_if_matches(self, **kwargs: Any) -> int:  # pragma: no cover
        raise AssertionError("the restart trigger never releases directly")


def _scope(calls: list[str] | None = None) -> Any:
    """A live J3A coordination scope whose ownership assertion is observable."""

    from app.services.mock_integration.coordination import CoordinationScope

    async def _assert_owned() -> None:
        if calls is not None:
            calls.append("assert_owned")

    return CoordinationScope(_assert_owned)


def _identity_known_entry() -> Any:
    """The canonical row with a physical identity, so a scope can be derived.

    The signed row's identity is ``UNKNOWN``, which is a real recovery gap (see
    the C3-7 tests).  Tests that need to exercise the *rest* of a path replace
    only that field, and never in the registry itself.
    """

    return dataclasses.replace(
        get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID),
        physical_account_id="binance-spot-demo-physical-1",
        identity_status="KNOWN",
    )


def _lane_scope(entry: Any) -> str:
    from app.services.mock_integration.coordination import (
        physical_account_scope_for_entry,
    )

    return physical_account_scope_for_entry(entry).claim_account_scope


def _attributed_claim(entry: Any, *, row_id: int = 1) -> DurableClaim:
    """A durable claim whose key really came from the J2B factory."""

    attempt = _envelope().order_attempt
    assert attempt is not None
    return DurableClaim(
        row_id=row_id,
        claim_account_scope=_lane_scope(entry),
        idempotency_key=attempt.idempotency_key,
        side="buy",
    )


# --------------------------------------------------------------------------
# Lineage builders — real J2B factory output, never hand-built identifiers
# --------------------------------------------------------------------------

_QUOTE = mal.LimitPriceQuote(
    price=Decimal("100"),
    source="binance_spot_demo:GET /api/v3/ticker/bookTicker",
    cutoff=datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
)
_STEP = mal.SpotStepSpec(
    step_size=Decimal("0.001"),
    step_version="exchangeInfo@2026-08-17T06:00:00Z",
    min_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
)


def _intent_draft(
    *, currency: str = "USDT", notional: Decimal = Decimal("10")
) -> DecisionIntentDraft:
    moment = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
    return DecisionIntentDraft(
        policy_version="j6b-test-policy",
        policy_version_hash="0" * 64,
        decision_timestamp=moment,
        market_data_cutoff=moment,
        symbol="BTCUSDT",
        side="buy",
        target_notional=notional,
        target_notional_currency=currency,  # type: ignore[arg-type]
        limit_policy={"kind": "explicit"},
        expiry_policy={"kind": "gtc"},
        rationale="J6B offline composition test",
    )


def _sizing() -> mal.LimitSizing:
    result = mal.compose_limit_sizing(
        target_notional=Decimal("10"),
        target_notional_currency="USDT",
        quote=_QUOTE,
        step=_STEP,
    )
    assert isinstance(result, mal.LimitSizing)
    return result


def _envelope(
    *,
    plan_draft: ExecutionPlanDraft | None = None,
    currency: str = "USDT",
) -> LineageEnvelope:
    factory = MockLineageFactory()
    intent_envelope = factory.create_intent_envelope(_intent_draft(currency=currency))
    draft = plan_draft or mal.compose_limit_plan_draft(
        normalized_symbol="BTCUSDT",
        sizing=_sizing(),
        step=_STEP,
        risk_caps={"cap_binding": "missing"},
    )
    plan_envelope = factory.create_plan_envelope(intent_envelope.decision_intent, draft)
    return factory.create_attempt_envelope(
        plan_envelope,
        OrderAttemptDraft(
            cycle_id="j6b-cycle-1",
            attempt_seq=1,
            lane_prefix=mal.SPOT_DEMO_LANE_PREFIX,
            broker_client_id_target=BrokerClientIdTarget.BINANCE_SPOT_DEMO,
        ),
    )


# ==========================================================================
# D6 sizing
# ==========================================================================


def test_sizing_floors_to_step_and_preserves_every_provenance_fact() -> None:
    """quantity = floor_to_step(notional / price), with all four D6 facts kept."""

    sizing = mal.compose_limit_sizing(
        target_notional=Decimal("10"),
        target_notional_currency="USDT",
        quote=_QUOTE,
        step=_STEP,
    )
    assert isinstance(sizing, mal.LimitSizing)
    # 10 / 100 = 0.1 exactly on a 0.001 step.
    assert sizing.quantity == Decimal("0.100")
    assert sizing.realized_notional == Decimal("10.000")
    assert sizing.rounding_delta == Decimal("0")
    provenance = sizing.provenance()
    assert provenance["mode"] == "floor_to_step"
    for key in mal.SIZING_PROVENANCE_KEYS:
        assert provenance[key], f"D6 provenance key {key} must be present"


def test_sizing_floors_a_remainder_down_and_records_the_delta() -> None:
    """A remainder is floored away and the discarded notional is recorded."""

    sizing = mal.compose_limit_sizing(
        target_notional=Decimal("10.05"),
        target_notional_currency="USDT",
        quote=_QUOTE,
        step=_STEP,
    )
    assert isinstance(sizing, mal.LimitSizing)
    assert sizing.quantity == Decimal("0.100")
    assert sizing.realized_notional == Decimal("10.000")
    assert sizing.rounding_delta == Decimal("0.05")
    assert sizing.realized_notional < Decimal("10.05")


def test_mutant_06_round_up_to_reach_min_notional_is_refused() -> None:
    """§F-6 — the floor is the only direction; reaching a minimum by rounding up
    would place a larger order than the decision authorized."""

    step = dataclasses.replace(_STEP, min_notional=Decimal("11"))
    blocked = mal.compose_limit_sizing(
        target_notional=Decimal("10"),
        target_notional_currency="USDT",
        quote=_QUOTE,
        step=step,
    )
    assert isinstance(blocked, mal.LimitSizingBlocked)
    assert blocked.reason is mal.SpotDemoLimitReason.SIZING_BELOW_MIN_NOTIONAL
    # The refusal is structural: a blocked sizing is never a plan.
    assert blocked.produces_plan is False
    assert "produces_plan" not in inspect.signature(mal.LimitSizingBlocked).parameters


def test_mutant_07_below_min_quantity_produces_no_plan_at_all() -> None:
    """§F-7 — under the venue minimum, no plan and no synthesized quantity."""

    blocked = mal.compose_limit_sizing(
        target_notional=Decimal("0.05"),
        target_notional_currency="USDT",
        quote=_QUOTE,
        step=_STEP,
    )
    assert isinstance(blocked, mal.LimitSizingBlocked)
    assert blocked.reason is mal.SpotDemoLimitReason.SIZING_BELOW_MIN_QTY
    assert not hasattr(blocked, "quantity")


def test_realized_notional_never_exceeds_the_target() -> None:
    """Property sweep: the floor invariant holds across a range of targets."""

    for cents in range(500, 2000, 37):
        target = Decimal(cents) / Decimal("100")
        sizing = mal.compose_limit_sizing(
            target_notional=target,
            target_notional_currency="USDT",
            quote=_QUOTE,
            step=_STEP,
        )
        if isinstance(sizing, mal.LimitSizingBlocked):
            continue
        assert sizing.realized_notional <= target
        assert sizing.rounding_delta >= 0


# ==========================================================================
# §F-8 — provenance completeness
# ==========================================================================


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source", "   "),
        ("cutoff", datetime(2026, 8, 17, 6, 0)),  # naive
        ("price", Decimal("0")),
    ],
)
def test_mutant_08a_price_provenance_must_be_complete(
    field_name: str, value: Any
) -> None:
    """§F-8 — an unattributed or undated price cannot size an order."""

    kwargs: dict[str, Any] = {
        "price": _QUOTE.price,
        "source": _QUOTE.source,
        "cutoff": _QUOTE.cutoff,
    }
    kwargs[field_name] = value
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.LimitPriceQuote(**kwargs)
    assert error.value.reason is mal.SpotDemoLimitReason.PRICE_PROVENANCE_INCOMPLETE


def test_mutant_08b_step_version_is_required() -> None:
    """§F-8 — filters without a snapshot version are not reproducible."""

    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.SpotStepSpec(
            step_size=Decimal("0.001"),
            step_version="",
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
    assert error.value.reason is mal.SpotDemoLimitReason.STEP_PROVENANCE_INCOMPLETE


#: Written out rather than read from the module: parametrizing over the
#: constant under test would make this suite shrink in step with a mutant that
#: shrinks the constant, which is a check with no discriminating power.
_EXPECTED_PROVENANCE_KEYS = (
    "price_source",
    "price_cutoff",
    "step_size",
    "step_version",
    "rounding_delta",
)


def test_the_five_d6_provenance_keys_are_exactly_these() -> None:
    """D6 — the required provenance set is pinned independently of the module."""

    assert mal.SIZING_PROVENANCE_KEYS == _EXPECTED_PROVENANCE_KEYS


@pytest.mark.parametrize("dropped", _EXPECTED_PROVENANCE_KEYS)
def test_mutant_08c_dropping_any_provenance_key_fails_before_io(
    dropped: str, client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-8 — each of the five provenance facts is individually load-bearing."""

    sizing = _sizing()
    provenance = sizing.provenance()
    del provenance[dropped]
    draft = mal.compose_limit_plan_draft(
        normalized_symbol="BTCUSDT",
        sizing=sizing,
        step=_STEP,
        risk_caps={"cap_binding": "missing"},
    )
    draft = ExecutionPlanDraft(
        **{**draft.model_dump(mode="python"), "tick_rounding": provenance}
    )
    envelope = _envelope(plan_draft=draft)

    # Asserted at the guard itself first, so a mutant that neuters it fails
    # *here* rather than falling through to some later registry check and
    # producing a red test whose traceback points somewhere else entirely.
    with pytest.raises(mal.SpotDemoLimitError) as direct:
        mal.assert_sizing_provenance_complete(envelope.execution_plan)
    assert direct.value.reason is mal.SpotDemoLimitReason.SIZING_PROVENANCE_INCOMPLETE
    assert dropped in direct.value.detail

    # And again through the real pre-dispatch chain, with zero transport.
    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(envelope)
    assert error.value.reason is mal.SpotDemoLimitReason.SIZING_PROVENANCE_INCOMPLETE
    assert httpx_mock.get_requests() == []


def test_negative_rounding_delta_is_reported_as_a_round_up(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """A delta below zero means the size grew; that is the round-up signature."""

    sizing = _sizing()
    provenance = sizing.provenance() | {"rounding_delta": "-0.5"}
    draft = ExecutionPlanDraft(
        **{
            **mal.compose_limit_plan_draft(
                normalized_symbol="BTCUSDT",
                sizing=sizing,
                step=_STEP,
                risk_caps={"cap_binding": "missing"},
            ).model_dump(mode="python"),
            "tick_rounding": provenance,
        }
    )
    envelope = _envelope(plan_draft=draft)
    with pytest.raises(mal.SpotDemoLimitError) as direct:
        mal.assert_sizing_provenance_complete(envelope.execution_plan)
    assert direct.value.reason is mal.SpotDemoLimitReason.SIZING_ROUND_UP_FORBIDDEN

    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(envelope)
    assert error.value.reason is mal.SpotDemoLimitReason.SIZING_ROUND_UP_FORBIDDEN
    assert httpx_mock.get_requests() == []


# ==========================================================================
# §F-5 — LIMIT only
# ==========================================================================


def test_mutant_05_notional_only_plan_is_refused_before_broker_io(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-5 — ``limit_price=None`` is the notional-only/MARKET shape."""

    sizing = _sizing()
    draft = ExecutionPlanDraft(
        **{
            **mal.compose_limit_plan_draft(
                normalized_symbol="BTCUSDT",
                sizing=sizing,
                step=_STEP,
                risk_caps={"cap_binding": "missing"},
            ).model_dump(mode="python"),
            "limit_price": None,
        }
    )
    envelope = _envelope(plan_draft=draft)
    with pytest.raises(mal.SpotDemoLimitError) as direct:
        mal.assert_limit_only_plan(envelope.execution_plan)
    assert direct.value.reason is mal.SpotDemoLimitReason.NOTIONAL_ONLY_PLAN_FORBIDDEN

    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(envelope)
    assert error.value.reason is mal.SpotDemoLimitReason.NOTIONAL_ONLY_PLAN_FORBIDDEN
    assert httpx_mock.get_requests() == []


def test_mutant_05b_plan_without_time_in_force_is_refused_before_broker_io(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-5 — a MARKET-shaped plan carries no time-in-force; LIMIT requires one."""

    draft = ExecutionPlanDraft(
        **{
            **mal.compose_limit_plan_draft(
                normalized_symbol="BTCUSDT",
                sizing=_sizing(),
                step=_STEP,
                risk_caps={"cap_binding": "missing"},
            ).model_dump(mode="python"),
            "time_in_force": None,
        }
    )
    envelope = _envelope(plan_draft=draft)
    with pytest.raises(mal.SpotDemoLimitError) as direct:
        mal.assert_limit_only_plan(envelope.execution_plan)
    assert direct.value.reason is mal.SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT

    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(envelope)
    assert error.value.reason is mal.SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT
    assert httpx_mock.get_requests() == []


def test_composition_only_emits_limit_orders() -> None:
    """The module can express exactly one order type."""

    assert mal.SPOT_DEMO_ORDER_TYPE == "LIMIT"
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert '"MARKET"' not in source


# ==========================================================================
# §F-9 / §83 correction 2 — currency
# ==========================================================================


def test_mutant_09_usd_intent_cannot_size_a_usdt_lane() -> None:
    """§F-9 — USD and USDT are distinct; there is no conversion."""

    blocked = mal.compose_limit_sizing(
        target_notional=Decimal("10"),
        target_notional_currency="USD",
        quote=_QUOTE,
        step=_STEP,
    )
    assert isinstance(blocked, mal.LimitSizingBlocked)
    assert blocked.reason is mal.SpotDemoLimitReason.QUOTE_CURRENCY_NOT_USDT


def test_mutant_09b_usd_intent_cannot_fan_out_to_this_lane(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-9 / C2-2 — the J2B factory refuses a USD intent against a USDT plan."""

    with pytest.raises(ValueError) as error:
        _envelope(currency="USD")
    assert "currency_conversion_not_authorized" in str(error.value)
    assert httpx_mock.get_requests() == []


def test_c2_2_three_way_currency_equality_is_required() -> None:
    """intent currency == plan quote currency == registry quote currency."""

    envelope = _envelope()
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert entry.quote_currency == "USDT"
    # Exact equality holds for the real triple.
    mal.assert_usdt_single_currency(
        envelope.decision_intent, envelope.execution_plan, entry
    )
    # A registry row claiming USD breaks the third leg.
    usd_entry = dataclasses.replace(entry, quote_currency="USD")
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_usdt_single_currency(
            envelope.decision_intent, envelope.execution_plan, usd_entry
        )
    assert error.value.reason is mal.SpotDemoLimitReason.QUOTE_CURRENCY_NOT_USDT


_FX_PATTERN = re.compile(
    r"(fx|parity|exchange[_-]?rate|usd[\s_:/-]*to[\s_:/-]*usdt"
    r"|usdt[\s_:/-]*to[\s_:/-]*usd)",
    re.IGNORECASE,
)


def _executable_source(path: pathlib.Path) -> str:
    """Source with comments and string literals removed.

    Prose that *forbids* conversion necessarily contains the words for it, so a
    naive text search over the whole file can only ever match its own
    prohibitions. Stripping comments and strings leaves the part that could
    actually perform a conversion.
    """

    import io
    import tokenize

    # f-strings tokenize as FSTRING_START/MIDDLE/END in 3.12+, so their literal
    # text is not a STRING token; the *expressions* inside them stay NAME/OP
    # tokens and are still scanned.
    dropped = {tokenize.COMMENT, tokenize.STRING} | {
        getattr(tokenize, name)
        for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if hasattr(tokenize, name)
    }
    kept: list[str] = []
    readline = io.StringIO(path.read_text(encoding="utf-8")).readline
    for token in tokenize.generate_tokens(readline):
        if token.type in dropped:
            continue
        kept.append(token.string)
    return "\n".join(kept)


def test_c2_3_no_fx_parity_or_conversion_path_exists() -> None:
    """C2-3 — exhaustive search of this module's executable surface finds none."""

    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1. No callable, attribute, argument, or imported name mentions conversion.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
            names.update(arg.arg for arg in node.args.args)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    assert [name for name in names if _FX_PATTERN.search(name)] == []

    # 2. Nothing in the executable text either, once prose is removed.
    offenders = [
        line
        for line in _executable_source(_MODULE_PATH).splitlines()
        if _FX_PATTERN.search(line)
    ]
    assert offenders == [], offenders

    # 3. The check discriminates: it does fire on a real conversion.
    assert _FX_PATTERN.search("rate = fx_rate('USD', 'USDT')")


def test_c2_4_sibling_binding_stays_pending() -> None:
    """C2-4 — the merged ROB-1269 disposition is consumed, not overridden."""

    assert mal.SIBLING_BINDING_FOR_EXECUTION == "PENDING"
    contract = (_REPO_ROOT / "docs/contracts/rob-1269-crypto-owner.md").read_text(
        encoding="utf-8"
    )
    assert "SIBLING_BINDING_FOR_EXECUTION = PENDING" in contract


def test_c2_5_alpaca_crypto_auto_mirror_is_policy_mirror_only() -> None:
    """C2-5 — ``AUTO_MIRROR`` is purpose-only and never a currency conversion."""

    for lane_id in ("crypto.alpaca.paper.default", "crypto.alpaca.paper.clean"):
        entry = get_lane_registry_entry(lane_id)
        assert entry.quote_currency == "USD"
        assert entry.writer is False
        assert entry.auto_order_enabled is False
        assert entry.lane_status is LaneStatus.NOT_READY
        assert entry.activation_status is ActivationStatus.DISABLED
        assert entry.scheduler_owner is None


# ==========================================================================
# §F-1 / §F-2 / §F-3 — writer domain
# ==========================================================================


def _pre_dispatch_error(
    client: BinanceSpotDemoExecutionClient, registry: Any
) -> BaseException | None:
    """Run the real pre-dispatch chain and hand back whatever it raised.

    Deliberately does **not** pre-judge the exception type. A guard that has been
    removed from the chain does not stop the chain — it lets a *later*, unrelated
    J2A check fail instead, and a bare ``pytest.raises(SpotDemoLimitError)``
    would then produce a red test whose traceback points at that other check.
    Returning the exception lets each test assert, on its own line, that the
    chain died at the guard under test and name what it got instead.
    """

    try:
        _composition(client).validate_pre_dispatch(_envelope(), registry=registry)
    except BaseException as exc:  # noqa: BLE001 - the assertion is the caller's
        return exc
    return None


def _binance_domain_guard_reached(
    client: BinanceSpotDemoExecutionClient,
    registry: Any,
    expected: mal.SpotDemoLimitReason,
) -> None:
    """Assert the pre-dispatch chain refuses *at* the writer-domain guard."""

    raised = _pre_dispatch_error(client, registry)
    assert isinstance(raised, mal.SpotDemoLimitError), (
        "the writer-domain invariant must be enforced by the pre-dispatch chain "
        f"itself, not only by a direct guard call; chain raised {raised!r}"
    )
    assert raised.reason is expected


def test_mutant_01_two_binance_writers_in_one_domain_fail() -> None:
    """§F-1 — identity is UNKNOWN, so every Binance demo lane is one domain."""

    mutated = tuple(
        dataclasses.replace(entry, writer=True)
        if entry.lane_id
        in (mal.SPOT_DEMO_CANONICAL_LANE_ID, mal.SPOT_DEMO_SIDECAR_LANE_ID)
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_binance_single_writer_domain(mutated)
    assert error.value.reason is mal.SpotDemoLimitReason.BINANCE_WRITER_CONFLICT
    # The signed registry itself is clean.
    mal.assert_binance_single_writer_domain(CANONICAL_LANE_REGISTRY)


def test_mutant_01b_shared_physical_identity_reaches_registry_writer_guard() -> None:
    """§F-1 — the bound shared identity makes the generic writer guard fire."""

    from app.services.mock_lane_registry import (
        RegistryStartupError,
        assert_single_writer,
    )

    mutated = tuple(
        dataclasses.replace(entry, writer=True)
        if entry.broker == "binance" and entry.account_profile == "spot_demo"
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    assert (
        len(
            {
                entry.physical_account_id
                for entry in mutated
                if entry.broker == "binance"
            }
        )
        == 1
    )
    with pytest.raises(RegistryStartupError):
        assert_single_writer(mutated)
    with pytest.raises(mal.SpotDemoLimitError):
        mal.assert_binance_single_writer_domain(mutated)


def test_mutant_01c_the_writer_domain_guard_runs_in_the_dispatch_chain(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-1 — the conservative guard is wired, not merely defined and exported.

    A guard that only fires when a test calls it by name protects nothing: the
    invariant has to be enforced on the path an order actually takes. This drives
    the real ``validate_pre_dispatch`` chain and asserts the refusal comes from
    the writer-domain guard rather than from a later registry check.

    The two writers are canonical + Futures Demo, deliberately **not** the B0-X
    sidecar: promoting the sidecar would also trip the sidecar guard, and a
    mutation that two guards can catch proves neither of them individually.
    """

    mutated = tuple(
        dataclasses.replace(entry, writer=True)
        if entry.lane_id
        in (mal.SPOT_DEMO_CANONICAL_LANE_ID, mal.SPOT_DEMO_FUTURES_LANE_ID)
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    _binance_domain_guard_reached(
        client, mutated, mal.SpotDemoLimitReason.BINANCE_WRITER_CONFLICT
    )
    # Discrimination: the signed registry gets *past* this guard and dies later,
    # on J2A's binding check — so the guard is not simply refusing everything.
    assert isinstance(
        _pre_dispatch_error(client, CANONICAL_LANE_REGISTRY), LaneGuardError
    )
    assert httpx_mock.get_requests() == []


def test_mutant_02c_the_sidecar_guard_runs_in_the_dispatch_chain(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-2 — a promoted sidecar is refused on the dispatch path, not only in a test."""

    mutated = tuple(
        dataclasses.replace(entry, scheduler_owner=SchedulerOwner.TASKIQ)
        if entry.lane_id == mal.SPOT_DEMO_SIDECAR_LANE_ID
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    _binance_domain_guard_reached(
        client, mutated, mal.SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY
    )
    assert httpx_mock.get_requests() == []


def test_mutant_03c_the_alpaca_crypto_guard_runs_in_the_dispatch_chain(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-3 — D1 is enforced on the dispatch path, not only by a direct call."""

    mutated = tuple(
        dataclasses.replace(entry, writer=True)
        if entry.lane_id == "crypto.alpaca.paper.default"
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    _binance_domain_guard_reached(
        client, mutated, mal.SpotDemoLimitReason.ALPACA_CRYPTO_MUTATION_UNASSIGNED
    )
    assert httpx_mock.get_requests() == []


def test_the_domain_invariants_are_reached_before_the_j2a_binding_check() -> None:
    """Ordering is the whole point: the guard must run *before* J2A's verdict.

    Placed after ``assert_lineage_registry_binding`` it would be dead code for
    this lane, because the signed registry always dies on the binding check
    first. This pins the order at the chain's own source rather than assuming it.
    """

    source = inspect.getsource(
        mal.BinanceSpotDemoLimitComposition.validate_pre_dispatch
    )
    domain_at = source.index("assert_binance_domain_invariants(")
    binding_at = source.index("assert_lineage_registry_binding(")
    assert domain_at < binding_at


@pytest.mark.parametrize("mutation", ["writer", "auto", "scheduler"])
def test_mutant_02_sidecar_must_stay_observation_only(mutation: str) -> None:
    """§F-2 — sidecar writer, auto, or a live recurring owner all fail."""

    changes: dict[str, Any] = {
        "writer": {"writer": True},
        "auto": {"auto_order_enabled": True},
        "scheduler": {"scheduler_owner": SchedulerOwner.TASKIQ},
    }[mutation]
    mutated = tuple(
        dataclasses.replace(entry, **changes)
        if entry.lane_id == mal.SPOT_DEMO_SIDECAR_LANE_ID
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_sidecar_observation_only(mutated)
    assert error.value.reason is mal.SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY
    mal.assert_sidecar_observation_only(CANONICAL_LANE_REGISTRY)


def test_sidecar_lane_id_cannot_borrow_this_composition(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """D2 — the sidecar composes no order even by presenting its own lane id."""

    draft = ExecutionPlanDraft(
        **{
            **mal.compose_limit_plan_draft(
                normalized_symbol="BTCUSDT",
                sizing=_sizing(),
                step=_STEP,
                risk_caps={"cap_binding": "missing"},
            ).model_dump(mode="python"),
            "lane_id": mal.SPOT_DEMO_SIDECAR_LANE_ID,
        }
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(_envelope(plan_draft=draft))
    assert error.value.reason is mal.SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    "lane_id", ["crypto.alpaca.paper.default", "crypto.alpaca.paper.clean"]
)
def test_mutant_03_alpaca_crypto_writer_is_refused(lane_id: str) -> None:
    """§F-3 — operator decision D1: no Alpaca crypto mutation wiring this epoch."""

    mutated = tuple(
        dataclasses.replace(entry, writer=True) if entry.lane_id == lane_id else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_alpaca_crypto_unwired(mutated)
    assert (
        error.value.reason is mal.SpotDemoLimitReason.ALPACA_CRYPTO_MUTATION_UNASSIGNED
    )
    mal.assert_alpaca_crypto_unwired(CANONICAL_LANE_REGISTRY)


def test_mutant_03b_module_imports_no_alpaca_surface() -> None:
    """§F-3 — no submit wiring can exist because nothing Alpaca is imported."""

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert [name for name in imported if "alpaca" in name.lower()] == []


# ==========================================================================
# §F-4 — the frozen ROB-845 asset
# ==========================================================================

#: Byte-frozen at the J6B base commit.  Changing any of these files is a
#: ROB-845 re-scope, not a J6B edit; this test is the tripwire.
_ROB845_FROZEN_SHA256: Mapping[str, str] = {
    "app/services/brokers/binance/paper_adapter.py": (
        "f6aa21a8875092e837efad7654f65a1398629f67f62f101a6ecb4cb84fabd794"
    ),
    "app/services/brokers/paper/composition.py": (
        "8cb5900d9cd9ea22edd4ac90b8e50addfd2529e7cb9efad4e530cb1ab632e8da"
    ),
    "app/services/brokers/capabilities.py": (
        "b468e35b0699932220d9bb32a0ea2b8c3cd8486c0bf4d5cb697db25a4c8e49e0"
    ),
}


@pytest.mark.parametrize("relative_path", sorted(_ROB845_FROZEN_SHA256))
def test_mutant_04a_rob845_asset_bytes_are_frozen(relative_path: str) -> None:
    """§F-4 — byte identity of the frozen adapter/capability files."""

    digest = hashlib.sha256((_REPO_ROOT / relative_path).read_bytes()).hexdigest()
    assert digest == _ROB845_FROZEN_SHA256[relative_path], (
        f"{relative_path} changed; ROB-845 is a frozen asset and J6B builds a "
        "separate LIMIT composition instead of editing it"
    )


def test_mutant_04b_rob845_binance_capability_shape_is_frozen() -> None:
    """§F-4 — behaviour, not only bytes: BUY / MARKET / notional-only stands."""

    capability = PAPER_BROKER_CAPABILITIES[Broker.BINANCE]
    assert capability.sides == frozenset({"buy"})
    assert capability.order_types == frozenset({"market"})
    assert capability.sizing_modes == frozenset({"notional"})
    assert capability.time_in_force == frozenset()
    # The new LIMIT composition is a different surface, not a relabel of it.
    assert mal.SPOT_DEMO_ORDER_TYPE.lower() not in capability.order_types
    assert mal.SPOT_DEMO_TIME_IN_FORCE.lower() not in capability.time_in_force


# ==========================================================================
# §F-11 — host and transport
# ==========================================================================


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.binance.com",  # live spot
        "https://fapi.binance.com",  # live futures
        "https://testnet.binance.vision",  # retired testnet
        "https://demo-fapi.binance.com",  # futures demo
        "https://demo-api.binance.com.evil.example",  # suffix spoof
    ],
)
def test_mutant_11_non_spot_demo_hosts_fail_before_any_request(
    base_url: str, monkeypatch: pytest.MonkeyPatch, httpx_mock: Any
) -> None:
    """§F-11 — live, testnet, and Futures hosts never reach a request build.

    The ROB-298 transport factory already rejects most of these at construction;
    where it does, the client cannot even be built, which is the same guarantee
    one layer earlier. Either way the assertion is: zero HTTP.
    """

    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", base_url)
    try:
        built = BinanceSpotDemoExecutionClient.from_env()
    except Exception:
        assert httpx_mock.get_requests() == []
        return
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_spot_demo_transport(built)
    assert error.value.reason is mal.SpotDemoLimitReason.TRANSPORT_HOST_NOT_SPOT_DEMO
    assert httpx_mock.get_requests() == []


def test_mutant_11b_a_subclassed_transport_is_refused(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-11 — a subclass can override the transport, so exact type is required."""

    class _LookalikeClient(BinanceSpotDemoExecutionClient):
        pass

    lookalike = object.__new__(_LookalikeClient)
    # Given a perfectly valid Spot Demo base URL, so the *only* thing that can
    # refuse it is the exact-type check. Without that check it would pass every
    # other gate — which is precisely the risk a subclassed transport poses.
    lookalike._base_url = _SPOT_DEMO_BASE
    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.assert_spot_demo_transport(lookalike)
    assert error.value.reason is mal.SpotDemoLimitReason.TRANSPORT_CLIENT_NOT_SPOT_DEMO
    # The genuine client passes, so the guard discriminates rather than refusing all.
    assert mal.assert_spot_demo_transport(client) == "demo-api.binance.com"
    assert httpx_mock.get_requests() == []


def test_module_names_no_live_or_futures_host() -> None:
    """No live/mainnet/testnet/futures host literal exists in this module."""

    source = _MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "api.binance.com/",
        "https://api.binance.com",
        "fapi.binance.com",
        "testnet.binance.vision",
    ):
        assert forbidden not in source


# ==========================================================================
# §F-10 — restart never reposts
# ==========================================================================


@pytest.mark.parametrize("outcome", list(mal.SpotDemoReadbackOutcome))
def test_mutant_10_no_restart_disposition_ever_authorizes_a_repost(
    outcome: mal.SpotDemoReadbackOutcome,
) -> None:
    """§F-10 — exhaustive over every readback outcome; repost is structural False."""

    disposition = mal.classify_restart_disposition(
        outcome, account_position_reconciled=True, remainder_known=True
    )
    assert disposition.repost is False
    assert "repost" not in inspect.signature(mal.RestartDisposition).parameters


def test_mutant_10b_unreadable_readback_holds_the_claim() -> None:
    """§F-10 — an unreadable readback is a held unknown, never an absence."""

    disposition = mal.classify_restart_disposition(
        mal.SpotDemoReadbackOutcome.UNREADABLE,
        account_position_reconciled=True,
        remainder_known=True,
    )
    assert disposition.may_release_claim is False
    assert disposition.operator_visible_state == mal.SPOT_DEMO_UNRECOVERABLE_STATE
    assert disposition.evidence_kind is mal.SpotDemoLaneEvidenceKind.UNKNOWN
    assert mal.terminal_evidence_for(disposition) == TerminalClaimEvidence()
    assert TerminalClaimEvidence().authorizes_release is False


def test_open_and_partial_orders_are_never_released() -> None:
    """A live order keeps its claim regardless of how confident the caller is."""

    for outcome in (
        mal.SpotDemoReadbackOutcome.OPEN,
        mal.SpotDemoReadbackOutcome.PARTIALLY_FILLED,
    ):
        disposition = mal.classify_restart_disposition(
            outcome, account_position_reconciled=True, remainder_known=True
        )
        assert disposition.may_release_claim is False


def test_not_created_still_requires_account_reconciliation() -> None:
    """C3-5 branch A — proven absence alone does not release the claim."""

    unreconciled = mal.classify_restart_disposition(
        mal.SpotDemoReadbackOutcome.NOT_CREATED, account_position_reconciled=False
    )
    assert unreconciled.may_release_claim is False
    assert unreconciled.operator_visible_state == mal.SPOT_DEMO_UNRECOVERABLE_STATE

    reconciled = mal.classify_restart_disposition(
        mal.SpotDemoReadbackOutcome.NOT_CREATED, account_position_reconciled=True
    )
    assert reconciled.may_release_claim is True
    evidence = mal.terminal_evidence_for(reconciled)
    assert evidence.authoritative_absence_proven is True
    assert evidence.authorizes_release is True


def test_terminal_fact_requires_both_reconcile_and_known_remainder() -> None:
    """C3-5 branch B — a partial answer does not release."""

    for reconciled, remainder in ((True, False), (False, True), (False, False)):
        disposition = mal.classify_restart_disposition(
            mal.SpotDemoReadbackOutcome.FILLED,
            account_position_reconciled=reconciled,
            remainder_known=remainder,
        )
        assert disposition.may_release_claim is False
    full = mal.classify_restart_disposition(
        mal.SpotDemoReadbackOutcome.FILLED,
        account_position_reconciled=True,
        remainder_known=True,
    )
    assert full.may_release_claim is True
    assert mal.terminal_evidence_for(full).authorizes_release is True


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("NEW", mal.SpotDemoReadbackOutcome.OPEN),
        ("PARTIALLY_FILLED", mal.SpotDemoReadbackOutcome.PARTIALLY_FILLED),
        ("FILLED", mal.SpotDemoReadbackOutcome.FILLED),
        ("CANCELED", mal.SpotDemoReadbackOutcome.CANCELED),
        ("REJECTED", mal.SpotDemoReadbackOutcome.REJECTED),
        ("EXPIRED", mal.SpotDemoReadbackOutcome.EXPIRED),
        ("SOMETHING_NEW_FROM_BINANCE", mal.SpotDemoReadbackOutcome.UNREADABLE),
        (None, mal.SpotDemoReadbackOutcome.UNREADABLE),
    ],
)
def test_unknown_native_status_is_unreadable_not_terminal(
    native: Any, expected: mal.SpotDemoReadbackOutcome
) -> None:
    """An unrecognised status is never optimistically read as terminal."""

    assert mal.classify_native_status(native) is expected


# ==========================================================================
# §F-12 — no scheduler
# ==========================================================================


def test_mutant_12_module_imports_no_scheduler_surface() -> None:
    """§F-12 — TaskIQ / Prefect / cron / launchd / systemd imports are absent."""

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = (
        "taskiq",
        "prefect",
        "cron",
        "celery",
        "apscheduler",
        "app.tasks",
        "app.flows",
    )
    offenders = [
        name for name in imported for token in forbidden if token in name.lower()
    ]
    assert offenders == [], offenders


def test_mutant_12b_a_recurring_request_is_refused(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """§F-12 — recurrence is refused outright, never quietly downgraded."""

    with pytest.raises(mal.SpotDemoLimitError) as error:
        _composition(client).validate_pre_dispatch(
            _envelope(), recurring_requested=True
        )
    assert (
        error.value.reason is mal.SpotDemoLimitReason.RECURRING_REGISTRATION_FORBIDDEN
    )
    assert httpx_mock.get_requests() == []


# ==========================================================================
# Acceptance — structural unreachability
# ==========================================================================


def test_execution_ready_is_unsatisfiable_for_this_lane() -> None:
    """Acceptance: the mutation path is structurally unreachable, not merely off.

    Exhaustive over every activation status and both writer/auto bits: ENABLED
    trips the signed-restriction guard, and every other status trips the
    activation guard. No configuration of this lane passes.
    """

    from app.services.mock_lane_registry import assert_entry_execution_ready

    base = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    checked = 0
    for status in ActivationStatus:
        for writer in (True, False):
            for auto in (True, False):
                entry = dataclasses.replace(
                    base,
                    activation_status=status,
                    writer=writer,
                    auto_order_enabled=auto,
                )
                with pytest.raises(LaneGuardError):
                    assert_entry_execution_ready(entry)
                checked += 1
    assert checked == len(ActivationStatus) * 4


@pytest.mark.asyncio
async def test_submit_with_the_signed_registry_dispatches_zero_http(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """Acceptance: even ``confirm=True`` reaches no transport and no claim."""

    intents = _NeverCalledIntents()
    from app.services.mock_integration.coordination import DurableSendClaimAdapter

    composition = mal.BinanceSpotDemoLimitComposition(
        client=client,
        claims=DurableSendClaimAdapter(intents),  # type: ignore[arg-type]
        connection_factory=lambda: None,
        persistence=None,
        dispatch_evidence=None,
        uncertainty_gate=None,
        lane_evidence=_RecordingLaneEvidence(),
    )
    with pytest.raises(LaneGuardError) as error:
        await composition.submit_limit_order(_envelope(), confirm=True)
    assert str(error.value).startswith("lane_binding_incomplete")
    assert mal.SPOT_DEMO_CANONICAL_LANE_ID in str(error.value)
    # Exactly zero: no HTTP, and no durable claim was even attempted.
    assert httpx_mock.get_requests() == []
    assert intents.calls == []


def test_registry_row_is_consumed_unchanged() -> None:
    """The signed row is read, never rewritten, by this job."""

    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert entry.writer is False
    assert entry.auto_order_enabled is False
    assert entry.lane_status is LaneStatus.NOT_READY
    assert entry.activation_status is ActivationStatus.BLOCKED
    assert entry.scheduler_owner is SchedulerOwner.DISABLED
    assert entry.physical_account_id == (
        "binance_demo:spot_plus_futures:credential_fingerprint="
        "sha256:e33925948f2cb6e03842cca9967b70f11f9242bc5c8f99c69ce0ca5cbc4d73df:"
        "one_shared_domain"
    )
    assert entry.identity_status == "KNOWN"
    assert entry.allowed_hosts == ("demo-api.binance.com",)


# ==========================================================================
# §D — primary terminal lane status
# ==========================================================================


def test_primary_terminal_status_is_the_policy_blocker() -> None:
    """§D — policy is primary; the absent scheduler is a secondary blocker."""

    assert (
        mal.SPOT_DEMO_PRIMARY_TERMINAL_LANE_STATUS
        is LaneStatus.AUTO_READY_BLOCKED_BY_POLICY
    )
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    blockers = mal.spot_demo_activation_blockers(entry)
    assert blockers[0] == "AUTO_READY_BLOCKED_BY_POLICY"
    assert mal.SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER in blockers
    # The scheduler status is never promoted to the primary position.
    assert LaneStatus.AUTO_READY_BLOCKED_BY_SCHEDULER.value not in blockers[0]


def test_an_absent_scheduler_owner_is_not_a_spelling_of_disabled() -> None:
    """§B — ``None`` and ``DISABLED`` are different facts and stay different.

    ``DISABLED`` is an explicit decision backed by in-repo evidence: Binance Demo
    has no scheduler registration. ``None`` is the *absence* of any bind
    authority, which is why those rows also carry ``MissingBinding.OWNER``.
    Reporting absence as disablement tells a downstream reader that an ownership
    decision was made when none was.

    The canonical Binance row happens to be ``DISABLED``, so testing only that
    row cannot see the difference — the ``None`` input has to be exercised
    directly, which is exactly what the signed table's Alpaca crypto rows are.
    """

    disabled = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert disabled.scheduler_owner is SchedulerOwner.DISABLED
    disabled_blockers = mal.spot_demo_activation_blockers(disabled)
    assert mal.SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER in disabled_blockers
    assert mal.SPOT_DEMO_ABSENT_SCHEDULER_OWNER_BLOCKER not in disabled_blockers

    absent = dataclasses.replace(disabled, scheduler_owner=None)
    assert absent.scheduler_owner is None
    absent_blockers = mal.spot_demo_activation_blockers(absent)
    assert mal.SPOT_DEMO_ABSENT_SCHEDULER_OWNER_BLOCKER in absent_blockers
    assert mal.SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER not in absent_blockers

    # The two blockers are distinct strings, not aliases of one another.
    assert (
        mal.SPOT_DEMO_ABSENT_SCHEDULER_OWNER_BLOCKER
        != mal.SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER
    )


def test_signed_rows_with_an_absent_owner_report_absence(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    """The real ``scheduler_owner=None`` rows in the signed table, unmodified."""

    for lane_id in ("crypto.alpaca.paper.default", "crypto.alpaca.paper.clean"):
        entry = get_lane_registry_entry(lane_id)
        assert entry.scheduler_owner is None
        blockers = mal.spot_demo_activation_blockers(entry)
        assert mal.SPOT_DEMO_ABSENT_SCHEDULER_OWNER_BLOCKER in blockers
        assert mal.SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER not in blockers


def test_j6b_does_not_modify_the_signed_registry() -> None:
    """Mechanically narrowing the signed allowlist is J2A-owned work."""

    registry_source = (_REPO_ROOT / "app/services/mock_lane_registry.py").read_text(
        encoding="utf-8"
    )
    # The superset the J2A verifier dispositioned is still exactly as merged.
    assert "AUTO_READY_BLOCKED_BY_SCHEDULER" in registry_source
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "CANONICAL_LANE_REGISTRY" not in assigned


# ==========================================================================
# §83 correction 3 — lane-native recovery ownership
# ==========================================================================


def test_c3_1_exactly_one_recovery_owner() -> None:
    """C3-1 — one owner, named, and it is a real importable object."""

    assert mal.SPOT_DEMO_RECOVERY_OWNER.endswith("BinanceSpotDemoLimitComposition")
    module_path, _, attribute = mal.SPOT_DEMO_RECOVERY_OWNER.rpartition(".")
    assert module_path == mal.__name__
    assert getattr(mal, attribute) is mal.BinanceSpotDemoLimitComposition
    assert "TBD" not in mal.SPOT_DEMO_RECOVERY_OWNER


def test_c3_2_restart_trigger_is_named_and_implemented() -> None:
    """C3-2 — the trigger names claim rediscovery and has a resolver."""

    assert mal.SPOT_DEMO_RESTART_TRIGGER == (
        "process_restart_rediscovers_durable_j2b_claims_for_physical_account"
    )
    assert callable(mal.BinanceSpotDemoLimitComposition.resolve_restart_claim)
    # The name has an executable entry point behind it, not just a resolver
    # waiting for someone else to find the claims.
    assert mal.SPOT_DEMO_RESTART_TRIGGER_ENTRYPOINT == (
        "BinanceSpotDemoLimitComposition.rediscover_restart_claims"
    )
    assert callable(mal.BinanceSpotDemoLimitComposition.rediscover_restart_claims)


@pytest.mark.asyncio
async def test_c3_2_restart_trigger_enumerates_surviving_claims_and_resolves_them(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """C3-2 — rediscovery really reads the reservation port and resolves each row.

    Naming a trigger is not owning one. This drives the production path end to
    end: enumerate within this lane's derived physical-account scope, derive each
    claim's client order id from its J2B key, read it back authoritatively, and
    dispose of it without ever reposting.
    """

    entry = _identity_known_entry()
    attempt = _envelope().order_attempt
    assert attempt is not None
    reservations = _RecordedReservations(
        [_Reservation(row_id=11, idempotency_key=attempt.idempotency_key, side="buy")]
    )
    evidence = _RecordingLaneEvidence()
    composition = _composition(
        client, lane_evidence=evidence, reservations=reservations
    )
    readbacks = _pin_readback(monkeypatch, client, "NEW")

    dispositions = await composition.rediscover_restart_claims(
        entry=entry, symbols={attempt.idempotency_key: "BTCUSDT"}
    )

    assert reservations.scopes == [_lane_scope(entry)]
    assert readbacks == [
        {"symbol": "BTCUSDT", "client_order_id": attempt.broker_client_order_id}
    ]
    assert len(dispositions) == 1
    assert dispositions[0].outcome is mal.SpotDemoReadbackOutcome.OPEN
    assert dispositions[0].repost is False
    assert dispositions[0].may_release_claim is False
    assert evidence.kinds == ["unknown"]
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_restart_trigger_leaves_unattributable_survivors_strictly_alone(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """A claim this lane's lineage cannot reproduce is not touched, only recorded.

    The physical account is shared, so a survivor may belong to another writer.
    Reading it back would be harmless; acting on it would not — and a silent skip
    would hide it from the operator entirely.
    """

    entry = _identity_known_entry()
    reservations = _RecordedReservations(
        [
            _Reservation(row_id=1, idempotency_key="not-a-j2b-key", side="buy"),
            _Reservation(
                row_id=2, idempotency_key="mock-idempotency-v1:short", side=None
            ),
        ]
    )
    evidence = _RecordingLaneEvidence()
    composition = _composition(
        client, lane_evidence=evidence, reservations=reservations
    )
    readbacks = _pin_readback(monkeypatch, client, "FILLED")

    dispositions = await composition.rediscover_restart_claims(entry=entry, symbols={})

    assert dispositions == ()
    assert readbacks == []
    assert evidence.kinds == ["unknown", "unknown"]
    for _, payload in evidence.records:
        assert payload["operator_visible_state"] == mal.SPOT_DEMO_UNRECOVERABLE_STATE
        assert payload["repost"] is False
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_restart_trigger_holds_a_survivor_whose_symbol_is_unknown(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """An attributable claim with no known symbol is held, never guessed at."""

    entry = _identity_known_entry()
    attempt = _envelope().order_attempt
    assert attempt is not None
    reservations = _RecordedReservations(
        [_Reservation(row_id=3, idempotency_key=attempt.idempotency_key, side="buy")]
    )
    evidence = _RecordingLaneEvidence()
    composition = _composition(
        client, lane_evidence=evidence, reservations=reservations
    )
    readbacks = _pin_readback(monkeypatch, client, "FILLED")

    assert await composition.rediscover_restart_claims(entry=entry, symbols={}) == ()
    assert readbacks == []
    assert evidence.kinds == ["unknown"]
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_restart_trigger_without_a_claim_source_fails_closed(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """No read-side port means no trigger — and it says so instead of pretending."""

    with pytest.raises(mal.SpotDemoLimitError) as error:
        await _composition(client).rediscover_restart_claims(
            entry=_identity_known_entry(), symbols={}
        )
    assert (
        error.value.reason is mal.SpotDemoLimitReason.RESTART_CLAIM_SOURCE_UNAVAILABLE
    )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_restart_trigger_enumerates_with_canonical_identity_binding(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """The canonical J2A identity, not a caller scope, drives enumeration."""

    reservations = _RecordedReservations([])
    composition = _composition(client, reservations=reservations)
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert await composition.rediscover_restart_claims(entry=entry, symbols={}) == ()
    assert reservations.scopes == [_lane_scope(entry)]
    assert httpx_mock.get_requests() == []


def test_c3_3_authoritative_readback_is_the_order_endpoint() -> None:
    """C3-3 — a single-order readback, not an open-orders listing."""

    assert "/api/v3/order" in mal.SPOT_DEMO_AUTHORITATIVE_READBACK
    assert "get_order_status" in mal.SPOT_DEMO_AUTHORITATIVE_READBACK
    assert "openOrders" not in mal.SPOT_DEMO_AUTHORITATIVE_READBACK


def test_c3_4_all_seven_lane_evidence_kinds_exist() -> None:
    """C3-4 — the closed set is exactly the seven required kinds."""

    assert mal.LANE_EVIDENCE_KINDS == {
        "ack",
        "unknown",
        "reject",
        "expiry",
        "partial_fill",
        "cancel",
        "terminal_reconciliation",
    }


def _pin_readback(
    monkeypatch: pytest.MonkeyPatch,
    client: BinanceSpotDemoExecutionClient,
    native: str | BaseException,
) -> list[dict[str, Any]]:
    """Replace the client's single-order readback with a recorded stub.

    The stub sits at the transport method, not at the composition, so the real
    ``readback`` → ``classify_native_status`` → ``classify_restart_disposition``
    chain still runs.
    """

    calls: list[dict[str, Any]] = []

    async def _stub(*, symbol: str, client_order_id: str) -> dict[str, Any]:
        calls.append({"symbol": symbol, "client_order_id": client_order_id})
        if isinstance(native, BaseException):
            raise native
        return {"status": native, "origClientOrderId": client_order_id}

    monkeypatch.setattr(client, "get_order_status", _stub)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native", "expected_kind"),
    [
        (RuntimeError("transport blew up"), "unknown"),
        ("REJECTED", "reject"),
        ("EXPIRED", "expiry"),
        ("PARTIALLY_FILLED", "partial_fill"),
        ("CANCELED", "cancel"),
        ("FILLED", "terminal_reconciliation"),
        (BinanceDemoOrderNotFound("absent"), "terminal_reconciliation"),
        ("NEW", "unknown"),
    ],
)
async def test_c3_4b_each_outcome_writes_its_lane_native_evidence(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
    native: str | BaseException,
    expected_kind: str,
) -> None:
    """C3-4 — every readback outcome lands in the closed evidence vocabulary."""

    evidence = _RecordingLaneEvidence()
    composition = _composition(client, lane_evidence=evidence)
    calls = _pin_readback(monkeypatch, client, native)
    claim = DurableClaim(
        row_id=1, claim_account_scope="scope", idempotency_key="k", side="buy"
    )

    disposition = await composition.resolve_restart_claim(
        claim, symbol="BTCUSDT", client_order_id="bnsd-abc"
    )

    assert calls == [{"symbol": "BTCUSDT", "client_order_id": "bnsd-abc"}]
    assert evidence.kinds[0] == expected_kind
    assert set(evidence.kinds) <= mal.LANE_EVIDENCE_KINDS
    # Nothing is reposted and nothing is released without reconciliation.
    assert disposition.repost is False
    assert disposition.may_release_claim is False
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_c3_4c_terminal_reconciliation_releases_only_with_full_evidence(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """C3-5 — with reconcile + known remainder, the claim releases through J3A."""

    evidence = _RecordingLaneEvidence()
    intents = _NeverCalledIntents()
    from app.services.mock_integration.coordination import DurableSendClaimAdapter

    composition = mal.BinanceSpotDemoLimitComposition(
        client=client,
        claims=DurableSendClaimAdapter(intents),  # type: ignore[arg-type]
        connection_factory=lambda: None,
        persistence=None,
        dispatch_evidence=None,
        uncertainty_gate=None,
        lane_evidence=evidence,
    )
    _pin_readback(monkeypatch, client, "FILLED")
    claim = DurableClaim(
        row_id=7, claim_account_scope="scope", idempotency_key="k", side="buy"
    )

    disposition = await composition.resolve_restart_claim(
        claim,
        symbol="BTCUSDT",
        client_order_id="bnsd-abc",
        account_position_reconciled=True,
        remainder_known=True,
    )

    assert disposition.may_release_claim is True
    assert intents.calls == ["release_if_matches"]
    assert evidence.kinds == ["terminal_reconciliation", "terminal_reconciliation"]
    assert evidence.records[-1][1]["released"] is True
    assert httpx_mock.get_requests() == []


# ==========================================================================
# Cancel — the same attribution / coordination / uncertainty contract as submit
# ==========================================================================


class _CancelSeam:
    """Records every DELETE the composition attempts, and can fail after send."""

    def __init__(self, *, result: Any = None, raises: BaseException | None = None):
        self.calls: list[tuple[str, str, bool]] = []
        self._result = result
        self._raises = raises

    async def __call__(
        self, *, symbol: str, client_order_id: str, confirm: bool = False
    ) -> Any:
        self.calls.append((symbol, client_order_id, confirm))
        if self._raises is not None:
            raise self._raises
        return self._result


class _NativeCancel:
    def __init__(self, status: str | None, broker_order_id: str = "9001") -> None:
        self.status = status
        self.broker_order_id = broker_order_id


async def _cancel(
    client: BinanceSpotDemoExecutionClient,
    seam: _CancelSeam | None,
    evidence: _RecordingLaneEvidence,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_order_id: str | None = None,
    claim: DurableClaim | None = None,
    attributed_native_order_id: str = "9001",
    known_remainder: Decimal | None = Decimal("0.1"),
    confirm: bool = False,
    scope_calls: list[str] | None = None,
) -> Any:
    entry = _identity_known_entry()
    if seam is not None:
        monkeypatch.setattr(client, "cancel_order", seam)
    attempt = _envelope().order_attempt
    assert attempt is not None
    return await _composition(client, lane_evidence=evidence).cancel_limit_order(
        _scope(scope_calls),
        claim if claim is not None else _attributed_claim(entry),
        entry=entry,
        symbol="BTCUSDT",
        client_order_id=(
            client_order_id
            if client_order_id is not None
            else str(attempt.broker_client_order_id)
        ),
        attributed_native_order_id=attributed_native_order_id,
        known_remainder=known_remainder,  # type: ignore[arg-type]
        confirm=confirm,
    )


def test_attributed_client_order_id_round_trips_the_j2b_factory() -> None:
    """Attribution is a real derivation, not a naming convention.

    J2B builds the durable claim key and the native client order id from the same
    digest, so one determines the other. Pinned against actual factory output —
    if J2B ever changes either derivation, this fails rather than silently
    letting an unrelated order id look attributed.
    """

    attempt = _envelope().order_attempt
    assert attempt is not None
    derived = mal.derive_attributed_client_order_id(attempt.idempotency_key)
    assert derived == attempt.broker_client_order_id
    assert derived.startswith(f"{mal.SPOT_DEMO_LANE_PREFIX}-")


@pytest.mark.asyncio
async def test_cancel_refuses_a_client_order_id_this_lane_did_not_produce(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """An arbitrary or third-party order id is not cancellable through this lane.

    ``confirm`` decides whether a mutation is *sent*; it says nothing about
    whether the thing being mutated is ours. Without this guard a caller holding
    any claim could DELETE someone else's order and have the lane record it as
    its own cancel.
    """

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    for foreign in ("someone-elses-order", "bnsd-arbitrary"):
        with pytest.raises(mal.SpotDemoLimitError) as error:
            await _cancel(
                client,
                seam,
                evidence,
                monkeypatch,
                client_order_id=foreign,
                confirm=True,
            )
        assert error.value.reason is mal.SpotDemoLimitReason.CANCEL_NOT_ATTRIBUTED
    # Nothing was sent and nothing was recorded.
    assert seam.calls == []
    assert evidence.records == []
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_refuses_a_claim_from_another_physical_account_scope(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """A claim outside this lane's derived scope is not this lane's to follow up."""

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    attempt = _envelope().order_attempt
    assert attempt is not None
    foreign_claim = DurableClaim(
        row_id=1,
        claim_account_scope="scope-belonging-to-another-account",
        idempotency_key=attempt.idempotency_key,
        side="buy",
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        await _cancel(
            client, seam, evidence, monkeypatch, claim=foreign_claim, confirm=True
        )
    assert error.value.reason is mal.SpotDemoLimitReason.CANCEL_NOT_ATTRIBUTED
    assert seam.calls == []
    assert evidence.records == []
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_dry_run_writes_no_durable_cancel_fact(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """A dry run cancels nothing, so it records nothing.

    A durable ``CANCEL`` row asserts the broker cancelled the order. On a dry run
    it did not, and a later reconciliation reading that row would conclude the
    order is gone when it is still live at the venue.
    """

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    disposition = await _cancel(client, seam, evidence, monkeypatch, confirm=False)

    assert disposition.dispatched is False
    assert disposition.evidence_kind is None
    assert evidence.records == []
    assert seam.calls == []
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_confirmed_records_the_cancel_kind(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """C3-4 — a broker-proven cancellation is the only thing recorded as ``cancel``."""

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    scope_calls: list[str] = []
    attempt = _envelope().order_attempt
    assert attempt is not None

    disposition = await _cancel(
        client, seam, evidence, monkeypatch, confirm=True, scope_calls=scope_calls
    )

    assert disposition.dispatched is True
    assert evidence.kinds == ["cancel"]
    assert evidence.records[0][1]["operation"] == "cancel"
    assert evidence.records[0][1]["broker_order_id"] == "9001"
    # The DELETE used the *derived* id, and lease ownership was re-proved both
    # before the follow-up description and again immediately before the send.
    assert seam.calls == [("BTCUSDT", str(attempt.broker_client_order_id), True)]
    assert scope_calls == ["assert_owned", "assert_owned"]
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "", "NEW", "PARTIALLY_FILLED"])
async def test_cancel_response_without_proof_is_unknown_not_cancel(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
    status: str | None,
) -> None:
    """A response that does not prove a cancellation is an unknown, not a cancel."""

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel(status))
    disposition = await _cancel(client, seam, evidence, monkeypatch, confirm=True)

    assert disposition.evidence_kind is mal.SpotDemoLaneEvidenceKind.UNKNOWN
    assert evidence.kinds == ["unknown"]
    assert (
        evidence.records[0][1]["operator_visible_state"]
        == mal.SPOT_DEMO_UNRECOVERABLE_STATE
    )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_post_send_uncertainty_leaves_durable_unknown_evidence(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """The DELETE may have reached the broker, so silence is not an option.

    ROB-298 §4 / ROB-395 semantics: an unproven outcome is recorded as an
    unknown carrying the operator-visible blocked state, and the exception is
    re-raised. Swallowing it, or recording nothing, would leave a possibly
    cancelled order looking untouched.
    """

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(raises=RuntimeError("simulated post-send uncertainty"))
    attempt = _envelope().order_attempt
    assert attempt is not None

    with pytest.raises(RuntimeError):
        await _cancel(client, seam, evidence, monkeypatch, confirm=True)

    assert seam.calls == [("BTCUSDT", str(attempt.broker_client_order_id), True)]
    assert evidence.kinds == ["unknown"], (
        "a cancel whose outcome is unknown must leave durable evidence saying so"
    )
    payload = evidence.records[0][1]
    assert payload["operation"] == "cancel"
    assert payload["operator_visible_state"] == mal.SPOT_DEMO_UNRECOVERABLE_STATE
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_order_id", "remainder"),
    [("", Decimal("0.1")), ("   ", Decimal("0.1")), ("9001", None)],
)
async def test_cancel_requires_the_j3a_followup_capability(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
    native_order_id: str,
    remainder: Decimal | None,
) -> None:
    """J3A's own follow-up predicate gates the cancel, unattributed id included."""

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    with pytest.raises(mal.SpotDemoLimitError) as error:
        await _cancel(
            client,
            seam,
            evidence,
            monkeypatch,
            attributed_native_order_id=native_order_id,
            known_remainder=remainder,
            confirm=True,
        )
    assert (
        error.value.reason is mal.SpotDemoLimitReason.CANCEL_FOLLOWUP_CAPABILITY_ABSENT
    )
    assert seam.calls == []
    assert evidence.records == []
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_runs_the_writer_domain_invariants(
    client: BinanceSpotDemoExecutionClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: Any,
) -> None:
    """The cancel path is not a side door around the registry invariants."""

    evidence = _RecordingLaneEvidence()
    seam = _CancelSeam(result=_NativeCancel("CANCELED"))
    monkeypatch.setattr(client, "cancel_order", seam)
    entry = _identity_known_entry()
    mutated = tuple(
        dataclasses.replace(item, writer=True)
        if item.lane_id
        in (mal.SPOT_DEMO_CANONICAL_LANE_ID, mal.SPOT_DEMO_FUTURES_LANE_ID)
        else item
        for item in CANONICAL_LANE_REGISTRY
    )
    with pytest.raises(mal.SpotDemoLimitError) as error:
        await _composition(client, lane_evidence=evidence).cancel_limit_order(
            _scope(),
            _attributed_claim(entry),
            entry=entry,
            symbol="BTCUSDT",
            client_order_id=str(_envelope().order_attempt.broker_client_order_id),
            attributed_native_order_id="9001",
            known_remainder=Decimal("0.1"),
            registry=mutated,
            confirm=True,
        )
    assert error.value.reason is mal.SpotDemoLimitReason.BINANCE_WRITER_CONFLICT
    assert seam.calls == []
    assert httpx_mock.get_requests() == []


def test_c3_4e_ack_and_unknown_submit_branches_are_classified() -> None:
    """C3-4 — the submit-path ACK/unknown branches, proven at their real seam.

    The dispatch path is structurally unreachable under the signed registry (see
    ``test_execution_ready_is_unsatisfiable_for_this_lane``), so it cannot be
    driven end to end. ``classify_submit_outcome`` is the seam the closure
    actually calls, and it is tested directly rather than described in prose.
    """

    class _Acknowledged:
        broker_order_id = "  12345  "
        status = "NEW"

    class _NoNativeId:
        broker_order_id = ""
        status = "NEW"

    from app.services.brokers.binance.spot_demo.execution_client import (
        SpotDemoDryRunResult,
    )

    acknowledged = mal.classify_submit_outcome(_Acknowledged())
    assert acknowledged.evidence_kind is mal.SpotDemoLaneEvidenceKind.ACK
    assert acknowledged.certainty is mal.MutationCertainty.DEFINITIVE
    assert acknowledged.broker_order_id == "12345"

    for response in (
        _NoNativeId(),
        object(),
        SpotDemoDryRunResult(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            client_order_id="bnsd-abc",
        ),
    ):
        outcome = mal.classify_submit_outcome(response)
        assert outcome.evidence_kind is mal.SpotDemoLaneEvidenceKind.UNKNOWN
        assert outcome.certainty is mal.MutationCertainty.UNCERTAIN
        assert outcome.broker_order_id is None

    # A raised submit is an uncertainty, never a proven non-send.
    assert mal.SUBMIT_RAISED_OUTCOME.certainty is mal.MutationCertainty.UNCERTAIN
    assert (
        mal.SUBMIT_RAISED_OUTCOME.evidence_kind is mal.SpotDemoLaneEvidenceKind.UNKNOWN
    )


def test_c3_4f_submit_closure_uses_the_classifier_it_claims_to() -> None:
    """The seam is load-bearing: the dispatch closure really calls it."""

    source = inspect.getsource(mal.BinanceSpotDemoLimitComposition.submit_limit_order)
    assert "classify_submit_outcome(result)" in source
    assert "SUBMIT_RAISED_OUTCOME" in source
    assert "_record_submit_outcome" in source


def test_c3_5_release_condition_is_the_j3a_evidence_gate() -> None:
    """C3-5 — release flows through ``release_with_terminal_evidence`` only."""

    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "release_with_terminal_evidence" in source
    # The unrestricted delete is never named here.
    assert "release_if_matches(" not in source


def test_c3_6_operator_visible_blocked_state_is_named() -> None:
    """C3-6 — a concrete operator-visible state, not a silent hold."""

    assert mal.SPOT_DEMO_UNRECOVERABLE_STATE == "unknown_pending_reconcile"
    disposition = mal.classify_restart_disposition(
        mal.SpotDemoReadbackOutcome.UNREADABLE
    )
    assert disposition.operator_visible_state == mal.SPOT_DEMO_UNRECOVERABLE_STATE


def test_c3_7_lifecycle_status_is_computed_from_the_recovery_items() -> None:
    """C3-7 — the lane is lifecycle-blocked because it is *computed* to be.

    A constant naming ``AUTO_READY_BLOCKED_BY_LIFECYCLE`` that nothing evaluates
    is a claim, not a state. The verdict here is derived from live objects, and
    today it is blocked for two concrete reasons: no production caller
    constructs this composition (so there is no bound lane-evidence store), and
    the restart trigger has no enumerable physical-account scope.
    """

    assert (
        mal.SPOT_DEMO_LIFECYCLE_BLOCKED_LANE_STATUS
        is LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE
    )
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert entry.activation_status is not ActivationStatus.ENABLED

    gaps = mal.spot_demo_recovery_contract_gaps(entry)
    assert set(gaps) == {"restart_trigger", "lane_native_evidence"}
    assert set(gaps) <= set(mal.SPOT_DEMO_RECOVERY_CONTRACT_ITEMS)
    assert (
        mal.spot_demo_lifecycle_lane_status(entry)
        is LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE
    )

    blockers = mal.spot_demo_activation_blockers(entry)
    assert LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE.value in blockers
    for gap in gaps:
        assert f"recovery_gap={gap}" in blockers


def test_c3_7_a_fully_wired_owner_has_no_recovery_gaps(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    """The verdict discriminates: it is not a constant dressed as a computation.

    Given an owner holding both ports and an identity-known row, every item is
    satisfied and the lifecycle verdict clears. Without this, ``blocked`` would
    be indistinguishable from ``always returns blocked``.
    """

    composition = _composition(client, reservations=_RecordedReservations([]))
    entry = _identity_known_entry()

    assert mal.spot_demo_recovery_contract_gaps(entry, composition=composition) == ()
    assert mal.spot_demo_lifecycle_lane_status(entry, composition=composition) is None
    blockers = mal.spot_demo_activation_blockers(entry, composition=composition)
    assert LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE.value not in blockers
    # §D is untouched: policy is still the primary terminal status.
    assert blockers[0] == LaneStatus.AUTO_READY_BLOCKED_BY_POLICY.value


def test_c3_7_each_missing_recovery_item_is_reported_on_its_own(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    """Every gap has an independent cause, so one cannot mask another."""

    wired = _composition(client, reservations=_RecordedReservations([]))
    unwired = _composition(client)

    # The signed identity plus a wired read-side port makes this one item ready.
    signed = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert mal.spot_demo_recovery_contract_gaps(signed, composition=wired) == ()
    # An unbound reservation port alone still blocks the same trigger.
    assert mal.spot_demo_recovery_contract_gaps(signed, composition=unwired) == (
        "restart_trigger",
    )


def test_c3_7_the_runbook_states_the_computed_status_not_a_wish() -> None:
    """The contract document must agree with what the code computes."""

    runbook = (
        _REPO_ROOT / "docs/runbooks/binance-spot-demo-mock-auto-limit.md"
    ).read_text(encoding="utf-8")
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    for gap in mal.spot_demo_recovery_contract_gaps(entry):
        assert f"`{gap}`" in runbook, f"unmet recovery item {gap} is undocumented"
    assert "spot_demo_recovery_contract_gaps" in runbook


def test_lane_evidence_port_is_required_at_construction(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    """A composition that cannot write its own evidence must not exist."""

    with pytest.raises(mal.SpotDemoLimitError) as error:
        mal.BinanceSpotDemoLimitComposition(
            client=client,
            claims=_claims(),
            connection_factory=lambda: None,
            persistence=None,
            dispatch_evidence=None,
            uncertainty_gate=None,
            lane_evidence=None,
        )
    assert error.value.reason is mal.SpotDemoLimitReason.LANE_EVIDENCE_PORT_UNAVAILABLE


def test_common_layer_owns_no_binance_retry_or_readback_queue() -> None:
    """The recovery owner is lane-native; J3A holds no Binance-specific queue."""

    coordination = (
        _REPO_ROOT / "app/services/mock_integration/coordination.py"
    ).read_text(encoding="utf-8")
    assert "binance" not in coordination.lower()


# ==========================================================================
# Upstream reuse — nothing is redefined
# ==========================================================================


def test_client_order_id_fits_the_binance_constraint() -> None:
    """The lane prefix keeps the J2B-derived id inside Binance's 36 chars."""

    from app.services.brokers.client_order_ids import (
        BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH,
        assert_broker_client_order_id,
    )

    envelope = _envelope()
    client_order_id = envelope.order_attempt.broker_client_order_id
    assert client_order_id is not None
    assert client_order_id.startswith(f"{mal.SPOT_DEMO_LANE_PREFIX}-")
    assert len(client_order_id) <= BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH
    assert_broker_client_order_id(
        target=BrokerClientIdTarget.BINANCE_SPOT_DEMO,
        client_order_id=client_order_id,
    )


def test_identifiers_come_only_from_the_j2b_factory() -> None:
    """No new lineage enum, hash domain, or identifier is minted here."""

    source = _MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("hashlib", "uuid", "mock-plan-v", "mock-intent-v", "sha256("):
        assert forbidden not in source


def test_plan_draft_binds_exactly_the_registry_identity() -> None:
    """The composed plan matches the signed row field for field."""

    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    plan = _envelope().execution_plan
    assert plan is not None
    assert plan.lane_id == entry.lane_id
    assert plan.broker == entry.broker
    assert plan.account_profile == entry.account_profile
    assert plan.account_mode == entry.account_mode.value
    assert plan.quote_currency == entry.quote_currency


def test_risk_caps_record_the_missing_binding_rather_than_a_number() -> None:
    """No cap is invented: the registry reports the cap binding as missing."""

    from app.services.mock_lane_registry import MissingBinding

    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert MissingBinding.CAP in entry.missing_bindings
    assert entry.max_order_notional is None
    plan = _envelope().execution_plan
    assert plan is not None
    assert plan.risk_caps == {"cap_binding": "missing"}


def test_no_module_level_side_effects() -> None:
    """Importing this module starts nothing: no clients, tasks, or connections."""

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    top_level_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert top_level_calls == []


def test_reason_codes_are_disjoint_from_the_coordination_vocabulary() -> None:
    """J6B reason codes never shadow J3A's, so an aggregate stays readable."""

    from app.services.mock_integration.coordination import COORDINATION_REASON_CODES

    overlap = mal.SPOT_DEMO_LIMIT_REASON_CODES & COORDINATION_REASON_CODES
    assert overlap == set()
