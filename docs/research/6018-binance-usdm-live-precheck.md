# #6018 — Binance USD-M 선물 실계좌 사전 점검

hk 6018 조사 문서다. **문서만 바뀐다** — 코드·env·allowlist·주문·계좌 설정은 전부 범위 밖이며,
이 문서는 라이브 승인이 아니다. 상위 조사는 `docs/research/5892-futures-options-survey.md`
§8.2 T7·§9 Q6("Binance 라이브: 지금은 열지 않는다. 한국 거주자 이용 가능 여부와 데모 대
라이브 차이(공식 근거)를 먼저 확인하고, 호스트 allowlist 확장은 별도 결정으로"). 조사일은
2026-11-21, 기준 SHA는 `origin/main` `bedafdf4e`다.

## 0. 읽는 법

- 5892와 같은 표기: 모든 사실 주장에 출처 링크가 있거나 `[unverified]` 표시. 레포 내부 사실은 `경로:`로 인용.
- **직접 확인**은 이번 조사(또는 5892 조사에서 이미 원문 열람 확인으로 기록된 것)에서 해당
  페이지를 읽었다는 뜻. **요약 기반**은 검색 결과 요약·페이지 일부만 읽힌 경우. **제3자**는
  블로그·포럼 등 비공식 근거다.
- Binance 공식 페이지는 이번 조사에서 본문이 렌더링되지 않는 경우가 많았다. 읽지 못한
  페이지는 전부 `[unverified]`로 둔다 — 추측으로 메우지 않는다.
- 계좌번호·자격증명·키 값은 어디에도 없다.

## 1. 한국 거주자 이용 가능 여부

**결론부터: `[unverified]`.** 읽을 수 있었던 공식 근거 어디에도 대한민국을 USD-M 선물에서
명시적으로 금지하는 항목은 찾지 못했지만, 금지 국가 목록의 정본 페이지를 이번 조사에서
열지 못했다. 라이브를 여는 결정(§5 #1) 전에 운영자가 직접 확인해야 한다.

| 근거 | 읽힌 내용 | 상태 |
|---|---|---|
| [Binance — Licenses, Registrations and Other Legal Matters](https://www.binance.com/en/legal/licenses) | "Restricted Jurisdictions" 절이 **List of Prohibited Countries** 와 Binance Terms of Use를 자격 요건의 정본으로 가리킨다. 남아공에서 선물·옵션을 제공하는 그룹 라이선스가 별도 기재돼 있다. | 직접 확인(2026-11-21) |
| [Binance — List of Prohibited Countries](https://www.binance.com/en/legal/list-of-prohibited-countries) | **본문을 읽지 못했다.** 이 목록이 국가별 금지의 정본이다. | `[unverified]` |
| [Binance — Terms of Use](https://www.binance.com/en/terms) | **본문을 읽지 못했다.** | `[unverified]` |
| [Binance — Country/Region Selector](https://www.binance.com/en/country-region-selector) | 지원 국가 100+ 목록(아시아·태평양 14개 포함)에 대한민국이 없다. 단, 이 셀렉터는 현지화 지원 국가 목록이지 **이용 금지 목록이 아니다** — 한국 미기재만으로 가능·불가능을 판정할 수 없다. | 직접 확인(2026-11-21) |
| [Binance 공지 — Unavailability of Futures and Derivatives in Germany, Italy, and the Netherlands](https://www.binance.com/en/support/announcement/unavailability-of-futures-and-derivatives-products-in-germany-italy-and-the-netherlands-f72d0fff2bd24b488213fa30284dd4cd?hl=en) | 2021-07-30 공지. 독일·이탈리아·네덜란드에서 선물·파생 신규 계좌를 닫고 기존 포지션은 90일 안에 정리하도록 했다. **선물 상품을 국가 단위로 닫은 공식 선례** — 같은 형식의 한국 공지는 이번 조사에서 찾지 못했다. | 직접 확인(2026-11-21) |
| Binance Square 글(제3자, 2025-08 기준 선물 불가 국가 목록) | 불가 국가 목록에 대한민국이 **없다**(북미·유럽 다수·아시아 일부·중동·아프리카·남미 일부 열거). | 제3자 — 공식 근거 아님 |

- **국내 법·세무 쟁점은 별개다.** 해외 가상자산 파생 이용에 대한 국내 규제 위치와 과세는
  이 문서의 판단 범위 밖이며 법률·세무 자문 대상이다 `[unverified]`. 5892 §4의
  "한국 거주자 Binance 선물 — `[unverified]`" 행을 그대로 유지한다.
- **확인 절차(권고)**: (a) 위 List of Prohibited Countries 최신본에서 대한민국 유무,
  (b) 본인 Binance 계정의 선물 활성화 UI가 지역 사유로 거부하는지, (c) Binance 코리아
  이용 관련 최신 이용약관·공지 — 셋을 모두 확인한 뒤에만 §3의 구현 티켓을 만든다.
  이 중 하나라도 제한 신호면 라이브는 열지 않는다.

## 2. 데모 대 라이브 차이 (공식 문서 기준)

레포의 데모 경로는 `app/services/brokers/binance/futures_demo/` + `demo-fapi.binance.com`
전용이다. 아래 차이 표의 "데모" 열은 레포 구현 기준이고 "라이브" 열은 공식 문서 기준이다.

| 항목 | 데모 (레포 구현) | 라이브 (공식 문서) | 근거·상태 |
|---|---|---|---|
| REST base URL | `https://demo-fapi.binance.com` (`constants.DEMO_URL`, `futures_demo/host_allowlist.py`) | `https://fapi.binance.com` | [general-info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) — 5892에서 직접 확인 |
| WS (유저 스트림) | `wss://demo-fstream.binance.com` (데모 WS, 서명 listenKey 필요) | `wss://fstream.binance.com` — 레포의 `PUBLIC_FUTURES_STREAM_HOSTS`는 이 호스트의 **unsigned market data만** 허용 | 같은 페이지(5892 직접 확인) + `app/services/brokers/binance/host_allowlist.py` |
| 문서 불일치 | — | 개발자 문서 일부 페이지가 아직 구 testnet `testnet.binancefuture.com`을 적는다. 어느 쪽이든 레포는 둘 다 거부한다 | 5892 §2.4·§11 |
| API 키 | 데모 사이트 발급 키(`BINANCE_FUTURES_DEMO_API_KEY`/`_SECRET_KEY`), 데모 계정과 쌍 | 라이브 키는 라이브 계정에서 발급·선물 권한 별도 부여·IP 제한 설정 가능 — 세부 절차는 이번 조사에서 공식 페이지를 못 읽었다 | `[unverified]` |
| 속도 제한 | 데모는 레포의 호출 빈도만으로 회피했고 한도 수치 미확인 | `exchangeInfo`의 `rateLimits`(REQUEST_WEIGHT는 IP별, ORDERS는 계정별, RAW_REQUESTS). 429 반복 위반 시 418으로 2분~3일 차단. 숫자 상한 `[unverified]` | [general-info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) — 5892 직접 확인 |
| 주문 경로 | `POST /fapi/v1/order` 동일 엔드포인트, 데모 호스트로만 전송 | 같은 경로의 라이브 호스트. `POST /fapi/v1/positionSide/dual`로 포지션 모드 변경은 진행 중 주문·포지션 있으면 -4067/-4068 거부. hedge 모드에서는 `reduceOnly` 사용 불가 | [trade/rest-api](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api) — 5892 직접 확인 |
| 체결 알림 | 데모에서 같은 listenKey 경로 사용 | `POST/PUT/DELETE /fapi/v1/listenKey`, 60분 안에 keepalive 없으면 닫힘. `ORDER_TRADE_UPDATE` 필드 세부 `[unverified]` | [user-data-streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams) — 5892 직접 확인 |
| 오류 의미 | 레포는 브로커 결과 불명을 `uncertain`으로만 기록하고 재전송·자동 취소·자동 청산 금지 | 공식 문서도 503 "unknown error"는 **실행 상태 불명이며 실패로 취급하지 말라**고 명시 — 레포 `uncertain` 규율과 동일한 판정 | [general-info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) — 5892 직접 확인 |
| 펀딩 | 데모도 funding을 시뮬레이션(데모 잔고에만 반영) | `GET /fapi/v1/fundingRate`(이력), `fundingInfo`(`fundingIntervalHours` 예시 8). **라이브는 실제 자금으로 펀딩 비용이 누적** — 보유 시간 비용이 데모보다 실질적으로 커진다 | [exchange-information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information) — 5892 직접 확인 |
| 수수료 | 레포 데모 모델은 taker 5bp 고정(`TAKER_FEE_RATE = 0.0005`, `app/services/brokers/binance/trend/constants.py:37` — trend/H5 상태가 `signal.fees_usdt`에 적용) | 실제 수수료율은 계정 등급·BNB 할인에 따라 다르다([fee 페이지](https://www.binance.com/en/fee/futureFee) 본문 미열람 `[unverified]`). 라이브에서는 실제 체결 수수료를 원장 증거로 기록해야 한다 | 레포 + `[unverified]` |
| 청산 | 데모가 청산을 시뮬레이션(세부 엔진 차이 `[unverified]`) | 실제 유지증거금·청산가·청산 수수료(insurance fund). `exchangeInfo`에 `marketTakeBound` 등 필드 — 5892 직접 확인 | exchange-information(5892) |
| 체결 품질 | 데모 체결은 시뮬레이션 — 슬리피지·부분체결 모형과 실 오더북의 차이 `[unverified]` | 실 오더북·실 슬리피지·실 부분체결. 데모 성과는 라이브 성과의 상한 근거일 뿐 | `[unverified]` |
| 데모 제약 | 심볼 집합·데이터 리셋 주기·미지원 기능 목록의 공식 근거를 두 조사 모두 찾지 못함 | — | `[unverified]` (5892 §10) |
| 계정 설정 | 데모 계정에서 isolated 1x·one-way를 readback으로 검증(truth gate) | **라이브 계정에서는 별도로 다시 맞춰야 한다** — 데모 설정이 라이브로 이전되지 않는다. truth gate의 readback 검사가 라이브 계정에서 그대로 필요하다 | `docs/runbooks/binance-trend-demo.md` §4 |

핵심 정리: **엔드포인트·서명·오류 의미·listenKey 규칙은 데모와 라이브가 같은 계약**이고(그래서
데모가 유효한 훈련장이다), 다른 것은 (1) 호스트·키·계좌, (2) 실제 돈이 오가는 수수료·펀딩·
청산, (3) 실제 오더북의 체결 품질, (4) 계정 설정의 재검증 필요성이다.

## 3. 라이브 활성화에 필요한 레포 변경 (미래 티켓 — 여기서는 구현하지 않는다)

현재 가드 위치(변경 대상이 될 코드, `경로:` 인용):

- `app/services/brokers/binance/futures_demo/host_allowlist.py` — `FUTURES_DEMO_HOSTS = frozenset({"demo-fapi.binance.com"})`, `assert_futures_demo_host`.
- `app/services/brokers/binance/futures_demo/transport.py` — `_LIVE_FUTURES_HOSTS`를 명시 deny path로 차단하고 `FUTURES_DEMO_HOSTS` 외 전부 `BinanceLiveHostBlocked`.
- `app/services/brokers/binance/host_allowlist.py` — `PUBLIC_HOSTS`(unsigned 공개 읽기) + `PUBLIC_FUTURES_STREAM_HOSTS = {"fstream.binance.com"}`(unsigned 시세 전용, 서명 경로에는 부재).
- `app/services/brokers/binance/futures_demo/preflight.py`·`execution_client.py` — `BINANCE_FUTURES_DEMO_ENABLED` + `BINANCE_FUTURES_DEMO_API_KEY`/`_SECRET_KEY` 이중 게이트.
- 레인 게이트: `BINANCE_TREND_DEMO_ENABLED`·`BINANCE_TREND_ALERT_ENABLED`(TREND),
  `BINANCE_H5_DEMO_ENABLED`·`BINANCE_H5_ALERT_ENABLED`(H5),
  `BINANCE_DEMO_STRATEGY_LOOP_ENABLED`(ROB-993).
- `app/services/brokers/binance/trend/constants.py` — `DEMO_URL`, `LOCKED_LIMITS`(심볼 4종·leg ≤60 USDT·동시 4포지션·gross 240 USDT·연속 hard-stop 2회/UTC일 래치·conflict 3건/UTC주 래치·1x).
- `app/services/brokers/binance/demo/ledger/`(repository·service)·`review.binance_trend_signals`/`_intents`·`review.binance_h5_*`·`binance_demo_order_ledger` 테이블 — 데모 원장·귀속 스키마.

하드룰 4(AGENTS.md)에 따라 호스트 allowlist 확장·하드 인바리언트 변경은 **운영자 결정이
있어야만** 구현한다. 아래 티켓은 어디서든 시작되지 않았다.

| # | 티켓 | 내용 | T | grade |
|---|---|---|---|---|
| L1 | 라이브 transport + allowlist 분리 | `FUTURES_LIVE_HOSTS = frozenset({"fapi.binance.com"})` 신설 — `FUTURES_DEMO_HOSTS`를 넓히지 않고 **별도 집합·별도 판정 경로**. demo/testnet 호스트 deny 유지, `PUBLIC_HOSTS`·`PUBLIC_FUTURES_STREAM_HOSTS` 불변 | T2 | B |
| L2 | 라이브 env 네임스페이스 | `BINANCE_FUTURES_LIVE_ENABLED`/`_API_KEY`/`_SECRET_KEY` 신설 — 데모 env와 비공유, 정확히 `"true"` 이중 게이트 + 호출별 confirm. 키는 선물 전용 scope + IP allowlist(운영자가 계정에서 설정) | T2 | B |
| L3 | 라이브 전용 locked 상수 | 신규 `LIVE_LIMITS`(leg·동시 포지션·gross·일일 손실 한도·래치 임계). 데모 `LOCKED_LIMITS` 불변. 값은 운영자 결정 — 시작 권고는 데모와 같거나 더 보수적 | T1 | A |
| L4 | 라이브 원장·귀속 | `binance_demo_order_ledger`의 product discriminator 방식으로 라이브 식별 분리(`usdm_live` discriminator 추가 vs 별도 `binance_live_order_ledger` 테이블은 설계에서 결정). 신호→intent→주문 NOT NULL 귀속 사슬은 데모와 동일. 원장 직접 SQL 금지 불변 | T2 | B |
| L5 | 라이브 대사 | `GET /fapi/v1/order`·`userTrades`·`income` 읽기 기반 evidence-first. terminal-only 대사 먼저, fill booking은 별도 안전 검토 뒤(KIS 선례와 같은 두 단계). 503/불명 결과는 `uncertain` 유지 | T2 | B |
| L6 | 라이브 감시·알림 | truth gate 라이브판(호스트 정체성 `fapi`·one-way·isolated 1x·flat vs state), heartbeat watcher, Discord alert — 기존 구조 재사용·라이브 라벨 분리. 스케줄러 등록 없음 유지(foreground + watcher) | T3 | C |
| L7 | 라이브 go/no-go 결정 문서 | §1 한국 거주자 확인 결과 + L1~L6 완료 근거 + 계정 설정 검증 절차 + 데모 증거 기준(§4)을 한 hk 문서로. **이 T7 문서는 그 선행 조사일 뿐이며 승인 문서가 아니다** | T1 | A |

## 4. 첫 라이브 레인 후보와 데모 증거

### 후보 비교 (데모 레인 둘)

| | TREND-TSMOM-v1 | H5 (binance-h5-demo) |
|---|---|---|
| 상태 | 활성 데모 레인(hk 4702/4719/4753/4760 계열), `research/experiments` heartbeat 등록 | 선배 데모 레인(hk 847 계열), 같은 데모 계정·클라이언트 |
| 크기 모형 | 고정 상수 `LOCKED_LIMITS`(leg 60·동시 4·gross 240 USDT) | NAV 비례(×0.01/×0.05), 동시 2 intents·심볼당 1 |
| 킬·래치 | 일일 연속 hard-stop 2회 래치, 주간 conflict 3건 래치(운영자 resolve 필수), manage-only 비상 관리 | 하드 스탑·신호 반전·time exit·TP, truth gate·watcher·alert 구조 동일 |
| 검증 표면 | truth gate 4검사(호스트 정체성·one-way·isolated 1x·flat vs state), `--once`/`--manage-only`, tick export→heartbeat | truth gate·heartbeat watch·alert 경로 동일 패턴 |
| 성과 증거 경로 | `review.binance_trend_signals`(상태·수수료·손익 필드, `app/models/binance_trend.py`) + `binance_demo_order_ledger` + heartbeat epoch | `review.binance_h5_signals`/`binance_h5_intents`/`binance_h5_nav_samples` + 원장 |
| 런북 | `docs/runbooks/binance-trend-demo.md` | `docs/runbooks/binance-h5-demo.md`, `binance-h5-ncp-manual-playbook.md` |

**권고: TREND-TSMOM-v1을 첫 라이브 후보로 둔다.** 근거 — 더 최근에 단단해진 킬스위치·
truth gate·manage-only·tick export·heartbeat 연결을 갖고 있고,
상한이 전부 코드 상수로 잠겨 있어 라이브판 `LIVE_LIMITS`로의 이식이 기계적이다.
H5는 같은 패턴의 선배라 두 번째 후보로 자연스럽다. ROB-993 데모 전략 루프는 계정 레인이
아니라 연구 루프라 후보에서 뺀다.

### 운영자가 먼저 봐야 할 데모 증거 (DB·브로커 조회는 이 문서 범위 밖 — 위치만 기록)

- **왕복 거래 수**: `review.binance_trend_signals`의 entered→flat 전이 횟수와
  `binance_demo_order_ledger`의 완결 legs.
- **실현손익**: `review.binance_trend_signals`의 청산 신호 행(데모 수수료 모형
  `TAKER_FEE_RATE`이 반영된 `fees_usdt`·손익 필드)과 `binance_demo_order_ledger`의 leg.
- **uncertain 수**: `binance_demo_order_ledger`의 `uncertain` intents와 각 해소 근거 —
  미해소 uncertain이 남은 상태에서는 라이브를 열지 않는다.
- **래치·conflict**: 연속 hard-stop 래치·주간 conflict 래치 발동 이력과 resolve 기록.
- **가동률**: `research/experiments`의 `TREND-TSMOM-v1` heartbeat epoch, truth gate PASS 연속.

권고: L7 go/no-go 문서에서 "완결 왕복 N건 이상 + 미해소 uncertain 0 + 래치 미발동 K주 +
truth gate 연속 PASS" 같은 정량 기준을 확정한다. 숫자는 운영자가 정한다.

## 5. 운영자 결정 목록 (권고 답 포함)

| # | 결정 | 권고 |
|---|---|---|
| 1 | 한국 거주자 USD-M 선물 이용 가능? | §1의 세 확인(금지국가 목록 정본·계정 UI·최신 약관/공지) 전까지 **미확정**. 하나라도 제한 신호면 라이브 금지 유지. 구현 티켓은 확인 뒤에만 만든다 |
| 2 | 라이브 개설 시점 | **지금 열지 않는다.** 데모 증거 기준(§4)을 L7에서 먼저 확정 |
| 3 | 첫 라이브 레인 | **TREND-TSMOM-v1**(§4). H5는 두 번째 |
| 4 | 라이브 env 네임스페이스 | `BINANCE_FUTURES_LIVE_*` 신설, 데모 env·키와 비공유(L2) |
| 5 | 호스트 allowlist | `FUTURES_LIVE_HOSTS` 별도 집합(L1). `FUTURES_DEMO_HOSTS`·`PUBLIC_HOSTS`·`PUBLIC_FUTURES_STREAM_HOSTS` 불변 |
| 6 | 라이브 상한 상수 | 신규 `LIVE_LIMITS`(L3). 시작값은 데모와 같거나 더 보수적으로, 수치는 운영자 |
| 7 | 원장 | 라이브 전용 식별 분리(L4). discriminator vs 별도 테이블은 설계 티켓에서 결정 — 분리 원장 선례(`binance_demo_order_ledger`)를 따른다 |
| 8 | 킬·감시 구조 | 데모와 동일: 기본 off 이중 게이트 + 호출별 confirm + truth gate + heartbeat watcher + Discord alert + manage-only. 스케줄러 등록 없음 |
| 9 | 비용 모형 | 라이브 수수료율·펀딩을 forecast에 반영 — `TAKER_FEE_RATE` 5bp를 계정 등급 기준 실수수료로 교정, funding 보유비용 항목화(L5 대사의 `income` 읽기로 실측 기록) |
| 10 | 법률·세무 | 해외 가상자산 파생 이용의 국내 법·세무 쟁점은 법률·세무 자문으로 분리 — 이 문서는 `[unverified]`로만 둔다 |

## 6. 확인하지 못한 것

- List of Prohibited Countries·Terms of Use 본문(렌더링 실패) — §1 `[unverified]`.
- Binance 선물 수수료율 표·API 키 권한/IP 제한 절차·데모 제약(심볼 집합·데이터 리셋)·
  `ORDER_TRADE_UPDATE` 필드·속도 제한 숫자 — 전부 `[unverified]` (일부는 5892 §10과 중복).
- 라이브 계정의 KYC·지역 판정이 계정 UI에서 실제로 어떻게 나오는지 — 자격증명 없이는
  확인 불가, 운영자 확인 영역.

## 7. 건드리지 않은 것

- 모든 코드·env·allowlist·스케줄러 — 이 PR은 문서 하나만 추가한다.
- `docs/research/5892-futures-options-survey.md` 본문 — 상위 조사의 `[unverified]` 행은
  이 문서의 §1·§2가 후속 기록이다.
