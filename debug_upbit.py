# debug_kis.py
import asyncio

import pandas as pd
from google import genai

from app.analysis.prompt import build_prompt
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models import PromptResult
from app.services import upbit
from data.coins_info import upbit_pairs


async def main():
    await upbit_pairs.prime_upbit_constants()
    coin_names = ["이더리움네임서비스", "솔라나", "엑스알피(리플)", "도지코인", "비트코인"]
    coin_names = ["에이다", "에테나", "레이디움", "서싱트", "스텔라루멘", "수이", "스트라이크", "온도파이낸스"]
    coin_names = ["이더리움", "이더리움네임서비스", "솔라나", "엑스알피(리플)", "도지코인", "비트코인"]
    coin_names = ["비트코인"]
    for coin_name in coin_names:
        stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
        df_100 = await upbit.fetch_ohlcv(stock_symbol, days=200)  # yfinance 100 캔들
        # 2) 장중 현재가 1행
        df_now = await upbit.fetch_price(stock_symbol)  # DataFrame 1행
        # 3) concat 으로 병합  (append 대신!)
        df_full2 = (
            pd.concat([df_100, df_now.reset_index(drop=True)], ignore_index=True)
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")  # 같은 날짜면 마지막 것만 유지
            .reset_index(drop=True)
        )
        prompt = build_prompt(df_full2, stock_symbol, coin_name)
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
                instrument_type="crypto",
                model_name=model_name
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)


async def generate_with_smart_retry(client, prompt, max_retries=3):
    """스마트 재시도: 429 에러 시 다음 모델로 즉시 전환"""

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

                    if finish_reason == "STOP" and not resp.text:
                        print(f"  시도 {attempt + 1}: STOP이지만 text가 None, 재시도...")
                        await asyncio.sleep(1 + attempt)
                        continue

                    if finish_reason in ["SAFETY", "RECITATION"]:
                        print(f"  {model} 차단됨: {finish_reason}, 다음 모델로 시도...")
                        break

                    if resp.text:
                        print(f"  {model} 성공!")
                        return resp.text, model

            except Exception as e:
                print(f"  시도 {attempt + 1} 실패: {e}")
                # 그 외 다른 에러는 잠시 후 재시도
                await asyncio.sleep(2 + attempt)
        else:
            # for 루프가 break 없이 완료되었을 때 (모든 재시도 실패)
            print(f"  {model} 모든 시도 실패, 다음 모델로...")
            continue

    return "모든 모델과 재시도 실패", "N/A"


if __name__ == "__main__":
    asyncio.run(main())
