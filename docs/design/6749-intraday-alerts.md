# hk 6749 장중 급등 알림 기록 설계

우리 데이터로 장중 급등을 기록해 가격이 왜 감지됐고 당시 어떤 근거를 알 수 있었는지 한 번에 조회한다. 첫 단계의 산출물은 전향 관측 기록과 읽기 계약이다. 푸시 발송, 신규 뉴스 서비스, 주문 경로, 스케줄러 등록은 범위 밖이다.

각 절의 앵커는 `auto_trader@4856dd4a1`의 현행 코드다. 아래의 writer, detector, linker 및 읽기 확장은 후속 구현 계약이다.

## 1. 감지 입력과 대상

| 입력 | 초기 용도 | 선택 근거 |
|---|---|---|
| research 닫힌 1분봉 | 주 감지 입력과 재생 기준 | 봉 시작·출처·venue·세션을 보존한다. 수집 지연을 포함한 분봉 기준 감지다. 최초 수신으로 인정되는 batch만 전향 감지에 쓴다. |
| Fillwire `serve` 시세 스트림 | 후속 저지연 경로 후보 | `kind=trade`, `source=ws`의 거래소 시각과 수신 시각으로 지연을 대조한다. 운영 입력으로 채택하려면 구독 ACK·연결 세대·유실 증거 및 venue별 커버리지를 먼저 검증한다. 호가·예상체결가·REST 복구 스냅샷은 체결 감지 입력에서 제외한다. |
| 장중 movers 스냅샷 | 종목 편입 시점과 순위의 근거 | 최초 포착 기록은 수집 대상 확장에 쓴다. 순위와 제공사 등락률만으로는 연속 두 분이나 실제 체결 venue를 확정할 수 없다. |

유니버스는 **보유 → 활성 워치 → 스크리너에 포착된 비보유** 순이다. 저장된 보유·워치 읽기를 재사용하고, 비보유 후보는 당일 movers 최초 포착 및 기록된 admitted 스크리너 pick의 합집합에서 가져온다. 정규화 종목 키로 중복을 제거하되 모든 편입 근거와 관측 시각을 보존한다. 설정된 예산은 뒤 순위부터 제한하고 보유를 보호한다. 편입 스냅샷의 ID, 사용 가능 시각 및 탈락 사유를 감지 실행과 함께 기록한다. 당시 편입을 증명할 수 없는 종목은 역사 재생에서 별도 표시한다. 편입과 수집은 독립이므로 `not_selected`, `budget_cut`, `fetch_error`, `no_trades_reported`, `coverage_unknown`을 확인 가능한 근거에 따라 노출한다.

룰은 버전·해시가 붙은 설정 데이터로 관리한다. 각 룰은 `rule_id, market, venue, session_segment, price_kind, baseline_kind, min_return_pct, min_volume?, max_input_age`를 가진다. 1차 범위는 KR 현물의 venue가 확인된 실제 체결 종가다. 정규화된 venue는 KRX와 NXT를 구분하고 저장소 표기 `NTX`는 입력 경계에서 NXT로 매핑한다. 통합·미상 venue와 `UNKNOWN` 세션은 커버리지로 표시한다. 같은 종목이라도 venue와 세션을 넘겨 분봉을 이어 붙이지 않는다.

- `prior_minute_close`는 동일 출처·venue·세그먼트에서 정확히 1분 앞선 닫힌 봉의 체결 종가다. 분이 비거나 입력이 stale이면 해당 룰은 평가 불가다.
- `previous_close`는 검증된 조정 기준 및 거래일의 직전 KRX 정규장 공식 종가다. 초기에는 KRX 룰만 사용한다. NXT에 추가할 때는 기준 venue·가격 종류를 명시한 독립 룰을 승인한다.
- 수익률은 `100 × (현재가 / 기준가 − 1)`로 정확한 십진수에서 비교한다. 가격은 양수이고 최소 변동률과 선택적 최소 거래량은 이상(≥)을 만족해야 한다. 두 기준의 수익률·판정·결손을 각각 기록한다. 동일 봉의 여러 룰이 통과하면 감지 한 건에 모은다. 룰 변경을 검증하는 재생은 별도 범위에서 실행한다.

수동 실행에서 이미 커밋된 research 봉과 유니버스 스냅샷을 읽어 감지한다. 감지 기준 시각은 실제 평가한 시각이다. 뒤늦은 분봉이나 출처 단절로 평가가 불가능할 때는 평가 결과와 사유를 남긴다. 감지의 유일한 쓰기 대상은 관측 writer다.

