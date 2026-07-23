"""ROB-1038 R2 security regressions for forecast resolution semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.daily_candles.provenance import (
    DAILY_SOURCE_CONTRACTS,
    with_equity_provenance,
)
from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_TOUCH_RULE = "window-touch-v1-high-gte-low-lte"
_TERMINAL_RULE = "terminal-close-v1-up-gte-down-lt"
_EVIDENCE_HASH = "a" * 64
_SERVICE_ACTOR = svc.AuthenticatedForecastActor(
    principal="service:forecast-review",
    authentication_method="service_identity",
)
_RAW_SAVE_FORECAST = svc.save_forecast


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    async def authenticated_save(*args, **kwargs):
        kwargs.setdefault("authenticated_actor", _SERVICE_ACTOR)
        return await _RAW_SAVE_FORECAST(*args, **kwargs)

    monkeypatch.setattr(svc, "save_forecast", authenticated_save)
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _touch_target(*, versioned: bool = True) -> dict:
    target = {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": 130.0,
    }
    if versioned:
        target["outcome_rule_version"] = _TOUCH_RULE
    return target


def _terminal_target(
    *,
    direction: str = "up",
    target_price: float = 130.0,
    adjustment_policy: str = "explicit-factor-v1",
    factor: float = 1.0,
    action_type: str = "none",
    action_ratio: float = 1.0,
    source_price_basis: str = "provider_adjusted",
    authority_type: str = "licensed_data_vendor",
    authority_id: str = "KIS",
    symbol: str = "SMCI",
    review_date: str = "2026-06-05",
) -> dict:
    target = {
        "kind": "terminal_close",
        "direction": direction,
        "target_price": target_price,
        "outcome_rule_version": _TERMINAL_RULE,
        "price_adjustment_policy": adjustment_policy,
    }
    if adjustment_policy == "unverified_fail_closed":
        return target
    target.update(
        {
            "target_to_close_factor": factor,
            "adjustment_provenance": {
                "contract_version": "corporate-action-adjustment-v1",
                "authority_type": authority_type,
                "authority_id": authority_id,
                "actor_principal": "service:forecast-review",
                "authentication_method": "service_identity",
                "symbol": symbol,
                "action_type": action_type,
                "action_ratio": action_ratio,
                "effective_date": review_date,
                "verified_through_date": review_date,
                "source": "KIS corporate-action feed",
                "source_ref": f"artifact://corporate-actions/{symbol}/{review_date}",
                "source_sha256": _EVIDENCE_HASH,
                "source_price_basis": source_price_basis,
            },
        },
    )
    return target


def _semantics_evidence(
    target: dict,
    *,
    contract_version: str,
) -> dict:
    return {
        "contract_version": contract_version,
        "authority_type": "service",
        "actor_principal": _SERVICE_ACTOR.principal,
        "authentication_method": _SERVICE_ACTOR.authentication_method,
        "source_target_sha256": svc._canonical_hash(target),
        "evidence_sha256": "b" * 64,
        "evidence_ref": "artifact://forecast-semantics/rob-1038",
        "reason": "operator verified the original natural-language event",
        "attested_at": "2026-07-23T15:30:00+09:00",
    }


def _terminal_candle(
    *,
    source: str = "kis",
    close: float = 129.0,
    candle_date: date = date(2026, 6, 5),
    is_final: bool = True,
    ingested_at: datetime | None = datetime(2026, 6, 5, 23, tzinfo=UTC),
) -> DailyCandleRow:
    row = DailyCandleRow(
        time_utc=datetime.combine(candle_date, datetime.min.time(), tzinfo=UTC),
        symbol="SMCI",
        partition="NASD",
        open=close,
        high=close + 10,
        low=close - 10,
        close=close,
        adj_close=None,
        volume=1000.0,
        value=close * 1000.0,
        source=source,
    )
    row = with_equity_provenance(
        row,
        final_through_date=candle_date if is_final else None,
    )
    return replace(row, ingested_at=ingested_at)


async def _insert_legacy_touch(
    db_session: AsyncSession,
    *,
    review_date: date = date(2020, 1, 1),
) -> TradeForecast:
    row = TradeForecast(
        created_by="legacy-writer",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_touch_target(versioned=False),
        probability=0.6,
        review_date=review_date,
        status="open",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_new_touch_requires_explicit_rule_version(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError, match="outcome_rule_version"):
        await svc.save_forecast(
            db_session,
            created_by="new-writer",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=_touch_target(versioned=False),
            probability=0.6,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_explicit_factor_rejects_self_attested_actor_without_trusted_boundary(
    db_session: AsyncSession,
):
    with pytest.raises(
        svc.ForecastValidationError,
        match="requires an authenticated forecast evidence actor",
    ):
        await _RAW_SAVE_FORECAST(
            db_session,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=_terminal_target(),
            probability=0.48,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_explicit_factor_rejects_actor_mismatch_with_trusted_boundary(
    db_session: AsyncSession,
):
    with pytest.raises(
        svc.ForecastValidationError,
        match="does not match authenticated actor",
    ):
        await _RAW_SAVE_FORECAST(
            db_session,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=_terminal_target(),
            probability=0.48,
            review_date="2026-06-05",
            authenticated_actor=svc.AuthenticatedForecastActor(
                principal="service:different-reviewer",
                authentication_method="service_identity",
            ),
        )


@pytest.mark.asyncio
async def test_legacy_versionless_touch_single_id_quarantines_before_candle_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    row = await _insert_legacy_touch(db_session)

    async def forbidden_read(*_args, **_kwargs):
        raise AssertionError("legacy quarantine must happen before candle lookup")

    monkeypatch.setattr(svc, "_read_window_candles", forbidden_read)
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_legacy_price_target"
    assert result["changed"] is False
    assert result["resolution_evidence"]["target_kind"] == "price_target"
    assert result["resolution_evidence"]["outcome_rule_version"] is None


@pytest.mark.asyncio
async def test_quarantine_does_not_consume_general_due_limit(db_session: AsyncSession):
    legacy = await _insert_legacy_touch(db_session)
    _, eligible = await svc.save_forecast(
        db_session,
        created_by="typed-writer",
        symbol="AAPL",
        instrument_type="equity_us",
        forecast_target=_touch_target(),
        probability=0.6,
        review_date="2020-01-02",
    )
    await db_session.commit()

    due = await svc.list_due_forecasts(
        db_session, now=datetime(2026, 6, 8, tzinfo=UTC), limit=1
    )
    quarantined = await svc.list_due_quarantined_forecasts(
        db_session, now=datetime(2026, 6, 8, tzinfo=UTC), limit=1
    )

    assert [row.forecast_id for row in due] == [eligible.forecast_id]
    assert [row.forecast_id for row in quarantined] == [legacy.forecast_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        {"kind": "thesis_holds"},
        _touch_target(),
    ],
)
async def test_terminal_same_id_cannot_change_kind(
    db_session: AsyncSession, replacement: dict
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        probability_range_low=0.4,
        probability_range_high=0.6,
        review_date="2026-06-05",
        session_label="origin-session",
        model_label="origin-model",
        evidence_ids=["artifact:origin"],
    )
    await db_session.commit()

    with pytest.raises(svc.ForecastValidationError, match="immutable"):
        await svc.save_forecast(
            db_session,
            forecast_id=row.forecast_id,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=replacement,
            probability=1.0,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_terminal_exact_replay_preserves_omitted_optional_provenance(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
        session_label="origin-session",
        model_label="origin-model",
        evidence_ids=["artifact:origin"],
        contrary_evidence="origin cutoff",
    )
    await db_session.commit()

    action, replayed = await svc.save_forecast(
        db_session,
        forecast_id=row.forecast_id,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    assert action == "unchanged"
    assert replayed.session_label == "origin-session"
    assert replayed.model_label == "origin-model"
    assert replayed.evidence_ids == ["artifact:origin"]
    assert replayed.contrary_evidence == "origin cutoff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_by", "post-review-author"),
        ("symbol", "AAPL"),
        ("instrument_type", "equity_kr"),
        ("direction", "down"),
        ("target_price", 131.0),
        ("probability", 0.49),
        ("probability_range_low", 0.41),
        ("forecast_start_date", "2026-06-02"),
        ("review_date", "2026-06-08"),
        ("horizon", "D+7"),
        ("model_label", "post-review-model"),
        ("policy_version", "post-review-policy"),
        ("artifact_uuid", "artifact:post-review"),
        ("evidence_ids", ["artifact:post-review"]),
        ("contrary_evidence", "post-review cutoff"),
    ],
)
async def test_terminal_immutable_claim_freezes_all_identity_dimensions(
    db_session: AsyncSession,
    field: str,
    value,
):
    initial = {
        "created_by": "claude",
        "symbol": "SMCI",
        "instrument_type": "equity_us",
        "forecast_target": _terminal_target(),
        "probability": 0.48,
        "probability_range_low": 0.4,
        "probability_range_high": 0.6,
        "forecast_start_date": "2026-06-01",
        "review_date": "2026-06-05",
        "horizon": "D+5",
        "model_label": "origin-model",
        "policy_version": "origin-policy",
        "artifact_uuid": "artifact:origin",
        "evidence_ids": ["artifact:origin"],
        "contrary_evidence": "origin cutoff",
    }
    _, row = await svc.save_forecast(db_session, **initial)
    await db_session.commit()

    mutation = {**initial, "forecast_id": row.forecast_id}
    if field in {"direction", "target_price"}:
        mutation["forecast_target"] = {
            **initial["forecast_target"],
            field: value,
        }
    else:
        mutation[field] = value
    if field == "symbol":
        mutation["forecast_target"] = _terminal_target(symbol=str(value))
    if field == "review_date":
        mutation["forecast_target"] = _terminal_target(review_date=str(value))

    with pytest.raises(svc.ForecastValidationError, match="immutable"):
        await svc.save_forecast(db_session, **mutation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        {"kind": "thesis_holds"},
        _touch_target(),
    ],
)
async def test_terminal_original_semantics_blocks_direct_mutation_manual_bypass(
    db_session: AsyncSession, replacement: dict
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    row.forecast_target = replacement
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=True,
        manual_outcome=True,
        manual_evidence=["post-review rewrite"],
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_claim_integrity"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.brier_score is None


def test_terminal_classifier_rejects_candle_without_finality_provenance():
    candle = DailyCandleRow(
        time_utc=datetime(2026, 6, 5, tzinfo=UTC),
        symbol="SMCI",
        partition="NASD",
        open=129.0,
        high=135.0,
        low=125.0,
        close=129.0,
        adj_close=None,
        volume=1000.0,
        value=129000.0,
        source="kis",
    )

    with pytest.raises(
        svc.TerminalCloseDataError, match="final regular-session provenance"
    ):
        svc.classify_terminal_close_outcome(
            [candle],
            review_date=date(2026, 6, 5),
            direction="up",
            target_price=130.0,
        )


@pytest.mark.asyncio
async def test_resolve_side_malformed_terminal_target_is_row_local_fail_closed(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    malformed = _terminal_target()
    malformed["outcome_rule_version"] = "unknown-version"
    row.forecast_target = malformed
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_invalid_target"
    assert result["changed"] is False
    assert "outcome_rule_version" in result["reason"]


@pytest.mark.asyncio
async def test_resolve_rejects_missing_durable_adjustment_authentication(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()
    row.semantics_evidence = {
        "contract_version": "terminal-adjustment-evidence-v1",
        "transition": "initial-explicit-factor-v1",
        "target_version": 1,
    }
    await db_session.commit()

    async def forbidden_read(*_args, **_kwargs):
        raise AssertionError("untrusted factor must fail before candle lookup")

    monkeypatch.setattr(svc, "_read_window_candles", forbidden_read)
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["status"] == "quarantined_untrusted_adjustment_evidence"
    assert result["changed"] is False
    assert "authentication binding is missing" in result["reason"]


@pytest.mark.asyncio
async def test_legacy_touch_attestation_activates_exact_original_claim(
    db_session: AsyncSession,
):
    legacy = await _insert_legacy_touch(db_session)
    legacy_target = dict(legacy.forecast_target)
    attested_target = {**legacy_target, "outcome_rule_version": _TOUCH_RULE}

    action, attested = await svc.save_forecast(
        db_session,
        forecast_id=legacy.forecast_id,
        created_by=legacy.created_by,
        symbol=legacy.symbol,
        instrument_type="equity_us",
        forecast_target=attested_target,
        probability=float(legacy.probability),
        review_date=legacy.review_date,
        expected_target_version=0,
        semantics_attestation=_semantics_evidence(
            legacy_target,
            contract_version="forecast-semantics-attestation-v1",
        ),
    )
    await db_session.commit()

    assert action == "updated"
    assert attested.target_version == 1
    assert attested.resolution_semantics_status == "active"
    assert attested.immutable_claim_hash == svc._canonical_hash(
        attested.immutable_claim
    )
    assert attested.semantics_evidence["decision"] == "window_touch"
    due = await svc.list_due_forecasts(db_session, now=datetime(2026, 6, 8, tzinfo=UTC))
    assert [row.forecast_id for row in due] == [legacy.forecast_id]


@pytest.mark.asyncio
async def test_legacy_touch_attestation_rejects_self_attested_actor(
    db_session: AsyncSession,
):
    legacy = await _insert_legacy_touch(db_session)
    legacy_target = dict(legacy.forecast_target)

    with pytest.raises(
        svc.ForecastValidationError,
        match="requires an authenticated forecast evidence actor",
    ):
        await _RAW_SAVE_FORECAST(
            db_session,
            forecast_id=legacy.forecast_id,
            created_by=legacy.created_by,
            symbol=legacy.symbol,
            instrument_type="equity_us",
            forecast_target={
                **legacy_target,
                "outcome_rule_version": _TOUCH_RULE,
            },
            probability=float(legacy.probability),
            review_date=legacy.review_date,
            expected_target_version=0,
            semantics_attestation=_semantics_evidence(
                legacy_target,
                contract_version="forecast-semantics-attestation-v1",
            ),
        )


@pytest.mark.asyncio
async def test_attested_touch_with_missing_authentication_binding_requarantines(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    legacy = await _insert_legacy_touch(db_session)
    legacy_target = dict(legacy.forecast_target)
    _, attested = await svc.save_forecast(
        db_session,
        forecast_id=legacy.forecast_id,
        created_by=legacy.created_by,
        symbol=legacy.symbol,
        instrument_type="equity_us",
        forecast_target={
            **legacy_target,
            "outcome_rule_version": _TOUCH_RULE,
        },
        probability=float(legacy.probability),
        review_date=legacy.review_date,
        expected_target_version=0,
        semantics_attestation=_semantics_evidence(
            legacy_target,
            contract_version="forecast-semantics-attestation-v1",
        ),
    )
    await db_session.commit()
    attested.semantics_evidence = {
        key: value
        for key, value in attested.semantics_evidence.items()
        if key != "authentication_binding"
    }
    await db_session.commit()

    async def forbidden_read(*_args, **_kwargs):
        raise AssertionError("untrusted attestation must fail before candle lookup")

    monkeypatch.setattr(svc, "_read_window_candles", forbidden_read)
    due = await svc.list_due_forecasts(
        db_session,
        now=datetime(2026, 6, 8, tzinfo=UTC),
    )
    quarantined = await svc.list_due_quarantined_forecasts(
        db_session,
        now=datetime(2026, 6, 8, tzinfo=UTC),
    )
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=attested.forecast_id,
        persist=False,
        backfill_missing=False,
    )

    assert due == []
    assert [row.forecast_id for row in quarantined] == [attested.forecast_id]
    assert result["status"] == "quarantined_untrusted_semantics_evidence"
    assert "authentication binding is missing" in result["reason"]


@pytest.mark.asyncio
async def test_legacy_terminal_supersession_records_bidirectional_evidence(
    db_session: AsyncSession,
):
    legacy = await _insert_legacy_touch(
        db_session,
        review_date=date(2026, 6, 5),
    )
    legacy_target = dict(legacy.forecast_target)

    action, terminal = await svc.save_forecast(
        db_session,
        created_by=legacy.created_by,
        symbol=legacy.symbol,
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=float(legacy.probability),
        review_date=legacy.review_date,
        supersedes_forecast_id=legacy.forecast_id,
        supersession_evidence=_semantics_evidence(
            legacy_target,
            contract_version="forecast-semantics-supersession-v1",
        ),
    )
    await db_session.commit()
    await db_session.refresh(legacy)

    assert action == "created"
    assert terminal.supersedes_forecast_id == legacy.forecast_id
    assert legacy.superseded_by_forecast_id == terminal.forecast_id
    assert legacy.resolution_semantics_status == "superseded"
    assert terminal.semantics_evidence == legacy.semantics_evidence
    assert terminal.semantics_evidence["from_forecast_id"] == str(legacy.forecast_id)
    assert terminal.semantics_evidence["to_forecast_id"] == str(terminal.forecast_id)
    assert (
        await svc.list_due_quarantined_forecasts(
            db_session, now=datetime(2026, 6, 8, tzinfo=UTC)
        )
        == []
    )


@pytest.mark.asyncio
async def test_terminal_adjustment_promotion_requires_version_cas_and_preserves_origin(
    db_session: AsyncSession,
):
    _, preregistered = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(adjustment_policy="unverified_fail_closed"),
        probability=0.48,
        review_date="2026-06-05",
        model_label="origin-model",
        evidence_ids=["artifact:origin"],
    )
    await db_session.commit()

    with pytest.raises(svc.ForecastValidationError, match="CAS mismatch"):
        await svc.save_forecast(
            db_session,
            forecast_id=preregistered.forecast_id,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=_terminal_target(),
            probability=0.48,
            review_date="2026-06-05",
            expected_target_version=2,
        )
    await db_session.rollback()
    await db_session.refresh(preregistered)
    assert preregistered.target_version == 1
    assert preregistered.forecast_target["price_adjustment_policy"] == (
        "unverified_fail_closed"
    )

    action, promoted = await svc.save_forecast(
        db_session,
        forecast_id=preregistered.forecast_id,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
        expected_target_version=1,
    )
    await db_session.commit()

    assert action == "updated"
    assert promoted.target_version == 2
    assert promoted.model_label == "origin-model"
    assert promoted.evidence_ids == ["artifact:origin"]
    assert promoted.semantics_evidence["transition"] == (
        "unverified_fail_closed->explicit-factor-v1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "error_fragment"),
    [
        (
            _terminal_target(
                factor=0.5,
                action_type="split",
                action_ratio=2.0,
            ),
            None,
        ),
        (
            _terminal_target(
                factor=10.0,
                action_type="reverse_split",
                action_ratio=0.1,
            ),
            None,
        ),
        (
            _terminal_target(
                factor=2.0,
                action_type="basis_change",
                action_ratio=0.5,
            ),
            None,
        ),
        (
            _terminal_target(authority_id="UNTRUSTED"),
            "allowlisted authority",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "symbol": "AAPL",
                },
            },
            "must match forecast symbol",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "source_sha256": "not-a-sha256",
                },
            },
            "64 lowercase hex",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "effective_date": "2026-06-06",
                },
            },
            "on or before review_date",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "source_price_basis": "unknown",
                },
            },
            "source_price_basis",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "authentication_method": "signed_artifact",
                },
            },
            "authentication_method is invalid",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": {
                    **_terminal_target()["adjustment_provenance"],
                    "untyped_note": "do not trust free-form provenance",
                },
            },
            "fields must exactly match",
        ),
        (
            {
                **_terminal_target(),
                "adjustment_provenance": None,
            },
            "adjustment_provenance",
        ),
    ],
)
async def test_corporate_action_contract_validates_ratio_authority_and_no_action(
    db_session: AsyncSession,
    target: dict,
    error_fragment: str | None,
):
    kwargs = {
        "created_by": "claude",
        "symbol": "SMCI",
        "instrument_type": "equity_us",
        "forecast_target": target,
        "probability": 0.48,
        "review_date": "2026-06-05",
    }
    if error_fragment is None:
        _, row = await svc.save_forecast(db_session, **kwargs)
        assert row.forecast_target == target
        await db_session.rollback()
    else:
        with pytest.raises(svc.ForecastValidationError, match=error_fragment):
            await svc.save_forecast(db_session, **kwargs)


@pytest.mark.parametrize("source", sorted(DAILY_SOURCE_CONTRACTS))
def test_all_five_writer_sources_require_exact_final_provenance(source: str):
    candle = _terminal_candle(source=source)
    outcome, observed, selected = svc.classify_terminal_close_outcome(
        [candle],
        review_date=date(2026, 6, 5),
        direction="up",
        target_price=130.0,
    )

    contract = DAILY_SOURCE_CONTRACTS[source]
    assert outcome is False
    assert observed == pytest.approx(129.0)
    assert selected.source_row_version == contract.source_row_version
    assert selected.price_basis == contract.price_basis


@pytest.mark.asyncio
async def test_terminal_resolve_fails_closed_on_corporate_action_basis_mismatch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(source_price_basis="raw"),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    async def read_kis(*_args, **_kwargs):
        return [_terminal_candle(source="kis")]

    monkeypatch.setattr(svc, "_read_window_candles", read_kis)
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["status"] == "unresolved_adjustment_basis_mismatch"
    assert result["changed"] is False
    assert result["resolution_evidence"]["evidence_source_price_basis"] == "raw"
    assert (
        result["resolution_evidence"]["candle_source_price_basis"]
        == "provider_adjusted"
    )


def test_terminal_session_gate_handles_weekend_and_us_holiday():
    weekend = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 6, 6),
        now=datetime(2026, 6, 8, 22, tzinfo=UTC),
    )
    holiday = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 7, 3),
        now=datetime(2026, 7, 6, 22, tzinfo=UTC),
    )

    assert "not a XNYS regular session" in weekend
    assert "not a XNYS regular session" in holiday


def test_terminal_session_gate_handles_us_early_close_and_kr_cutoff():
    early_close_date = date(2026, 11, 27)
    assert svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=early_close_date,
        now=datetime(2026, 11, 27, 17, 59, tzinfo=UTC),
    )
    assert (
        svc._terminal_close_session_failure(
            instrument_type="equity_us",
            review_date=early_close_date,
            now=datetime(2026, 11, 27, 18, 1, tzinfo=UTC),
        )
        is None
    )

    kr_date = date(2026, 6, 5)
    assert svc._terminal_close_session_failure(
        instrument_type="equity_kr",
        review_date=kr_date,
        now=datetime(2026, 6, 5, 6, 34, tzinfo=UTC),
    )
    assert (
        svc._terminal_close_session_failure(
            instrument_type="equity_kr",
            review_date=kr_date,
            now=datetime(2026, 6, 5, 6, 35, tzinfo=UTC),
        )
        is None
    )


def test_terminal_candle_finality_gate_tracks_us_dst_close():
    before_dst = _terminal_candle(
        candle_date=date(2026, 3, 6),
        ingested_at=datetime(2026, 3, 6, 20, 30, tzinfo=UTC),
    )
    after_dst = _terminal_candle(
        candle_date=date(2026, 3, 9),
        ingested_at=datetime(2026, 3, 9, 20, 30, tzinfo=UTC),
    )

    assert svc._terminal_candle_finality_failure(
        before_dst,
        instrument_type="equity_us",
        review_date=date(2026, 3, 6),
    )
    assert (
        svc._terminal_candle_finality_failure(
            after_dst,
            instrument_type="equity_us",
            review_date=date(2026, 3, 9),
        )
        is None
    )


def test_terminal_candle_finality_requires_us_ingestion_after_early_close():
    review_date = date(2026, 11, 27)
    at_close = _terminal_candle(
        candle_date=review_date,
        ingested_at=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
    )
    after_close = _terminal_candle(
        candle_date=review_date,
        ingested_at=datetime(2026, 11, 27, 18, 0, 1, tzinfo=UTC),
    )

    assert svc._terminal_candle_finality_failure(
        at_close,
        instrument_type="equity_us",
        review_date=review_date,
    )
    assert (
        svc._terminal_candle_finality_failure(
            after_close,
            instrument_type="equity_us",
            review_date=review_date,
        )
        is None
    )


@pytest.mark.asyncio
async def test_preview_persist_candle_correction_fails_cas(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    current = [_terminal_candle(close=129.0)]

    async def read_current(*_args, **_kwargs):
        return current

    monkeypatch.setattr(svc, "_read_window_candles", read_current)
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    preview = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )
    contract = preview["resolution_contract"]
    assert contract["target_kind"] == "terminal_close"
    assert contract["outcome_rule_version"] == _TERMINAL_RULE
    assert len(contract["evidence_fingerprint"]) == 64
    assert preview["computed"]["resolution_detail"]["resolution_contract"] == contract

    current[:] = [_terminal_candle(close=131.0)]
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=True,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
        expected_target_version=contract["target_version"],
        expected_claim_hash=contract["immutable_claim_hash"],
        expected_resolution_fingerprint=contract["resolution_fingerprint"],
    )

    assert result["status"] == "resolution_cas_mismatch"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.outcome is None


@pytest.mark.asyncio
async def test_preview_persist_target_version_race_fails_cas(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    async def read_current(*_args, **_kwargs):
        return [_terminal_candle(close=131.0)]

    monkeypatch.setattr(svc, "_read_window_candles", read_current)
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_touch_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()
    preview = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
    )
    contract = preview["resolution_contract"]

    _, updated = await svc.save_forecast(
        db_session,
        forecast_id=row.forecast_id,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_touch_target(),
        probability=0.48,
        review_date="2026-06-05",
        contrary_evidence="new evidence arrived after preview",
    )
    await db_session.commit()
    assert updated.target_version == contract["target_version"] + 1

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=True,
        backfill_missing=False,
        expected_target_version=contract["target_version"],
        expected_claim_hash=contract["immutable_claim_hash"],
        expected_resolution_fingerprint=contract["resolution_fingerprint"],
    )
    assert result["status"] == "resolution_cas_mismatch"
    assert result["changed"] is False
