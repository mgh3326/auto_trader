"""Prompt templates for news analysis using LLM."""

from datetime import datetime


def build_news_analysis_prompt(
    title: str,
    content: str,
    stock_symbol: str | None = None,
    stock_name: str | None = None,
    source: str | None = None,
) -> str:
    stock_info = ""
    if stock_name and stock_symbol:
        stock_info = f"\n\n관련 종목: {stock_name} ({stock_symbol})"
    elif stock_name:
        stock_info = f"\n\n관련 종목: {stock_name}"

    source_info = f"\n출처: {source}" if source else ""

    prompt = f"""당신은 전문 금융 뉴스 분석가입니다. 다음 뉴스 기사를 분석해 주세요.{source_info}{stock_info}

뉴스 제목: {title}

기사 본문:
{content}

다음 항목들을 분석해 주세요:

1. **감정 분석**: 뉴스가 긍정적(positive), 부정적(negative), 중립적(neutral)인지 판단
2. **요약**: 200-300자 내외로 기사의 핵심 내용을 요약 (한국어)
3. **핵심 포인트**: 3-5개의 중요한 정보를 bullet point로 정리 (한국어)
4. **주요 키워드**: 뉴스에서 언급된 중요한 키워드 3-5개 추출
5. **주가 영향 분석**: 관련 종목이 있는 경우, 해당 뉴스가 주가에 미칠 영향을 분석 (한국어)
6. **신뢰도**: 분석 결과에 대한 신뢰도를 0-100점으로 평가

JSON 형식으로 답변해 주세요:

{{
    "sentiment": "positive|negative|neutral",
    "sentiment_score": -1.0 ~ 1.0 (negative ~ positive),
    "summary": "200-300자 요약",
    "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"],
    "topics": ["키워드1", "키워드2", "키워드3"],
    "price_impact": "주가 영향 분석 내용 (없으면 null)",
    "price_impact_score": -1.0 ~ 1.0 (없으면 null),
    "confidence": 0-100
}}

중요: 키워드와 요약은 반드시 한국어로 작성해 주세요.
JSON 외의 다른 설명 없이 오직 JSON만 출력하세요.
"""
    return prompt.strip()
