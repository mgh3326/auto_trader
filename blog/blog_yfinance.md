# yfinance로 애플·테슬라 분석하기: 해외 주식 데이터 수집 완벽 가이드

## 들어가며

[지난 글](./blog_kis_api.md)에서 한투 API로 삼성전자를 분석하며 실시간 국내 주식 데이터 수집 방법을 알아봤습니다. 이번에는 **yfinance 라이브러리**를 활용하여 애플(AAPL), 테슬라(TSLA) 같은 미국 주식을 분석하는 완벽한 방법을 소개합니다. API 키 없이 무료로 펀더멘털 데이터까지 수집할 수 있는 yfinance의 강력한 기능을 함께 살펴봅시다.

![yfinance 글로벌 주식 분석](이미지_URL_여기에_입력)
*yfinance를 활용한 글로벌 주식 시장 데이터 수집*

## 왜 yfinance인가?

해외 주식 데이터를 수집하는 방법은 여러 가지가 있습니다:

### 1. KIS 해외주식 API
- **장점**: 실시간 데이터, 국내/해외 통합 관리
- **단점**:
  - API 호출 제한이 빡빡함 (분당 10~20회)
  - 일부 데이터 누락 (일봉 데이터 부족)
  - 복잡한 인증 절차

### 2. yfinance (Yahoo Finance API)
- **장점**:
  - **완전 무료** - API 키 불필요
  - **풍부한 데이터** - 일봉 200일 이상, PER/PBR 등 펀더멘털
  - **간단한 사용법** - 3줄이면 데이터 수집 완료
  - **글로벌 커버리지** - 미국, 유럽, 아시아 주식 모두 지원
- **단점**:
  - 15분 지연 데이터 (실시간 X)
  - 공식 API가 아니라 변경 가능성 있음

**결론**: 개인 투자자에게는 yfinance가 압도적으로 편리합니다!

## 1. yfinance 설치 및 기본 사용법

### 1-1. 설치

```bash
pip install yfinance pandas ta google-genai
```

### 1-2. 기본 데이터 수집

```python
import yfinance as yf

# 애플 주식 티커
ticker = yf.Ticker("AAPL")

# 기본 정보
print(ticker.info["shortName"])  # Apple Inc.
print(ticker.info["currentPrice"])  # 현재가

# 과거 데이터 (최근 1개월)
hist = ticker.history(period="1mo")
print(hist.tail())
```

정말 간단하죠? 이제 본격적으로 AI 분석용 데이터를 수집해봅시다.

## 2. 해외 주식 데이터 수집

### 2-1. 일봉 데이터 가져오기

```python
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf


async def fetch_ohlcv(ticker: str, days: int = 200) -> pd.DataFrame:
    """
    최근 N일 일봉 OHLCV 데이터 조회

    Args:
        ticker: 주식 심볼 (예: "AAPL", "TSLA", "NVDA")
        days: 조회할 일수 (기본 200일)

    Returns:
        DataFrame with columns: date, open, high, low, close, volume
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days * 2)  # 휴일 감안하여 넉넉히

    # yfinance로 데이터 다운로드
    df = yf.download(
        ticker,
        start=start,
        end=end,
        interval="1d",
        progress=False,
        auto_adjust=False  # 배당/분할 조정 안 함
    )

    # 컬럼명 정리 (MultiIndex → 단일 레벨)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    # 날짜 컬럼 추가 및 정리
    df = df.reset_index(names="date")
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # 필요한 컬럼만 선택
    df = df[["date", "open", "high", "low", "close", "volume"]]

    # 최근 N일만 반환
    df = df.tail(days).reset_index(drop=True)

    if df.empty:
        raise ValueError(f"{ticker} 데이터를 찾을 수 없습니다")

    return df
```

### 2-2. 현재가 정보 가져오기

yfinance는 `fast_info`를 통해 빠르게 현재가를 조회할 수 있습니다.

```python
async def fetch_current_price(ticker: str) -> pd.DataFrame:
    """
    현재가 조회 (15분 지연)

    Returns:
        DataFrame with 1 row: date, open, high, low, close, volume
    """
    info = yf.Ticker(ticker).fast_info

    current_data = {
        "date": datetime.now(timezone.utc).date(),
        "open": getattr(info, "open", 0.0),
        "high": getattr(info, "day_high", 0.0),
        "low": getattr(info, "day_low", 0.0),
        "close": getattr(info, "last_price", 0.0),  # 현재가
        "volume": getattr(info, "last_volume", 0),
    }

    return pd.DataFrame([current_data])
```

