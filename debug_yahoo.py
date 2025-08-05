# debug_kis.py
import asyncio

import pandas as pd

from app.analysis.prompt import build_prompt
from app.services import yahoo


async def main():
    stock_symbol = "BRK-B"
    df_100 = await yahoo.fetch_ohlcv(stock_symbol)  # yfinance 100 캔들

    # 2) 장중 현재가 1행
    df_now = await yahoo.fetch_price(stock_symbol)  # DataFrame 1행

    # 3) concat 으로 병합  (append 대신!)
    df_full2 = (
        pd.concat([df_100, df_now.reset_index(drop=True)], ignore_index=True)
        .sort_values("date")
        .reset_index(drop=True)
    )
    prompt = build_prompt(df_full2, stock_symbol, "$")
    print(prompt)  # 잘 생성되는지 확인


if __name__ == "__main__":
    asyncio.run(main())
