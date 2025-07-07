# debug_kis.py
import asyncio, json

import pandas as pd

from app.services.kis import kis          # 경로는 실제 패키지 구조에 맞게

async def main():
    # price = await kis.inquire_price("005930")   # 삼성전자
    # print(json.dumps(price, indent=2, ensure_ascii=False))
    rows = await kis.volume_rank()        # TOP 30 레코드
    print(json.dumps(rows[:5], indent=2, ensure_ascii=False))  # 5줄만 보기

    keep = [
        "data_rank",  # 순위
        "hts_kor_isnm",  # 종목명
        "prdy_ctrt",  # 등락률(%)
        "acml_vol",  # 누적 거래량
        "avrg_vol",  # 평균 거래량
        "acml_tr_pbmn"  # 누적 거래대금
    ]

    df = (
        pd.DataFrame(rows)[keep]  # 원하는 컬럼만 남기고
        .astype({
            "data_rank": int,
            "prdy_ctrt": float,
            "acml_vol": int,
            "avrg_vol": int,
            "acml_tr_pbmn": int
        })
        .rename(columns={
            "data_rank": "순위",
            "hts_kor_isnm": "종목명",
            "prdy_ctrt": "등락률(%)",
            "acml_vol": "누적거래량",
            "avrg_vol": "평균거래량",
            "acml_tr_pbmn": "누적거래대금(원)"
        })
        .set_index("순위")
    )

    print(df.to_string())

if __name__ == "__main__":
    asyncio.run(main())