# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

AI 기반 자동 거래 분석 시스템으로, 다양한 금융 데이터를 수집하고 out-of-process MCP consumer(claude 세션 등)를 통해 투자 분석을 제공합니다.

**주요 특징:**
- 다중 시장 지원: 국내주식(KIS), 해외주식(KIS/Yahoo Finance), 암호화폐(Upbit)
- 다중 시간대 분석: 일봉 200개 + 분봉(60분/5분/1분)
- AI 분석: out-of-process MCP consumer(claude 세션 등)가 담당 (런타임은 in-process LLM provider 미탑재 — ROB-501 가드)

## 핵심 명령어

### 테스트
```bash
make test                         # 모든 테스트 실행
make test-unit                    # 단위 테스트만
make test-integration             # 통합 테스트만
make test-cov                     # 커버리지 리포트 포함
uv run pytest tests/test_*.py -v -k "test_name"  # 특정 테스트만
```

### 코드 품질
```bash
make lint                         # Ruff + ty 검사
make format                       # Ruff로 코드 포맷팅
make typecheck                    # ty 타입 체킹
make security                     # bandit, safety 보안 검사
```

### 데이터베이스
```bash
# 마이그레이션 생성 및 적용
uv run alembic revision --autogenerate -m "migration message"
uv run alembic upgrade head

# 마이그레이션 롤백
uv run alembic downgrade -1

# 현재 버전 확인
uv run alembic current
```

### 개발 도구
```bash
python manage_users.py list                 # 사용자 권한/상태 확인
python websocket_monitor.py --mode both     # 통합 WebSocket 모니터링
python kis_websocket_monitor.py             # KIS WebSocket 모니터링
python upbit_websocket_monitor.py           # Upbit WebSocket 모니터링
```

## 작업 시작 전 필독 계약

작업을 시작하기 전에 이 파일 전체와 아래 계약 파일 전부를 읽어라. 관심 기능만 골라 읽는 것은 허용되지 않는다.

@docs/agent-contracts/mcp-and-analysis-contracts.md
@docs/agent-contracts/broker-and-ledger-contracts.md
@docs/agent-contracts/market-data-and-screener-contracts.md
@docs/agent-contracts/approval-and-shadow-contracts.md
@docs/agent-contracts/data-model-and-symbol-contracts.md
@docs/agent-contracts/developer-reference-contracts.md

필독 파일 중 하나라도 없거나 읽을 수 없으면 작업을 중단하라. 찾아보기 표는 탐색 보조일 뿐 필독 면제가 아니다. 문서 속 예시 명령은 실행 승인이나 운영 권한을 부여하지 않는다.

## 아키텍처

아키텍처의 분야별 기능 계약은 위의 필독 파일에 lossless-first로 분리되어 있다. 아래 최소 하드룰과 명시적 필독 목록은 어떤 진입점에서도 선택 사항이 아니다.

**Compatibility locator — analyze quick fast projection (ROB-1311):** complete contract text remains mandatory in `docs/agent-contracts/market-data-and-screener-contracts.md`. The root locator preserves the quick-only removed-field enumeration required by the static guard: `nxt_tradable`, `price_source`, `session_state`, `krx_prev_close`, `change_pct`, `venue`, `quote_asof`, `delayed`, `price_data_state`, `fresh_artifact_exists`. For a current quote, call `get_quote`.

**Compatibility locator — W5 retry algebra:** the complete mandatory contract text remains in `docs/agent-contracts/approval-and-shadow-contracts.md`. This root-retained, byte-identical source excerpt exists only for the root-only static consumer `tests/services/order_proposals/callback_inbox/test_retry_contract_surfaces.py::test_claude_md_states_the_worker_owned_rule`; it neither replaces nor reduces the required contract file.

