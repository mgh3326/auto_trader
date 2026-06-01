"""Built-in deterministic alias dictionaries for the news entity matcher (ROB-130).

Data-only module. Keep entries narrow and high-signal. Each entry is a (symbol,
market, canonical_name, alias_terms) tuple. `alias_terms` are matched
case-insensitively against title + summary + joined keywords. Korean terms are
matched as substrings; English terms are matched on word boundaries.

These dictionaries are intentionally a small, high-precision set covering the
acceptance-criteria examples (AMZN, 005930, BTC) plus the most-traded peers.
Long-tail mapping is delegated to the DB symbol universe + `stock_aliases`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliasEntry:
    symbol: str  # canonical DB form (e.g. "005930", "AMZN", "BTC")
    market: str  # "kr" | "us" | "crypto"
    canonical_name: str  # display name
    aliases: tuple[str, ...]  # case-insensitive substring/word-boundary terms


KR_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("005930", "kr", "삼성전자", ("삼성전자", "삼전", "삼전닉스", "Samsung Electronics")),
    # ROB-130: "닉스" is a 2-char alias requested by the spec; accepts some
    # false-positive risk for higher recall on KR market chatter.
    AliasEntry(
        "000660", "kr", "SK하이닉스", ("SK하이닉스", "하이닉스", "닉스", "삼전닉스", "SK Hynix")
    ),
    AliasEntry("035420", "kr", "NAVER", ("네이버", "NAVER")),
    AliasEntry("035720", "kr", "카카오", ("카카오",)),
    AliasEntry("323410", "kr", "카카오뱅크", ("카카오뱅크",)),
    AliasEntry("377300", "kr", "카카오페이", ("카카오페이",)),
    AliasEntry("207940", "kr", "삼성바이오로직스", ("삼성바이오", "삼성바이오로직스")),
    AliasEntry("005380", "kr", "현대차", ("현대차", "현대자동차", "Hyundai Motor")),
    AliasEntry("005490", "kr", "POSCO홀딩스", ("POSCO", "포스코")),
    AliasEntry("373220", "kr", "LG에너지솔루션", ("LG에너지솔루션", "LG엔솔")),
    AliasEntry("000270", "kr", "기아", ("기아", "기아차", "Kia")),
    AliasEntry("006400", "kr", "삼성SDI", ("삼성SDI", "삼성에스디아이")),
    AliasEntry("068270", "kr", "셀트리온", ("셀트리온",)),
    AliasEntry("066570", "kr", "LG전자", ("LG전자", "엘지전자", "LG Electronics")),
    AliasEntry("105560", "kr", "KB금융", ("KB금융", "KB금융지주")),
)

US_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("AAPL", "us", "Apple", ("Apple", "AAPL", "애플")),
    AliasEntry("AMZN", "us", "Amazon", ("Amazon", "AMZN", "아마존")),
    AliasEntry("NVDA", "us", "Nvidia", ("Nvidia", "NVDA", "엔비디아")),
    AliasEntry("TSLA", "us", "Tesla", ("Tesla", "TSLA", "테슬라")),
    AliasEntry("META", "us", "Meta", ("Meta Platforms", "META", "메타")),
    AliasEntry(
        "GOOGL", "us", "Alphabet", ("Alphabet", "Google", "GOOGL", "GOOG", "구글")
    ),
    AliasEntry("MSFT", "us", "Microsoft", ("Microsoft", "MSFT", "마이크로소프트")),
    AliasEntry("AMD", "us", "AMD", ("AMD", "Advanced Micro Devices")),
    AliasEntry("AVGO", "us", "Broadcom", ("Broadcom", "AVGO")),
    AliasEntry("BRK.B", "us", "Berkshire Hathaway B", ("Berkshire Hathaway",)),
)

CRYPTO_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("BTC", "crypto", "Bitcoin", ("Bitcoin", "BTC", "비트코인", "KRW-BTC")),
    AliasEntry("ETH", "crypto", "Ethereum", ("Ethereum", "ETH", "이더리움", "KRW-ETH")),
    AliasEntry("SOL", "crypto", "Solana", ("Solana", "SOL", "솔라나", "KRW-SOL")),
    AliasEntry("XRP", "crypto", "Ripple", ("Ripple", "XRP", "리플", "KRW-XRP")),
    AliasEntry(
        "DOGE", "crypto", "Dogecoin", ("Dogecoin", "DOGE", "도지코인", "KRW-DOGE")
    ),
)

ALL_ALIASES: tuple[AliasEntry, ...] = KR_ALIASES + US_ALIASES + CRYPTO_ALIASES

# ROB-155: US scope classification constants.
# Broad-market terms indicate macro/index/sector framing rather than a specific
# company thesis. Keep this list tight — overly broad terms would suppress
# legitimate company-specific articles.
US_BROAD_MARKET_TERMS: tuple[str, ...] = (
    "s&p 500",
    "s&p500",
    "dow jones",
    "nasdaq",
    "market index",
    "stock market",
    "federal reserve",
    "fed rate",
    "interest rate",
    "inflation",
    "recession",
    "gdp",
    "earnings season",
    "sector rotation",
    "big tech",
    "tech sector",
    "mega cap",
    "magnificent seven",
    "mag 7",
)

# Big-tech symbols whose incidental co-mention in market-wide articles should be
# demoted (response layer only; persisted rows are untouched).
US_BIG_TECH_GROUP_SYMBOLS: frozenset[str] = frozenset(
    {"AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "NVDA", "TSLA"}
)

# ROB-169: KR investment relevance constants.
# Broad-market terms that indicate a market-wide investment story even without
# a specific stock_symbol/relatedSymbols. Keep tight and high-precision.
KR_BROAD_MARKET_TERMS: tuple[str, ...] = (
    "코스피",
    "코스닥",
    "kospi",
    "kosdaq",
    "코스피200",
    "kospi200",
    "코스닥150",
    "krx",
    "유가증권",
    "지수",
    "선물",
    "옵션",
    "etf",
    "etn",
    "리츠",
    "공모주",
    "ipo",
    "상장",
    "상폐",
    "유상증자",
    "무상증자",
    "배당",
    "배당락",
    "기준금리",
    "한국은행",
    "한은",
    "금융통화위원회",
    "금통위",
    "환율",
    "원달러",
    "원/달러",
    "달러원",
    "위안화",
    "엔화",
    "유가",
    "wti",
    "원유",
    "금값",
    "구리",
    "철광석",
    "리튬",
    "대출금리",
    "물가",
    "소비자물가",
    "cpi",
    "ppi",
    "gdp",
    "수출",
    "수입",
    "무역수지",
    "경상수지",
    "증시",
    "주식시장",
    "양도세",
    "금융시장",
)

# Industry / policy / sector keywords that signal investment relevance even
# when the article does not name a specific listed company.
KR_INVEST_KEYWORDS: tuple[str, ...] = (
    "반도체",
    "메모리",
    "디램",
    "낸드",
    "파운드리",
    "hbm",
    "ai 반도체",
    "배터리",
    "이차전지",
    "전고체",
    "양극재",
    "음극재",
    "전기차",
    "수소차",
    "조선",
    "방산",
    "원전",
    "smr",
    "바이오",
    "제약",
    "신약",
    "임상",
    "건설",
    "부동산",
    "리츠",
    "상업용 부동산",
    "통신",
    "5g",
    "6g",
    "철강",
    "석유화학",
    "정유",
    "유통",
    "면세",
    "엔터",
    "콘텐츠",
    "ott",
    "게임",
    "플랫폼",
    "이커머스",
    "물류",
    "해운",
    "항공",
    "핀테크",
    "인터넷은행",
    "빅테크",
    "관세",
    "수출규제",
    "지원금",
    "보조금",
    "감세",
    "증세",
    "법인세",
    "금융위",
    "금감원",
    "공정위",
    "세제개편",
    "예산",
    "재정",
    "한미 정상회담",
    "한일 정상회담",
)

# Society/crime/celebrity/sports/accident noise terms — used to suppress KR
# rows that are neither symbol-specific nor market-wide.
KR_CRIME_TERMS: tuple[str, ...] = (
    "살해",
    "살인",
    "강도",
    "강간",
    "성폭행",
    "성추행",
    "납치",
    "감금",
    "유괴",
    "협박",
    "폭행",
    "폭언",
    "음주운전",
    "뺑소니",
    "마약",
    "필로폰",
    "도박",
    "사기",
    "보이스피싱",
    "스미싱",
    "스토킹",
    "피의자",
    "용의자",
    "구속",
    "체포",
    "기소",
    "재판",
    "선고",
    "징역",
    "벌금",
    "사이코패스",
    "성범죄",
    "아동학대",
    "가정폭력",
    "데이트폭력",
)

KR_SOCIETY_TERMS: tuple[str, ...] = (
    "연예",
    "연예인",
    "아이돌",
    "트로트",
    "스캔들",
    "열애",
    "결혼",
    "이혼",
    "재혼",
    "가요",
    "예능",
    "드라마",
    "스포츠",
    "야구",
    "축구",
    "농구",
    "배구",
    "골프",
    "프로야구",
    "kbo",
    "k리그",
    "올림픽",
    "월드컵",
    "아시안게임",
    "교통사고",
    "화재",
    "추락",
    "익사",
    "실종",
    "행방불명",
    "여고생",
    "여중생",
    "초등학생",
    "유치원",
    "어린이집",
    "학교폭력",
    "학폭",
    "층간소음",
    "주거침입",
    "고독사",
)

# Catch-all noise terms that are not strictly society/crime but still pure
# non-investment context. Keep tight to avoid silencing legitimate stories.
KR_NOISE_TERMS: tuple[str, ...] = (
    "날씨",
    "한파",
    "폭염",
    "장마",
    "태풍",
    "황사",
    "미세먼지",
    "운세",
    "복권",
    "로또",
    "맛집",
    "여행",
    "관광",
    "맛벌이",
    "건강검진",
    "다이어트",
    "헬스",
)

# Big-cap KR symbols whose incidental co-mention in market-wide rollup articles
# does not justify keeping the row in a symbol-specific bucket. Currently used
# only in the scope tag — symbol demotion is left to a future ROB.
KR_BIG_CAP_GROUP_SYMBOLS: frozenset[str] = frozenset(
    {"005930", "000660", "035420", "035720", "207940", "005380", "005490", "373220"}
)