근거 · `app/services/research_candles/session_minutes_collector.py:179`(닫힌 봉), `app/services/research_candles/session_minutes_collector.py:424`(writer), `app/services/research_candles/session_minutes_collector.py:770`(수집 대상), `app/services/kr_news_collector.py:253`(보유·워치·최초 포착), `app/services/invest_momentum_events/intraday.py:330`(최초 포착), `app/services/retro_v1/daily_bars.py:106`(수신 시각 적격성), `docs/design/6667-plan-to-execution.md:46`(Fillwire 소비 경계), `app/services/spike_attribution/detect.py:54`(일봉 검출의 기준가 분리).

## 2. 최초 감지와 근거 개정

| 기록 | 작성자 | 데이터 |
|---|---|---|
| 감지 실행·커버리지 | detector | 실행 ID, 유니버스·설정 버전, 조회 범위, source별 성공·실패·잘림, 대상별 평가 결과와 결손. 실행의 관측·저장 시각을 보존한다. |
| 최초 감지 `detected` | detector → writer | `alert_id`, 실거래일·종목·venue·세션, 감지 봉과 각 기준가의 불변 참조 및 값, 룰별 수치·판정, 가격 입력의 발생·수신 시각, 감지·저장 시각. 최초 문구는 **급등 감지 · 원인 미확인**이다. |
| 근거 개정 `evidence_linked` / `evidence_retracted` | linker → writer | 같은 `alert_id`, 개정 순번, 근거 ID·버전·출처, 종목 매칭 근거, 발생·공개·수신 시각, 연결을 확인한 시각과 저장 시각. 철회는 이전 개정 참조와 이유를 보탠다. |

`alert_id`는 버전이 고정된 canonical key의 결정적 ID다. 키는 `(scope, market, symbol, venue, session_date, session_segment, bar_start_utc, price_kind)`이며 `scope`는 전향 기록 또는 특정 재생이다. 한 봉에 하나의 감지 행을 유니크 제약으로 보장한다. 동일 입력 재시도는 기존 행을 반환하고 내용 충돌은 오류로 남긴다. 개정은 `(alert_id, operation, evidence_ref, evidence_version)`으로 멱등화하고, writer가 alert 단위로 순번 할당과 append를 원자적으로 처리한다. 최초 행을 포함한 모든 기록에서 UPDATE·DELETE를 금지한다. 조회는 개정 순서에 따라 현재 상태를 접되 최초 행을 함께 반환한다.

각 근거 값에는 출처·발생 시각 또는 구간·`first_received_at`을 붙인다. 뉴스·공시는 제공사 공개 시각과 그 정밀도도 보존한다. 창이 닫힌 봉의 발생 구간은 `[bar_start, bar_start + 1분)`이고 수신 시각은 적격 research `retrieved_at`이다. 계산값은 피연산자와 룰이 모두 준비된 시각 이후에 알 수 있고 `evaluated_at`에 계산한다. 종목 매핑·관계 판단의 확인 시각도 별도 보존한다. 실제 조회 가능성은 기록의 저장 시각으로 판단한다. 불명인 최초 수신은 null과 사유로 남긴다. 공개 시각·DB 생성 시각·백필 수집 시각을 최초 수신의 대용으로 쓰지 않는다.

DART·KRX·KIS 공시와 기존 KR 뉴스 저장소에서 종목 키, 원문 참조, 시간 근접성으로 후보를 좁힌다. 후보를 검색하는 상대 시간 창은 설정에 둔다. 날짜 정밀도만 있는 공시는 날짜를 유지하고 장중 순서는 미확정으로 표시한다. 여러 공급자가 전한 같은 공시를 공시 번호 등 확인 가능한 원문 식별자로 묶고 공급자별 수신은 각각 남긴다. 뉴스는 원문과 종목 관련성 확인을 거쳐 개정에 첨부한다. 수신이 감지 뒤인 근거는 사후 근거로 표시하고, 수신이 불명인 근거는 당시 가용성 불명으로 표시한다. 기사 연결은 맥락을 추가하며 인과 판정은 `unconfirmed`로 유지한다.

근거 · `app/models/market_events.py:134`(공식 이벤트 v2의 시계), `app/services/market_events/repository.py:61`(공개 정밀도), `app/services/symbol_news_store.py:256`(뉴스 최초 수신), `app/services/kr_news_collector.py:55`(제공 시각 해석), `app/services/spike_attribution/attribute.py:95`(원인 미확인 사유), `app/services/spike_attribution/cache.py:217`(일봉 캐시는 파생 조회용).

## 3. 관련 기업과 관계 근거

관계는 `(subject_symbol, related_symbol, relation_type, direction, evidence_ref, effective_at?, first_received_at?, verified_at?)`로 기록한다. 근거 출처·문서 위치와 원문에서 입증하는 범위를 포함한다. 관계는 근거 개정에 불변 스냅샷으로 붙인다. 공시·회사 자료가 입증하는 관계와 업종 제공사의 분류를 각자 표시한다.