**재시도 대수 (🔴 이걸 바꾸기 전에 런북 §5 를 읽을 것)**:
콜백 코어는 모든 예외를 `{"handled": False, "reason": "internal_error"}` 로 삼킨다.
그 문자열은 **브로커 leg 가 시작되지 않았다는 증거가 아니다** —
`revalidate_and_submit` 가 브로커에 닿은 뒤 commit 전에 던져진 예외도 같은 결과이고,
롤백은 nonce 를 **미소비** 상태로, published binding 을 **유효** 상태로 남긴다.
재시도하면 합법으로 보이면서 두 번 제출된다.

따라서 재실행 가능한 유일한 부류는 **코어에 진입하지 않았음이 증명된 실패**뿐:
- `retry_wait` ← **worker-owned pre-core phase 실패만**. 현재 명시적으로
  `PreCoreFailure` 를 만드는 경로는 코어 진입 전 notifier 해석 실패뿐이고,
  `schedule_retry` 가 조건부 UPDATE 로 DB 에서 `state='processing'` +
  `handler_entered_at`/`handler_completed_at`/`terminal_state_pending` 전부 NULL
  임을 재확인해야 기록된다. 🔴 **핸들러가 반환하는
  `mutation_not_started`/`retry`/`retryable`/`safe_to_retry` 는 진단용이며 재실행
  권한을 전혀 만들지 못한다**(`IGNORED_HANDLER_RETRY_KEYS`) — 이미 mutate 한
  핸들러도 똑같이 반환할 수 있기 때문이다. 코어 진입 마커 이후의 모든
  예외·결과·reason 은 terminal(succeeded/discarded/dead_letter)일 뿐 재시도 없음.
  저장 envelope 복원이 불가능하면 `discarded/envelope_invalid`, 현재 chat
  allowlist 에서 빠졌으면 `discarded/chat_revoked` 이며 둘 다 재시도하지 않는다
- `succeeded` ← `handled=True` (`results: ["unverified"]` 포함 — 모호한 *전송*은
  proposal/order 상태머신 소관이고, 콜백 재실행은 해결이 아니라 중복 위험)
- `discarded` ← 명시적 비즈니스 거부(nonce_replay/expired/guard_blocked/…), chat 취소,
  복원 불가 envelope
- `dead_letter` ← 코어 진입 후 `internal_error`·크래시·contract 위반, 또는 3회 소진.
  **자동 replay 없음. 권한 필드 스크럽됨. 운영자가 새 승인 카드를 발급해야 한다.**

내구 마커 3개가 프로세스가 죽은 뒤에도 이 판정을 가능하게 한다:
`handler_entered_at`(코어 호출 직전 단독 커밋 — "진입 전 사망"과 "진입 후 사망"의
유일한 내구 차이) / `handler_completed_at` + `terminal_state_pending`(코어 반환 직후
단독 커밋 — 마지막 커밋을 잃으면 recovery 가 **재실행이 아니라 스크럽 보수**를 한다).
🔴 세 마커는 **인과 순서**이고(완료⇒진입, 판정⇒진입+완료) DB CHECK
(`ck_..._handler_marker_order`)가 강제하며, **단조(monotonic)** 다 — 어떤 API 도
NULL 로 되돌리지 못한다. 재시도가 합법인 이유는 마커를 지웠기 때문이 아니라 CAS
술어가 애초에 NULL 이었음을 증명했기 때문이다. 또한 `processing` 행은
`started_at` 이 NOT NULL 이어야 한다(`ck_..._processing_started_at`) — NULL 이면
staleness 비교가 영원히 거짓이라 복구 스캔에서 보이지 않는다.

### 최소 하드룰 — 절대 위반 금지

1. **런타임 LLM 경계 (ROB-501)**: `app/**` 런타임 코드에 in-process LLM provider
   (Gemini/OpenAI/Grok 등) import·인스턴스화 금지. LLM 판단은 out-of-process MCP consumer 몫.
   정적 가드 테스트(`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`)가
   이를 스캔한다.
2. **브랜치 보호**: `main`·`production` 직접 push 금지. 모든 코드 변경은 feature branch + PR.
3. **Worktree 규칙**: canonical repo `/Users/mgh3326/work/auto_trader`는 항상 `main` 체크아웃 고정.
   코드 변경은 `/Users/mgh3326/work/auto_trader.<issue-id>` worktree에서 수행.
   머지된 브랜치 위에 계속 커밋 금지 — follow-up은 최신 `origin/main` 기준 새 브랜치로 시작.
