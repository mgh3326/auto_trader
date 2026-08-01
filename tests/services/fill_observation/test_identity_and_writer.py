from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.trading import InstrumentType
from app.services.fill_observation import writer as writer_module
from app.services.fill_observation.contracts import (
    BrokerFillEvidence,
    FillObservationWriteStatus,
    FillSettlementStatus,
)
from app.services.fill_observation.errors import (
    FillObservationIdentityConflict,
    InvalidFillEvidence,
    NonMonotonicFillCumulative,
)
from app.services.fill_observation.identity import (
    derive_fill_observation_identity,
    has_positive_fill,
    normalize_fill_evidence,
)
from app.services.fill_observation.writer import (
    FILL_OBSERVATION_WRITER_ENABLED_ENV,
    FillObservationWriter,
    _record_with_repository,
    fill_observation_writer_enabled,
)

pytestmark = pytest.mark.unit


def _evidence(**overrides: object) -> BrokerFillEvidence:
    values: dict[str, object] = {
        "broker": "toss",
        "account_ref": "acct-live-1",
        "account_mode": "live",
        "venue": "toss_us",
        "order_id": "order-42",
        "instrument_type": InstrumentType.equity_us,
        "symbol": "BRK-B",
        "side": "BUY",
        "currency": "usd",
        "evidence_source": "reconciler",
        "evidence_ref": "toss_live_order_ledger:42",
        "observed_at": datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        "cumulative_quantity": Decimal("2.5000"),
        "average_price": Decimal("430.25"),
        "filled_at": datetime(2026, 8, 1, 11, 59, tzinfo=UTC),
    }
    values.update(overrides)
    return BrokerFillEvidence(**values)  # type: ignore[arg-type]


class _FakeObservationRepository:
    def __init__(
        self,
        *,
        recorded: Decimal = Decimal(0),
        existing: object | None = None,
        settlements: list[SimpleNamespace] | None = None,
    ) -> None:
        self.recorded = recorded
        self.existing = existing
        self.lock_keys: list[int] = []
        self.appends: list[dict[str, object]] = []
        self.settlements: list[SimpleNamespace] = list(settlements or [])

    async def lock_order_scope(self, lock_key: int) -> None:
        self.lock_keys.append(lock_key)

    async def find_by_identity(self, _identity: str) -> object | None:
        return self.existing

    async def recorded_quantity(self, _evidence: object) -> Decimal:
        return self.recorded

    async def append(self, **kwargs: object) -> tuple[object, int]:
        self.appends.append(kwargs)
        return SimpleNamespace(id=91), len(kwargs["projection_names"])

    async def latest_settlement(
        self,
        fill_observation_id: int,
    ) -> SimpleNamespace | None:
        rows = [
            row
            for row in self.settlements
            if row.fill_observation_id == fill_observation_id
        ]
        return max(rows, key=lambda row: row.revision) if rows else None

    async def append_settlement(
        self,
        *,
        fill_observation_id: int,
        evidence: object,
        settlement: object,
        revision: int,
    ) -> SimpleNamespace:
        row = SimpleNamespace(
            fill_observation_id=fill_observation_id,
            revision=revision,
            settlement_hash=settlement.settlement_hash,  # type: ignore[attr-defined]
            evidence=evidence,
        )
        self.settlements.append(row)
        return row


class _FakeWriterSession:
    def __init__(self) -> None:
        self.transactions = 0

    async def __aenter__(self) -> _FakeWriterSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeWriterSession:
        self.transactions += 1
        return self


def test_identity_is_decimal_canonical_and_reobservation_stable() -> None:
    first = normalize_fill_evidence(_evidence())
    second = normalize_fill_evidence(
        _evidence(
            cumulative_quantity="2.5",
            evidence_ref="another-poll:99",
            observed_at=first.observed_at + timedelta(minutes=5),
        )
    )

    first_identity = derive_fill_observation_identity(first)
    second_identity = derive_fill_observation_identity(second)

    assert first.symbol == "BRK.B"
    assert first_identity.value == second_identity.value
    assert first_identity.fill_fact_hash == second_identity.fill_fact_hash
    assert first_identity.order_lock_key == second_identity.order_lock_key
    assert -(2**63) <= first_identity.order_lock_key < 2**63


