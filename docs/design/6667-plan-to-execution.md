# hk 6667 일일 계획에서 모의 실행까지

Claude의 일일 판단을 AT가 검증·발행하고, Fillwire 시세를 읽는 Go 감지기가 E1에 실행을 요청하며, 체결 원장으로 결과를 회고한다. hk 6667 설계 회신(댓글 35372)의 빠진 연결을 정의한다. 첫 재생은 KR KIS 모의 경로와 가짜 브로커로 닫는다. 5750의 `plan_only`와 5438의 shadow 계약을 유지한다. 운영 활성화·실주문·DAG 등록은 범위 밖이다.

AT 근거는 `auto_trader@fe28b451c2803d4e77dd95f50ff795fce68da780`, FW 근거는 `fillwire@873169acd85519fd122a16055a9b6f8abafec0df`(main)이다. 아래 경로와 줄 번호는 해당 커밋의 현재 코드·계약이다. 각 절의 향후 동작은 구현 요구다.

## 1. 전략 규칙과 오늘의 계획

| 데이터 | 작성·확정 | 내용 |
|---|---|---|
| 전략 규칙 | Claude가 제안하고 AT가 승인된 규칙을 참조 | `strategy_id`, `strategy_rule_version`. 신호 해석·위험 규칙의 개정에만 버전을 올린다. AT 정책의 `policy_version`과 별도로 보관한다. |
| 계획 행 | Claude가 제안하고 AT가 검증·발행 | 정규화 `symbol`, `market`, `venue`, 거래 세션, `new/add/trim/risk_sell/hold/watch`, 판단 근거, 평가 가능한 발동 조건과 무효화 조건, 매수 상한가/매도 하한가, 제안 만기, 출처별 기준 시각과 입력 번들 참조. 장중 신호·가격 경계·근거가 달라지면 계획 버전만 갱신한다. |
| 실행 권한 | 별도 AT 실행 경로가 intent를 저장하고 E1이 child를 검증·예약·송신 | 기존 승인 및 정책 범위 내 실제 계좌, 수량·예산, 유효 만기, 가격·노출 한도. 유효 만기는 계획·승인·세션의 상한 중 가장 이른 시각이다. Go가 제안하는 child 매개변수도 E1이 검증한다. |

5750의 head·불변 변경분·`row_key`와 계좌 참조를 재사용한다. 버전은 `input_bundle_id`와 `input_bundle_sha256`을 함께 가리킨다. venue·세션은 행의 실행 조건에 둔다. `action_basis`를 보존하고, `add`의 no-add-floor를 적용한다. `risk_sell`은 규칙이 결정하며 Claude가 변경할 수 없다. 발행본을 읽을 때 버전까지의 행 변경분을 접는다. AT는 기준 버전 CAS, 심볼·venue·세션, 정확한 십진 가격과 방향, 조건 어휘 및 만기, 번들 해시와 시점, 계좌 귀속을 검증하고 같은 트랜잭션에서 검증한 버전을 `PUBLISHED`로 가리킨다. 필수 입력 누락·stale·시점 불명은 사유가 있는 `watch`로만 발행한다. 잘못된 형식이나 경쟁 쓰기는 거부한다.

5933의 준비→`forecast_save`→확정 계약을 따른다. 결정적 `adoption_key`·`forecast_id`를 준비하고 재시도 전에 읽는다. 5750의 plan-only 채택은 일치하는 단일 기각 판단을 확인해 기존 예측에 연결한다. 실행 경로는 별도 승인에 결속된 비기각 판단을 멱등 기록하고 그 결정 ID를 intent에 고정한다. 브로커 주문 식별이 확정되면 그 ID에 주문을 연결한다. 기존 링크 서비스는 B에서 KIS 모의 범위를 별도로 지원해야 한다. 회고는 계획 기각 근거와 실행 판단을 구별하며 실행 판단은 기각 코호트에서 제외한다. 계획 `link_incomplete` 또는 실행 판단 결속이 미확정이면 회고에 기록하고 신규 실행 권한을 발급하지 않는다. 계획 발행은 제안·주문·감시·위임장을 만들지 않는다. 별도 승인된 AT 경로에서만 발행 버전을 읽어 모의 intent를 생성할 수 있다.

근거(AT) · `app/schemas/trading_policy.py:2347`(5949 정책 입력), `docs/design/5750-kr-daily-plan-v0.md:61,95,141,337,381,385,421`, `app/mcp_server/tooling/forecast_tools.py:143,181,200`, `app/services/decision_evidence.py:312,323,340`, `docs/design/5438-go-execution-path.md:53,266`.

