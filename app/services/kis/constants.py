"""KIS API constants (URLs, TR IDs, and configuration values)."""

# Base URL
BASE_URL = "https://openapi.koreainvestment.com:9443"

# ============================================================================
# DOMESTIC STOCK CONSTANTS
# ============================================================================

# Domestic Stock - Price & Volume
DOMESTIC_VOLUME_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"
DOMESTIC_VOLUME_TR = "FHPST01710000"  # 실전 전용

DOMESTIC_PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DOMESTIC_PRICE_TR = "FHKST01010100"

DOMESTIC_DAILY_CHART_URL = (
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
DOMESTIC_DAILY_CHART_TR = "FHKST03010100"  # 일봉·주식·실전/모의 공통

DOMESTIC_MINUTE_CHART_URL = (
    "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
)
DOMESTIC_MINUTE_CHART_TR = "FHKST03010200"  # 분봉 조회 TR ID

# Domestic Stock - Balance & Orders
DOMESTIC_BALANCE_URL = "/uapi/domestic-stock/v1/trading/inquire-balance"
DOMESTIC_BALANCE_TR = "TTTC8434R"  # 실전투자 주식잔고조회
DOMESTIC_BALANCE_TR_MOCK = "VTTC8434R"  # 모의투자 주식잔고조회

DOMESTIC_ORDER_URL = "/uapi/domestic-stock/v1/trading/order-cash"
DOMESTIC_ORDER_BUY_TR = "TTTC0802U"  # 실전투자 국내주식 매수주문
DOMESTIC_ORDER_BUY_TR_MOCK = "VTTC0802U"  # 모의투자 국내주식 매수주문
DOMESTIC_ORDER_SELL_TR = "TTTC0801U"  # 실전투자 국내주식 매도주문
DOMESTIC_ORDER_SELL_TR_MOCK = "VTTC0801U"  # 모의투자 국내주식 매도주문

DOMESTIC_ORDER_INQUIRY_URL = "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
DOMESTIC_ORDER_INQUIRY_TR = (
    "TTTC8036R"  # 국내주식 정정취소가능주문조회 (실전/모의 공통)
)

DOMESTIC_ORDER_CANCEL_URL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
DOMESTIC_ORDER_CANCEL_TR = "TTTC0803U"  # 실전투자 국내주식 정정취소주문
DOMESTIC_ORDER_CANCEL_TR_MOCK = "VTTC0803U"  # 모의투자 국내주식 정정취소주문

# ============================================================================
# OVERSEAS STOCK CONSTANTS
# ============================================================================

# Overseas Stock - Price & Charts
OVERSEAS_DAILY_CHART_URL = "/uapi/overseas-price/v1/quotations/dailyprice"
OVERSEAS_DAILY_CHART_TR = "HHDFS76240000"  # 해외주식 기간별시세

OVERSEAS_PERIOD_CHART_URL = (
    "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
)
OVERSEAS_PERIOD_CHART_TR = "FHKST03030100"

OVERSEAS_MINUTE_CHART_URL = (
    "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
)
OVERSEAS_MINUTE_CHART_TR = "FHKST03010200"

OVERSEAS_PRICE_URL = "/uapi/overseas-price/v1/quotations/price"
OVERSEAS_PRICE_TR = "HHDFS00000300"  # 해외주식 현재가 조회

# Overseas Stock - Balance & Orders
OVERSEAS_BALANCE_URL = "/uapi/overseas-stock/v1/trading/inquire-balance"
OVERSEAS_BALANCE_TR = "TTTS3012R"  # 실전투자 해외주식 잔고조회
OVERSEAS_BALANCE_TR_MOCK = "VTTS3012R"  # 모의투자 해외주식 잔고조회

OVERSEAS_MARGIN_URL = "/uapi/overseas-stock/v1/trading/foreign-margin"
OVERSEAS_MARGIN_TR = "TTTC2101R"  # 실전투자 해외증거금 통화별조회
OVERSEAS_MARGIN_TR_MOCK = "VTTS2101R"  # 모의투자 해외증거금 통화별조회

OVERSEAS_BUYABLE_AMOUNT_URL = "/uapi/overseas-stock/v1/trading/inquire-psamount"
OVERSEAS_BUYABLE_AMOUNT_TR = "TTTS3007R"  # 실전투자 해외주식 매수가능금액조회
OVERSEAS_BUYABLE_AMOUNT_TR_MOCK = "VTTS3007R"  # 모의투자 해외주식 매수가능금액조회

OVERSEAS_ORDER_URL = "/uapi/overseas-stock/v1/trading/order"
OVERSEAS_ORDER_BUY_TR = "TTTT1002U"  # 실전투자 해외주식 매수주문
OVERSEAS_ORDER_BUY_TR_MOCK = "VTTT1002U"  # 모의투자 해외주식 매수주문
OVERSEAS_ORDER_SELL_TR = "TTTT1006U"  # 실전투자 해외주식 매도주문
OVERSEAS_ORDER_SELL_TR_MOCK = "VTTT1006U"  # 모의투자 해외주식 매도주문

OVERSEAS_ORDER_INQUIRY_URL = "/uapi/overseas-stock/v1/trading/inquire-nccs"
OVERSEAS_ORDER_INQUIRY_TR = "TTTS3018R"  # 해외주식 미체결내역 조회

OVERSEAS_ORDER_CANCEL_URL = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
OVERSEAS_ORDER_CANCEL_TR = "TTTT1004U"  # 실전투자 해외주식 정정취소주문
OVERSEAS_ORDER_CANCEL_TR_MOCK = "VTTT1004U"  # 모의투자 해외주식 정정취소주문

# ============================================================================
# MARGIN & INTEGRATED MARGIN
# ============================================================================

INTEGRATED_MARGIN_URL = "/uapi/domestic-stock/v1/trading/intgr-margin"
INTEGRATED_MARGIN_TR = "TTTC0869R"  # 실전투자 통합증거금 조회
INTEGRATED_MARGIN_TR_MOCK = "VTTC0869R"  # 모의투자 통합증거금 조회

# ============================================================================
# EXCHANGE CODE MAPPINGS
# ============================================================================

# Overseas exchange codes (for API calls)
OVERSEAS_EXCHANGE_MAP = {
    "NASD": "NAS",  # 나스닥
    "NYSE": "NYS",  # 뉴욕
    "AMEX": "AMS",  # 아멕스
}

# Full exchange names (for display)
OVERSEAS_EXCHANGE_NAMES = {
    "NASD": "나스닥",
    "NYSE": "뉴욕",
    "AMEX": "아멕스",
    "SEHK": "홍콩",
    "SHAA": "중국상해",
    "SZAA": "중국심천",
    "TKSE": "일본",
    "HASE": "베트남하노이",
    "VNSE": "베트남호치민",
}

# Currency codes
OVERSEAS_CURRENCIES = {
    "USD": "미국 달러",
    "HKD": "홍콩 달러",
    "CNY": "위안화",
    "JPY": "엔화",
    "VND": "베트남 동",
}

# ============================================================================
# MARKET CODES
# ============================================================================

# Domestic market codes
DOMESTIC_MARKET_CODES = {
    "K": "코스피",
    "Q": "코스닥",
    "J": "통합",
}

# ============================================================================
# API RESPONSE CODES
# ============================================================================

# Error codes
ERROR_TOKEN_EXPIRED = "EGW00123"  # 토큰 만료
ERROR_TOKEN_INVALID = "EGW00121"  # 유효하지 않은 토큰
ERROR_QUANTITY_EXCEEDED = "APBK0400"  # 주문 가능한 수량 초과

# Success code
SUCCESS_CODE = "0"

# ============================================================================
# TIMEOUT & RETRY SETTINGS
# ============================================================================

DEFAULT_TIMEOUT = 5  # seconds
CHART_REQUEST_TIMEOUT = 10  # seconds (for chart data requests)

# Pagination settings
MAX_PAGES = 10  # Maximum number of pages to fetch for continuous queries
PAGE_DELAY = 0.1  # Delay between page requests in seconds

# Token refresh retry settings
MAX_TOKEN_RETRIES = 2
TOKEN_RETRY_DELAY = 0.5  # seconds

# ============================================================================
# DATA CONSTANTS
# ============================================================================

# Default values for data fetching
DEFAULT_CHART_DAYS = 200  # Number of days to fetch for charts
DEFAULT_CANDLES = 200  # Number of candles to fetch
DEFAULT_PER_CALL_DAYS = 150  # Days to fetch per API call for charts
MAX_CHART_ITERATIONS = 5  # Maximum iterations for fetching charts

# Chart periods
CHART_PERIOD_DAY = "D"  # 일봉
CHART_PERIOD_WEEK = "W"  # 주봉
CHART_PERIOD_MONTH = "M"  # 월봉

# Chart time units (minutes)
CHART_TIME_UNIT_1MIN = "01"  # 1분봉
CHART_TIME_UNIT_3MIN = "03"  # 3분봉
CHART_TIME_UNIT_5MIN = "05"  # 5분봉
CHART_TIME_UNIT_10MIN = "10"  # 10분봉
CHART_TIME_UNIT_15MIN = "15"  # 15분봉
CHART_TIME_UNIT_30MIN = "30"  # 30분봉
CHART_TIME_UNIT_45MIN = "45"  # 45분봉
CHART_TIME_UNIT_60MIN = "60"  # 60분봉

# Price adjustment
PRICE_ADJUSTED = "0"  # 수정주가
PRICE_ORIGINAL = "1"  # 원본주가

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def get_exchange_code_3digit(exchange_code: str) -> str:
    """Convert 4-digit exchange code to 3-digit code for API calls.

    Args:
        exchange_code: 4-digit code (e.g., "NASD", "NYSE", "AMEX")

    Returns:
        3-digit code (e.g., "NAS", "NYS", "AMS")
    """
    return OVERSEAS_EXCHANGE_MAP.get(exchange_code, exchange_code[:3])


def get_mock_tr_id(tr_id: str, is_mock: bool) -> str:
    """Get mock TR ID if is_mock is True, otherwise return original TR ID.

    Args:
        tr_id: Original TR ID (real trading)
        is_mock: True for mock trading, False for real trading

    Returns:
        Appropriate TR ID for the mode
    """
    # Mock TR IDs follow pattern: T -> V for most cases
    if is_mock and tr_id.startswith("TTT"):
        return tr_id.replace("TTT", "VTT", 1)
    if is_mock and tr_id.startswith("TTT"):
        return tr_id.replace("TTT", "VTT", 1)
    if is_mock and tr_id.startswith("TTT"):
        return tr_id.replace("TTT", "VTT", 1)
    return tr_id
