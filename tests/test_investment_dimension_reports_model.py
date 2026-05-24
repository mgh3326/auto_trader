from app.models.investment_dimension_reports import (
    DIMENSIONS,
    STANCES,
    InvestmentDimensionReport,
)


def test_model_table_and_vocab():
    assert InvestmentDimensionReport.__tablename__ == "investment_dimension_reports"
    assert InvestmentDimensionReport.__table_args__[-1] == {"schema": "review"}
    assert DIMENSIONS == ("market", "news", "fundamentals", "sentiment")
    assert STANCES == ("bullish", "neutral", "bearish")
    cols = InvestmentDimensionReport.__table__.c
    assert cols["symbol"].nullable is True  # market-wide
    assert cols["dimension"].nullable is False
    assert cols["report_text"].nullable is True
