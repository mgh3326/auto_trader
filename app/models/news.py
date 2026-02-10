"""Database models for news articles and LLM analysis results."""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class NewsArticle(Base):
    __tablename__ = "news_articles"

    __table_args__ = (UniqueConstraint("url", name="uq_news_article_url"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    url: Mapped[str] = mapped_column(
        String(2048), nullable=False, index=True, comment="뉴스 기사 URL"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="뉴스 제목")
    source: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="뉴스 출처 (매일경제, 이데일리, etc.)"
    )
    author: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="기자명"
    )

    article_content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="기사 본문"
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="기사 요약 (LLM 생성)"
    )

    stock_symbol: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        index=True,
        comment="관련 종목 코드 (e.g., 삼성전자)",
    )
    stock_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="관련 종목명 (e.g., 삼성전자)"
    )

    article_published_at: Mapped[datetime | None] = mapped_column(
        nullable=True, index=True, comment="기사 발행일시"
    )
    scraped_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="스크래핑 수집일시"
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="데이터 생성일시"
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=True, comment="데이터 수정일시"
    )

    user = relationship("User", backref="news_articles")
    analysis_results: Mapped[list["NewsAnalysisResult"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<NewsArticle(id={self.id}, title='{self.title[:50]}...', url='{self.url[:50]}...')>"


class NewsAnalysisResult(Base):
    __tablename__ = "news_analysis_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="뉴스 기사 ID",
    )

    model_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="사용된 LLM 모델명 (e.g., gemini-2.5-pro)",
    )

    sentiment: Mapped[Sentiment] = mapped_column(
        nullable=False,
        index=True,
        comment="감정 분석 (positive/negative/neutral)",
    )
    sentiment_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="감정 점수 (-1.0 ~ 1.0, negative ~ positive)",
    )

    summary: Mapped[str] = mapped_column(
        Text, nullable=False, comment="기사 요약 (한국어)"
    )
    key_points: Mapped[list] = mapped_column(
        JSONB, nullable=False, comment="핵심 포인트 리스트 (JSON 배열)"
    )
    topics: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="주요 키워드/토픽 (JSON 배열)"
    )

    price_impact: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="주가 영향 분석 (한국어)"
    )
    price_impact_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="주가 영향 점수 (-1.0 ~ 1.0, negative ~ positive)",
    )

    confidence: Mapped[int] = mapped_column(
        nullable=False,
        comment="분석 신뢰도 (0-100)",
    )
    analysis_quality: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="분석 품질 (high/medium/low)",
    )

    prompt: Mapped[str] = mapped_column(
        Text, nullable=False, comment="LLM에 전달한 프롬프트"
    )
    raw_response: Mapped[str] = mapped_column(
        Text, nullable=False, comment="LLM 원본 응답"
    )

    processing_time_ms: Mapped[int | None] = mapped_column(
        nullable=True, comment="LLM 처리 시간 (밀리초)"
    )

    created_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="분석 생성일시"
    )
    updated_at: Mapped[datetime] = mapped_column(nullable=True, comment="분석 수정일시")

    article = relationship(
        "NewsArticle", back_populates="analysis_results", lazy="joined"
    )

    __table_args__ = (
        Index("ix_news_analysis_article_sentiment", "article_id", "sentiment"),
        Index("ix_news_analysis_sentiment_created", "sentiment", "created_at"),
    )

    # LLM model info
    model_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="사용된 LLM 모델명 (e.g., gemini-2.5-pro)",
    )

    # Sentiment analysis
    sentiment: Mapped[Sentiment] = mapped_column(
        nullable=False,
        index=True,
        comment="감정 분석 (positive/negative/neutral)",
    )
    sentiment_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="감정 점수 (-1.0 ~ 1.0, negative ~ positive)",
    )

    # Key insights
    summary: Mapped[str] = mapped_column(
        Text, nullable=False, comment="기사 요약 (한국어)"
    )
    key_points: Mapped[list] = mapped_column(
        JSONB, nullable=False, comment="핵심 포인트 리스트 (JSON 배열)"
    )
    topics: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="주요 키워드/토픽 (JSON 배열)"
    )

    # Price impact analysis (if stock is related)
    price_impact: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="주가 영향 분석 (한국어)"
    )
    price_impact_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="주가 영향 점수 (-1.0 ~ 1.0, negative ~ positive)",
    )

    # Confidence and quality
    confidence: Mapped[int] = mapped_column(
        nullable=False,
        comment="분석 신뢰도 (0-100)",
    )
    analysis_quality: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="분석 품질 (high/medium/low)",
    )

    # Prompt and response for debugging
    prompt: Mapped[str] = mapped_column(
        Text, nullable=False, comment="LLM에 전달한 프롬프트"
    )
    raw_response: Mapped[str] = mapped_column(
        Text, nullable=False, comment="LLM 원본 응답"
    )

    # Processing metadata
    processing_time_ms: Mapped[int | None] = mapped_column(
        nullable=True, comment="LLM 처리 시간 (밀리초)"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, comment="분석 생성일시"
    )
    updated_at: Mapped[datetime] = mapped_column(nullable=True, comment="분석 수정일시")

    # Relationships
    article = relationship(
        "NewsArticle", back_populates="analysis_results", lazy="joined"
    )

    # Indexes for common queries
    __table_args__ = (
        Index("ix_news_analysis_article_sentiment", "article_id", "sentiment"),
        Index("ix_news_analysis_sentiment_created", "sentiment", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<NewsAnalysisResult(id={self.id}, article_id={self.article_id}, sentiment='{self.sentiment}', confidence={self.confidence})>"
