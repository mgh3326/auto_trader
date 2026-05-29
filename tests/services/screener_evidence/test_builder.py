from app.services.screener_evidence import build_candidate_evidence
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
        source_preset="crypto_momentum",
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
        "source_preset": "crypto_momentum",
    }


def _crypto_row(symbol, name, change_rate, rsi, trade_amount, *, warning=False):
    return {
        "symbol": symbol,
        "name": name,
        "source": "tvscreener_upbit",
        "change_rate": change_rate,
        "price": 100.0,
        "rsi": rsi,
        "trade_amount_24h": trade_amount,
        "market_warning": warning,
    }


def test_builder_crypto_momentum_scores_and_sorts_desc():
    rows = [
        _crypto_row("KRW-AAA", "에이", 2.0, 55.0, 10.0),
        _crypto_row("KRW-BBB", "비이", 8.0, 60.0, 20.0),
    ]
    out = build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=rows)
    assert [e.symbol for e in out] == ["KRW-BBB", "KRW-AAA"]  # higher change first
    top = out[0]
    assert top.score == 9.0  # clamp(5 + 8/2)
    assert top.score_label == "+8.00%"
    assert top.reasons == ["단기 상승 모멘텀 후보"]
    assert top.source == "tvscreener_upbit"
    assert top.market == "crypto"


def test_builder_crypto_oversold_uses_rsi_label_and_reason():
    rows = [_crypto_row("KRW-CCC", "씨이", -1.0, 28.0, 5.0)]
    out = build_candidate_evidence(market="crypto", preset="crypto_oversold", rows=rows)
    assert out[0].score_label == "RSI 28.0"
    assert out[0].reasons == ["RSI 저점권 후보"]
    assert out[0].score == 9.4  # clamp((50-28)/5 + 5)


def test_builder_crypto_high_volume_rank_score_and_label():
    rows = [
        _crypto_row("KRW-HI", "하이", 1.0, 50.0, 999.0),
        _crypto_row("KRW-LO", "로우", 1.0, 50.0, 1.0),
    ]
    out = build_candidate_evidence(
        market="crypto", preset="crypto_high_volume", rows=rows
    )
    assert out[0].symbol == "KRW-HI"
    assert out[0].score == 10.0
    assert out[0].reasons == ["24시간 KRW 거래대금 상위"]
    assert out[0].score_label == "거래대금 999"


def test_builder_marks_market_warning_risk_flag():
    rows = [_crypto_row("KRW-WARN", "워언", 1.0, 50.0, 5.0, warning=True)]
    out = build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=rows)
    assert out[0].risk_flags == ["Upbit 유의 종목"]


def test_builder_equity_top_gainers_uses_change_rate_and_source():
    rows = [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "source": "kis",
            "change_rate": 3.0,
            "price": 78500.0,
            "daily_volume": 14_000_000,
            "consecutive_up_days": 3,
        },
    ]
    out = build_candidate_evidence(market="kr", preset="top_gainers", rows=rows)
    assert out[0].source == "kis"
    assert out[0].score == 6.5  # clamp(5 + 3/2)
    assert out[0].score_label == "+3.00%"
    assert out[0].reasons == ["단기 상승 모멘텀 후보", "3일 연속 상승"]
    assert out[0].volume_value == 14_000_000.0
    # ROB-359 Scope E — provenance lineage records which ranking surfaced it.
    assert out[0].source_preset == "top_gainers"


def test_builder_empty_rows_returns_empty():
    assert (
        build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=[])
        == []
    )


def test_consecutive_gainers_preset_reasons_and_score():
    from app.services.screener_evidence.builder import build_candidate_evidence

    rows = [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "source": "kis",
            "change_rate": 2.0,
            "close": 70000,
            "week_change_rate": 8.0,
            "consecutive_up_days": 6,
            "volume": 1_000_000,
        }
    ]
    out = build_candidate_evidence(
        market="kr", preset="consecutive_gainers", rows=rows
    )
    assert len(out) == 1
    ev = out[0]
    assert ev.source_preset == "consecutive_gainers"
    assert ev.price == 70000.0  # reads `close` when `price` absent
    assert any("연속 상승" in r for r in ev.reasons)
    assert ev.score > 5.0  # +2.0% momentum -> above neutral
