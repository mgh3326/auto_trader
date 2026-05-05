import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import Integer, JSON, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base
from app.models.analysis import StockInfo, StockAnalysisResult
from app.schemas.research_pipeline import SummaryOutput, SummaryDecision, PriceAnalysis, BullBearArgument
from app.services.legacy_stock_analysis_adapter import LegacyStockAnalysisAdapter

@pytest_asyncio.fixture
async def async_db():
    """In-memory SQLite async session."""
    engine = create_async_engine("sqlite+aiosqlite://")
    
    # Handle PostgreSQL-specific JSONB for SQLite
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.types import JSON

    @compiles(JSONB, "sqlite")
    def compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    async with engine.begin() as conn:
        # Create only the tables needed for this test to avoid issues with other models
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[StockInfo.__table__, StockAnalysisResult.__table__]
        )
        
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()

@pytest.mark.asyncio
async def test_legacy_stock_analysis_adapter_mapping(async_db: AsyncSession):
    # Setup: Create StockInfo
    stock_info = StockInfo(
        symbol="005930",
        name="삼성전자",
        instrument_type="equity_kr"
    )
    async_db.add(stock_info)
    await async_db.commit()
    await async_db.refresh(stock_info)
    
    # Setup: Create SummaryOutput
    summary = SummaryOutput(
        decision=SummaryDecision.BUY,
        confidence=85,
        bull_arguments=[BullBearArgument(text="Bullish argument", cited_stage_ids=[1])],
        bear_arguments=[BullBearArgument(text="Bearish argument", cited_stage_ids=[2])],
        price_analysis=PriceAnalysis(
            appropriate_buy_min=50000.0,
            appropriate_buy_max=55000.0,
            appropriate_sell_min=70000.0,
            appropriate_sell_max=75000.0,
            buy_hope_min=48000.0,
            buy_hope_max=52000.0,
            sell_target_min=80000.0,
            sell_target_max=85000.0
        ),
        reasons=["Reason 1", "Reason 2"],
        detailed_text="Detailed analysis text",
        model_name="test-model",
        prompt_version="v1"
    )
    
    summary_id = 123
    
    # Act
    adapter = LegacyStockAnalysisAdapter()
    result = await adapter.write(async_db, summary, summary_id, stock_info.id)
    
    # Assert
    assert result.stock_info_id == stock_info.id
    assert result.decision == "buy"
    assert result.confidence == 85
    assert result.appropriate_buy_min == 50000.0
    assert result.appropriate_buy_max == 55000.0
    assert result.appropriate_sell_min == 70000.0
    assert result.appropriate_sell_max == 75000.0
    assert result.buy_hope_min == 48000.0
    assert result.buy_hope_max == 52000.0
    assert result.sell_target_min == 80000.0
    assert result.sell_target_max == 85000.0
    assert result.reasons == ["Reason 1", "Reason 2"]
    assert result.detailed_text == "Detailed analysis text"
    assert result.model_name == "test-model"
    # prompt should encode summary id and prompt version
    assert result.prompt == "research_summary:123/prompt_version:v1"

@pytest.mark.asyncio
async def test_legacy_stock_analysis_adapter_default_model_name(async_db: AsyncSession):
    # Setup
    stock_info = StockInfo(symbol="AAPL", name="Apple", instrument_type="equity_us")
    async_db.add(stock_info)
    await async_db.commit()
    await async_db.refresh(stock_info)
    
    summary = SummaryOutput(
        decision=SummaryDecision.HOLD,
        confidence=50,
        bull_arguments=[],
        bear_arguments=[],
        reasons=[],
        model_name=None, # Testing default
        prompt_version="v2"
    )
    
    # Act
    adapter = LegacyStockAnalysisAdapter()
    result = await adapter.write(async_db, summary, 456, stock_info.id)
    
    # Assert
    assert result.model_name == "research_pipeline"
    assert result.prompt == "research_summary:456/prompt_version:v2"