def test_sequence_identity_takes_precedence_over_cumulative_quantity() -> None:
    first = normalize_fill_evidence(_evidence(broker_fill_sequence="fill-1"))
    second = normalize_fill_evidence(_evidence(broker_fill_sequence="fill-2"))

    first_identity = derive_fill_observation_identity(first)
    second_identity = derive_fill_observation_identity(second)

    assert first_identity.kind == "broker_fill_sequence"
    assert first_identity.value != second_identity.value
    assert first_identity.partition_key == second_identity.partition_key


def test_crypto_venue_symbol_is_not_rewritten_as_us_share_class() -> None:
    normalized = normalize_fill_evidence(
        _evidence(
            broker="upbit",
            venue="upbit",
            instrument_type=InstrumentType.crypto,
            symbol="KRW-BTC",
            currency="KRW",
        )
    )
    assert normalized.symbol == "KRW-BTC"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"evidence_ref": ""}, "evidence_ref"),
        (
            {"broker_fill_sequence": None, "cumulative_quantity": None},
            "broker_fill_sequence or cumulative_quantity",
        ),
        ({"cumulative_quantity": Decimal("-1")}, "must not be negative"),
        ({"average_price": Decimal("0")}, "greater than zero"),
        (
            {"observed_at": datetime(2026, 8, 1, 12, 0)},
            "timezone-aware",
        ),
    ],
)
def test_invalid_or_missing_broker_evidence_fails_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(InvalidFillEvidence, match=message):
        normalize_fill_evidence(_evidence(**overrides))


@pytest.mark.asyncio
async def test_default_off_writer_does_not_open_a_database_session() -> None:
    def forbidden_session() -> object:
        raise AssertionError("disabled writer must not construct a DB session")

    writer = FillObservationWriter(forbidden_session, enabled=False)  # type: ignore[arg-type]
    result = await writer.write(_evidence())

    assert result.status is FillObservationWriteStatus.WRITER_DISABLED
    assert result.fill_delta_quantity == 0
    assert result.observation_id is None


def test_writer_env_gate_is_default_false_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FILL_OBSERVATION_WRITER_ENABLED_ENV, raising=False)
    assert fill_observation_writer_enabled() is False

    monkeypatch.setenv(FILL_OBSERVATION_WRITER_ENABLED_ENV, "unexpected")
    assert fill_observation_writer_enabled() is False

    monkeypatch.setenv(FILL_OBSERVATION_WRITER_ENABLED_ENV, "true")
    assert fill_observation_writer_enabled() is True


@pytest.mark.asyncio
async def test_enabled_writer_owns_one_observation_and_outbox_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _FakeObservationRepository()
    session = _FakeWriterSession()
    monkeypatch.setattr(
        writer_module,
        "FillObservationRepository",
        lambda _session: repository,
    )
    writer = FillObservationWriter(
        lambda: session,  # type: ignore[arg-type]
        enabled=True,
    )

    result = await writer.write(_evidence())

    assert result.status is FillObservationWriteStatus.INSERTED
    assert result.outbox_count == 1
    assert session.transactions == 1
    assert len(repository.appends) == 1


@pytest.mark.asyncio
async def test_zero_cumulative_is_no_fill_and_opens_no_session() -> None:
    def forbidden_session() -> object:
        raise AssertionError("zero fill evidence must not construct a DB session")

    writer = FillObservationWriter(forbidden_session, enabled=True)  # type: ignore[arg-type]
    result = await writer.write(_evidence(cumulative_quantity=Decimal(0)))

    normalized = normalize_fill_evidence(_evidence(cumulative_quantity=Decimal(0)))
    assert has_positive_fill(normalized) is False
    assert result.status is FillObservationWriteStatus.NO_FILL_EVIDENCE
    assert result.fill_delta_quantity == 0


@pytest.mark.asyncio
async def test_progressive_cumulative_partial_preserves_only_the_increase() -> None:
    evidence = normalize_fill_evidence(_evidence(cumulative_quantity="5.5"))
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(recorded=Decimal("2.5"))

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.INSERTED
    assert result.fill_delta_quantity == Decimal("3.0")
    assert result.outbox_count == 1
    assert repository.appends[0]["fill_delta_quantity"] == Decimal("3.0")
    assert repository.lock_keys == [identity.order_lock_key]


@pytest.mark.asyncio
async def test_same_cumulative_reobservation_is_zero_delta() -> None:
    evidence = normalize_fill_evidence(
        _evidence(broker_fill_sequence="new-sequence", cumulative_quantity="2.5")
    )
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(recorded=Decimal("2.5"))

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.NO_DELTA
    assert result.fill_delta_quantity == 0
    assert repository.appends == []