## 2. E1의 발행 버전 검사와 취소

현재 E1 예약에는 `policy_version`을 가진 intent와 멱등 child 저널이 있다. 계획 참조는 없다. 새 계획 연동 intent에는 불변 `(plan_head_id, plan_version, row_key)`를 결속한다. child의 권한은 이 서버 저장 참조에서 읽는다. 기존 비연동 호출과 구분하며, 연동 경로에서 참조 누락이나 발행본 읽기 실패는 닫힌 상태다. 기존 게이트·위임장·정책 및 상한 검사는 그대로 적용한다.

1. **예약**. 기존 키 replay/conflict를 먼저 판정한다. 신규 submit/amend는 child 예약 트랜잭션에서 해당 head의 최신 `PUBLISHED` 포인터를 읽어 버전·행의 실행 자격을 대조한다. 서버 진입 차단(§4)과 기존 노출도 검사한다. head 발행·사건 차단과 동일 계좌·정규화 심볼·venue의 모든 계획 intent에 걸쳐 순서를 보장하는 공통 직렬화 경계에서 검사하고 예약을 커밋한다. v2 발행 커밋이 먼저면 v1은 `refused/plan_version_stale`, 브로커 호출 0건이다. 미발행 초안은 기준을 움직이지 않는다. head 단위 발행이므로 다른 행만 바뀌어도 기존 intent는 stale이다. 별도 AT 승인 경로가 최신 fold에서 변경 없는 행도 다시 검증해 새 참조를 발급한다. 발행 자체로 재발급하거나 기존 참조를 변경하지 않는다.
2. **송신 직전**. 예약 커밋 뒤, AT 브로커 어댑터가 첫 전송으로 진입하기 직전에 권한·최신 포인터·진입 차단·자기 예약을 제외한 충돌 노출을 다시 읽는다. 발행 및 차단 커밋과 마지막 확인→전송 진입은 예약과 동일한 직렬화 계약을 따른다. 차단 커밋이 먼저인 신규 진입은 증명된 미송신으로 끝난다. 확인을 어댑터 준비나 큐 등록 전에 하고 경계를 놓는 방식은 허용하지 않는다. 전송 진입의 순서를 증명할 수 있는 어댑터 훅이 없다면 타임아웃이 있는 호출 전체를 경계 안에 둔다. v2가 먼저 발행되면 v1을 전송 전에 차단하고 멱등 저널에 확정된 미송신 결과를 남긴다. KIS의 기존 `sending → accepted/uncertain` 전이에는 별도 미송신 전이가 없으므로, 연동 구현에서 증명된 미송신에 한정된 전이와 예약 정리를 함께 정의·검증한다. 프로세스 사망·네트워크 불명은 기존 보수적 대사로 남긴다.
3. **기존 주문**. 전송 진입이 먼저였으면 v2 발행 뒤에도 기존 주문과 노출을 추적한다. 취소는 현재 저널의 주문을 대상으로 별도 키·권한·확인으로 요청한다. 버전 검사는 submit/amend 범위이며 이전 버전의 유효한 cancel을 막지 않는다. 발행이 취소를 만들거나 주문을 교체하는 일은 없다. 동일 계좌·심볼·venue의 이전 intent에 미결 예약·열린 주문·불명 송신·미대사 체결이 하나라도 있으면 신규 submit/amend는 `refused/prior_intent_unsettled`로 끝난다. 모든 계획 버전·행을 대조하고 기존 intent별 상한 검사도 적용한다. 종결 뒤에는 이전 체결 수량과 명목을 누적하여 기존 승인 예산의 잔여분과 현재 보유를 검증한다. 새 버전·intent·취소 응답만으로 예산을 초기화하지 않는다. 증거가 불완전하면 차단을 유지한다.

근거(AT) · `app/services/execution_intent_gateway/store.py:237,264,285,290,309`, `app/services/execution_intent_gateway/gateway.py:179,202,237,296`, `app/services/execution_intent_gateway/kis_mock_forwarder.py:169,193,223`, `app/services/execution_intent_gateway/upbit_live_forwarder.py:114,136,233`(기존 송신 경계의 참고 사례), `docs/design/1440-intent-gateway-contract.md:221,252,262`, `docs/design/5750-kr-daily-plan-v0.md:65,70`.

