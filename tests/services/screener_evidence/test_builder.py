from app.services.screener_evidence.models import CandidateEvidence


def test_candidate_evidence_to_payload_dict_round_trips():
    ev = CandidateEvidence(
        symbol="KRW-BTC",
        market="crypto",
        name="비트코인",
        score=8.4,
        score_label="+4.20%",
        change_rate=4.2,
        price=95_000_000.0,
        volume_value=123_456_000_000.0,
        reasons=["단기 상승 모멘텀 후보"],
        source="tvscreener_upbit",
        risk_flags=[],
    )
    payload = ev.to_payload_dict()
    assert payload == {
        "symbol": "KRW-BTC",
        "market": "crypto",
        "name": "비트코인",
        "score": 8.4,
        "score_label": "+4.20%",
        "change_rate": 4.2,
        "price": 95_000_000.0,
        "volume_value": 123_456_000_000.0,
        "reasons": ["단기 상승 모멘텀 후보"],
        "source": "tvscreener_upbit",
        "risk_flags": [],
    }
