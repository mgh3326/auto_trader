# AGENTS.md — 외부 에이전트(Codex 등) 진입점

> **이 파일은 얇은 포인터다. 정본은 `CLAUDE.md`.**
> 작업을 시작하기 전에 리포지토리 루트의 `CLAUDE.md`를 **전부** 읽어라. 아키텍처, 명령어,
> 표면별 계약(레저/브로커/MCP), 런북 맵은 모두 거기에 있고, 이 파일과 충돌하면 `CLAUDE.md`가
> 이긴다. 아래 하드룰은 `CLAUDE.md`에서 추린 요약이며, `CLAUDE.md`를 읽지 못한 경우에도
> 절대 위반하면 안 되는 최소 집합이다.

## 하드룰 — 절대 위반 금지

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
    변경 금지. 스케줄러/자동화 트리거로 연결하지 마라.
13. **screener pick log**: `buy_candidate_fanout` 안에서 쓰지 마라 (no-write
    계약). 관측 레코더는 바깥, `SCREENER_PICK_LOG_ENABLED` 기본 false,
    fail-open, 스케줄러 금지. 가격은 exact decimal 문자열.
14. **NHPLUG 모의 read-only (Stage 1)**: 데이터는 `moapi.nhplug.com:8443`만,
    `/n2/acctinfo`의 `acct_type=03` allowlist만 사용하며, build 후 scheme·host·port와
    `act_no`를 send 직전에 재검증한다. 동일 계좌번호의 상충 type 응답은 거부한다. 운영 OAuth 호스트는 `nhplug/auth.py`의
    token/revoke 2 path 예외뿐이다. `NHPLUG_MOCK_ENABLED` default false 유지,
    vendor SDK·`NHPLUG_BASE_URL`/`NHPLUG_AUTH_URL`·주문 endpoint/TR 추가 금지.
    주문 메서드/MCP/레저/reconcile/스케줄러는 Stage 1 범위 밖이다.

## 최소 명령어

```bash
uv sync --all-groups        # 의존성 (test/dev 포함)
make test                   # 테스트 (make test-unit / make test-integration)
make lint                   # Ruff + ty
make format                 # 포맷팅
uv run alembic upgrade head # DB 마이그레이션
```

## 더 읽을 것

- `CLAUDE.md` — 정본 (아키텍처, 표면별 계약, 워크플로우, 문제 해결 전체)
- `docs/runbooks/` — 실행 표면별 런북 (smoke CLI, reconcile, 활성화 절차)

## 유지 규약

`CLAUDE.md`에 새 안전 경계·계약이 추가되면 이 파일의 하드룰 요약도 같은 PR에서 갱신한다.
이 파일이 `CLAUDE.md`와 어긋나면 그 자체가 버그다.