4. **브로커 실행 표면은 전부 default-disabled**: demo/mock/live 실행 경로는 env 게이트
   (예: `BINANCE_SPOT_DEMO_ENABLED`, `KIWOOM_MOCK_ENABLED`, `TOSS_API_ENABLED`) +
   per-call `confirm=True` 이중 게이트 뒤에 있다. **게이트 완화, 호스트 allowlist 확장,
   fail-closed→fail-open 전환, 하드 인바리언트 상수(레버리지 1x, notional cap, 동시 포지션
   상한 등) 변경 금지.** 이런 변경이 필요해 보이면 멈추고 운영자에게 보고하라.
5. **레저 쓰기는 서비스 레이어 경유만**: `alpaca_paper_order_ledger`,
   `binance_demo_order_ledger`, `kis_live_order_ledger`, `live_order_ledger`,
   `toss_live_order_ledger` 등 주문 레저에 직접 SQL INSERT/UPDATE/DELETE 금지.
   fill 기록은 evidence-first — 브로커 증거 없이 `filled` 마킹 금지.
6. **스케줄러 등록 금지**: 신규 TaskIQ/cron/Prefect 스케줄 연결은 명시 승인 없이 금지.
   기본은 scheduleless 출고(CLI/수동 lever만).
7. **kis_mock 주문은 귀속 없이 나가지 않는다**: `place_order(is_mock=True)` /
   `account_mode="kis_mock"` 는 **`strategy` 필수**. 브로커 전송 **이전에**
   `review.kis_mock_signal_ledger` 에 신호 행이 커밋돼야 하고, 실패하면 주문을 보내지 않는다
   (`error_code`: `attribution_required` / `signal_record_unavailable`).
   **이 게이트를 우회·완화하거나 placeholder strategy 를 만들어 넣지 마라** — 귀속 불가는
   값이 아니라 에러다. 귀속을 못 정하겠으면 멈추고 운영자에게 보고하라.
8. **심볼 형식**: DB 기준은 `.` 구분(`BRK.B`). 변환은 `app/core/symbol.py`
   (`to_kis_symbol`/`to_yahoo_symbol`/`to_db_symbol`)만 사용하고 직접 문자열 치환 금지.
9. **검증·보고 규율**: 완료 주장 전 관련 테스트를 실제 실행하고 결과 원문을 보고하라.
   실패·스킵을 성공으로 보고 금지. push 완료 주장은 `git ls-remote`로 대조 가능해야 한다.
10. **Secrets**: API 키·토큰 repo 커밋 금지. 로그·보고에 secret 값 출력 금지
   (missing env는 key 이름만 보고).
11. **Telegram 승인 콜백 durable inbox (W5)**: 게이트 3종
    (`ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED` / `..._WORKER_ENABLED` /
    `..._RECOVERY_SCHEDULE_ENABLED`) 전부 default false 유지. 콜백 코어의 게이트
    (published-binding preflight, 단일소비 nonce, commit lease, target lock,
    approval hash) 우회·이동 금지. **코어 진입 후의 generic `internal_error` 를
    재시도로 바꾸지 마라** — 브로커 미전송 증거가 아니라서 재주문이 된다
    (`docs/runbooks/telegram-callback-durable-inbox.md` §5). terminal 스크럽 DB
    CHECK 완화 금지. W5 TaskIQ envelope/result boundary는 job의 canonical UUID
    하나 또는 빈 recovery envelope만 허용하며, incoming retry/private labels와
    label metadata를 폐기하고 SmartRetry 권한을 주지 않는다. raw args/kwargs/
    labels/results/exception strings를 확장하거나 로그·Sentry·결과에 남기지 마라.
    `callback_query.from`은 exact built-in dict, `from.id`는 required exact
    bounded int, present `update_id`는 매번 exact bounded int여야 하며 coercion은
    금지한다. terminal 11-field scrub과 마지막 `update_identity_digest`를
    완화하지 마라.
