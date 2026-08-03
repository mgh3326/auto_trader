# Kiwoom Live Read-Only Market Data — 런북

`KiwoomLiveReadOnlyClient` 는 이 레포에서 **주문 가능한 live 호스트
(`https://api.kiwoom.com`) 에 붙는 유일한 클라이언트**다. 차트만 읽을 수 있고 그
외에는 아무것도 못 한다. 이 문서는 그 경계와 운영 절차를 기술한다.

- 코드: `app/services/brokers/kiwoom/live_market_data.py`
- 비교 하니스: `app/services/brokers/kiwoom/chart_compare.py`
- 실행 CLI: `scripts/kiwoom_live_readonly_compare.py` (default-disabled)
- 테스트: `tests/test_kiwoom_live_readonly_guard.py`, `tests/test_kiwoom_chart_compare.py`

---

## 1. 안전 경계 — 4개 층

### 층 1: env 게이트 (default off)

`KIWOOM_LIVE_MARKETDATA_ENABLED=false` 가 기본. 미설정이면
`KiwoomLiveReadOnlyDisabled`. 🔴 게이트 검사는 **생성자가 아니라 dispatch 시점**에
일어난다 — `from_app_settings` 를 우회해 직접 생성한 클라이언트도 게이트가 꺼져 있으면
전송할 수 없다.

### 층 2: allowlist (api-id · path)

| 종류 | 허용 | 강제 시점 |
|---|---|---|
| api-id | `ka10080` `ka10081` `ka10082` `ka10083` (차트 4종만) | 토큰 해석·소켓 오픈 **이전** |
| path | `/api/dostk/chart` 만 | 토큰 해석·소켓 오픈 **이전** |

주문 TR(`kt10000`~`kt10003`)·계좌 TR·US 주문 TR 은 전부 거부된다.

### 층 3: 호스트/경로 고정 + 전송 직전 재검증

- 생성자는 `https://api.kiwoom.com` 외 base URL 을 전부 거부한다(mock 호스트 포함).
- 🔴 요청 build **후**, `client.send` **직전**에 `request.url.host` 와
  `request.url.path` 를 **둘 다** 재검증한다. 사전검사를 통과했더라도 resolve 결과가
  다르면 전송하지 않는다.
- 🔴 `follow_redirects=False` 를 OAuth·chart 양쪽에 **명시적으로 고정**한다. httpx
  기본값에 의존하지 않는다 — 3xx 는 검증을 통과한 요청이 다른 호스트/경로로 갈 수 있는
  유일한 경로이고, 이 호스트에는 주문 API 가 산다.

### 층 4: 계좌번호 부재 (2중)

1. **코드**: 클라이언트는 계좌번호를 인자로 받지도, 속성으로 갖지도, Settings 에서 읽지도
   않는다. Settings 의 live 표면은 `app_key`/`app_secret`/`base_url` **3개뿐**이며
   `kiwoom_account_no` 는 **없다**.
2. **정적 가드**: `tests/test_kiwoom_live_readonly_guard.py` 의 AST 가드가 신규 live
   모듈에서 주문 상수·주문 모듈 import·`kiwoom_account_no`/`KIWOOM_ACCOUNT_NO` 참조를
   **문자열 우회까지 포함해** 금지한다. 위반하면 빌드가 깨진다.
3. **운영**: 전용 자격증명 파일에 `KIWOOM_ACCOUNT_NO` 를 **넣지 않는다** → 계좌번호가
   프로세스 환경에 아예 존재하지 않는다.

> ### ⚠️ 보장 강도 — 정확히 읽어라
>
> 위 조합의 정확한 보장은 **"우발 주문 방지 + 정적 검출"** 이다.
>
> 🔴 **"구조적으로 주문이 불가능하다"고 쓰지 마라.** `KIWOOM_ACCOUNT_NO` 값은
> 배포 env 파일에 여전히 존재하고, Settings 에 한 줄만 추가하면 도달 가능해진다.
> AST 가드는 **그 한 줄을 빌드 실패로 만드는 장치**이지 물리적 불가능성의 증명이 아니다.

---

## 2. 자격증명

🔴 **전용 최소 env 파일만 사용한다.**

```
/Users/mgh3326/services/auto_trader/shared/.env.kiwoom-readonly.native   (mode 600)
  KIWOOM_APP_KEY / KIWOOM_APP_SECRET            ← live 시세 읽기
  KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET  ← mock 비교용
  🔴 ACCOUNT_NO 없음 · DATABASE_URL 없음 · 타 브로커 없음
```

🔴 **`ENV_FILE=.env.prod` 절대 금지.** 그 파일에는 운영 DB URL 과 다른 브로커의 live
자격증명 전부가 들어 있어, 시세 읽기 하나를 위해 그것들을 프로세스에 올리는 것은 이
설계와 정면으로 충돌한다. CLI 는 파일명에 `prod` 가 들어가면 거부한다.

