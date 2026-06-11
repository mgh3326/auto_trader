# ROB-502 사전 조사: get_market_news / get_market_issues 실측 품질 리뷰

- **작성일**: 2026-06-10 (KST 저녁, 데이터는 당일 24h 윈도우)
- **목적**: "지금 유의미한 뉴스가 나오는가?" 실측 → 도구 폐기 여부 결정 재료
- **방법**: 로컬 prod DB(`localhost:5432/auto_trader`)에 대해 두 MCP 도구의 실제 구현(`_get_market_news_impl`, `build_market_issues`)을 read-only로 직접 실행

---

## TL;DR

1. **이슈 전제와 달리 news-ingestor는 지금도 시간당 활발히 적재 중** (당일 18:52까지 kr/us/crypto-core 모두 success). 런북의 "paused" 표기와 실제 운영 상태가 불일치.
2. **데이터 신선도는 문제 없음.** KR 961건/7d, US ~3,500건/7d, crypto ~380건/7d.
3. **헤드라인 풀 품질은 유의미함** — KR 코스피 변동성/수급, US 매크로(CPI·ECB·Fed), crypto BTC/ETH 시장 뉴스.
4. **진짜 문제는 공급이 아니라 선별 품질**: US 라이프스타일 기사 혼입(plumber 기사가 Big Tech 섹션), 포함/제외 판정 어긋남(유의미 기사가 excluded), issues 하위 랭크의 단일기사 노이즈 클러스터.
5. 폐기하면 KR 시장 브리핑(현재 가장 품질 좋은 부분)과 **스냅샷 리포트 파이프라인의 news surface**(primary 소스 계약이 `news_ingestor`)도 공급이 끊김.

---

## 1. 공급 현황 (news_ingestion_runs / news_articles)

### 최근 ingestion runs (전부 success, 시간당)

| market | feed_set | status | started_at | inserted |
|---|---|---|---|---|
| us | us-core | success | 2026-06-10 18:52 | 27 |
| crypto | crypto-core | success | 2026-06-10 18:37 | 2 |
| kr | kr-core | success | 2026-06-10 18:07 | 17 |
| us | us-core | success | 2026-06-10 17:52 | 16 |
| crypto | crypto-core | success | 2026-06-10 17:37 | 2 |
| kr | kr-core | success | 2026-06-10 17:07 | 20 |

### feed_source별 적재량 (최근 7일 활성 소스)

| market | feed_source | 총 건수 | 최근 7일 | 최신 적재 |
|---|---|---|---|---|
| us | rss_yahoo_finance_topstories | 18,178 | 3,131 | 06-10 18:52 |
| us | rss_cnbc_us_markets | 1,454 | 247 | 06-10 18:52 |
| us | rss_marketwatch_topstories | 944 | 169 | 06-10 18:52 |
| kr | browser_naver_mainnews | 5,573 | 961 | 06-10 18:07 |
| kr | browser_naver_research_* (5종) | ~3,150 | ~380 | 06-10 15:07 |
| crypto | rss_coindesk | 886 | 142 | 06-10 18:37 |
| crypto | rss_cointelegraph | 921 | 115 | 06-10 17:37 |
| crypto | rss_decrypt | 575 | 87 | 06-10 18:37 |
| crypto | rss_bitcoin_magazine | 177 | 34 | 06-10 03:37 |
| us | rss_fed_press | 38 | 1 | 06-10 05:52 |

비활성(중단된) 소스: `research_on_demand_finnhub`/`research_on_demand_naver`(06-01 이후 0건 — ROB-491 전환분), `http_tvscreener_news_kr`, `browser_cnbc_news`, `browser_investing_news`(4월 이후 중단).

---

## 2. get_market_news 실출력 (hours=24, limit=10, briefing_filter=True)

### KR — 24h 전체 237건 → 포함 10 / 제외 17

**[장전 주요 뉴스] 8건**