| 타입 | 판정 근거 | 033500 예 |
|---|---|---|
| `equity` 지분 | 공시·사업보고서의 보유 주체, 대상, 기준일 | 동성케미컬. 방향과 비율은 원문 확인 후 기입. |
| `supply_chain` 공급망 | 계약 또는 사업보고서상 공급자·고객 및 제품 | 이 사례에서는 미확인. |
| `same_product` 동일 제품 | 양사 사업·제품 자료에서 같은 시장의 제품을 확인 | 한국카본. LNG 보냉재 제품 근거 필요. |
| `sector` 업종 | 분류 제공사·분류값·조회 시각 | LG화학·에코프로는 브리프에 제시된 일반 업종 피어. |

위 종목 예시는 브리프 기반 재생 입력이다. 원문 참조가 없는 관계는 검증 대기이며 조회에서도 그 상태를 표시한다. 관련 종목에 개별 감지 근거 없이 추가 감지를 생성하지 않는다.

근거 · `app/mcp_server/tooling/fundamentals/_sector_peers.py:32`(기존 업종 피어), `app/services/symbol_news_store.py:374`(기사의 관련 심볼 저장 경계).

## 4. 동성화인텍 2026-11-26 재생

이 재생은 브리프에서 제공한 기록을 입력으로 삼는다. 당시 운영 감지, 공시 최초 수신 및 실제 발송 시간을 복원할 수 없다.

| 단계 | 재생 입력 | 기대 기록 |
|---|---|---|
| 가격 감지 | `033500`, Toss 출처 실제 1분봉의 KST 표시 14:49 종가 11,090원 → 14:50 종가 11,480원, 두 번째 봉 거래량 25,312주. 재생 전용 룰 `prior_minute_close ≥ 3%`, `min_volume=10000`. KRX 정규장·대상 편입·연속 봉은 fixture에 명시한 가정이며 실제 venue 및 당시 편입 시각은 미확인이다. | 약 3.5166817%로 조건 통과. `detected` 한 행, **급등 감지 · 원인 미확인**. 직전 종가 룰은 기준가 미상. 봉 두 개의 실제 `first_received_at`은 null. 역사 재생은 테스트 실행의 시계를 사용할 수 있으나 실제 시각과 구별한다. |
| 공시 연결 | DART 50억 원 자기주식 취득 신탁계약 공시. 원문 번호·세부 공개 시각·최초 수신 시각은 제공된 기록에서 미확인이다. fixture 공시 참조는 실제 공시 번호와 구별한다. | 같은 `alert_id`에 `evidence_linked` 1개. 033500 직접 공시로 표시하고 감지 시점 가용성과 인과는 미확정. |
| 기사 연결 | Naver 기사 제공사 표기 14:59 KST. 실제 최초 수신 미확인. 시장 이슈 뉴스 조회는 12시간 동안 0건이었다. 당시 KR 뉴스 수집기의 5분 주기 운영 시작은 16:25 KST라는 브리프 정보만 있다. | 원문 관련성을 충족하는 fixture 기사 개정을 같은 ID에 append. 제공사 시계에서 가격 변동이 기사보다 앞선다. 기사 수신 지연과 0건의 원인은 미확정. 중복 입력 재생 후에도 감지 1건·근거 개정 2건. |
| Factortrail 조회 | `retro_timeline(market="kr", date="2026-11-26", symbol="033500")`의 오프라인 fixture 응답에 재생 projection을 명시적으로 공급한다. 브리프 당시 Factortrail 1분봉은 0건이었다. | 한 재생 alert에 최초 행과 두 개정. 가격·공시·기사 수신 공백, Factortrail 0행과 AT의 Toss 관측을 각 출처별로 표시한다. 0행의 원인은 진단 증거가 없으면 `coverage_unknown`이다. |

과거 표기 14:49·14:50은 공급자 라벨 그대로 보존한다. 봉 시작·끝 의미는 당시 원문으로 확인 가능한 경우만 UTC 봉 시작으로 확정한다. 테스트는 두 라벨이 연속 봉이라는 가정을 명시한다. 라이브 수집기에서 적격인 동등 입력을 검증하는 별도 fixture에는 venue, 정규화된 봉 시작, true first receipt 및 편입 기록을 포함한다.

다음 운영 관측은 기존 수집 결과를 사람이 수동 호출로 기록한다. 해당 구간부터 처음 조건을 충족한 건에 봉 종료→가격 최초 수신→감지→저장, 공시·기사 공개→최초 수신→연결 저장을 각각 측정한다. 시계가 불명하거나 순서가 역전된 구간은 결손 또는 시계 오차로 남긴다. 첫 실측에는 푸시 0건이 기대된다.

