from app.schemas.n8n import N8nKrMorningReportResponse


def test_kr_morning_report_schema_accepts_manual_toss_cash():
    payload = N8nKrMorningReportResponse(
        success=True,
        as_of="2026-03-19T08:50:00+09:00",
        date_fmt="03/19 (목)",
        cash_balance={
            "kis_krw": 45000,
            "kis_krw_fmt": "4.5만",
            "toss_krw": None,
            "toss_krw_fmt": "수동 관리",
            "total_krw": 45000,
            "total_krw_fmt": "4.5만",
        },
    )

    assert payload.cash_balance.toss_krw is None
    assert payload.cash_balance.toss_krw_fmt == "수동 관리"