| 시각 | 제목 |
|---|---|
| 18:40 | "수익률 대박나도 무섭다?" 이상한 사상 최고치…미국, 일본과 다른 한국 '공포지수' [투자360] |
| 18:24 | 하락 방어 움직임 커졌다…"코스피 추가 하락 경고등" |
| 18:19 | 코로나19·美 상호관세 쇼크…고비 때 요동친 코스피, 한달 뒤 '반등' |
| 17:53 | "내 주식 강제로 팔렸어요"… 개미, 롤러코스피 속 '반대매매' 공포 |
| 17:53 | 코스피 확 뛰어든 큰손들 … 집까지 팔아 삼전닉스 베팅 |
| 17:25 | 금융위기보다 잦은 사이드카…코스피, 레버리지 ETF가 부른 역대급 변동성 |
| 17:10 | 8% 급등→4.5% 급락에 시장 패닉…"변수 수두룩" 롤러코스피 언제까지 |
| 16:58 | "언제 볕들려나"...코스피 80% 오를 동안 엔터주는 '털썩' |

**[업종/테마] 2건**

| 시각 | 제목 |
|---|---|
| 18:36 | 美 주식 덜어내는 서학개미…반도체만 공격 베팅 |
| 18:21 | 美반도체 ETF 하락 베팅 급증…삼전·SK하닉에 불똥 튀나 |

**제외(노이즈 판정) 17건** — ⚠️ 유의미한데 빠진 것 다수:

- "스페이스X 담아라"… 국내 운용사 美우주항공ETF 눈치게임
- 급락·급등 반복에 커진 공포…VKOSPI 역대 최고 수준 ⚠️
- 고유가 덕에…'목재펠릿 공장' 펀드 EOD 문턱서 기사회생
- 개장 후 '시장가 매수'는 위험…무늬만 우주 ETF도 주의
- [단독] 집팔아 '삼전닉스' 들어간 야수들…생각보다 훨씬 더 많았다
- 암치료제 개발 뉴베일런트, GSK 품으로
- "中메모리 CXMT 상장땐 오히려 삼전 돋보일것" ⚠️
- AI인프라·전력·뷰티 차익실현…국민연금, 주도주 비중 줄였다 ⚠️
- 최고가 행진 꺾이자…금·은 ETF서 발뺀다
- 8% 급락 후 '빚투' 1700억 강제청산…'반대매매' 올들어 최고치 ⚠️
- '루나 악몽' 재현되나 … 스트레티지 주의보
- 내일은 '네 마녀의 날'…삼전·SK하닉 단일종목 레버리지, 변동성 커질까 ⚠️
- 한투PE, SKC 투자 1년만에 1200억 잭팟
- 젠슨 황의 선물…포털주 정체론 털어낸 네이버
- 급락·급등, 오늘은 급락…"추세추종 매매 열기 식고 있다"
- "상장 효과 없다" 스팩합병주 13곳 일제히 하락…절반 이상 '반토막'
- 메타버스 ETF 부활…기판주 올라타고 수익률 30%대 껑충

### US — 24h 전체 254건 → 포함 10 / 제외 11

**[Macro/Fed] 5건**

- Energy prices take center stage as the ECB prepares to decide on rates (17:25)
- China May wholesale inflation hits near 4-year high on Iran war, AI costs; CPI misses (10:57)
- The May inflation numbers are due out Wednesday morning. Here's what to expect (03:54)
- Sales of million-dollar homes suggest inflation is spurring the wealthy to buy now (05:00)
- Federal Reserve: annual bank stress test results June 24 (05:00, rss_fed_press)

**[Finance / Credit / Rates] 3건**

- Here's why shares in SoftBank, no longer Japan's most valuable, have fallen by a fifth in the last week (18:39)
- The SpaceX IPO could lead to 8% of America's current-account deficit being refinanced in a single day (17:07)
- The true national debt just hit $1 million per U.S. household (04:11)

**[Big Tech / AI / Semis] 2건**

