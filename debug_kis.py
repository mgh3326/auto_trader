# debug_kis.py
import asyncio

import pandas as pd

from app.analysis.prompt import build_prompt
from app.core.config import settings
from app.services.disclosures import dart
from app.services.kis import kis  # 경로는 실제 패키지 구조에 맞게
from data.disclosures import dart_corp_index
from data.stocks_info import KOSPI_NAME_TO_CODE, KOSDAQ_NAME_TO_CODE, KRX_NAME_TO_CODE
from google import genai

from pydantic import BaseModel, Field
from typing import Literal


class TradePlan(BaseModel):
    ticker: str
    side: Literal["BUY", "SELL", "HOLD"]
    buy_price: int = Field(ge=1)
    sell_price: int = Field(ge=1)
    qty: int = Field(ge=1)


async def main():
    # price = await kis.inquire_price("005930")   # 삼성전자
    # print(json.dumps(price, indent=2, ensure_ascii=False))
    # await dart.init_dart()
    # a = await dart.list_filings("삼성전자")
    client = genai.Client(api_key=settings.google_api_key)  # 환경변수 GEMINI_API_KEY 필요

    stock_name = ("한국타이어앤테크놀로지")
    stock_code = KRX_NAME_TO_CODE.get(stock_name)
    df = await kis.inquire_daily_itemchartprice(stock_code)
    today_now = await kis.inquire_price(stock_code)  # 1행 DataFrame, index='code'
    df = (
        pd.concat([df, today_now.reset_index(drop=True)], ignore_index=True)
        .sort_values("date")  # 확실히 오름차순
        .drop_duplicates(subset=["date"], keep="last")  # 같은 날짜면 마지막 것만 유지
        .reset_index(drop=True)
    )

    prompt = build_prompt(df, stock_code, stock_name)
    # resp = client.models.generate_content(
    #     model="gemini-2.5-pro",
    #     contents=prompt,
    #     config={
    #         "response_mime_type": "application/json",
    #         "response_schema": TradePlan,  # ← 타입 강제!
    #     },
    # )
    # plan: TradePlan = resp.parsed
    res = await generate_with_smart_retry(client, prompt)
    print(prompt)
    print(res)


def safe_get_response_text(resp) -> str:
    """Gemini 응답에서 안전하게 텍스트 추출"""
    if not resp:
        return "응답 객체가 없습니다."

    # 기본 text 속성 확인
    if hasattr(resp, 'text') and resp.text:
        return resp.text

    # candidates를 통한 대체 접근
    if hasattr(resp, 'candidates') and resp.candidates:
        candidate = resp.candidates[0]
        if hasattr(candidate, 'content') and candidate.content:
            if hasattr(candidate.content, 'parts') and candidate.content.parts:
                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text:
                        return part.text

        # finish_reason 확인
        if hasattr(candidate, 'finish_reason'):
            return f"응답 생성 중단됨. 이유: {candidate.finish_reason}"

    return "응답 텍스트를 추출할 수 없습니다."


async def generate_with_smart_retry(client, prompt, max_retries=3):
    """스마트 재시도: 상황별로 다른 대기시간 적용 + 모델 대체"""

    models_to_try = ["gemini-2.5-pro", "gemini-2.5-flash"]

    for model in models_to_try:
        print(f"모델 시도: {model}")

        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )

                if resp and resp.candidates:
                    candidate = resp.candidates[0]
                    finish_reason = getattr(candidate, 'finish_reason', None)

                    # STOP인데 text가 None인 경우 → 재시도
                    if finish_reason == "STOP" and not resp.text:
                        print(f"  시도 {attempt + 1}: STOP이지만 text가 None, 재시도...")
                        await asyncio.sleep(1 + attempt)  # 점진적 대기
                        continue

                    # SAFETY나 다른 차단 사유면 이 모델로는 재시도 무의미 → 다음 모델로
                    if finish_reason in ["SAFETY", "RECITATION"]:
                        print(f"  {model} 차단됨: {finish_reason}, 다음 모델로 시도...")
                        break  # 내부 루프 탈출 → 다음 모델로

                    # 정상 응답
                    if resp.text:
                        print(f"  {model} 성공!")
                        return resp.text

            except Exception as e:
                print(f"  시도 {attempt + 1} 실패: {e}")
                await asyncio.sleep(2 + attempt)  # 오류 시 더 긴 대기

        print(f"  {model} 모든 시도 실패, 다음 모델로...")

    return "모든 모델과 재시도 실패"


if __name__ == "__main__":
    asyncio.run(main())