12. **매수 게이트 A/B shadow (ROB-1301)**: variant B는 순수 기록이다.
    라이브 게이트 문언·주문·워치·제안 승격 금지. 채점 전 중간값으로 정책
    변경 금지. 스케줄러/자동화 트리거로 연결하지 마라. Q6 epoch의
    `collection_armed_at`/고정 28일 경계/policy projection hash는 변경 금지이며,
    `first_valid_record_at`으로 시작점을 옮기지 마라. 0건도
    `INSUFFICIENT_SAMPLE / NO_FIRING`으로 종료하고 readiness는 창 종료와 전 사건
    성숙의 AND다. caller wiring은 epoch marker serving 뒤 별도 PR에서만 한다.
13. **screener pick log**: `buy_candidate_fanout` 안에서 쓰지 마라 (no-write
    계약). 관측 레코더는 바깥, `SCREENER_PICK_LOG_ENABLED` 기본 false,
    fail-open, 스케줄러 금지. 가격은 exact decimal 문자열.
14. **NHPLUG 모의 read-only (Stage 1)**: 데이터는 `moapi.nhplug.com:8443`만,
    `/n2/acctinfo`의 `acct_type=03` allowlist만 사용하며, build 후 scheme·host·port와
    `act_no`를 send 직전에 재검증한다. 동일 계좌번호의 상충 type 응답은 거부한다. 운영 OAuth 호스트는 `nhplug/auth.py`의
    token/revoke 2 path 예외뿐이다. `NHPLUG_MOCK_ENABLED` default false 유지,
    vendor SDK·`NHPLUG_BASE_URL`/`NHPLUG_AUTH_URL`·주문 endpoint/TR 추가 금지.
    주문 메서드/MCP/레저/reconcile/스케줄러는 Stage 1 범위 밖이다.
15. **Kiwoom ACCEPTANCE authority cessation (ROB-1340)**: confirmed BUY·cancel·reconcile은
    하나의 PostgreSQL coordination scope에서만 수행한다. cancel 직전 ownership 상실 시
    취소를 보내지 말고 cycle JSON live-order-risk를 먼저 append한 뒤 기존 Telegram
    notifier로 알린다. authority attempt/terminal DB 행은 append-only이며 current-cycle
    완전 열거 + exact committed receipt coverage + active-hold 0일 때만
    `RELEASE_VERIFIED`다. 빈 lock, pool close, PID 부재만으로 승격 금지.

16. **execution-ledger HTTP ingest (fillwire P0)**: `EXECUTION_LEDGER_INGEST_TOKEN`
    미설정 403 · 오류 401 을 유지하고 세션 쿠키가 이 토큰을 대체하게 만들지 마라.
    멱등키 `(broker, account_mode, venue, broker_order_id, fill_seq)` 를 약화하지 말고
    (`venue`/`account_mode` 제거 금지), 외부 ingest 행의 `source` 는 항상 `websocket`
    강제 · outer `source_run_id` 가 authority 다. 항목별 savepoint 를 전체 롤백으로
    바꾸지 마라. 원장 커밋이 권한자이며 다운스트림 실패로 커밋된 fill 을 되돌리지 마라.
    reconcile 트리거는 기존 커널만 호출하고 `dry_run` 기본 true · dry-run `backfilled=0`
    을 유지한다. `WS_LEDGER_SINK` 기본 `db` 유지, `http` 실패는 DB fail-open(유실 0),
    `WS_LEDGER_SINK_URL` 의 loopback/exact-path/no-userinfo 검증과
    `follow_redirects=False` 를 완화하지 마라. 마이그레이션·스케줄러 추가 금지.

17. **MCP 레인 계약**: `config/mcp_lane_allowlists/`의 `tool<TAB>basis`를 보존하고,
    등록 변경은 `tests/mcp_server/test_lane_allowlist_contract.py`와
    `test_profile_tool_snapshot.py`로 검증한다. D 제거·C niche 관측의 감사 범위와
    보존 예외는 `docs/runbooks/mcp-surface-cleanup-20260905.md`를 따른다.

