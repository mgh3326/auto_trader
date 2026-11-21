# #5892 — 선물·옵션 시장 확장 조사

hk 5892 조사 문서다. 코드·계정 개설·주문은 범위 밖이고 이 문서만 바뀐다. 조사일은 2026-11-21, 기준 SHA는 `origin/main` `f23853b76`이다.

## 0. 읽는 법

- 모든 사실 주장에는 출처 링크가 있거나 `[unverified]`로 표시했다. 레포 내부 사실은 `경로:` 로 인용한다.
- **직접 확인**은 그 페이지를 이번 조사에서 읽었다는 뜻이다. **요약 기반**은 페이지 일부만 읽혔거나 검색 요약에 의존했다는 뜻이다. **제3자**는 블로그·뉴스다(공식 근거가 아니다).
- `[의견]`은 이 문서의 권고다. 운영자가 정해야 하는 것은 §9 번호 질문(Q1…)으로만 모았다.
- 계좌번호·자격증명·키 값은 어디에도 없다.
- 공식 문서 열람 한계: KIS 포털 API 상세 페이지는 렌더링되지 않았다(메뉴만 나옴). 그래서 KIS의 tr_id·경로는 공식 GitHub 샘플 코드에서 읽었다. README가 "샘플은 별도 공지 없이 업데이트될 수 있다"고 밝히므로 **공식 스펙이 아니라 샘플 코드**다. [출처](https://github.com/koreainvestment/open-trading-api)

## 1. 결론 요약

| 질문 | 답 |
|---|---|
| 지금 레포에 국내·해외 선물 연결이 있나 | **없다.** 선물 경로는 Binance USD-M **데모** 하나뿐이다(`app/services/brokers/binance/futures_demo/`, 호스트 `demo-fapi.binance.com`만 허용). |
| 국내 선물옵션 모의 API가 있나 | **KIS: 주간 주문만 모의 TR이 샘플 코드에 있다**(주문 `VTTO1101U`, 요약 기반). 야간 모의는 샘플 코드에 분기가 없다. 체결통보 WebSocket(`H0IFCNI0`)은 모의 지원 문구가 있다. |
| 해외선물 모의 API가 있나 | **확인 못 함.** KIS 공식 안내는 해외선물옵션 모의투자가 eFriend Plus(64Bit)에서만 제공된다고 한다. API 모의 여부는 `[unverified]`. |
| 키움 REST | 공식 API 목록에 선물옵션이 **없다**(국내주식·미국주식 범주만). |
| 그 밖에 이미 붙은 브로커 | NH PLUG 포털에 국내·해외 파생 TR 그룹이 **나열**되어 있으나 서비스 상태에 의심 신호가 있다. Toss는 주식 전용, Alpaca는 옵션만(선물 못 찾음). |
| 하드룰과의 충돌 | 새 실행 표면은 전부 새 env 게이트·호출별 confirm이 필요하고, 1계약 명목이 기존 모든 금액 상한을 넘을 가능성이 커서 **운영자 결정이 반드시 필요한 변경이 6가지**다(§6.8). |
| 권고 순서 | ① KIS 국내선물 **모의(주간)** → ② 해외선물은 읽기 전용·shadow → ③ Binance live는 별도 결정(§8). |

## 2. 베뉴별 조사

### 2.1 KIS 국내 선물·옵션

**접근 방식**

- 공식 GitHub 저장소에 국내선물옵션 예제가 있다. REST 기본 경로는 `/uapi/domestic-futureoption/v1/`이다. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_user/domestic_futureoption/domestic_futureoption_functions.py)
- 서버: 실전 `https://openapi.koreainvestment.com:9443`, 모의 `https://openapivts.koreainvestment.com:29443`, WebSocket 실전 `ws://ops.koreainvestment.com:21000`, 모의 `ws://ops.koreainvestment.com:31000`. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/kis_devlp.yaml) 레포의 `kis_mock_base_url` 기본값도 같은 모의 호스트다(`app/core/config.py:269`).
- 계좌: 샘플 설정은 국내선물옵션 계좌를 상품코드 `"03"`, 해외선물옵션 계좌를 `"08"`로 둔다. 주식 계좌와 **별도 계좌번호**를 쓴다. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/kis_devlp.yaml) 선물옵션 이용에 별도 신청이 필요한지는 `[unverified]`.
- 인증: 모의 토큰은 "1분당 1회 발급", 모의 REST 호출 제한은 "낮다"고 README가 밝힌다(초당 숫자는 없음). [출처](https://github.com/koreainvestment/open-trading-api)

**REST (직접 확인 = 샘플 코드에서 원문으로 읽음)**

| 기능 | 실전 tr_id | 모의 tr_id | 경로(`/uapi/domestic-futureoption/v1/` 이하) |
|---|---|---|---|
| 잔고현황 | `CTFO6118R` | `VTFO6118R` | `trading/inquire-balance` |
| 주문체결내역 | `TTTO5201R` | `VTTO5201R` | `trading/inquire-ccnl` |
| 기준일 체결내역 | `CTFO5139R` | 샘플에 없음 | `trading/inquire-ccnl-bstime` |
| 잔고정산손익 | `CTFO6117R` | 샘플에 없음 | `trading/inquire-balance-settlement-pl` |
| 잔고평가손익 | `CTFO6159R` | 샘플에 없음 | `trading/inquire-balance-valuation-pl` |
| 기간약정수수료 일별 | `CTFO6119R` | 샘플에 없음 | `trading/inquire-daily-amount-fee` |

출처: 위 샘플 코드. 아래 항목은 같은 파일의 **요약 기반**(원문 확인 필요, `[unverified]`):

| 기능 | tr_id |
|---|---|
| 주문 | 실전 주간 `TTTO1101U`, 실전 야간 `STTN1101U`, 모의 주간 `VTTO1101U` (모의 야간은 코드가 오류를 낸다고 요약됨) |
| 정정·취소 | 실전 주간 `TTTO1103U`, 실전 야간 `TTTN1103U`, 모의 주간 `VTTO1103U` |
| 주문가능 | `TTTO5105R` / `VTTO5105R`, 야간 `STTN5105R` |
| 예수금 | `CTRP6550R` |
| 야간 잔고·체결·증거금 | `CTFN6118R`, `STTN5201R`, `CTFN7107R` |

제3자 블로그도 주문이 `/uapi/domestic-futureoption/v1/trading/order`, 실전 주간 `TTTO1101U`이고 종목코드 필드가 `PDNO`가 아니라 `SHTN_PDNO`라고 적는다. [제3자](https://algolab.co.kr/blog/kis-domestic-futures-options-api-ttto1101u-2026) 주식 주문 코드를 복사해 쓰면 실패한다는 주의가 같이 있다.

토큰 규칙: 샘플 `kis_auth`는 모의일 때 `T`/`J`/`C`로 시작하는 tr_id를 `V`로 바꾼다. 야간 함수는 고정 `CTFN`/`STTN` ID이고 모의 분기가 없다. 모의에서 어떻게 동작하는지는 시험하지 않았다 `[unverified]`. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_user/kis_auth.py)

**WebSocket (직접 확인)** [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_user/domestic_futureoption/domestic_futureoption_functions_ws.py)

| 데이터 | TR | 모의 |
|---|---|---|
| 선물옵션 실시간 체결통보 | `H0IFCNI0` | "실전계좌와 모의투자 모두 지원" |
| KRX 야간선물 / 야간옵션 체결통보 | `H0MFCNI0` / `H0EUCNI0` | 언급 없음 |
| 지수선물 체결·호가 | `H0IFCNT0` / `H0IFASP0` | 실전만 |
| 지수옵션 체결·호가 | `H0IOCNT0` / `H0IOASP0` | 실전만 |
| 야간선물 체결·호가 | `H0MFCNT0` / `H0MFASP0` | 언급 없음 |

즉 **모의로는 체결통보만 받고, 호가·체결가 스트림은 실전 키로만 받는다.** 이 레포의 주식 체결 수집이 `H0STCNI0`/`H0STCNI9`를 쓰는 것(`docs/agent-contracts/broker-and-ledger-contracts.md` Execution Ledger HTTP Ingest 절)과 같은 방식으로 `H0IFCNI0`를 붙일 수 있다.

**호출 한도**: 실전·모의 모두 공식 초당 수치를 읽지 못했다 `[unverified]`. 샘플의 한 호가 함수가 "1초당 최대 1건 권장"이라고만 적는다.

**거래 시간·요건**: §3과 §4에 따로 정리했다.

### 2.2 KIS 해외 선물·옵션

- REST 경로 기본은 `/uapi/overseas-futureoption/v1/`. 샘플 코드에서 원문으로 읽은 것: 당일주문내역 `trading/inquire-ccld` `OTFM3116R`, 일별 주문내역 `inquire-daily-order` `OTFM3120R`, 일별 체결내역 `inquire-daily-ccld` `OTFM3122R`, 예수금 `inquire-deposit` `OTFM1411R`, 기간 손익 `inquire-period-ccld` `OTFM3118R`, 시세 `quotations/inquire-price` `HHDFC55010000`. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_user/overseas_futureoption/overseas_futureoption_functions.py)
- 요약 기반 `[unverified]`: 주문 `trading/order` `OTFM3001U`, 정정·취소 `order-rvsecncl` `OTFM3002U`/`OTFM3003U`, 주문가능 `inquire-psamount` `OTFM3304R`, 미결제 `inquire-unpd` `OTFM1412R`, 증거금 `margin-detail` `OTFM3115R`. 제3자 블로그도 주문 TR을 `OTFM3001U`로 적는다. [제3자](https://algolab.co.kr/blog/kis-overseas-futures-algotrading-guide-2026)
- WebSocket: 체결내역통보 `HDFFF2C0`, 주문내역통보 `HDFFF1C0`, 호가 `HDFFF010`, 체결가 `HDFFF020`. [출처](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_user/overseas_futureoption/overseas_futureoption_functions_ws.py)
- 시세 비용: 같은 파일이 "CME, SGX 실시간시세 유료시세 신청 필수"라고 쓴다. 신청하지 않으면 해당 종목 구독이 `SUBSCRIBE ERROR : mci send failed`로 실패하고, 신청 뒤 토큰 재발급이 필요하며 반영에 최대 2시간이 걸린다고 적혀 있다. 금액은 제3자 블로그가 CME 월 228.8달러라고 적는다 `[unverified]`. [제3자](https://algolab.co.kr/blog/overseas-futures-api-quote-fee-cme-2026)
- **모의**: KIS 공식 안내는 "해외선물옵션 모의투자는 eFriend Plus(64Bit)에서만 제공"이라고 한다. 초기 자금 USD 100,000 등, 주문 유형은 지정가·시장가만(Stop·Stop Limit 불가), 일일정산 뒤 추가증거금 입금 불가. 이 페이지는 API 모의 환경을 언급하지 않는다. [출처](https://securities.koreainvestment.com/main/research/virtual/_static/TF07db010000.jsp) 샘플 설정에 `my_paper_future` 항목은 있으나 API 모의가 실제로 동작하는지는 `[unverified]`.
- 거래소·상품 목록, 거래 시간, 증거금 금액, 수수료: KIS 공식 페이지에서 찾지 못했다 `[unverified]`.

### 2.3 키움증권

- REST API(`openapi.kiwoom.com`) 공식 가이드의 목록은 `[국내주식]`·`[미국주식]` 범주만 보인다. 선물옵션·해외선물 범주는 없다. [출처](https://openapi.kiwoom.com/guide/apiguide)
- 제3자 비교 글은 "REST API에 선물이 아예 없다"는 키움의 공식 답변을 인용한다 `[unverified, 제3자]`. [제3자](https://algolab.co.kr/blog/mini-kospi200-futures-api-broker-comparison-2026)
- 레거시 OpenAPI+(Windows COM) 개발 가이드가 선물옵션을 다루는지는 PDF 텍스트를 읽지 못해 `[unverified]`. 어쨌든 이 레포의 키움 연동은 REST 모의 호스트(`mockapi.kiwoom.com`) 전용이라(`docs/agent-contracts/broker-and-ledger-contracts.md` Kiwoom 절) COM 방식은 다른 아키텍처다.
- 결론: **키움은 선물·옵션 후보에서 제외한다.**

### 2.4 Binance USD-M 선물 — 라이브 vs 지금의 데모

레포 현재 상태 (`docs/agent-contracts/broker-and-ledger-contracts.md` USD-M Futures Demo 절, `docs/design/3796-crypto-trend-demo.md`):

- 실행 클라이언트는 `demo-fapi.binance.com`만 허용하고 `fapi.binance.com`·`testnet.binancefuture.com`은 transport에서 거부한다(fail-closed).
- 레버리지 1x(set_leverage echo 검증), one-way 모드, 심볼 allowlist, 격리 마진, `reduceOnly` 청산.
- TREND-TSMOM-v1은 BTC/ETH/SOL/XRP USDT, leg 60 USDT·동시 4개·gross 240 USDT 상수를 가진다(`constants.LOCKED_LIMITS`).
- 3796 설계는 "Binance 실전은 데모 통과 전략이 나오면 별도 결정으로 연다"고 못 박았다.

라이브와의 차이(공식 문서):

- 기본 URL: 라이브 REST `https://fapi.binance.com`, 데모 REST `https://demo-fapi.binance.com`, 데모 WS `wss://demo-fstream.binance.com`. 일반 정보 페이지가 "대부분의 엔드포인트를 테스트넷에서 쓸 수 있다"고 쓴다. [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) 같은 사이트의 거래·유저데이터 페이지에는 아직 옛 호스트 `testnet.binancefuture.com`이 적혀 있어 문서가 서로 어긋난다.
- 속도 제한: `exchangeInfo`의 `rateLimits`가 REQUEST_WEIGHT·ORDERS·RAW_REQUESTS를 알려 주고, 주문 한도는 계좌별·가중치 한도는 IP별이며 429 후 반복하면 418로 2분~3일 차단된다. 숫자 상한은 읽은 페이지에 없다 `[unverified]`. [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)
- 주문: `POST /fapi/v1/order`, 포지션 모드 `POST /fapi/v1/positionSide/dual`(진행 중 주문·포지션이 있으면 -4067/-4068 거부), `reduceOnly`는 헤지 모드에서 사용 불가. [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api)
- 체결 알림: `POST/PUT/DELETE /fapi/v1/listenKey`, 60분 안에 keepalive가 없으면 닫힌다. `ORDER_TRADE_UPDATE` 이벤트 필드와 유저 스트림 URL은 `[unverified]`. [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams)
- 503 "알 수 없는 오류"는 실행 상태 불명이며 실패로 취급하지 말라고 문서가 말한다(레포의 `uncertain` 규율과 같은 뜻). [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)
- 펀딩: `GET /fapi/v1/fundingRate`(이력), `fundingInfo`(예시 값 `fundingIntervalHours: 8`). 일반 8시간이라는 진술은 `[unverified]`. 청산 수수료율·`marketTakeBound` 필드는 exchangeInfo에 있다. [출처](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)
- 데모의 제약(심볼 집합, 데이터 초기화, 미지원 기능)·한국 거주자의 선물 이용 가능 여부: 공식 근거를 읽지 못했다 `[unverified]`. **라이브를 연다면 첫 확인 사항이다**(§9 Q6).
- 레포 안의 알려진 데모 한계: BTCUSDT는 MIN_NOTIONAL 50 > cap 10이라 제외됐다(`broker-and-ledger-contracts.md`).

### 2.5 이미 레포에 붙은 다른 브로커

| 브로커 | 파생 지원 | 근거 |
|---|---|---|
| NH PLUG (모의 Stage 2, `moapi.nhplug.com:8443`) | 포털 TR 목록에 "국내파생_주문/조회/시세/실시간", "해외파생_주문/조회/시세", 해외파생 체결통보가 **나열**된다. 모의 도메인 칸은 비어 있고, 같은 페이지에 "서비스 해지가 정상적으로 완료되었습니다" 배너가 있어 서비스 상태를 `[unverified]`로 둔다. | [출처](https://nhplug.com/apiservice) |
| 토스증권 | 국내·미국 **주식**만. 선물·옵션 항목 없음(OpenAPI JSON 본문은 읽지 못함). | [출처](https://developers.tossinvest.com/llms.txt) |
| Alpaca | 옵션 거래 문서가 있다. 읽은 범위에서 선물은 없다(나머지 목차는 `[unverified]`). 이 레포는 US 주식 paper만 쓴다. | [출처](https://docs.alpaca.markets) |
| Upbit | 현물 전용 `[unverified: 이번 조사에서 공식 문서에 접속하지 못함]`. | — |

NH PLUG의 레포 계약은 지정가·KRX·비SOR 모의 **주식** 주문에 고정이다(`AGENTS.md` 하드룰 14). 파생은 이 계약 밖이라 쓰려면 새 결정이 필요하다.

## 3. 거래 시간·휴장

| 시장 | 시간 | 출처 |
|---|---|---|
| KRX KOSPI200 선물·옵션 정규 | 08:45~15:45, 최종거래일 08:45~15:20 | [KRX 상품명세](https://open.krx.co.kr/contents/OPN/01/01040201/OPN01040201.jsp), [옵션](https://open.krx.co.kr/contents/OPN/01/01040202/OPN01040202.jsp) |
| KRX 야간 | 18:00~06:00(시가 단일가 17:50~18:00, 종가 단일가 05:50~06:00). 야간에는 1단계 가격제한(±8%)만 적용 | [미니옵션 명세](https://www.krx.co.kr/contents/OPN/01/01040205/OPN01040205.jsp) |
| 야간 개장 시점 | 2025-06-09 | [제3자 보도](https://www.fnnews.com/news/202506091108409820) |
| CME Globex(ES·MES 등) | 거의 24시간(일 1회 휴지) | `[unverified]` — CME 사양 페이지가 시간 초과로 열리지 않았다 |

- 휴장 예: 2025-12-31 18시 야간거래 휴장, 수능일(2026-11-19)에 파생 거래시간 임시 조정. 날짜별 확정은 KRX 공지를 봐야 한다. [제3자 검색 요약](https://www.samsungpop.com/ux/kor/customer/notice/notice/noticeViewContent.do?MenuSeqNo=23456) 야간 세션의 서머타임 예외는 KRX 가이드 PDF를 읽지 못해 `[unverified]`. [KRX 야간 가이드](https://open.krx.co.kr/contents/OPN/01/01041401/Guide_to_Night_Session_in_KRX_Derivatives_Market.pdf)
- 2026-09-14에 도입된 16~20시 주식 애프터마켓은 파생 야간과 별개다. [제3자](https://www.koreatimes.co.kr/southkorea/20260915/133-bil-traded-in-first-krx-after-hours-stock-trading-session)
- **레포 영향**: 야간 세션은 한 세션이 날짜를 넘긴다(18:00~다음날 06:00). 이 레포의 거래일 키(KR=KST 달력일, US=20:00 ET, 주문번호 재사용 규칙)와 달력(`exchange_calendars` XKRX는 주식 달력)은 파생 야간 세션을 모델링하지 않는다 `[의견·추정: 코드를 모두 읽지는 않음]`. 거래일 정의가 선행 설계 항목이다.

## 4. 계좌·자격 요건

| 항목 | 내용 | 출처 |
|---|---|---|
| 국내 파생 기본예탁금 | 선물(변동성지수 제외)·옵션 매수는 1천만원 이상, 그 외 모든 거래는 2천만원 이상(회사 설정). 전문투자자는 면제 | [금투협 표준 거래설명서](https://law.kofia.or.kr/service/law/lawFullScreenContent.do?seq=274&historySeq=1610) — 모델 서식이므로 증권사 약관·법령이 우선한다고 그 페이지가 밝힌다. 개정 이력은 확인 못 함 `[unverified]` |
| 국내 파생 사전교육·모의거래 | 최저 1시간 사전교육, 최저 3시간 모의거래(일반개인) | 같은 페이지 |
| 옵션 매도 등 | 변동성지수선물·옵션매도는 미결제약정 보유 10거래일 이상 경험이 필요 | 같은 페이지 |
| 해외 파생 사전교육·모의거래 | 2025-12-15 시행. 처음 거래하는 일반개인은 사전교육 1시간 이상·모의거래 3시간 이상(투자성향·연령별로 시간 증가) | [KIS 안내](https://securities.koreainvestment.com/main/research/virtual/_static/TF07db010000.jsp), [KB증권 공지](https://kbsec.com/go.able?idt=20251114&linkcd=s060901010000&seq=10009222), [제3자 보도](https://v.daum.net/v/20251116120248347) |
| 해외 파생 기본예탁금 | 찾지 못함 | `[unverified]` |
| 증거금 | 상품·증권사별. KIS는 증거금·주문가능을 TR로 조회한다(국내 `inquire-psbl-order`, 해외 `margin-detail`, 둘 다 요약 기반). 금액 `[unverified]` | §2 |
| 세금 | 국세청은 파생상품을 별도 과세 대상으로 두고 연 1회 5월 확정신고(예정신고 없음)라고 안내한다. **세율·공제액은 읽지 못했다** `[unverified]` | [국세청](https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=2308&cntntsId=7707) — 요약 기반 |
| 한국 거주자 Binance 선물 | `[unverified]` | — |

계좌 개설·교육 이수는 운영자 본인의 일이며 이 조사 범위 밖이다(Q2).

## 5. 고려할 상품

| 상품 | 계약 사양 | 출처·상태 |
|---|---|---|
| KOSPI200 선물 | 승수 25만원, 호가 0.05pt = 12,500원, 결제월 3·6·9·12월(3년 이내 7개), 최종거래일 = 결제월의 둘째 목요일, 현금결제, 가격제한 ±8/15/20% 단계, 개인 미결제약정 10,000계약 | [KRX](https://open.krx.co.kr/contents/OPN/01/01040201/OPN01040201.jsp) — 요약 기반 |
| 미니 KOSPI200 선물 | 승수 5만원(2017-03 인하). 호가·결제월 규칙은 미니 선물 페이지(`01040204`)를 읽지 못했다 | [금융위 자료](https://www.fsc.go.kr/in090301/view?dicId=1790&curPage=14)(승수), 나머지 `[unverified]` |
| KOSPI200 옵션 | 승수 25만원, 프리미엄 10pt 미만 0.01pt(2,500원)·이상 0.05pt(12,500원), 유럽식(최종거래일만 행사), 현금결제, 개인 10,000 델타 | [KRX](https://open.krx.co.kr/contents/OPN/01/01040202/OPN01040202.jsp) — 직접 확인 |
| 위클리옵션 | 월요일·목요일 만기가 매주 상장(둘째 목요일 제외), 행사가 ATM±120pt·2.5pt 간격 | 같은 페이지. 별도 증거금·거래 규칙은 `[unverified]` |
| 미니 KOSPI200 옵션 | 승수 5만원, 호가 0.01/0.02/0.05pt(500/1,000/2,500원) | [KRX](https://www.krx.co.kr/contents/OPN/01/01040205/OPN01040205.jsp) — 직접 확인 |
| 마이크로 KOSPI200 | KRX 페이지에서 찾지 못함 | `[unverified]` |
| E-mini S&P 500 (ES) | 지수 × $50, 틱 0.25pt = $12.50, 3·6·9·12월 셋째 금요일 만기, 현금결제 | [CME](https://www.cmegroup.com/markets/equities/sp/e-mini-sandp500.html) — 검색 요약 기반 |
| Micro E-mini S&P 500 (MES) | 지수 × $5, 틱 0.25pt = $1.25, 같은 분기 주기 | [CME](https://www.cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.html) — 검색 요약 기반 |
| 나스닥100 E-mini/Micro (NQ/MNQ) | 사양 읽지 못함 | `[unverified]` |
| 코인 무기한 계약 | USDT 정산, 펀딩이 주기적으로 정산, 롤오버 없음. 최소 notional 등은 exchangeInfo | §2.4 |

롤오버·만기: 지수선물과 옵션은 만기가 있다. 국내는 최종거래일이 둘째 목요일이고 그날은 15:20에 거래가 끝난다(KRX 위 페이지). 포지션을 유지하려면 만기 전에 다음 결제월로 넘겨야 한다. 코인 무기한 계약은 만기가 없다.

명목 가치 예시(**가정**): KOSPI200 지수가 100pt 이상이라면 미니 1계약의 명목은 100 × 50,000 = 5,000,000원 이상이고, 일반 선물 1계약은 25,000,000원 이상이다. 현재 지수 수준은 이번 조사에서 읽지 않았다 `[가정]`.

## 6. 이 레포의 규칙과 부딪히는 곳

전부 현재 `main`의 계약 문서 기준이다. **굵은 "운영자 결정"** 표시는 AGENTS.md가 멈추고 운영자에게 보고하라고 정한 종류의 변경이다.

### 6.1 하드룰 4 — 브로커 실행 표면은 전부 default-disabled

- 새 베뉴의 주문 경로는 env 게이트(예: `KIS_FUTURES_MOCK_ENABLED` 같은 새 이름)와 호출별 `confirm=True`의 이중 게이트를 가져야 한다. 기본 off.
- Binance 라이브(`fapi.binance.com`)는 transport가 데모 호스트만 허용하는 fail-closed다. 라이브를 열려면 **호스트 allowlist 확장**이 필요하다 → **운영자 결정**(하드룰 4: "호스트 allowlist 확장 … 금지, 필요해 보이면 멈추고 보고"). 3796 설계도 "별도 결정"이라 했다.
- 하드 인바리언트 상수(레버리지 1x, notional cap, 동시 포지션 상한)는 올리지 않는다. Binance 선물 경로의 상수는 그대로 두고 새 경로는 **새 locked 상수**로 정의한다(3796 §1.3 방식). 값은 운영자가 정한다.
- 국내선물은 1x 개념이 없다 — 증거금 거래라 레버리지가 상품·지수 수준에서 나온다. "1x"를 그대로 이식할 수 없고 계약 수·명목 상한으로 대신 표현해야 한다 → **운영자 결정**(Q3).

### 6.2 금액 상한이 1계약을 못 담는다

기존 상한: kis_mock 단건 `max_single_notional_krw = 5,000,000`(`app/services/kis_mock_runner/envelope.py:51`), Upbit 위임장 `per_intent_max_notional_krw = 480,000`(`docs/design/5438-go-execution-path.md` D9), Binance 선물 leg 60 USDT. §5의 가정대로면 미니 선물 1계약(≥ 5,000,000원)은 첫 값과 같거나 그 이상이고 나머지는 훨씬 넘는다. 즉 **기존 상한을 그대로 재사용할 수 없고** 선물 전용 상한(계약 수 + 명목)을 새로 정해야 한다 → **운영자 결정**(Q3). 새 상한을 정할 때 기존 값은 건드리지 않는다.

### 6.3 손실매도 가드·평단 개념

손실매도 가드(`매도 지정가 < 평단 × 1.01`이면 카드)는 "매도 = 보유 청산"이라는 현물 전제에 묶여 있다. 선물은 **매도가 신규 진입(숏)** 일 수 있고 평단·FIFO lot·일일정산(marked-to-market) 개념이 다르다. 가드를 그대로 쓰면 모든 숏 진입이 오작동하고, 면제하면 가드를 완화하는 것이다. 포지션 방향을 인식하는 별도 가드를 만들지, 선물 전용 승인 카드를 쓸지 → **운영자 결정**(Q4). 가드 완화는 어떤 경우에도 암묵적으로 하지 않는다.

### 6.4 원장·대사 (하드룰 5, 16)

- 주문 원장은 서비스 레이어 경유만. 베뉴마다 새 원장이 필요하다 — Binance 데모가 `binance_demo_order_ledger`를 따로 둔 선례(`broker-and-ledger-contracts.md`)와 같다. KIS 국내 선물은 `kis_futures_*` 계열 신규 원장 + 새 writer 서비스가 필요하다 `[의견]`.
- `execution_ledger`는 지금 선물·옵션을 담을 수 없다: `broker IN ('kis','upbit','toss','kiwoom')`(Binance도 아직 없음), `currency IN ('KRW','USD')`(USDT 불가), `instrument_type`에 future/option이 없다(`app/models/execution_ledger.py:58-66`, `app/models/trading.py:19-24`). 이 제약에 값을 더하는 것은 hk 4734(Kiwoom) 선례처럼 **운영자 결정이 있어야 하는** 추가형 마이그레이션이다(하드룰 16). 가장 안전한 시작은 원장을 분리해 두는 것이다.
- 가격·수량 모델: 승수·틱·만기·결제월·일일정산(손익이 매일 현금으로 정산됨)·증거금이 필요하다. 현재의 FIFO lot·평단 모델(`app/services/execution_ledger/kis_lots.py` 등)은 이를 담지 못한다.
- 체결 증거는 evidence-first를 유지한다. 체결통보 WebSocket(`H0IFCNI0`)은 provisional이고 REST 체결내역(`TTTO5201R` 계열)이 대사 권한자가 되는 구조가 레포 관행과 맞다.

### 6.5 kis_mock 귀속 사슬의 유사물 (하드룰 7)

`kis_mock`·`kiwoom_mock`은 브로커 전송 **이전에** 신호 행(strategy·correlation_id·signal_source NOT NULL)이 커밋돼야 하고 실패하면 주문이 나가지 않는다. 선물 모의에도 같은 형태가 필요하다: `review.kis_futures_mock_signal_ledger`(가칭), 같은 `MissingAttribution`/placeholder 금지 규칙, 송신 전 `order_send_intents` 예약(`account_scope`에 새 값). Kiwoom이 hk 4723에서 한 방식 그대로이며 새로 설계할 것이 많지 않다. **게이트를 완화하거나 placeholder strategy를 만들지 않는다.**

### 6.6 심볼 (하드룰 8)

선물 종목코드(KIS는 `SHTN_PDNO`)와 결제월 표기는 `app/core/symbol.py`의 `.` 구분 주식 규칙과 무관하다. 파생 심볼 변환·만기 파싱은 같은 모듈에 추가해야 한다(직접 문자열 치환 금지) `[의견]`.

### 6.7 스케줄러·승인

- 신규 스케줄 등록은 명시 승인 전까지 금지(하드룰 6): 모든 CLI는 scheduleless로 낸다.
- 만기 전 롤오버 알림·포지션 점검 등은 사람 카드 또는 수동 CLI로 시작한다.
- 승인 창(`approval_window`)·`order_proposals`의 계좌·시장 조합은 주식 정규장 기준이다. 선물을 제안 경로로 태우려면 새 (account_mode, market) 조합과 파생 세션 캘린더가 필요하다 → 별도 설계.

### 6.8 이 변경들 중 운영자 결정이 필요한 것

1. 새 실행 표면의 허용 자체(국내선물 모의·해외선물·Binance 라이브) — 하드룰 4
2. Binance 라이브 호스트 allowlist 확장 — 하드룰 4
3. 선물 전용 상한 상수(계약 수·명목·동시 포지션) — 하드룰 4
4. 손실매도 가드의 선물 의미(방향 인식 or 카드) — 가드 완화 여부
5. `execution_ledger`에 broker·instrument·currency 값을 더할지, 원장을 분리할지 — 하드룰 5·16
6. 야간 세션을 거래일·승인 창에 어떻게 넣을지(및 야간 모의 미지원 시 야간 live를 허용할지)

## 7. 목표 아키텍처에서의 위치

기준 설계: `docs/design/5438-go-execution-path.md`(Python은 **무엇을**·인가·상한, Go는 **언제·얼마에**를 그 안에서) 및 hk 5848(fillwire를 venueport로: 실행 파일 하나·하위 명령·`serve`, 5848(d)가 이름 변경). 이 문서는 hk 5848의 하위 티켓 제목만 읽었고 venueport 내부 코드는 읽지 않았다 `[unverified]`.

| 책임 | 위치 | 이유 |
|---|---|---|
| 체결통보 WebSocket 수집(`H0IFCNI0`, `HDFFF2C0`, Binance `listenKey`) | **venueport** | fillwire가 이미 KIS 체결·Upbit 호가를 기록하는 곳이다. 수집한 프레임을 auto_trader로 넘기는 경로는 **T2가 원장을 고른 뒤에 정한다**. 기존 ingest 라우트(`/trading/api/execution-ledger/fills/ingest`)는 `ExecutionLedgerRepository`로 `execution_ledger`에만 쓰고 그 테이블은 선물을 담지 못하므로(§6.4), 분리 원장(Q5)을 고르면 그대로는 못 쓴다. 이 경우 별도 ingest 라우트와 새 원장 writer 서비스가 필요하다(같은 토큰 인증·멱등키·항목별 savepoint·`source` 강제 규칙을 복사). `execution_ledger`를 확장하는 쪽을 고르면 기존 라우트를 쓰되 broker·instrument_type·currency CHECK 확장(하드룰 16 예외 결정)이 선행한다. |
| 시세·호가 읽기(야간 포함) | **venueport** | 모의에서는 호가 WS가 실전 키만 지원하므로 키 분리·소유자가 먼저 정해져야 한다. |
| 주문 전송·한도·kill·위임장·귀속·원장 | **auto_trader (authority core)** | 5438이 Upbit에서 정한 형태: 게이트웨이가 위임장·kill·상한을 검사하고 forwarder가 주문 서비스를 호출한다. 새 베뉴는 `*_forwarder` 하나씩 추가한다. |
| 타이밍·지정가 추격 | 1단계는 없음, 이후 **venueport** 실행기 | 5438 D11(추격은 1단계에서 제외)과 같은 이유. |
| 만기·롤오버 판단 | **auto_trader** 전략 쪽(결정 잡) | 의도(intent)를 만드는 쪽이 계약 만기를 알아야 한다. |

원칙: 브로커 호출이 닿는 모든 경로는 auto_trader의 게이트웨이·원장 뒤에 둔다. venueport는 읽기·수집에서 시작하고, 주문 권한은 모의에서 한 번 검증된 뒤에만 별도 결정으로 준다 `[의견]`.

## 8. 권고 순서와 첫 티켓

### 8.1 순서 — 모의 먼저, 베뉴별

| 단계 | 대상 | 이유 |
|---|---|---|
| 0 | Binance USD-M **데모** 계속(TREND-TSMOM-v1). 라이브 금지 | 이미 가동 중이고 3796이 "데모 통과 후 별도 결정"이라고 정했다. |
| 1 | **KIS 국내 선물 모의(주간)** | 모의 주문·정정취소·체결조회·체결통보 TR이 샘플 코드에 있다(§2.1). 레포는 KIS 모의 계좌·귀속 사슬 경험이 가장 많다. |
| 2 | KIS 국내 선물 실전 **읽기 전용**(잔고·증거금) → 별도 결정 후 라이브 | 모의 야간·호가 스트림이 없어 모의와 실전이 다르기 때문. |
| 3 | KIS 해외선물: **읽기 전용 + shadow**. API 모의가 확인될 때까지 주문 금지 | 공식 모의는 HTS뿐(§2.2). 유료 시세 신청이 필요하다. |
| 4 | Binance 라이브(별도 결정) | 호스트 allowlist 변경 + 한국 거주자 가능 여부 확인이 선행. |
| 제외 | 키움 | REST에 선물 없음(§2.3). |
| 보류 | NH PLUG | 파생 TR은 나열돼 있으나 서비스 상태 배너 확인 필요(§2.5). |

### 8.2 첫 번째 트레이서 불릿 티켓 (순서대로, 각각 독립 머지 가능한 PR 하나씩)

| # | 티켓 | 내용 | Acceptance(요지) | 정지·결정 필요 |
|---|---|---|---|---|
| T1 | KIS 선물 **읽기 어댑터** (설계 포함, 주문 없음) | 계좌 상품코드 `03` 지원, 잔고·예수금·주문가능·종목마스터 읽기, 읽기 전용 view로 주문 TR 도달 불가(AST 테스트, data_plane `broker_adapters.py` 패턴) | 모의 계좌로 읽기 1회 라운드트립(운영자 수동), 주문 TR 경로 부재 테스트 | 새 env 게이트(읽기용)만 |
| T2 | 파생 상품·계좌 모델 설계 문서 | 승수·틱·결제월·일일정산·증거금, 거래일 정의(야간), 원장 분리안 | Q3·Q4·Q5의 운영자 결정이 문서에 기록된 뒤 승인 | **운영자 결정 §6.8 #3~#6** |
| T3 | KIS 선물 **모의 주문 경로** (주간, 지정가, 1계약) | `VTTO1101U`/`VTTO1103U`, 귀속 사슬, 송신 전 예약, 신규 원장·서비스, 기본 off 이중 게이트, 스케줄 없음 | 모의 주문 1건 왕복(운영자 수동 스모크), uncertain은 재전송하지 않음, 가드 우회 없음 | T2의 결정, 하드룰 4 |
| T4 | 체결통보 수집 (`H0IFCNI0`) | venueport 읽기 하위 명령 + T2가 고른 원장의 ingest 경로. 분리 원장이면 **새 ingest 라우트·writer 서비스·토큰 미들웨어 항목**을 이 티켓에 포함(기존 라우트는 `execution_ledger` 전용이라 그대로 못 씀), `execution_ledger` 확장이면 기존 라우트 + CHECK 마이그레이션(T2의 결정 뒤) | 모의 체결이 **선택된 원장**에 같은 payload 재전송 시 `unchanged` 멱등으로 들어감(기존 `(broker, account_mode, venue, broker_order_id, fill_seq)` 키 규칙 유지) | T2의 원장 결정(§6.8 #5) |
| T5 | 선물 **대사**(reconcile) | `TTTO5201R`/`VTTO5201R` 기반 terminal-only → 이후 full, 기존 대사 게이트 모델 재사용 | terminal-only 먼저, 체결 부킹은 별도 게이트 | 하드룰 4 |
| T6 | 해외선물 읽기 + shadow | `OTFM*` 읽기 TR, 유료 시세 신청 여부 결정 후 호가 | 값이 없으면 null+사유(0 금지) | 시세 비용 Q9 |
| T7 | Binance 라이브 사전 점검(문서만) | 한국 거주자 이용 가능, 호스트 allowlist 변경안, 라이브 전용 상수·kill | Q6의 결정이 hk doc에 있어야 구현 티켓 생성 | **하드룰 4** |

모든 PR은 스케줄러 등록 없이 낸다.

## 9. 운영자에게 묻는 것 (권고 포함)

| Q | 질문 | 권고 |
|---|---|---|
| Q1 | 첫 번째 확장 베뉴는? | **KIS 국내 선물 모의(주간)** — §8.1 단계 1. |
| Q2 | 국내·해외 파생 계좌 개설과 사전교육(1시간)·모의거래(3시간) 이수는 누가 언제? | 운영자 본인. 이수 전에는 T1(읽기)·모의 계좌 신청만 진행. 현재 요건·예탁금은 `[unverified]` 항목이 있으니 증권사 최신 공지를 먼저 확인. |
| Q3 | 선물 전용 상한 상수는? (최대 계약 수, 1계약 명목 상한, 동시 포지션, 일일 손실 한도) | 금액은 운영자가 정한다. 시작은 **최소 단위 1계약·동시 1포지션**으로 두고, 값은 T2 문서에서 확정. 기존 상한은 건드리지 않는다. |
| Q4 | 손실매도 가드의 선물 의미 | **선물 전용 방향 인식 가드를 새로 만들고** 기존 가드는 불변. 신규 진입(매도 숏)은 가드 대상이 아니고, 청산 가격이 진입가 기준 미달이면 사람 카드로 보낸다. 값은 운영자. |
| Q5 | 원장 | **분리 원장**(베뉴별 신규 테이블)으로 시작. 이 경우 체결 ingest도 기존 `execution_ledger` 전용 라우트를 못 쓰므로 별도 ingest 경로가 필요하다(T4). `execution_ledger` 확장은 선물이 모의에서 증명된 뒤 별도 결정(하드룰 16 예외 형식). |
| Q6 | Binance 라이브 | **지금은 열지 않는다.** 한국 거주자 이용 가능 여부와 데모 대 라이브 차이(공식 근거)를 먼저 확인하고, 호스트 allowlist 확장은 별도 결정으로. |
| Q7 | 야간 세션 | **1단계 제외.** 모의 야간이 없고(요약 기반) 거래일 정의가 날짜를 넘긴다. |
| Q8 | 옵션 | **선물만 먼저.** 옵션 매도는 무한 손실이며 매도 자격에 경험 요건이 있다. 옵션 매수는 이후 단계. |
| Q9 | 해외선물 시세 유료 신청(CME 월 228.8달러는 제3자 수치) | shadow에 쓸 데이터가 필요해질 때까지 **신청하지 않는다.** |
| Q10 | NH PLUG 파생 | KIS 모의가 막힐 때의 대안으로만. 서비스 상태(해지 배너)를 먼저 확인. |
| Q11 | 롤오버 | **자동 롤오버 없음.** 최종거래일 전에 포지션을 닫고, 다음 결제월은 새 의도로 연다. |
| Q12 | 파생 달력 | `exchange_calendars`에 파생 세션이 없으면(`[unverified]`) KRX 공지 기반 달력을 새로 두되, 알 수 없는 날은 거래 불가로 처리(fail-closed). |

## 10. 확인하지 못한 것 (후속 조사 대상)

- KIS 포털 공식 TR 명세(경로·tr_id·모의 여부·호출 한도): 렌더링 실패. 샘플 코드 요약본의 TR들은 코드 원문으로 재확인 필요.
- KIS 선물옵션 API 신청 절차, 증거금률·수수료, 해외 거래소·상품 목록·거래 시간.
- KRX 미니 선물 사양, 마이크로 선물 존재 여부, 증거금률, 야간 가이드(PDF 판독 실패).
- CME 사양 페이지(시간 초과)와 거래 시간.
- Binance: 레버리지·마진 타입 엔드포인트 원문, `ORDER_TRADE_UPDATE` 필드, 유저 스트림 URL, 데모 제약, 주문 속도 한도 숫자, 한국 거주자 이용.
- 국내·해외 파생상품 양도소득세율·공제.
- 기본예탁금의 최근 개정 이력.
- NH PLUG 서비스 상태, 키움 OpenAPI+의 파생 지원.

## 11. 알았지만 건드리지 않은 것

- Binance 개발자 문서 안에서 데모 호스트가 페이지마다 다르게 적혀 있다(`demo-fapi` vs `testnet.binancefuture.com`). 레포는 `demo-fapi`만 허용하므로 영향은 없다.
- `execution_ledger_reconcile_runs`의 `broker` CHECK는 `('kis','upbit','kiwoom')`이고 `execution_ledger`는 `('kis','upbit','toss','kiwoom')`로 서로 다르다(`app/models/execution_ledger.py:58,188`). 이 조사와 무관하다.