@pytest.mark.asyncio
async def test_identical_identity_replay_is_zero_delta_and_zero_outbox() -> None:
    evidence = normalize_fill_evidence(_evidence())
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(
        existing=SimpleNamespace(id=17, fill_fact_hash=identity.fill_fact_hash),
        settlements=[
            SimpleNamespace(
                fill_observation_id=17,
                revision=1,
                settlement_hash=identity.settlement.settlement_hash,
            )
        ],
    )

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.DUPLICATE
    assert result.observation_id == 17
    assert result.fill_delta_quantity == 0
    assert result.outbox_count == 0
    assert result.settlement_status is FillSettlementStatus.UNCHANGED
    assert result.settlement_revision == 1
    assert repository.appends == []
    assert len(repository.settlements) == 1


@pytest.mark.asyncio
async def test_identity_payload_conflict_fails_closed_with_zero_write() -> None:
    evidence = normalize_fill_evidence(_evidence())
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(
        existing=SimpleNamespace(id=17, fill_fact_hash="f" * 64)
    )

    with pytest.raises(FillObservationIdentityConflict):
        await _record_with_repository(
            repository,  # type: ignore[arg-type]
            evidence=evidence,
            identity=identity,
            projection_names=("legacy_dual_read_validation.v1",),
        )

    assert repository.appends == []
    assert repository.settlements == []


@pytest.mark.asyncio
async def test_cumulative_regression_fails_closed_with_zero_write() -> None:
    evidence = normalize_fill_evidence(_evidence(cumulative_quantity="2"))
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(recorded=Decimal("3"))

    with pytest.raises(NonMonotonicFillCumulative):
        await _record_with_repository(
            repository,  # type: ignore[arg-type]
            evidence=evidence,
            identity=identity,
            projection_names=("legacy_dual_read_validation.v1",),
        )

    assert repository.appends == []


@pytest.mark.parametrize(
    "settlement_drift",
    [
        {"fee_total": Decimal("1.5")},
        {"average_price": Decimal("430.31")},
        {"last_fill_price": Decimal("430.40")},
        {"cumulative_notional": Decimal("1075.75")},
        {"filled_at": datetime(2026, 8, 1, 11, 59, 30, tzinfo=UTC)},
        {"symbol": "brk-b"},
    ],
)
def test_settlement_and_case_drift_keep_the_same_fill_fact(
    settlement_drift: dict[str, object],
) -> None:
    first = derive_fill_observation_identity(normalize_fill_evidence(_evidence()))
    second = derive_fill_observation_identity(
        normalize_fill_evidence(_evidence(**settlement_drift))
    )

    assert first.value == second.value
    assert first.fill_fact_hash == second.fill_fact_hash


def test_us_symbol_case_drift_normalizes_without_touching_other_instruments() -> None:
    assert normalize_fill_evidence(_evidence(symbol="brk-b")).symbol == "BRK.B"
    assert normalize_fill_evidence(_evidence(symbol="aapl")).symbol == "AAPL"

    crypto = normalize_fill_evidence(
        _evidence(
            broker="upbit",
            venue="upbit",
            instrument_type=InstrumentType.crypto,
            symbol="krw-btc",
            currency="KRW",
        )
    )
    assert crypto.symbol == "krw-btc"


@pytest.mark.parametrize(
    ("overrides", "changes_fill_fact"),
    [
        ({"side": "sell"}, True),
        ({"symbol": "AAPL"}, True),
        ({"currency": "krw"}, True),
        ({"instrument_type": InstrumentType.equity_kr}, True),
        ({"fee_total": Decimal("1.5")}, False),
        ({"evidence_source": "websocket"}, False),
    ],
)
def test_fill_fact_hash_covers_only_stable_broker_facts(
    overrides: dict[str, object],
    changes_fill_fact: bool,
) -> None:
    baseline = derive_fill_observation_identity(normalize_fill_evidence(_evidence()))
    variant = derive_fill_observation_identity(
        normalize_fill_evidence(_evidence(**overrides))
    )

    assert (baseline.fill_fact_hash != variant.fill_fact_hash) is changes_fill_fact


