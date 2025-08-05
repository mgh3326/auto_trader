# debug_kis.py
import asyncio, json

import pandas as pd

from app.analysis.prompt import build_prompt
from app.services.kis import kis  # 경로는 실제 패키지 구조에 맞게
import ta

from data.stocks_info import KOSPI_NAME_TO_CODE


async def main():
    # price = await kis.inquire_price("005930")   # 삼성전자
    # print(json.dumps(price, indent=2, ensure_ascii=False))
    stock_code = KOSPI_NAME_TO_CODE.get("삼성전자")
    df = await kis.inquire_daily_itemchartprice(stock_code)
    today_now = await kis.inquire_price(stock_code)  # 1행 DataFrame, index='code'
    df_full = (
        pd.concat([df, today_now.reset_index(drop=True)], ignore_index=True)
        .sort_values("date")  # 확실히 오름차순
        .reset_index(drop=True)
    )

    df_full["diff"] = df_full.close.diff()
    df_full["pct"] = df_full.close.pct_change() * 100

    prompt = build_prompt(df, stock_code)
    print(prompt)


if __name__ == "__main__":
    asyncio.run(main())
