"""Unit tests for kr_preopen_news_brief_service (ROB-62)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.kr_preopen_news_brief_service import (
    READINESS_CONFIDENCE_CAP,
    build_brief,
)

_FORBIDDEN_CANDIDATE_KEYS = {
    "quantity",
    "price",
    "side",
    "order_type",
    "dry_run",
    "watch",
    "order_intent",
}


def _readiness(
    *,
    is_ready: bool = True,
    is_stale: bool = False,
    latest_run_uuid: str | None = "run-uuid",
    warnings: list[str] | None = None,
    source_counts: dict | None = None,
    max_age_minutes: int = 180,
) -> SimpleNamespace:
    return SimpleNamespace(
        is_ready=is_ready,
        is_stale=is_stale,
        latest_run_uuid=latest_run_uuid,
        warnings=warnings or [],
        source_counts=source_counts or {"browser_naver_mainnews": 10},
        max_age_minutes=max_age_minutes,
    )


def _candidate(**kwargs) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "symbol": "005930",
        "candidate_kind": "proposed",
        "side": "buy",
        "confidence": 75,
        "rationale": "Good momentum",
        "payload": {"name": "삼성전자", "sector": "반도체"},
        "warnings": [],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _run(
    *,
    candidates: list | None = None,
    advisory_links: list | None = None,
    run_id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        candidates=candidates if candidates is not None else [],
        advisory_links=advisory_links if advisory_links is not None else [],
    )


# --- Readiness: ok ---


@pytest.mark.unit
def test_readiness_ok_confidence_at_most_90():
    r = _readiness(is_ready=True, is_stale=False)
    brief = build_brief(readiness=r, research_run=None, base_confidence=70)

    assert brief.news_readiness == "ok"
    assert brief.confidence.overall <= READINESS_CONFIDENCE_CAP["ok"]
    assert brief.confidence.overall <= 90
    assert brief.advisory_only is True


@pytest.mark.unit
def test_readiness_ok_no_stale_risk_flag():
    r = _readiness(is_ready=True, is_stale=False)
    brief = build_brief(readiness=r, research_run=None)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "news_stale" not in flag_codes
    assert "news_unavailable" not in flag_codes


# --- Readiness: stale ---


@pytest.mark.unit
def test_readiness_stale_confidence_at_most_60():
    r = _readiness(is_ready=False, is_stale=True, warnings=["news_stale"])
    brief = build_brief(readiness=r, research_run=None, base_confidence=70)

    assert brief.news_readiness == "stale"
    assert brief.confidence.overall <= READINESS_CONFIDENCE_CAP["stale"]
    assert brief.confidence.overall <= 60


@pytest.mark.unit
def test_readiness_stale_news_stale_risk_flag_present():
    r = _readiness(is_ready=False, is_stale=True, warnings=["news_stale"])
    brief = build_brief(readiness=r, research_run=None)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "news_stale" in flag_codes
    severities = {f.code: f.severity for f in brief.risk_flags}
    assert severities["news_stale"] == "warn"


@pytest.mark.unit
def test_readiness_stale_per_flag_confidence_clamped():
    r = _readiness(is_ready=False, is_stale=True, warnings=["news_stale"])
    run = _run(candidates=[_candidate(confidence=90)])
    brief = build_brief(readiness=r, research_run=run, base_confidence=70)

    cap = READINESS_CONFIDENCE_CAP["stale"]
    for flag in brief.candidate_flags:
        assert flag.confidence <= cap, (
            f"candidate confidence {flag.confidence} exceeds cap {cap}"
        )


# --- Readiness: degraded ---


@pytest.mark.unit
def test_readiness_degraded_confidence_at_most_40():
    r = _readiness(is_ready=False, is_stale=False, warnings=["news_sources_empty"])
    brief = build_brief(readiness=r, research_run=None, base_confidence=70)

    assert brief.news_readiness == "degraded"
    assert brief.confidence.overall <= READINESS_CONFIDENCE_CAP["degraded"]
    assert brief.confidence.overall <= 40


@pytest.mark.unit
def test_readiness_degraded_ingestion_partial_flag_present():
    r = _readiness(is_ready=False, is_stale=False, warnings=["news_sources_empty"])
    brief = build_brief(readiness=r, research_run=None)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "ingestion_partial" in flag_codes


# --- Readiness: unavailable ---


@pytest.mark.unit
def test_readiness_unavailable_confidence_zero():
    r = _readiness(
        is_ready=False,
        is_stale=True,
        latest_run_uuid=None,
        warnings=["news_unavailable"],
    )
    brief = build_brief(readiness=r, research_run=None)

    assert brief.news_readiness == "unavailable"
    assert brief.confidence.overall == 0
    assert brief.confidence.cap_reason == "news_unavailable"


@pytest.mark.unit
def test_readiness_unavailable_empty_flag_lists():
    r = _readiness(
        is_ready=False,
        is_stale=True,
        latest_run_uuid=None,
        warnings=["news_unavailable"],
    )
    brief = build_brief(readiness=r, research_run=None)

    assert brief.sector_flags == []
    assert brief.candidate_flags == []


@pytest.mark.unit
def test_readiness_unavailable_news_unavailable_risk_flag():
    r = _readiness(is_ready=False, latest_run_uuid=None, warnings=["news_unavailable"])
    brief = build_brief(readiness=r, research_run=None)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "news_unavailable" in flag_codes


# --- TradingAgents evidence ---


@pytest.mark.unit
def test_tradingagents_absent_produces_info_flag():
    r = _readiness(is_ready=True, is_stale=False)
    brief = build_brief(readiness=r, research_run=None)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "tradingagents_unavailable" in flag_codes
    severities = {f.code: f.severity for f in brief.risk_flags}
    assert severities["tradingagents_unavailable"] == "info"


@pytest.mark.unit
def test_tradingagents_absent_no_exception():
    r = _readiness(is_ready=True, is_stale=False)
    brief = build_brief(readiness=r, research_run=None)
    # No exception; brief is valid
    assert brief.advisory_only is True


@pytest.mark.unit
def test_tradingagents_present_confidence_bumped_but_under_cap():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        advisory_links=[
            {
                "provider": "tradingagents",
                "advisory_only": True,
                "execution_allowed": False,
            }
        ]
    )
    brief_with = build_brief(readiness=r, research_run=run, base_confidence=70)

    run_no_ta = _run(advisory_links=[])
    brief_without = build_brief(readiness=r, research_run=run_no_ta, base_confidence=70)

    cap = READINESS_CONFIDENCE_CAP["ok"]
    assert brief_with.confidence.overall <= cap
    assert brief_without.confidence.overall <= cap
    # With TradingAgents the confidence should be >= without (bumped by +10)
    assert brief_with.confidence.overall >= brief_without.confidence.overall


@pytest.mark.unit
def test_tradingagents_no_flag_when_evidence_present():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        advisory_links=[
            {
                "provider": "tradingagents",
                "advisory_only": True,
                "execution_allowed": False,
            }
        ]
    )
    brief = build_brief(readiness=r, research_run=run)

    flag_codes = [f.code for f in brief.risk_flags]
    assert "tradingagents_unavailable" not in flag_codes


# --- CandidateImpactFlag forbidden key check ---


@pytest.mark.unit
def test_candidate_flags_contain_no_forbidden_execution_keys():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        candidates=[
            _candidate(symbol="005930", confidence=70),
            _candidate(id=2, symbol="000660", confidence=60, side="sell"),
        ]
    )
    brief = build_brief(readiness=r, research_run=run)

    for flag in brief.candidate_flags:
        flag_dict = flag.model_dump()
        violations = _FORBIDDEN_CANDIDATE_KEYS & set(flag_dict.keys())
        assert not violations, (
            f"CandidateImpactFlag for {flag.symbol} contains forbidden keys: {violations}"
        )


@pytest.mark.unit
def test_candidate_flags_advisory_only_invariant():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(candidates=[_candidate()])
    brief = build_brief(readiness=r, research_run=run)

    assert brief.advisory_only is True


# --- Candidate extraction ---


@pytest.mark.unit
def test_candidate_flags_mapped_from_proposed_only():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        candidates=[
            _candidate(symbol="005930", candidate_kind="proposed"),
            _candidate(id=2, symbol="000660", candidate_kind="other"),
            _candidate(id=3, symbol="035420", candidate_kind="holdings"),
        ]
    )
    brief = build_brief(readiness=r, research_run=run)

    symbols = {f.symbol for f in brief.candidate_flags}
    assert "005930" in symbols
    assert "000660" in symbols
    assert "035420" not in symbols, "holdings kind must be excluded"


@pytest.mark.unit
def test_candidate_buy_maps_to_positive_direction():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(candidates=[_candidate(side="buy")])
    brief = build_brief(readiness=r, research_run=run)

    assert brief.candidate_flags[0].direction == "positive"


@pytest.mark.unit
def test_candidate_sell_maps_to_negative_direction():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(candidates=[_candidate(side="sell")])
    brief = build_brief(readiness=r, research_run=run)

    assert brief.candidate_flags[0].direction == "negative"


@pytest.mark.unit
def test_candidate_count_capped_at_10():
    r = _readiness(is_ready=True, is_stale=False)
    candidates = [_candidate(id=i, symbol=f"{i:06d}") for i in range(15)]
    run = _run(candidates=candidates)
    brief = build_brief(readiness=r, research_run=run)

    assert len(brief.candidate_flags) <= 10


@pytest.mark.unit
def test_sector_flags_aggregate_candidate_sectors():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        candidates=[
            _candidate(
                symbol="005930",
                side="buy",
                payload={"name": "삼성전자", "sector": "반도체"},
            ),
            _candidate(
                id=2,
                symbol="000660",
                side="buy",
                payload={"name": "SK하이닉스", "sector": "반도체"},
            ),
        ]
    )
    brief = build_brief(readiness=r, research_run=run)

    assert brief.sector_flags
    assert brief.sector_flags[0].sector == "반도체"
    assert brief.sector_flags[0].direction == "positive"
    assert brief.sector_flags[0].confidence <= brief.confidence.overall


@pytest.mark.unit
def test_sector_flags_mixed_when_candidate_directions_conflict():
    r = _readiness(is_ready=True, is_stale=False)
    run = _run(
        candidates=[
            _candidate(
                symbol="005930",
                side="buy",
                payload={"name": "삼성전자", "sector": "반도체"},
            ),
            _candidate(
                id=2,
                symbol="000660",
                side="sell",
                payload={"name": "SK하이닉스", "sector": "반도체"},
            ),
        ]
    )
    brief = build_brief(readiness=r, research_run=run)

    assert brief.sector_flags[0].direction == "mixed"


# --- Cap table exhaustive parametrize ---


@pytest.mark.unit
@pytest.mark.parametrize(
    "readiness_status,warnings,is_ready,is_stale,latest_run_uuid,expected_cap",
    [
        ("ok", [], True, False, "run", READINESS_CONFIDENCE_CAP["ok"]),
        (
            "stale",
            ["news_stale"],
            False,
            True,
            "run",
            READINESS_CONFIDENCE_CAP["stale"],
        ),
        (
            "degraded",
            ["news_sources_empty"],
            False,
            False,
            "run",
            READINESS_CONFIDENCE_CAP["degraded"],
        ),
        (
            "unavailable",
            ["news_unavailable"],
            False,
            True,
            None,
            READINESS_CONFIDENCE_CAP["unavailable"],
        ),
    ],
)
def test_confidence_cap_per_readiness(
    readiness_status,
    warnings,
    is_ready,
    is_stale,
    latest_run_uuid,
    expected_cap,
):
    r = _readiness(
        is_ready=is_ready,
        is_stale=is_stale,
        latest_run_uuid=latest_run_uuid,
        warnings=warnings,
    )
    brief = build_brief(readiness=r, research_run=None, base_confidence=99)

    assert brief.news_readiness == readiness_status
    assert brief.confidence.overall <= expected_cap