def test_sequence_identity_ignores_cumulative_growth_in_the_fill_fact() -> None:
    early = derive_fill_observation_identity(
        normalize_fill_evidence(
            _evidence(
                broker_fill_sequence="fill-1",
                cumulative_quantity="2.5",
                fill_quantity="2.5",
            )
        )
    )
    later = derive_fill_observation_identity(
        normalize_fill_evidence(
            _evidence(
                broker_fill_sequence="fill-1",
                cumulative_quantity="10",
                fill_quantity="2.5",
            )
        )
    )
    contradiction = derive_fill_observation_identity(
        normalize_fill_evidence(
            _evidence(
                broker_fill_sequence="fill-1",
                cumulative_quantity="10",
                fill_quantity="9",
            )
        )
    )

    assert early.value == later.value == contradiction.value
    assert early.fill_fact_hash == later.fill_fact_hash
    assert early.settlement.settlement_hash != later.settlement.settlement_hash
    # The sequence's own quantity is the fact it is keyed on, so a contradiction
    # there is still a conflict.
    assert contradiction.fill_fact_hash != early.fill_fact_hash


def test_cumulative_identity_ignores_per_poll_reported_increment() -> None:
    first = derive_fill_observation_identity(
        normalize_fill_evidence(_evidence(fill_quantity="2.5"))
    )
    second = derive_fill_observation_identity(
        normalize_fill_evidence(_evidence(fill_quantity=None))
    )

    assert first.value == second.value
    assert first.fill_fact_hash == second.fill_fact_hash
    assert first.settlement.settlement_hash != second.settlement.settlement_hash


@pytest.mark.asyncio
async def test_late_fee_settlement_is_a_revision_not_a_conflict() -> None:
    booked = normalize_fill_evidence(_evidence(fee_total=Decimal(0)))
    booked_identity = derive_fill_observation_identity(booked)
    repository = _FakeObservationRepository(
        existing=SimpleNamespace(id=17, fill_fact_hash=booked_identity.fill_fact_hash),
        settlements=[
            SimpleNamespace(
                fill_observation_id=17,
                revision=1,
                settlement_hash=booked_identity.settlement.settlement_hash,
            )
        ],
    )

    settled = normalize_fill_evidence(
        _evidence(fee_total=Decimal("1.5"), evidence_ref="another-poll:99")
    )
    settled_identity = derive_fill_observation_identity(settled)
    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=settled,
        identity=settled_identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.DUPLICATE
    assert result.fill_delta_quantity == 0
    assert result.outbox_count == 0
    assert result.settlement_status is FillSettlementStatus.RECORDED
    assert result.settlement_revision == 2
    assert repository.appends == []
    assert [row.revision for row in repository.settlements] == [1, 2]


@pytest.mark.asyncio
async def test_insert_records_the_first_settlement_revision() -> None:
    evidence = normalize_fill_evidence(_evidence())
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository()

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.INSERTED
    assert result.settlement_status is FillSettlementStatus.RECORDED
    assert result.settlement_revision == 1
    assert repository.settlements[0].fill_observation_id == 91


@pytest.mark.asyncio
async def test_fill_without_settlement_values_records_no_revision() -> None:
    evidence = normalize_fill_evidence(
        replace(
            _evidence(),
            broker_fill_sequence="fill-seq-11",
            cumulative_quantity=None,
            fill_quantity=None,
            average_price=None,
            filled_at=None,
        )
    )
    assert has_positive_fill(evidence) is False

    with_quantity = normalize_fill_evidence(
        replace(
            _evidence(),
            broker_fill_sequence="fill-seq-11",
            cumulative_quantity=None,
            fill_quantity=Decimal("3"),
            average_price=None,
            filled_at=None,
        )
    )
    identity = derive_fill_observation_identity(with_quantity)
    assert identity.settlement.has_values is True


@pytest.mark.asyncio
async def test_no_delta_reobservation_records_no_settlement_revision() -> None:
    evidence = normalize_fill_evidence(
        _evidence(broker_fill_sequence="new-sequence", cumulative_quantity="2.5")
    )
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(recorded=Decimal("2.5"))

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.NO_DELTA
    assert result.settlement_status is FillSettlementStatus.NOT_APPLICABLE
    assert result.settlement_revision is None
    assert repository.settlements == []


@pytest.mark.asyncio
async def test_sequence_only_fill_uses_reported_positive_quantity() -> None:
    evidence = normalize_fill_evidence(
        replace(
            _evidence(),
            broker_fill_sequence="fill-seq-7",
            cumulative_quantity=None,
            fill_quantity=Decimal("0.125"),
        )
    )
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository()

    result = await _record_with_repository(
        repository,  # type: ignore[arg-type]
        evidence=evidence,
        identity=identity,
        projection_names=("legacy_dual_read_validation.v1",),
    )

    assert result.status is FillObservationWriteStatus.INSERTED
    assert result.fill_delta_quantity == Decimal("0.125")
