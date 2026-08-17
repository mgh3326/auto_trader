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
) -> mal.BinanceSpotDemoLimitComposition:
    return mal.BinanceSpotDemoLimitComposition(
        client=client,
        claims=_claims(),
        connection_factory=lambda: None,
        persistence=None,
        dispatch_evidence=None,
        uncertainty_gate=None,
        lane_evidence=lane_evidence or _RecordingLaneEvidence(),
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


def test_mutant_01b_conservative_domain_ignores_unknown_physical_ids() -> None:
    """§F-1 — the registry's own guard cannot fire while identity is UNKNOWN.

    This is exactly why J6B carries a stricter conservative guard: proving the
    weaker one silent is what makes the stronger one necessary rather than
    decorative.
    """

    from app.services.mock_lane_registry import assert_single_writer

    mutated = tuple(
        dataclasses.replace(entry, writer=True)
        if entry.broker == "binance" and entry.account_profile == "spot_demo"
        else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    assert all(
        entry.physical_account_id is None
        for entry in mutated
        if entry.broker == "binance"
    )
    # Silent: no known physical account to collide on.
    assert_single_writer(mutated)
    # Not silent:
    with pytest.raises(mal.SpotDemoLimitError):
        mal.assert_binance_single_writer_domain(mutated)


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
    assert entry.physical_account_id is None
    assert entry.identity_status == "UNKNOWN"
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


@pytest.mark.asyncio
async def test_c3_4d_cancel_writes_the_cancel_kind(
    client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    """C3-4 — the cancel kind is written by the cancel path."""

    evidence = _RecordingLaneEvidence()
    composition = _composition(client, lane_evidence=evidence)
    result = await composition.cancel_limit_order(
        symbol="BTCUSDT", client_order_id="bnsd-abc", confirm=False
    )
    assert evidence.kinds == ["cancel"]
    # confirm=False is the ROB-298 operator gate: a dry run, zero HTTP.
    assert type(result).__name__ == "SpotDemoDryRunResult"
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


def test_c3_7_lane_stays_lifecycle_blocked_until_activation_is_approved() -> None:
    """C3-7 — this job does not activate anything."""

    assert (
        mal.SPOT_DEMO_LIFECYCLE_BLOCKED_LANE_STATUS
        is LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE
    )
    entry = get_lane_registry_entry(mal.SPOT_DEMO_CANONICAL_LANE_ID)
    assert entry.activation_status is not ActivationStatus.ENABLED


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
