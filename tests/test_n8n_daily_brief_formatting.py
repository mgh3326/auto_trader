from __future__ import annotations

import pytest

from app.services.n8n_formatting import (
    fmt_amount,
    fmt_date_with_weekday,
    fmt_pnl,
    fmt_value,
)


@pytest.mark.unit
class TestDailyBriefFormatting:
    def test_fmt_date_with_weekday(self):
        from datetime import datetime
        from app.core.timezone import KST

        dt = datetime(2026, 3, 17, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/17 (화)"

    def test_fmt_date_with_weekday_sunday(self):
        from datetime import datetime
        from app.core.timezone import KST

        dt = datetime(2026, 3, 15, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/15 (일)"

    def test_fmt_value_krw_man(self):
        assert fmt_value(15_000_000, "KRW") == "1,500만"

    def test_fmt_value_krw_eok(self):
        assert fmt_value(150_000_000, "KRW") == "1.5억"

    def test_fmt_value_usd(self):
        assert fmt_value(42_000, "USD") == "$42,000"

    def test_fmt_value_none(self):
        assert fmt_value(None, "KRW") == "-"

    def test_fmt_pnl_negative(self):
        assert fmt_pnl(-5.2) == "-5.2%"

    def test_fmt_pnl_positive(self):
        assert fmt_pnl(3.1) == "+3.1%"

    def test_fmt_pnl_none(self):
        assert fmt_pnl(None) == "-"