`Settings` 는 무관한 필수 필드(`KIS_APP_KEY`, `DATABASE_URL`, `SECRET_KEY` 등)를
요구하므로, CLI 가 **명백히 동작하지 않는 placeholder** 를 채워 넣는다. DB 는 열지
않으며 placeholder DSN 은 닫힌 포트를 가리킨다.

---

## 3. 실행

```bash
# dry run — 네트워크 호출 0
uv run python -m scripts.kiwoom_live_readonly_compare

# 실제 비교 (🔴 --confirm-live-read 없이는 아무 호출도 안 나간다)
uv run python -m scripts.kiwoom_live_readonly_compare \
    --confirm-live-read \
    --redis-url redis://localhost:6399/0 \
    --out /tmp/kiwoom_live_compare.json
```

🔴 **`--redis-url` 로 일회용 Redis 를 지정하라.** OAuth 토큰이 캐시되는데, 기본
`REDIS_URL` 은 배포가 공유하는 캐시(운영 OHLCV 약 2.4만 키)를 가리킬 수 있다. 읽기 전용
비교가 그 저장소에 키를 쓰지 않도록 격리한다.

```bash
redis-server --port 6399 --save '' --appendonly no --daemonize yes
# ... 실행 ...
redis-cli -p 6399 shutdown nosave
```

기본 안전값: 호출 간격 2.0초 · 동시성 1 · 총 호출 상한 200(`--max-calls`).

---

## 4. Rate limit (2026-08-03 실측, mock 호스트)

| 간격 | 결과 |
|---|---|
| 2.0s | OK |
| 1.0s | OK |
| 0.5s | OK |
| 0.2s | ❌ `HTTPStatusError` |
| 0.05s | ❌ `HTTPStatusError` |

→ 임계는 **0.5초와 0.2초 사이**. 운영 기본값은 여유를 둔 **2.0초**를 유지한다.
live 호스트의 간격별 특성은 2.0초에서만 관측했다(전 구간 성공) — 그 이하는 미측정.

---

## 5. 데이터 동일성 (2026-08-03 실측)

20종목(코스피 10 + 코스닥 10) × 일봉 600행 + 5분봉 900행, mock/live 양쪽.

- 행 커버리지: 40/40 페어에서 mock·live 모두 동일 (only-mock 0, only-live 0)
- 비교 셀 252,000 중 불일치 36 → **99.9857%**
- 🔴 불일치 36건은 **전부 형성 중인 최신 봉**(당일 일봉 + 최근 5분봉)에서 발생.
  개장 중 두 호출이 약 2초 차로 나가므로 누적 거래량·현재가가 당연히 다르다.
  **최신 봉 제외 시 불일치 0 → 100.000000%**
- 상폐 종목 `051170`: live 에서 `return_code=0` + **1행** 반환(EMPTY 아님)

⚠️ 장중 실행 시 최신 봉 차이는 **정상**이다. 장 마감 후 재실행하면 이 잡음이 사라진다.

### 3자 대조 (KIS 프로즌 샘플)

`herdr-artifacts/kr-corpus-v1/crosscheck/kis_frozen_sample.csv` (읽기 전용,
sha256 `e648cffb…f0b4`) 대조 결과:

| 종목 | MATCH | MISMATCH | 비고 |
|---|---|---|---|
| 035420 | 301 | 0 | |
| 005930 | 300 | 1 | 20260731 `trde_qty` (fable 기존 관측과 일치) |
| 000660 | 293 | 1 | 20260731 `trde_qty` |
| 068270 | 41 | **223** | 아래 참고 |

🔴 **live 와 mock 의 대조 결과가 완전히 동일했다** — 즉 이 불일치는 mock-vs-live 문제가
**아니다**.

**068270(셀트리온) 223건**: 2026-06-03 을 경계로 이전 날짜가 전부 어긋난다. 가격비
(KIS/Kiwoom)는 0.999957~0.999967 로 **약 0.004%**, 서로 다른 값이 11종. 즉 corporate
action 자체가 아니라 **수정주가 역산 반올림 규칙 차이**다(예: 20250703 종가 Kiwoom
164236 vs KIS 164230).

🔴 **어느 쪽이 맞는지 단정하지 않는다 — `UNDETERMINED`.** 다만 운영상 함의는 분명하다:
**Kiwoom 과 KIS 의 과거 수정주가를 완전일치로 대조하면 실패한다. 허용오차가 필요하다.**

---

## 6. 금지 사항

- 🔴 주문·계좌 API 호출 **0** (live·mock 양쪽, preview 포함)
- 🔴 `ENV_FILE=.env.prod` **0** · secret 값 출력/로그/보고서 기재 **0**
- 🔴 공유 Redis 에 토큰 쓰기 금지 → `--redis-url` 로 격리
- 대량 수집·DB 저장(Stage 2)은 **별도 승인** 사항이다. 비교 결과가 좋아도 자동으로
  넘어가지 않는다
- 스케줄러 등록 없음 — CLI 수동 실행만