## 계약 찾아보기

이 표는 작업 위치를 찾는 보조 수단이다. 표의 행 하나만 읽어도 된다는 뜻이 아니며, 위의 모든 파일을 읽은 뒤에만 사용한다.

| 작업 분야 | 필독 계약 파일 | 포함된 기준 기능 절 |
|---|---|---|
| MCP·분석 | [MCP·분석 계약](docs/agent-contracts/mcp-and-analysis-contracts.md) | MCP 표면, LLM 경계, Hermes, 리포트·뉴스 계약 |
| 브로커·레저 | [브로커·레저 계약](docs/agent-contracts/broker-and-ledger-contracts.md) | Alpaca/Binance/KIS/Kiwoom/NHPLUG/토스, execution ledger |
| 시장 데이터·스크리너 | [시장 데이터·스크리너 계약](docs/agent-contracts/market-data-and-screener-contracts.md) | 시장 이벤트·리서치·스크리너·freshness |
| 승인·shadow | [승인·shadow 계약](docs/agent-contracts/approval-and-shadow-contracts.md) | Telegram durable inbox, buy gate A/B |
| 데이터 모델·심볼 | [데이터 모델·심볼 계약](docs/agent-contracts/data-model-and-symbol-contracts.md) | DB 구조, 심볼, API 클라이언트, 정책 YAML |
| 개발 참고 | [개발 참고 계약](docs/agent-contracts/developer-reference-contracts.md) | 환경, 테스트, 문제 해결, 참고 문서 |

## 유지 규약

새 기능은 관련 분야 계약에 같은 PR에서 추가한다. 새 안전 경계·계약이 생기면 두 진입점의 최소 하드룰과 명시적 필독 목록도 필요 시 같은 PR에서 함께 갱신한다. 이 구조는 총 필독 토큰 감소를 주장하지 않는다.

## 브랜치 & PR 워크플로우

### 브랜치 보호
- **main**, **production** 브랜치는 보호됨 — 직접 push 금지
- 모든 코드 변경은 Pull Request를 통해 머지

### 브랜치 역할
- **main**: 개발 브랜치 (모든 PR의 base)
- **production**: 배포 브랜치 (GHCR 이미지 빌드 트리거)

### 브랜치 네이밍
```
feature/<task-id>-<설명>     # 새 기능 (예: feature/ROB-16-branch-protection)
fix/<task-id>-<설명>         # 버그 수정
chore/<설명>                 # 유지보수
```

### 워크플로우
1. `main` 브랜치에서 feature branch 생성
2. 코드 변경 후 커밋
3. PR 생성 (base: `main`)
4. 리뷰 후 머지
5. 배포 시 `main` → `production` 머지

### Worktree 운영 규칙 (필수)

**canonical repo `/Users/mgh3326/work/auto_trader` 는 항상 `main` 체크아웃 고정. 배포 머지 시에만 `production` 으로 일시 전환.** canonical repo에서 feature/fix 브랜치를 체크아웃하거나 작업하지 않습니다.

코드 변경은 worktree에서 수행합니다. 다만 **새 Linear 이슈/병렬 작업**과 **같은 Linear 이슈의 follow-up**을 구분합니다:

- 새 Linear 이슈, 병렬 작업, 기존 worktree가 dirty인 경우, 또는 이전 diff/reference를 보존해야 하는 경우: 새 worktree를 만듭니다.
- 같은 Linear 이슈의 follow-up이고 기존 issue worktree가 clean하며 재사용 가능하면: 기존 worktree를 재사용해도 됩니다. 물리 worktree를 매번 새로 만드는 것이 필수는 아닙니다.
- PR이 merge된 브랜치 위에서 계속 커밋하지 않습니다. follow-up 작업은 항상 최신 `origin/main` 기준 새 branch로 시작합니다.
- worktree 재사용 전에는 `git status --short`, 필요한 diff/reference 백업, `git fetch --prune`을 먼저 확인합니다.

