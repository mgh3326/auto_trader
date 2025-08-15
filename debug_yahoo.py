# debug_kis.py
import asyncio

import pandas as pd
from google import genai

from app.analysis.prompt import build_prompt
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.prompt import PromptResult
from app.services import yahoo


async def main():
    stock_symbols = ["QQQM", "QQQ", "BRK-B", "SPLG", "IVV", "VOO"]
    stock_symbols = ["TSLA", "AAPL", "CONY", "CWD"]
    stock_symbols = ["PLTR","IONQ"]
    stock_symbols = ["FIG","NVDA","AMZN"]
    stock_symbols = ["BRK-B","CONY"]
    stock_symbols = ["TSLL"]
    for stock_symbol in stock_symbols:
        df_100 = await yahoo.fetch_ohlcv(stock_symbol, 200)  # yfinance 100 캔들

        # 2) 장중 현재가 1행
        df_now = await yahoo.fetch_price(stock_symbol)  # DataFrame 1행

        # 3) concat 으로 병합  (append 대신!)
        df_full2 = (
            pd.concat([df_100, df_now.reset_index(drop=True)], ignore_index=True)
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")  # 같은 날짜면 마지막 것만 유지
            .reset_index(drop=True)
        )
        prompt = build_prompt(df_full2, stock_symbol, "", "$")
        client = genai.Client(api_key=settings.google_api_key)  # 환경변수 GEMINI_API_KEY 필요
        res, model_name = await generate_with_smart_retry(client, prompt)
        print(prompt)
        print(res)
        async with AsyncSessionLocal() as db:
            record = PromptResult(
                prompt=prompt,
                result=res,
                symbol=stock_symbol,
                name=stock_symbol,
                instrument_type="equity_us",
                model_name=model_name
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)


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
                        return resp.text, model

            except Exception as e:
                print(f"  시도 {attempt + 1} 실패: {e}")
                await asyncio.sleep(2 + attempt)  # 오류 시 더 긴 대기

        print(f"  {model} 모든 시도 실패, 다음 모델로...")

    return "모든 모델과 재시도 실패"


if __name__ == "__main__":
    asyncio.run(main())
