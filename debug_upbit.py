# debug_kis.py
import asyncio

import pandas as pd

from app.analysis.prompt import build_prompt
from app.services import upbit
from data.coins_info import upbit_pairs


async def main():
    await upbit_pairs.prime_upbit_constants()
    coin_name = "비트코인"
    stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
    df_100 = await upbit.fetch_ohlcv(stock_symbol, days=200)  # yfinance 100 캔들

    # 2) 장중 현재가 1행
    df_now = await upbit.fetch_price(stock_symbol)  # DataFrame 1행

    # 3) concat 으로 병합  (append 대신!)
    df_full2 = (
        pd.concat([df_100, df_now.reset_index(drop=True)], ignore_index=True)
        .sort_values("date")
        .reset_index(drop=True)
    )
    prompt = build_prompt(df_full2, stock_symbol, coin_name)
    print(prompt)  # 잘 생성되는지 확인


if __name__ == "__main__":
    asyncio.run(main())
