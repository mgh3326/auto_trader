# OpenClaw MCP 기반 Screening Web 설계

작성일: 2026-02-17  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경

- 기존 웹은 Jinja 기반 페이지가 분산되어 있고, MCP 기능 확장 속도를 UI가 따라가지 못하고 있다.
- OpenClaw는 MCP를 잘 활용하고 있으나, 다종목 탐색(예: 낮은 RSI + 거래량 조건) 응답 지연이 체감된다.
- 신규 웹은 기존 화면 재사용이 아니라, Screening/Report/Order 흐름을 중심으로 별도 UX를 제공해야 한다.

목표:

- 스크리닝은 빠르게, 단일 종목 리포트는 깊게 제공한다.
- 리포트 생성 엔진은 OpenClaw + MCP로 통일한다.
- 초기 릴리스는 Jinja + API-first로 빠르게 출시하고, 이후 React로 단계적 전환 가능하게 만든다.

## 2. 요구사항 확정

- 1순위: Screening/Report.
- 2순위: 최소 주문 UI 포함(매수/매도 실행).
- 스크리닝 캐시: 전종목 5분.
- 수동 새로고침 버튼 제공(실시간 반응).
- 리포트 캐시: Redis 1시간.
- 리포트 저장: 초기에는 Redis-only (DB 영속화는 후속).
- 리포트 경로: OpenClaw 요청 -> MCP 분석 -> callback 수신 -> 상태 폴링.
- 신규 웹 계약에서는 `model_name` 파라미터를 사용하지 않는다.

## 3. 대안 검토 및 선택

### 대안 A: Jinja 중심 유지
- 장점: 구현/배포 단순, 즉시 출시 가능.
- 단점: 필터/상태/비동기 작업이 늘수록 프론트 코드 복잡도 급증.

### 대안 B: React 즉시 도입
- 장점: 인터랙션/상태 관리 우수, 장기 유지보수 유리.
- 단점: 학습 비용 + 빌드/배포 파이프라인 추가로 초기 속도 저하.

### 대안 C: 하이브리드 API-first (채택)
- 1차: FastAPI 내부 Jinja 화면 + JSON API(fetch).
- 2차: 같은 API 계약을 그대로 쓰는 React 화면으로 교체.
- 장점: 지금의 속도와 미래 확장성을 동시에 확보.

## 4. 아키텍처

## 4.1 라우팅/배포 전략

- 초기: 기존 FastAPI 앱 내부 신규 라우트로 시작 (`/screener`).
- 원칙: UI 템플릿과 데이터 API를 분리 (`/api/screener/*`).
- 이후: React 분리 시 API/인증/백엔드 로직 재사용.

## 4.2 핵심 흐름

1. 사용자가 다종목 조건으로 스크리닝 조회.
2. 서버가 5분 캐시 확인 후 즉시 반환(없으면 계산/저장).
3. 사용자가 특정 종목 리포트 생성 요청.
4. 서버가 OpenClaw에 비동기 분석 요청(job 생성).
5. 웹은 `job_id` 기반 상태 폴링.
6. OpenClaw callback 수신 시 Redis에 결과 저장 후 `completed`.
7. 최소 주문 UI에서 주문 API 호출(기존 거래 서비스 위임).

## 4.3 리포트 엔진 경계

- 신규 웹 리포트는 내부 Gemini 직접 호출 경로를 사용하지 않는다.
- OpenClaw MCP 분석 결과만 신뢰 소스로 취급한다.
- callback payload는 UI에서 불필요한 `model_name` 의존 없이 처리한다.

## 5. API 계약(초안)

## 5.1 Screening

- `GET /api/screener/list`
  - 목적: 다종목 스크리닝 결과 조회
  - 정책: Redis 5분 캐시
- `POST /api/screener/refresh`
  - 목적: 현재 조건 key 캐시 무효화 + 강제 재계산

## 5.2 Report (OpenClaw)

- `POST /api/screener/report`
  - 입력: `market`, `symbol` (필수), 필요 시 `name`
  - 출력: `job_id`, `status=queued`
- `GET /api/screener/report/{job_id}`
  - 출력: `queued | running | completed | failed`
  - completed 시 리포트 payload 포함
- `POST /api/screener/callback`
  - 목적: OpenClaw 결과 수신
  - 처리: 결과 캐시 저장 + 상태 완료 전환

## 5.3 Order