## 3. 체결·잔량의 즉시 투영

투영의 단위는 계좌·심볼·venue와 intent→child→브로커 주문 lineage다. AT는 E1 fills와 주문 상태 증거로 E1 귀속 확인 보유와 열린 주문을 접는다. 귀속된 유효 매수 체결에서 귀속 매도 체결을 뺀 수량이 확인 보유다. 계좌 전체 보유나 매도 가능 수량이 필요한 게이트는 별도 계좌 증거를 사용하며 이 투영을 입력으로 쓰지 않는다. KIS 주문번호의 거래일을 포함하고, 유효 원장 행만 읽어 provisional supersession을 적용한다. 동일 fill ID 갱신이나 권위 행 교체가 가능하므로 스냅샷의 누적값을 재계산한다. 두 읽기 사이 증거가 어긋나거나 부족하면 미확정으로 남기고 후속 주문을 막는다.

E1 귀속 보유 0에서 10주 매수가 접수되면 확인 보유 0, 열린 수량 10이다. 유효 체결 4주가 커밋된 후 첫 일관된 투영은 확인 보유 4, 열린 수량 6이다. 완전체결이면 10/0이다. 4주 체결과 나머지 6주 취소가 일치하게 확정되면 4/0이고 취소량 6을 별도로 남긴다. 취소 응답과 부분체결이 경합하면 취소 요청·접수 사실을 기록하고 최종 누적 체결과 종결 잔량 증거가 합치할 때까지 `settling`으로 유지한다. 종결이 4주 체결을 보고하는데 fills에 아직 0주라면 잔량 0을 확정하지 않는다. 취소량은 포지션에 반영하지 않고, 매도 체결은 보유를 줄인다. 취소·만료·완료 후의 후속 결정은 확인 보유·잔량으로 다시 심사한다.

5750의 `kind=fill` 관측과 행 fold는 재사용하되, 송신 버전의 최신 기준은 실행 조건을 검증해 발행한 버전으로 둔다. 체결만으로 실행 조건을 재발행하지 않는다. 회고는 plan/version·intent·child·유효 fill 또는 차단 코드와 시각을 연결한다. 매수 체결, 발동하지 않아 미매수, 조건은 맞았지만 실행 불가를 증거로 남긴다. 증거가 사라졌거나 미확정인 경우도 명시한다.

근거(AT) · `docs/design/5750-kr-daily-plan-v0.md:91,316`, `app/routers/execution_intent_gateway.py:328`, `app/services/execution_intent_gateway/query.py:220,234,251,451,544,614`, `docs/design/1440-intent-gateway-contract.md:92`.

## 4. 계획 근거가 바뀌었을 때 재평가

공시, VI, 세션 전환, 거래정지/해제 중 현재 행의 근거·유효성에 영향을 주는 사건만 AT에서 재평가 요청으로 모은다. 영향받는 head·행·발행 버전과 사건 ID·관측 시각으로 중복을 제거하고 한 묶음으로 외부 Claude 세션에 제공한다. 세션이 읽는 요청에는 새 입력 번들·변경 사유·영향 행을 포함한다. VI·정지·세션 불일치처럼 즉시 실행 자격을 잃게 하는 사건은 재평가를 기다리는 동안에도 신규 진입을 차단한다. AT는 영향받는 head·행과 계좌·심볼·venue 범위에 서버 진입 차단을 기록한다. 기록과 해제는 §2의 예약 및 마지막 확인→전송 진입과 직렬화한다. E1은 차단을 읽지 못하면 신규 진입을 막으며 명시 cancel 권한은 별도 판정한다. 권위 있는 해제·세션 증거와 현재 행의 재검증이 모두 확인되어야 차단을 해제한다. 새 버전은 §1 검증과 발행을 거친다. 가격 틱은 감지기 평가로, 일반 체결은 §3 투영으로 처리한다. 둘 다 Claude 호출을 만들지 않는다.

근거(AT) · `app/models/market_events.py:112,162`, `app/mcp_server/tooling/kr_flow_signals.py:77,178`, `app/schemas/trading_policy.py:2188,2203`, `docs/design/5750-kr-daily-plan-v0.md:316,337`.

## 5. Go 감지기와 복구

