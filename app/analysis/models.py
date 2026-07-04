from pydantic import BaseModel, Field


class PriceRange(BaseModel):
    """가격 범위를 나타내는 모델"""

    min: float = Field(description="가격 범위의 최소값")
    max: float = Field(description="가격 범위의 최대값")


class PriceAnalysis(BaseModel):
    """매매 가격 분석 결과"""

    appropriate_buy_range: PriceRange = Field(
        description="현재 시점에서 매수하기에 적정한 가격 범위 (현재가 기준)"
    )
    appropriate_sell_range: PriceRange = Field(
        description="보유중일 때 매도하기에 적정한 가격 범위 (단기 목표)"
    )
    buy_hope_range: PriceRange = Field(
        description="조금 더 저렴하게 사고 싶은 이상적인 매수 가격 범위 (지정가 주문용)"
    )
    sell_target_range: PriceRange = Field(
        description="최종적으로 도달하기를 기대하는 매도 가격 범위 (장기 목표)"
    )