### 2-3. 펀더멘털 정보 가져오기

```python
async def fetch_fundamental_info(ticker: str) -> dict:
    """
    기본 정보 조회 (PER, PBR, EPS, BPS, 배당수익률 등)
    """
    info = yf.Ticker(ticker).info

    fundamental_data = {
        "PER": info.get("trailingPE"),  # 주가수익비율
        "PBR": info.get("priceToBook"),  # 주가순자산비율
        "EPS": info.get("trailingEps"),  # 주당순이익
        "BPS": info.get("bookValue"),  # 주당순자산가치
        "배당수익률": info.get("trailingAnnualDividendYield"),
    }

    return fundamental_data
```

## 3. 데이터 구조 통일 (국내/해외)

1편에서 만든 `build_prompt` 함수를 그대로 사용할 수 있습니다! 핵심은 **DataFrame 구조를 통일**하는 것입니다.

### 공통 DataFrame 형식

```python
# 국내(KIS) + 해외(yfinance) 모두 동일한 구조
df = pd.DataFrame({
    "date": [...],      # 날짜
    "open": [...],      # 시가
    "high": [...],      # 고가
    "low": [...],       # 저가
    "close": [...],     # 종가
    "volume": [...]     # 거래량
})
```

이렇게 구조를 통일하면:
1. 기술적 지표 계산 함수 재사용
2. 프롬프트 생성 로직 재사용
3. 국내/해외 주식 동시 분석 가능

## 4. 해외 주식용 프롬프트 생성

1편에서 만든 `build_prompt` 함수를 그대로 사용하되, **통화 단위만 변경**합니다.

```python
# 국내 주식
prompt_kr = build_prompt(
    df=df_samsung,
    ticker="005930",
    stock_name="삼성전자",
    currency="₩",  # 원화
    unit_shares="주",
    fundamental_info=info
)

# 해외 주식
prompt_us = build_prompt(
    df=df_apple,
    ticker="AAPL",
    stock_name="Apple Inc.",
    currency="$",  # 달러
    unit_shares="shares",
    fundamental_info=info
)
```

프롬프트 형식은 동일하고, 통화 기호와 단위만 다릅니다!

## 5. 실행 예시

이제 애플(AAPL)을 실제로 분석해봅시다.

```python
import asyncio
from google import genai

async def main():
    # 애플 주식 데이터 수집
    ticker = "AAPL"
    stock_name = "Apple Inc."

    print("1단계: 데이터 수집 중...")
    # 1. 데이터 수집
    df_historical = await fetch_ohlcv(ticker, days=200)
    df_current = await fetch_current_price(ticker)
    fundamental_info = await fetch_fundamental_info(ticker)

    print(f"  - 일봉 데이터: {len(df_historical)}개")
    print(f"  - 현재가: ${df_current.iloc[0]['close']:.2f}")

    # 2. 데이터 병합 (1편에서 만든 함수 재사용)
    df_merged = merge_historical_and_current(df_historical, df_current)

    # 3. 프롬프트 생성 (1편에서 만든 함수 재사용, 달러 기호만 변경)
    print("\n2단계: AI 분석용 프롬프트 생성 중...")
    prompt = build_prompt(
        df=df_merged,
        ticker=ticker,
        stock_name=stock_name,
        currency="$",  # 달러
        unit_shares="shares",
        fundamental_info=fundamental_info
    )

    print("\n생성된 프롬프트:")
    print("=" * 80)
    print(prompt)
    print("=" * 80)

    # 4. AI 분석 (Google Gemini)
    print("\n3단계: Gemini AI에 분석 요청 중...")
    client = genai.Client(api_key="your_google_api_key")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    print("\nGemini AI 분석 결과:")
    print("=" * 80)
    print(response.text)
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
```

### 실제 실행 결과

#### 1단계: 데이터 수집
```
일봉 데이터: 200개
현재가: $262.24
병합 완료: 201개 데이터
```

#### 2단계: 생성된 프롬프트