`serve`는 수집만 맡는다(FW #47). 별도 Go 프로세스는 KIS 공유 세션의 Redis Streams(FW #41, 기본 off)를 읽는다. 현재 초기 구독은 설정의 종목 목록이며 `Promote`는 Fillwire 프로세스 안에서 호출된다. §7 P가 별도 감지기의 계획 종목 요청을 기존 세션의 구독 제어에 연결한다. 새 KIS 세션이나 소켓 서버를 열지 않는다. AT 발행본과 이미 승인된 E1 intent를 받아 세션·venue·무효 조건·시세 신선도·가격 경계를 확인하고 안정적인 멱등 키로 child를 요청한다. 가격 터치는 신호 증거다. FW #35의 Upbit 그림자 fill은 시뮬레이션 기록이며 확인 보유는 §3 원장 증거에서만 바뀐다.

정상 수신 중에도 연결 세대, 구독 승인, 스트림 커서와 연속성, 거래소 시각·도착 지연을 확인한다. quote gap, 지연 한도 초과, trim/로컬 큐 유실 또는 연속성을 증명할 수 없는 경우 해당 스트림 범위의 신규 진입을 차단한다. Redis ID의 숫자 차이만으로 유실을 판정하지 않는다. 생산 쪽에서 유실·세션 변경 증거를 알릴 수 있어야 하고 증명 불가 시 차단을 유지한다. 복구 순서는 기존 구독 목록 재생 및 승인 확인 → 재연결 세대의 최신 시세·세션·신선도 확인 → 최신 발행본과 E1 미확정 child 대사 → 새 틱부터 조건 평가다. 진입 평가와 복구의 최신 시세 확인에는 승인된 현재 연결 세대의 `source=ws`만 사용한다. 같은 스트림의 `source=rest` 스냅샷은 최신 도착 시각이어도 구독·연속성 복구를 증명하거나 child를 발동할 수 없다. 밀린 가격 터치는 주문으로 재생하지 않는다. 재시작 후 송신 여부가 불명확한 child는 E1 증거 없이 재전송하지 않는다. 차단은 기존 권한을 가진 명시적 취소를 대신하지 않는다.

근거(FW) · `cmd/fillwire/serve.go:93`, `cmd/fillwire/serve_kis.go:102,131,139,150`, `internal/kisquote/decode.go:45,171,179`, `internal/kisquote/lane.go:286,379,415,643,654,688,726`(제한 큐·실패·trim·REST 스냅샷), `internal/exec/upbit/plan.go:122,147,201`, `internal/exec/e1/client.go:93,305`. AT · `docs/design/5438-go-execution-path.md:313`.

## 6. 재생 검증

가짜 시계, 기록된 입력, 가짜 브로커 전송 스파이를 사용한다. AT 테스트는 E1 경계의 실제 처리 함수를 통과하고, Go 테스트는 Redis·E1 어댑터를 fixture로 대체한다.

| 재생 | 추가할 테스트와 fixture | 확인할 결과 |
|---|---|---|
| (a) v2 발행 경쟁 | AT pytest `tests/services/execution_intent_gateway/test_plan_version.py::test_v2_publish_blocks_v1_at_reservation_or_send`. 동일 정책·계좌·유효 기간의 v1/v2, v1 submit 키, 발행 장벽을 예약 전 또는 예약 커밋 직후에 해제하는 두 입력. 비교군은 초안만 있는 경우와 v1 송신 진입 후에 v2를 발행하는 경우. | 두 경합 모두 submit 0·cancel 0. 예약 전은 `refused/plan_version_stale`, 예약 뒤는 증명된 미송신으로 기록되며 재호출도 0. 두 비교군은 각각 submit 1·cancel 0. 송신 후 v1 열린 10주를 남긴 채 같은 계좌·심볼·venue의 v2 intent를 제출하면 추가 송신 0·`refused/prior_intent_unsettled`. 다른 행만 발행한 경우 기존 v1은 송신 0·stale이며 별도 승인 뒤에만 최신 참조를 발급한다. VI·정지·세션 불일치 차단을 예약 전 또는 예약 뒤 전송 전에 기록하면 각각 예약 거부 또는 확정 미송신이며 submit 0·cancel 0. |
| (b) 취소와 부분체결 | AT pytest `tests/services/execution_intent_gateway/test_plan_projection.py::test_cancel_partial_fill_settles_before_followup`. v1 10주 주문, cancel 접수와 최종 4주 체결·취소 잔량 6주 증거, fills 커밋 전후를 각각 재생한다. 취소보다 먼저 4주가 도착하는 순서와 E1 외부 보유 100주를 추가한 계좌 fixture도 넣는다. | 초기 submit 1, 명시 cancel 1. 확인된 4주가 들어오면 보유 4. 종결 전 열린 수량 6 또는 `settling`이고 후속 submit 0. 체결과 종결이 일치하면 4/0, 취소량 6, 후속 판단 1회 허용. 외부 보유 fixture에서도 E1 귀속은 4/0이며 계좌 수준 판단은 독립 계좌 증거의 104주를 사용한다. 그 판단을 승인하지 않는 이 fixture의 총 broker send는 2이고 Claude 호출은 0. |
| (c) 시세 연속성 상실 | FW go test `./internal/exec/plandetector -run TestEntryBlockedUntilSubscriptionReplayAndFreshQuote`(신설). 기록된 quote, 정상 cursor, gap/lag/trim/큐 드롭 각각의 장애 fixture, reconnect, 구독 승인, 이전 세대 틱, 장애·복구 중 최신 `source=rest` 호가와 새 세대 `source=ws` 호가, 최신 발행본·정상 E1 상태. | 장애부터 복구 검증 완료까지 child 0·가짜 broker send 0, 상태 `blocked`. REST만 도착한 경우에도 child 0·송신 0·`blocked`. 복구 검증 뒤에도 밀린 터치는 0, 다음 적격 새 틱에서 child 1·fake E1 전달 기준 broker send 1. 중복 틱에 추가 전송 0. fill fixture 없이 확인 보유는 0. |

기존 테스트 근거(AT) · `tests/services/execution_intent_gateway/test_gateway.py:94,204,248`, `tests/services/execution_intent_gateway/test_intent_exposure.py:167,215`. FW · `internal/exec/upbit/plan_test.go:1`, `docs/upbit-shadow.md:84`.

## 7. 구현 티켓과 선행 조건

| 단위 | 확장 대상·차단 선행 | 첫 검증 |
|---|---|---|
| A. 입력→발행 읽기 | 5750 T1(5933 저장·링크) 및 T2(입력) 기반. T4 Claude 입력과 발행 계약을 연결. | 번들·계좌 참조가 고정된 발행 읽기, stale CAS 및 watch-only, 주문 부작용 0. |
| B. 발행본→E1 예약→송신 | A·D 및 C의 원장 읽기 투영 이후. 5438의 5440 intent 생성과 5441 gateway 후속을 KIS 모의 경계에 연결. 비기각 실행 판단·주문 링크와 버전 간 노출 검사 담당. | §6(a), 기존 정책 검사와 비연동 계약 회귀. |
| C. 원장→잔량·보유 | 5750 T3(T1·T2 이후)와 1440 fills/노출 읽기 확장. 위임장 T4b fold는 위임장 결속 행에 적용한다. 미결속 행은 FIFO 재조회와 `evaluate_lot`을 따른다. 읽기 투영은 A·D와 병행, plan 연동 재생은 B 이후. | §6(b), supersession과 거래일 identity. |
| D. 사건→재평가 묶음 | A 이후. 5750의 사건·입력 번들 후속과 §4의 서버 진입 차단 기록. | 여러 사건을 한 요청으로 묶고 일반 fill의 요청 수는 0. |
| P. Fillwire 시세·구독 증거 | Fillwire #41 후속. 같은 수집 세션의 계획 종목 구독 요청 경로와 연결 세대·구독 ACK·연속성·유실 증거를 제공. | 소비자가 세대 교체·구독 전·유실을 판정하고 REST와 WS를 구별. |
| E. Redis→Go 감지→E1 | 5439 후속 및 Fillwire #41. §6(c) 오프라인 재생은 병행, 계획 통합은 B·C·D·P 이후. | §6(c), 재연결과 미확정 child에서 재전송 0. |
| F. 모의 통합 회고 | B·C·D·P·E 이후, hk 6667/#5716 후속. 첫 검증은 기록 재생. 열린 장 모의 검증과 수집 활성화는 기존 운영 승인 경계를 따른다. | 단일 시나리오에 매수·미발동·차단 증거와 plan/intent/child/fill 연결. |

근거(AT) · `docs/design/5750-kr-daily-plan-v0.md:91,433,440`, `docs/design/5438-go-execution-path.md:374,414`, `docs/agent-contracts/broker-and-ledger-contracts.md:303`. FW · `cmd/fillwire/serve_kis.go:150`, `internal/exec/e1/client.go:305`.
