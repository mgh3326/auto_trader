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
    ) -> None:
        self.recorded = recorded
        self.existing = existing
        self.lock_keys: list[int] = []
        self.appends: list[dict[str, object]] = []

    async def lock_order_scope(self, lock_key: int) -> None:
        self.lock_keys.append(lock_key)

    async def find_by_identity(self, _identity: str) -> object | None:
        return self.existing

    async def recorded_quantity(self, _evidence: object) -> Decimal:
        return self.recorded

    async def append(self, **kwargs: object) -> tuple[object, int]:
        self.appends.append(kwargs)
        return SimpleNamespace(id=91), len(kwargs["projection_names"])


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
    assert first_identity.evidence_hash == second_identity.evidence_hash
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
        existing=SimpleNamespace(id=17, evidence_hash=identity.evidence_hash)
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
    assert repository.appends == []


@pytest.mark.asyncio
async def test_identity_payload_conflict_fails_closed_with_zero_write() -> None:
    evidence = normalize_fill_evidence(_evidence())
    identity = derive_fill_observation_identity(evidence)
    repository = _FakeObservationRepository(
        existing=SimpleNamespace(id=17, evidence_hash="f" * 64)
    )

    with pytest.raises(FillObservationIdentityConflict):
        await _record_with_repository(
            repository,  # type: ignore[arg-type]
            evidence=evidence,
            identity=identity,
            projection_names=("legacy_dual_read_validation.v1",),
        )

    assert repository.appends == []


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