- `POST /api/screener/order`
  - 입력: `market`, `symbol`, `side`, `quantity`, `price|order_type`
  - 처리: 기존 주문 서비스 호출
  - 출력: 성공/실패 + 원인 코드

## 6. 캐시/상태 모델

## 6.1 Redis 키(예시)

- 스크리닝 결과: `screener:list:{hash(filters)}`
  - TTL: 300초
- 리포트 상태: `screener:report:status:{job_id}`
  - 값: `queued|running|completed|failed`
- 리포트 결과: `screener:report:result:{market}:{symbol}`
  - TTL: 3600초
- 요청-결과 연결: `screener:report:job:{job_id}`
  - `market/symbol/result_key` 매핑

## 6.2 리포트 중복 요청 정책

- 동일 `market+symbol`에서 유효한 1시간 캐시가 있으면 즉시 반환.
- 캐시 miss일 때만 OpenClaw job 생성.

## 7. 에러 처리

- OpenClaw 요청 실패: 즉시 `failed` 상태와 오류 메시지 반환.
- callback 지연: 폴링 타임아웃 메시지 + 재시도 액션 제공.
- callback payload 검증 실패: `failed` 처리 후 원인 로깅.
- 주문 실패: provider 원인(잔고/가격단위/시장상태)을 사용자에게 노출.

## 8. 화면 구성

- Screening 메인(`/screener`)
  - 필터(시장/정렬/RSI/거래량/limit)
  - `검색`, `새로 갱신` 버튼
  - 결과 테이블 + 종목별 `Report` 액션
- Report 상세
  - 상태 배지(`queued/running/completed/failed`)
  - 완료 시 분석 요약/근거/가격 범위 노출
- 최소 주문 패널
  - Buy/Sell 탭
  - 수량/가격 입력 + 실행

## 9. 디자인 소스 연계 (`screener.pen`)

디자인 원본:

- `/Users/robin/PycharmProjects/auto_trader/design/screener.pen`

핵심 프레임 매핑:

- `MMpvV` (`HiFi - Screening Main`) -> `/screener` 메인 화면 기준
- `nPxiZ` (`HiFi - Report Detail View`) -> `/screener/report/{id}` 상세 화면 기준
- `b4bGx` (`Mobile - Screening Page`) -> 모바일 레이아웃 기준
- `jofpS` (`HiFi - Login Page`) / `VpVzU` (`HiFi - Sign Up Page`) -> 인증 화면 참고
- `MziDt`, `RNf1N`, `WY3tE` -> 로그인 실패/세션 만료/권한 없음 에러 상태 참고

재사용 컴포넌트 참조:

- 버튼: `KiPSY`(Primary), `8YupF`(Secondary), `NVezV`(Accent), `7momf`(Danger), `baZfW`(Ghost)
- 상태 배지: `RIJqP`(Queued), `Tlf7r`(Running), `o2vL1`(Completed), `OKCZD`(Failed)
- 사이드바: `pUOAe`
- 테이블: `xx6vT`(HeaderRow), `Hu6HE`(Row), `bER9w`(HeaderCell), `3GCcW`(Cell)

디자인 변수(토큰):

- `bg-page=#F5F3EF`, `bg-dark=#1A1A1A`, `accent=#C05A3C`
- `success=#4A7C59`, `error=#B54A4A`, `border-light=#D1CCC4`
- 텍스트 토큰: `text-primary`, `text-secondary`, `text-tertiary`, `text-light`

적용 원칙:

- 구현 시 `.pen` 프레임/컴포넌트 ID를 UI 명세의 참조 키로 사용한다.
- 기존 Bootstrap 스타일을 그대로 따르지 않고, `.pen` 토큰을 CSS 변수로 우선 매핑한다.
- 모바일(`b4bGx`)은 테이블 축약 카드 패턴을 기본으로 한다.

## 10. 테스트 전략

- API 단위 테스트
  - 스크리닝 캐시 hit/miss/refresh
  - 리포트 job 생성/상태 폴링/callback 완료
  - 주문 API 정상/실패 분기
- 통합 테스트
  - OpenClaw callback 시 상태 전이 검증
  - Redis TTL/재사용 정책 검증(5분/1시간)
- UI 스모크 테스트
  - 필터 적용/새로고침/리포트 상태 배지/주문 실행 버튼 동작

## 11. 비목표

- 초기 릴리스에서 React 프런트 즉시 분리
- 리포트 결과 DB 영속화
- 실시간 WebSocket 푸시 기반 상태 업데이트
- 고급 주문 기능(조건부/분할/전략 주문)
