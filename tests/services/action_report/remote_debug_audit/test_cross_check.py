from app.services.action_report.remote_debug_audit.cross_check import (
    SymbolQuote,
    build_audit,
    cross_check_symbol,
)
from app.services.action_report.remote_debug_audit.naver_quote import NaverQuote


def _at(symbol="005930", name="삼성전자", price=81000.0, status="ok"):
    return SymbolQuote(symbol=symbol, name=name, last_price=price, quote_status=status)


def test_ok_when_resolved_name_matches_price_within_band() -> None:
    f = cross_check_symbol(
        _at(), NaverQuote("005930", "삼성전자", 81500.0), tolerance_pct=5.0
    )
    assert f["status"] == "ok"
    assert f["symbol_resolved"] is True
    assert f["name_match"] is True
    assert f["at_quote_present"] is True
    assert f["price_within_tolerance"] is True


def test_price_mismatch_flags_warning() -> None:
    f = cross_check_symbol(
        _at(price=80000.0),
        NaverQuote("005930", "삼성전자", 120000.0),
        tolerance_pct=5.0,
    )
    assert f["price_within_tolerance"] is False
    assert f["status"] == "mismatch"


def test_unresolved_naver_symbol() -> None:
    f = cross_check_symbol(_at(symbol="999999", name=None), None, tolerance_pct=5.0)
    assert f["symbol_resolved"] is False
    assert f["status"] == "unavailable"
    assert f["reason_code"] == "naver_symbol_unresolved"


def test_at_quote_missing_when_status_not_ok() -> None:
    f = cross_check_symbol(
        _at(price=None, status="unavailable"),
        NaverQuote("005930", "삼성전자", 81000.0),
        tolerance_pct=5.0,
    )
    assert f["at_quote_present"] is False
    assert f["status"] == "at_quote_missing"


def test_build_audit_assembles_gaps_and_never_blocks() -> None:
    findings = [
        cross_check_symbol(
            _at(), NaverQuote("005930", "삼성전자", 81200.0), tolerance_pct=5.0
        ),
        cross_check_symbol(
            _at(symbol="000660", name="SK하이닉스", price=100000.0),
            NaverQuote("000660", "SK하이닉스", 200000.0),
            tolerance_pct=5.0,
        ),
        cross_check_symbol(_at(symbol="999999", name=None), None, tolerance_pct=5.0),
    ]
    audit = build_audit(
        snapshot_bundle_uuid="b-1", report_uuid="r-1", findings=findings
    )
    assert audit["source"] == "naver_remote_debug"
    assert audit["affects_report_generation"] is False
    assert audit["checked_symbols"] == 3
    # Two findings resolved on the Naver side (mismatch still counts as resolved),
    # one unresolved. ``symbols_resolved`` is the live-smoke acceptance signal.
    assert audit["symbols_resolved"] == 2
    assert audit["snapshot_bundle_uuid"] == "b-1"
    severities = {g["severity"] for g in audit["gaps"]}
    assert "blocking" not in severities
    kinds = {g["kind"] for g in audit["gaps"]}
    assert "naver_price_mismatch" in kinds
    assert "naver_symbol_unresolved" in kinds