- SoftBank sinks 10% as Asia tech stocks tumble, tracking Wall Street losses (15:18)
- ⚠️ **"My plumber charged $160 to fix a problem in my bathroom — but appears to have created another one. Do I pay again?"** (18:15) ← MarketWatch 라이프스타일 기사가 Big Tech 섹션에 포함됨 (대표적 필터 누수)

**제외 11건** — ⚠️ 오히려 유의미한 기사가 빠짐:

- Oil choppy after U.S. completes Iran strikes following Apache helicopter attack ⚠️
- Tech stocks dive as Friday's incoming SpaceX IPO creates 'bad psychology' ⚠️
- As SpaceX IPO anticipation heats up, what 2026's biggest IPOs say about investor demand
- Another jump in Boeing deliveries shows why we got into the stock
- 102-year-old fashion giant faces 400 store closures
- Buyer swoops in for actress Dakota Johnson's $6 million midcentury modern gem in L.A. (제외 타당)
- What next for BP? Leadership exits test investor confidence ⚠️
- Kalshi rolls out whistleblower services to curb insider trading
- GM follows Ford by making a big energy bet
- AST SpaceMobile's stock experiences rocky trading
- The weird reason why a team's World Cup loss can trigger a sharp drop in stock prices

### crypto — 24h 전체 60건 → 포함 10 / 제외 3

**[BTC/ETH Market] 10건**

- ETH crash to $1K looms if key support breaks: Will futures traders step in?
- 'Maximal' ban on insider trading would hurt prediction markets, says researcher
- Bitcoin ETFs are no bigger today than when Trump won the election
- Bitcoin and gold fall together as a rate-hike bet hits every hedge
- Seattle-Area Man Gets Prison for Laundering Foreign Fraud Funds With Bitcoin, Ethereum (저가치)
- Traditional Finance is Rushing Into Crypto as Institutions Buy Bitcoin's Dip
- SpaceX's pre-IPO market on Hyperliquid has fallen 27% in three weeks
- Live updates: What next for bitcoin as it faces headwinds from Fed rates
- XRP drops 4.5% as heavy selling breaks another support level
- A Quantum Clock Is Ticking for Bitcoin and Crypto—Here's How Stellar Is Preparing

**제외 3건**: EU Orders Meta to Open WhatsApp / Polymarket insider trading 재판 / Anthropic 모델 출시 — 제외 타당.

---

## 3. get_market_issues 실출력 (24h, limit=5)

### KR — 상위 클러스터 품질 좋음

| rank | 이슈 | 방향 | 기사/소스 | 대표 기사 |
|---|---|---|---|---|
| 1 | 삼성전자 | down | 11건/7곳 | 美반도체 ETF 하락 베팅 급증 / 中메모리 CXMT 상장땐 오히려 삼전 돋보일것 / 네 마녀의 날 변동성 |
| 2 | SK하이닉스 | down | 13건/9곳 | 집팔아 '삼전닉스' / 외인 삼전닉스 1.8조 순매도 / 레버리지 ETF 손익 비대칭 |
| 3 | LG전자 | neutral | 2건/2곳 | 40만원 넘보던 LG전자 5거래일간 43%↓ / 목표주가 40만원 |
| 4 | (단일기사: 공포지수 기사) | up | 1건/1곳 | ⚠️ 단일기사가 클러스터로 등재 |
| 5 | (단일기사: 우주항공ETF) | neutral | 1건/1곳 | ⚠️ 〃 |

### US — 상위 2개만 유의미, 3~5는 노이즈

| rank | 이슈 | 방향 | 기사/소스 | 비고 |
|---|---|---|---|---|
| 1 | Broadcom | up | 4건/3곳 | CEO pivot / Anthropic $35B capacity / 등급 상향 — 좋음 |
| 2 | Apple | neutral | 6건/5곳 | WWDC 이후 pullback — 좋음 |
| 3 | (단일기사: SoftBank) | neutral | 1건/1곳 | ⚠️ |
| 4 | (단일기사: plumber 기사) | neutral | 1건/1곳 | ⚠️⚠️ 라이프스타일 노이즈가 시장 이슈로 등재 |
| 5 | (단일기사: Dakota Johnson 부동산) | neutral | 1건/1곳 | ⚠️⚠️ 〃 |