```bash
# canonical repo 업데이트
cd /Users/mgh3326/work/auto_trader
git fetch --prune origin
git switch main
git pull --ff-only

# 새 Linear 이슈/병렬 작업: 새 worktree 생성
git worktree add ../auto_trader.<issue-id> -b <branch-name> origin/main

# 같은 Linear 이슈 follow-up: 기존 worktree가 clean하면 재사용
cd /Users/mgh3326/work/auto_trader.<issue-id>
git status --short
git fetch --prune origin
git switch -c <new-followup-branch> origin/main

# PR 머지 후 정리 (필요 diff/reference가 없고 clean한 상태에서)
cd /Users/mgh3326/work/auto_trader
git worktree remove ../auto_trader.<issue-id>
git branch -D <branch-name>
```

- **표준 worktree 경로**: `/Users/mgh3326/work/auto_trader.<issue-id>` (예: `/Users/mgh3326/work/auto_trader.rob-287`)
- 이전 경로 `.claude/worktrees/`, `~/.claude/worktrees/`, `~/auto_trader/.worktrees/` 는 deprecated — 남아 있다면 표준 경로로 이관하거나 prune
- 이관: `git worktree move <old-path> <new-path>` (dirty 없는 상태에서)
- 삭제된 원격 브랜치(`upstream gone`) 는 주기적으로 `git fetch --prune && git branch -vv | grep ': gone\]'` 로 확인하고 정리

### CI required check — `ci-required` shadow 집계 (ROB-1294)

branch protection 은 지금도 `lint` · `taskiq-smoke` · `test (3.13, 1..4)` **여섯 이름에 직접**
결합돼 있다. shard 수·lane topology 를 바꾸려면 branch protection 편집이 함께 필요하고,
그 편집을 빠뜨리면 required check 가 영구 pending 이거나 조용히 미강제가 된다.

- **분류기**: `scripts/ci/classify_changes.py` — 변경 경로 → lane 결정적 매핑 (stdlib only)
- **집계기**: `scripts/ci/aggregate_required.py` — 고정 이름 게이트의 판정 로직
- **워크플로우 job**: `.github/workflows/test.yml` 의 `change-classifier` · `ci-required`
- **계약 테스트**: `tests/ci/`
- **런북**: `docs/runbooks/ci-required-aggregator.md`

**현재 상태 = shadow.** 🔴 `ci-required` 는 **required check 가 아니며** 이 작업은 branch
protection/GitHub 설정을 하나도 쓰지 않았다. 분류기 출력은 어떤 job 도 skip 시키지 않는다
(`tests/ci/test_ci_required_workflow_contract.py` 가 이를 기계 검증한다 — 기존 여섯 job 의
표시 이름·matrix·`if` 부재가 변하면 red).

**fail-closed 규칙**: 분류기는 unknown path·rename/copy/delete·공유 CI/config/test 인프라·
빈 change set·base SHA 부재를 전부 `run_all=true` 로 떨어뜨리고, 지정된 SHA 해석 실패·git
실패·malformed diff 는 성공으로 세탁하지 않고 **job 을 red** 로 만든다. 집계기는 child 의
`failure`/`cancelled`/미인가 `skipped`/결과 부재/미지의 결과 문자열을 전부 red 로 보며,
`--authorize-skip` 로 명시된 skip 만 green 이다 (워크플로우는 현재 아무것도 인가하지 않음).

**cutover 는 운영자 전용이며 이번 범위 밖** — 절차는 런북 §5.

## 주요 워크플로우

### 1. 데이터베이스 모델 변경

```bash
# 1. app/models/에서 모델 수정
# 2. 마이그레이션 자동 생성
uv run alembic revision --autogenerate -m "description"

# 3. 생성된 마이그레이션 파일 검토 (alembic/versions/)
# 4. 적용
uv run alembic upgrade head

# 5. 문제 시 롤백
uv run alembic downgrade -1
```

**중요:** Alembic은 async 엔진 사용 - `alembic/env.py` 참고
