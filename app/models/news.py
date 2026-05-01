"""Database models for news articles and LLM analysis results."""

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class NewsArticle(Base):
    __tablename__ = "news_articles"

    __table_args__ = (
        UniqueConstraint("url", name="uq_news_article_url"),
        Index("ix_news_articles_keywords", "keywords", postgresql_using="gin"),
        Index("ix_news_articles_published_feed", "article_published_at", "feed_source"),
        Index("ix_news_articles_market_published", "market", "article_published_at"),
    )

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

    article_content: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="기사 본문 (RSS 뉴스는 NULL 가능)"
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="기사 요약 (LLM 생성)"
    )
    feed_source: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
        comment="RSS 피드 소스 (mk_stock, yna_market 등)",
    )
    market: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="kr",
        server_default="kr",
        index=True,
        comment="Market scope (kr, us, crypto)",
    )
    keywords: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="키워드 배열"
    )
    is_analyzed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
        comment="LLM 분석 완료 여부",
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


class NewsIngestionRun(Base):
    __tablename__ = "news_ingestion_runs"

    __table_args__ = (
        UniqueConstraint("run_uuid", name="uq_news_ingestion_runs_run_uuid"),
        CheckConstraint(
            "status IN ('success', 'partial', 'failed', 'dry_run_ok')",
            name="ck_news_ingestion_runs_status",
        ),
        Index("ix_news_ingestion_runs_market_finished", "market", "finished_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    feed_set: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    source_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    inserted_count: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    def __repr__(self) -> str:
        return (
            "<NewsIngestionRun("
            f"id={self.id}, run_uuid='{self.run_uuid}', market='{self.market}', "
            f"status='{self.status}')>"
        )


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

    def __repr__(self) -> str:
        return f"<NewsAnalysisResult(id={self.id}, article_id={self.article_id}, sentiment='{self.sentiment}', confidence={self.confidence})>"