### crypto — 상위 2개 좋음, 중복 클러스터 존재

| rank | 이슈 | 방향 | 기사/소스 | 비고 |
|---|---|---|---|---|
| 1 | Bitcoin | mixed | 20건/13곳 | rate-hike 영향, ETF 정체 — 좋음 |
| 2 | Ripple | neutral | 2건/2곳 | XRP 지지선 붕괴 — 좋음 |
| 3 | (단일: EU 러시아 제재 크립토 플랫폼) | neutral | 1건/1곳 | |
| 4 | (단일: 일본 3대은행 스테이블코인) | neutral | 1건/1곳 | ⚠️ 5번과 같은 뉴스 |
| 5 | (단일: 일본 3대은행 스테이블코인) | neutral | 1건/1곳 | ⚠️ 중복 클러스터 미병합 |

---

## 4. 평가 요약

| 항목 | 평가 |
|---|---|
| 데이터 신선도 | ✅ 문제 없음 — 시간당 적재 중 |
| KR 품질 | ✅ 유의미 (브리핑·이슈 클러스터 모두) |
| crypto 품질 | ✅ 유의미 |
| US 품질 | ⚠️ 절반 — 매크로 섹션 좋음, 라이프스타일 노이즈 혼입 + 유의미 기사 오제외 |
| 선별 로직 | ⚠️ 포함/제외 판정 어긋남(양방향), 단일기사 클러스터 노이즈, 중복 클러스터 미병합 |

**결론**: "유의미한 뉴스가 안 나온다" → 실측상 부정확. "유의미한 것과 노이즈가 섞여 나오는데 선별이 불완전하다"가 정확함.

---

## 5. 폐기 시 영향 범위 (코드 grounding)

- `get_market_news` + `get_market_issues` (`app/mcp_server/tooling/news_handlers.py`, `NEWS_TOOL_NAMES` 묶음) 제거
- ingest 엔드포인트: `POST /api/v1/news/ingest/bulk` (`app/routers/news_analysis.py:84`, 토큰 인증 `app/middleware/auth.py:54`), `/news/bulk`, `/news/analyze`
- `news_ingestion_runs` 모델/테이블 (`app/models/news.py:177`)
- **스냅샷 리포트 파이프라인 의존**: `invest_data_source_contract.py:173` — news surface의 **primary** 소스가 `news_ingestor`로 계약됨 (naver_finance는 supplementary). `investment_snapshots` 모델 CHECK 제약(`'news_ingestor'` 포함) + `source_kind_mapping.py:46`도 연동 → 폐기 시 계약 재지정 필요
- 외부: `robin-prefect-automations`의 `news_ingestor_kr_core` 등 flow 중지 필요 (현재 실제로 가동 중)
- ROB-491 `get_news`(종목 단위)는 독립 경로(`symbol_news_service`/`symbol_news_store`)라 무영향

## 6. 결정 옵션

| 옵션 | 내용 | 비용/리스크 |
|---|---|---|
| A. 유지 + 선별 품질 개선 | US 라이프스타일 피드 제외, 단일기사 클러스터 억제, 포함/제외 판정 보정 | 외부 ingestor 서비스 운영 부담 지속 |
| B. 폐기 | 도구 2종 + ingest 엔드포인트 + 부속 제거, 스냅샷 news 계약 재지정, 외부 flow 중지 | KR 브리핑(최고 품질 부분) 상실, 리포트 news surface 공급원 재설계 필요 |
| C. 폐기하되 on-demand 대체 | ingestor/엔드포인트 제거, get_market_news를 호출 시 Finnhub general news + 네이버 주요뉴스 직접 수집으로 전환 | 구현 비용 중간, 외부 서비스 의존 제거하면서 기능 보존 |
| D. 보류 | 본 실측을 Linear에 기록만 | 이슈 전제("ingestor 안 쓴다")와 실제 가동 상태 불일치 해소 필요 |