근거 · `app/services/retro_v1/get_bars.py:85`(분봉 보조 필드), `app/services/research_candles/session_coverage.py:158`(0행 진단), `app/services/retro_v1/retro_timeline.py:438`(분봉 커버리지), `contracts/retro/v1/examples/retro_timeline.yaml:1`(기존 조회 예제).

## 5. Factortrail 및 회고 읽기

Factortrail의 기존 `retro_timeline` MCP 도구가 AT의 `GET /trading/api/retro/v1/retro_timeline`에서 같은 market·date·symbol로 조회한다. AT가 가산 필드 `intraday_alerts`에 alert ID별 최초 행·개정과 당시 가용성, `coverage.intraday_alerts`에 수집 및 평가 공백을 제공한다. alert 단독인 종목도 행으로 조회할 수 있다. 기존 판단 상태와 주문·체결 이벤트 접기는 기존 근거만 사용한다. 기본 읽기는 전향 기록이며 033500 검증은 fixture 격리 범위의 같은 조회를 사용한다. 응답 ref는 opaque이며 사용자 범위와 기존 접근 계약을 따른다.

한 호출에서 아래를 읽는다.

- **왜 발동했나** · 룰 버전, 기준 가격 종류·값, 현재 값, 임계값, venue·세션, 유니버스 편입 근거.
- **당시 알 수 있었나** · 감지 시각까지 수신과 종목 확인이 증명된 근거, 사후에 수신한 근거, 시각 불명인 근거를 구분한다. 수신 완료와 실제 개정 기록 시각을 따로 제공한다.
- **왜 놓쳤나** · 커버리지·평가 기록에서 미편입/예산 컷/입력 부족/부적격/임계값 미달을 증거와 함께 반환한다. 읽기 실패·잘림 시 해당 구간은 `source_unavailable`로 표시한다. 빈 응답에는 기록 범위와 평가 여부를 함께 제공한다.

후속 Toss 비교에서는 운영자가 별도로 확보한 해당 푸시 시각과 출처를 읽기에서 대조한다. 동일 종목·세션의 합리적 매칭 근거가 있을 때만 연결하고, 서로 다른 시계의 지연은 따로 표시한다. 거래 회고는 동일 종목·시점의 `alert_id`를 관측 참조로 읽어 해당 결정 시점과 비교한다. 결정이나 주문의 인과 관계는 별도 증거가 있어야 한다.

근거 · `app/routers/retro_v1.py:40`(기존 dispatch), `app/services/retro_v1/retro_timeline.py:174`(기존 상태), `app/services/retro_v1/retro_timeline.py:524`(커버리지), `app/services/retro_v1/retro_timeline.py:559`(집계), `contracts/retro/v1/openapi.yaml:812`(공유 계약).

## 6. 구현 티켓과 차단 선행

| 단위 | 차단 선행 | 종료 증거 |
|---|---|---|
| A. 관측 writer | 이 설계 | 감지·커버리지·개정 append, duplicate/conflict/동시 개정 검증. 근거 · `app/services/market_events/repository.py:161`. |
| B. KR 감지기와 수동 실행 | A, 적격 research 입력과 유니버스 읽기 | 연속 봉 적중 및 세션·venue·가격 종류·결손 fixture 검증. 근거 · `app/services/research_candles/session_minutes_collector.py:179`. |
| C. 공시·뉴스·관계 linker | A, 기존 공식 이벤트와 뉴스 저장소. 단독 fixture는 B와 병행, 가격부터의 통합 재생은 B 필요 | 공시·기사 개정 및 시계 불명·철회 검증. 뉴스 관련성 hk 6302에서 제공 가능한 근거를 읽고 미가용이면 검증 대기로 남긴다. 근거 · `app/models/market_events.py:134`, `app/services/symbol_news_store.py:354`. |
| D. AT·Factortrail 읽기 | A+B+C | 계약 fixture의 §4 기대 응답 검증과 기존 상태 회귀 확인. 머지와 해당 수집 입력 준비 뒤 다음 적격 관측의 수동 기록·지연 측정. 근거 · `app/services/retro_v1/retro_timeline.py:559`. |
| E. US·crypto 확장 | D와 KR 관측 지연·커버리지 확인 | US 거래일·주간 venue 및 crypto의 24시간 기준·거래소별 가격을 따로 정의한다. 두 시장은 독립 테스트한다. 근거 · `app/services/research_candles/session_minutes_collector.py:317`, `app/services/retro_v1/daily_bars.py:117`. |