```
Apple Inc.(AAPL) (관측일 2025-10-21)
MACD 히스토 -0.13, RSI14 66.3, BB폭 7.1%, Stoch %K 89.5

[기본 정보]
- PER: 39.85
- PBR: 59.18
- EPS: 6.58
- BPS: 4.43
- 배당수익률: 0.00

[가격 지표]
- MA 5/20/60/120/200 : 254.71 / 254.19 / 237.46 / 221.37 / 222.23 $
- 현재가 : 262.24$
- 전일 대비 : 0$ (+0.00%)
- RSI(14) : 66.3

[거래량 지표]
- VMA 5/20/60/120/200 : 60,711,640.00 / 47,626,540.00 / 56,539,246.67 / 54,923,377.50 / 56,600,357.00 vol
- 오늘 거래량 : 90,370,300.00shares
- 전일 대비 : +0.00%

[최근 10거래일 (날짜·종가·거래량)]
2025-10-07  256.48  31955800
2025-10-08  258.06  36496900
2025-10-09  254.04  38322000
2025-10-10  245.27  61999100
2025-10-13  247.66  38142900
2025-10-14  247.77  35478000
2025-10-15  249.34  33893600
2025-10-16  247.45  39777000
2025-10-17  252.29  49147000
2025-10-20  262.24  90370300

[질문]
위 정보만으로 오늘 매수·관망·매도 중 하나를 선택하고,
근거를 3줄 이내로 한글로 설명해 주세요.
적절한 매수가, 매도가, 매수 희망가, 매도 목표가도 제시해 주세요.
```

#### 3단계: Gemini AI 분석 결과

```
**관망 (Hold)**

**근거:**
1. 이동평균선이 정배열을 형성하고 현재가가 모든 이동평균선 위에 있어 강한 상승 추세를 보입니다.
2. 하지만 RSI14(66.3)와 Stoch %K(89.5)가 과매수 구간에 진입하여 단기 조정 가능성이 높습니다.
3. 높은 PER(39.85)과 PBR(59.18)은 밸류에이션 부담을 시사합니다.

**매수 희망가:** $250.00 ~ $255.00 (단기 조정 시 5일선 또는 20일선 부근)
**매도 목표가:** $275.00 ~ $280.00 (추세가 이어진다면 다음 저항선 또는 심리적 목표가)
```

![애플(AAPL) 차트 분석](이미지_URL_여기에_입력)
*애플 주가 차트와 Gemini AI의 분석 결과 (2025.10.21 기준)*

### 분석 해석

Gemini AI는 애플 데이터를 분석하여:
- **강세 확인**: 이동평균선 정배열 (MA5 > MA20 > MA60 > MA120 > MA200)
- **과매수 경고**: RSI 66.3, Stoch %K 89.5로 단기 조정 가능성
- **밸류에이션 부담**: PER 39.85, PBR 59.18로 고평가 구간
- **전략 제안**: 관망 후 $250-255 매수, $275-280 목표가 설정

실제로 주가가 $245 → $262 (약 7% 상승) 후 과열 신호가 나타난 상황입니다.

## 6. KIS 해외주식 API vs yfinance 비교

실전 비교표:

![API 비교 인포그래픽](이미지_URL_여기에_입력)
*KIS 해외주식 API와 yfinance의 주요 차이점*

| 항목 | yfinance | KIS 해외주식 API |
|------|----------|-----------------|
| **가격** | 무료 | 무료 (계좌 필요) |
| **API 키** | 불필요 | 필요 (토큰 관리) |
| **호출 제한** | 거의 없음 | 분당 10~20회 |
| **일봉 데이터** | 무제한 | 일부 종목 누락 |
| **현재가** | 15분 지연 | 실시간 |
| **펀더멘털** | PER, PBR, EPS 등 풍부 | 제한적 |
| **사용 난이도** | ⭐ 매우 쉬움 | ⭐⭐⭐ 복잡 |
| **안정성** | ⭐⭐⭐ 비공식 API | ⭐⭐⭐⭐⭐ 공식 API |

**결론**:
- **백테스팅/분석**: yfinance 추천 (데이터 풍부)
- **실시간 거래**: KIS API 필수 (체결 가능)
- **혼합 전략**: 분석은 yfinance, 실제 거래는 KIS

## 7. 국내/해외 통합 전략

![국내/해외 통합 아키텍처](이미지_URL_여기에_입력)
*KIS API와 yfinance를 통합한 데이터 수집 구조*

### 전략 1: 데이터 소스 통일

```python
async def fetch_stock_data(symbol: str, market: str = "KR"):
    """
    시장 구분 없이 주식 데이터 수집

    Args:
        symbol: 종목 코드/심볼 (예: "005930", "AAPL")
        market: 시장 (KR: 국내, US: 미국)

    Returns:
        통일된 DataFrame 형식
    """
    if market == "KR":
        # KIS API 사용
        df = await kis.inquire_daily_itemchartprice(symbol)
        currency = "₩"
        unit = "주"
    else:
        # yfinance 사용
        df = await yfinance.fetch_ohlcv(symbol)
        currency = "$"
        unit = "shares"

    return df, currency, unit
```

### 전략 2: 포트폴리오 통합 분석

```python
# 국내 + 해외 포트폴리오
portfolio = {
    "KR": ["삼성전자", "SK하이닉스"],
    "US": ["AAPL", "TSLA", "NVDA"]
}

# 일괄 분석
for market, stocks in portfolio.items():
    for stock in stocks:
        df, currency, unit = await fetch_stock_data(stock, market)
        prompt = build_prompt(df, stock, stock, currency, unit)
        result = await analyze_with_gemini(prompt)
        print(f"{stock}: {result}")
```

## 8. 다양한 종목 예시

yfinance는 전 세계 주식을 지원합니다:

```python
# 미국 주식
us_stocks = {
    "AAPL": "Apple",
    "TSLA": "Tesla",
    "NVDA": "Nvidia",
    "MSFT": "Microsoft",
    "GOOGL": "Google",
    "AMZN": "Amazon",
}

# ETF
etfs = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "VOO": "Vanguard S&P 500",
}

# 암호화폐
crypto = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}

# 모두 동일한 방식으로 수집 가능!
for symbol, name in us_stocks.items():
    df = await fetch_ohlcv(symbol)
    print(f"{name}: {len(df)}개 데이터")
```

## 9. 주의사항 및 팁

### yfinance 사용 시 주의사항

1. **15분 지연**: 실시간 거래는 불가능
2. **비공식 API**: Yahoo Finance 정책 변경 시 작동 안 할 수 있음
3. **데이터 정확성**: 때때로 누락되거나 잘못된 데이터 있음

### 해결 방법

```python
# 1. 데이터 검증
if df.empty or len(df) < 100:
    print(f"경고: {ticker} 데이터 부족")
    return None

# 2. 재시도 로직
for attempt in range(3):
    try:
        df = await fetch_ohlcv(ticker)
        break
    except Exception as e:
        print(f"재시도 {attempt + 1}/3: {e}")
        await asyncio.sleep(1)

# 3. 대체 소스 준비
if yfinance_failed:
    df = await fetch_from_alpha_vantage(ticker)  # 백업
```

## 10. 성능 최적화

### 병렬 처리

```python
import asyncio

async def analyze_multiple_stocks(symbols: list):
    """여러 종목 동시 분석"""
    tasks = [
        fetch_and_analyze(symbol)
        for symbol in symbols
    ]
    results = await asyncio.gather(*tasks)
    return results

# 100개 종목도 빠르게!
results = await analyze_multiple_stocks(
    ["AAPL", "TSLA", "NVDA", ...] * 30
)
```

## 마치며

이번 글에서는 yfinance를 활용하여:
1. 해외 주식 데이터 수집 (일봉, 현재가, 펀더멘털)
2. 국내/해외 데이터 구조 통일
3. 1편의 프롬프트 생성 로직 재사용
4. **실제 애플(AAPL) 분석 결과 확인**
5. KIS API vs yfinance 비교

까지 완료했습니다!

**핵심 포인트:**
- yfinance는 무료로 전 세계 주식 데이터 제공
- 국내/해외 데이터 구조를 통일하여 코드 재사용
- 1편에서 만든 `build_prompt`를 그대로 사용 가능
- Gemini AI는 PER/PBR 등 밸류에이션도 고려하여 분석

**다음 편 예고**: Upbit API로 암호화폐도 자동 분석! 비트코인, 이더리움의 기술적 지표를 AI에게 물어보고, 실시간 WebSocket으로 시세 모니터링까지 구현해보겠습니다.

**시리즈 전체 보기:**
- [1편: 한투 API로 실시간 주식 데이터 수집하기: AI 투자 분석의 시작](./blog_kis_api.md)
- 2편: yfinance로 애플·테슬라 분석하기: 해외 주식 데이터 수집 완벽 가이드 (현재 글)

---

**참고 링크:**
- [yfinance 공식 문서](https://pypi.org/project/yfinance/)
- [전체 프로젝트 코드 (GitHub)](https://github.com/mgh3326/auto_trader)
- [1편: 한투 API로 실시간 주식 데이터 수집하기](./blog_kis_api.md)
- [Google Gemini API 문서](https://ai.google.dev/)
