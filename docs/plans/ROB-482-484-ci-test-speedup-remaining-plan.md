# ROB-482/483/484 CI 테스트 속도 — 미비 작업 실행 플랜

작성: 2026-06-10 (검증 세션). 실행: 트랙별 별도 세션(worktree). 검증: 플랜 작성 세션이 수행.
Linear 원본: ROB-482 코멘트 스레드(검증+Track A), ROB-483 코멘트(통합 플랜), ROB-484 본문(Track E).

## 0. 현재 상태 (2026-06-10 실측)

| 항목 | 상태 |
|---|---|
| ROB-482 (1단계: sysmon+concurrency+모킹) | **Done** — PR #1221 머지. wall 9:35→6:39, "Run tests" 8:58→5:53, pytest 529.68s→345.95s |
| ROB-483 Phase A (worksteal trial) | **In Progress** — PR #1222 OPEN, 채택 기준 미충족 (아래 Track C) |
| ROB-483 Phase B (4-shard 샤딩) | 미착수 |
| ROB-484 (required checks + auto-merge) | **완전 미착수** — `allow_auto_merge=false`, required checks 404 |

ROB-482 검증에서 발견된 잔여 갭:

1. **새 31초 straggler**: `tests/test_mcp_screen_stocks_filters_and_rsi.py::TestScreenStocksPhase2Spec::test_kr_etf_category_semiconductor` 31.01s (최신 main run slowest-25 1위). 구현이 스펙의 전역 choke point 대신 개별 테스트에 `_can_use_tvscreener_stock_path → False` 패치 방식을 써서, 미커버 파일의 KR 경로가 여전히 **실제 TradingView HTTP**(capability probe, 30s timeout)를 발생시킴.
2. **worksteal trial 오염**: PR #1222에 dist와 무관한 앱 코드(`candidate_universe.py`) + action_report 테스트 수정 다수 혼입. 또한 trial run 1건이 실제 격리 위반으로 실패(`test_journal_collector.py::test_empty_active_reports_ok_status` — 타 테스트가 commit한 row 가시화. 이슈 본문이 예측한 '전역 테이블 읽기' 클래스의 실발현).
3. **worksteal 이득 증거 없음**: loadfile(main) pytest 345.95s vs worksteal(trial) 386.43s — 각 1샘플. ROB-482가 straggler를 제거해 원래 전제(~50s 이득)가 약화됨.

## 1. 트랙 구조와 의존성

```
Track A (tvscreener 전역 차단)  ─┐
                                ├─→ Track C (worksteal A/B 판정) ─→ Track D (샤딩)
Track B (#1222 분리)            ─┘
Track E (ROB-484 설정)  — 즉시 가능 (D 머지 시점에 contexts 갱신 dance만 조율)
```

- A ∥ B: 병렬 가능 (파일 겹침 없음 — A는 conftest/tvscreener 테스트, B는 candidate_universe/action_report).
- 모든 코드 트랙: origin/main 기준 새 브랜치 + 표준 worktree (`/Users/mgh3326/work/auto_trader.<id>`), TDD, ruff는 **app/ + tests/ 둘 다** (CI lint가 둘 다 검사).

---

## 2. Track A — tvscreener 전역 네트워크 차단 (ROB-482 follow-up PR)

**브랜치**: `rob-482-tvscreener-global-block` / PR 제목 `test(ROB-482): ...` (이슈 재사용, 새 Linear 이슈 생성 금지 — 쿼타 ~243/250)

**목표**: 테스트 스위트 어디서도 실 scanner.tradingview.com HTTP가 발생하지 않도록 구조적으로 차단. ~31초 + 잔여 네트워크 flake 제거.

**구현 (TDD)**:

1. RED 먼저: stub 없는 상태에서 `test_kr_etf_category_semiconductor`가 네트워크에 의존함을 확인 (CI 31.01s의 메커니즘: `fetch_etf_all_cached`만 모킹, KR 경로 capability probe는 미차단).
2. `tests/conftest.py`에 autouse fixture 추가: tvscreener HTTP 경계를 즉시 `TvScreenerError` raise stub으로 패치.
   - choke point 후보 (좁은 쪽 선택): ① `app.services.tvscreener_retry.fetch_tvscreener_with_retry` ② `tvscreener_service.py`의 lib 호출 지점(`query_stock_screener` 등).
   - **import-site 바인딩 주의**: 호출 시점 lookup이 일어나는 지점에 패치할 것 (모듈이 `from X import f`로 가져간 이름은 X 패치로 안 잡힘).
3. **예외 표면** — 이 경계를 직접 사용하는 5개 파일은 전부 로컬 실행으로 영향 확인, 깨지는 케이스는 marker(예: `tvscreener_boundary`) opt-out 또는 fixture override 제공:
   - `tests/test_tvscreener_stocks.py`, `test_tvscreener_capabilities.py`, `test_tvscreener_integration.py`, `test_tvscreener_stock_enrichment_fields.py`, `test_invest_kr_fundamentals_snapshots_provider.py`
4. 동일 계열 의심 잔존 테스트 함께 확인: `test_us_early_return_filters_applied_complete`(5.04s), `test_kr_dividend_yield_equivalence`(4.00s), `test_kr_stocks_default`(7.2s), `test_screen_stocks_smoke`(6.06s).
5. (선택 정리) PR #1221이 넣은 개별 `_can_use_tvscreener_stock_path → False` 패치들은 전역 stub과 중복되면 제거 가능 — 단 테스트 의도(legacy 경로 강제)가 있으면 유지.

**Acceptance**:
- 로컬 풀스위트 green + ruff(app/+tests/).
- CI green 후: slowest-25에 screen_stocks/tvscreener 계열 **>5s 0건**, CI 로그 `scanner.tradingview.com` **0건**.
- 기대: pytest ~346s → ~310s.

---

## 3. Track B — PR #1222에서 비-dist 변경 분리 (ROB-483 fix PR)

**브랜치**: `rob-483-split-nondisk-fixes` (origin/main 기준) / PR 제목 `fix(ROB-483): ...`

**대상**: #1222의 커밋 `53c34e95`(test: normalize worksteal setup contracts) 중심 cherry-pick.

1. **커밋 ① (앱 fix)**: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`의 `_preset_loader_rows` + 회귀 테스트.
   - 먼저 **main에서 재현 여부 판별**: `_SnapshotLoadResult`는 ROB-427 PR3(#1130)/ROB-440(#1141)이 도입한 로더 계약. ROB-345/347 collector(`_collect_kr_presets`)가 이 계약을 반환하는 로더를 실제로 호출하면 main에서도 깨지는 **실 버그** → PR 본문에 lineage 명시 + 재현 테스트. 테스트 경로에서만 도달하면 defensive normalization으로 명명.
2. **커밋 ② (테스트 격리 hardening)**: `tests/_investment_reports_helpers.py` truncate 강화 + `test_journal_collector` 등 action_report 테스트 수정 — dist 전략과 무관하게 유익한 위생 + worksteal enabling.
3. PR-B 머지 후 **#1222를 rebase하여 `.github/workflows/test.yml`의 `--dist=worksteal` 1줄만 남김**.

**Acceptance**: PR-B CI green; rebase 후 #1222 diff가 test.yml 1줄.

---

## 4. Track C — worksteal A/B 재측정 + 채택 판정 (A·B 머지 후)

**판정 룰 사전 고정 — 사후 조정 금지**:

1. #1222를 A·B 포함 origin/main에 rebase → empty commit으로 **CI 3회** 트리거.
2. 같은 base의 main(loadfile) run 3회와 pytest 시간 비교 (부족하면 main empty-commit으로 보충).
3. **채택 조건: ① 3연속 green AND ② worksteal 메디안이 loadfile 메디안보다 ≥20s 빠름.**
4. 하나라도 미충족 → **PR 닫고 loadfile 유지**, 부정 결과를 ROB-483에 기록하고 Phase B는 loadfile로 진행.

**증거**: run id 6개 + pytest 시간 표 + 결론 코멘트 (ROB-483).

**배경**: 미채택이 디폴트. 격리 위반이 이미 1회 실발현된 변경이므로, 이득이 증명될 때만 리스크를 받는다.

---

## 5. Track D — Phase B: pytest-split 4-shard 샤딩 (C 판정 후)

ROB-483 본문 스펙 그대로. 핵심 체크리스트:

- [ ] `pytest-split` **0.11.0+** (pytest 9.0.1 호환; 구버전은 pytest<9 캡) test 그룹 추가
- [ ] matrix `group: [1,2,3,4]` + `--splits 4 --group N --durations-path .test_durations`, `--dist`는 Track C 결론
- [ ] `pytest --store-durations`로 `.test_durations` 생성·커밋 + 갱신 주기 문서화
- [ ] shard 커맨드에서 `--cov-fail-under=30` **제거** (부분 커버리지 스퓨리어스 실패) → codecov project status로 대체
- [ ] codecov-action `flags: shard${{ matrix.group }}` (같은 SHA 다중 업로드 자동 병합)
- [ ] **머지 dance** (Track E가 먼저 적용된 경우, PR 본문 런북에 포함): ① 머지 직전 required contexts에서 `test (3.13)` 제거 ② 샤딩 PR 머지 ③ `test (3.13, 1)`~`(3.13, 4)` 등록

**Acceptance**: 전체 wall ≤4:30, shard 간 실행 편차 <20%, 파일 경계 분할로 인한 신규 flake 0 (3연속 green).

---

## 6. Track E — ROB-484: required checks + auto-merge (operator, user 승인 게이트)

코드 PR 없음. gh api 2건 + 검증. Track D 이전 적용 가능·권장 (즉시 가치).

1. required status checks 등록 — contexts `["lint", "test (3.13)", "taskiq-smoke"]` (`security`는 `|| true`라 항상 green — 제외). **PUT은 전체 교체** — 기존 `required_pull_request_reviews`(approving 1명) 재명시.
2. `gh api -X PATCH repos/mgh3326/auto_trader -f allow_auto_merge=true`
3. 운영 전환: PR마다 `gh pr merge --auto --squash --delete-branch` 큐잉, main push 실패는 Discord notify에 위임.

⚠️ docs-only 스킵을 도입한다면 workflow-level `paths-ignore` 금지 (required check 미보고 → 머지 영구 블록). in-job early-exit(dorny/paths-filter)만 호환.

---

## 7. 공통 검증 프로토콜 (각 트랙 완료 시 검증 세션에 제출)

- PR 링크 + CI run id + 실측 수치 (slowest-25, pytest 시간)
- 로컬 풀스위트·ruff(app/+tests/) 결과
- 검증 세션 확인 항목: 스펙 대비 diff / CI 로그 네트워크 호출 부재(A) / trial 순수성·판정 룰 준수(C) / required checks 정합(D·E)

## 8. 세션 킥오프 문구

- Track A: "docs/plans/ROB-482-484-ci-test-speedup-remaining-plan.md §2 (Track A)대로 tvscreener 전역 차단 follow-up PR을 TDD로 구현해줘"
- Track B: "같은 플랜 §3 (Track B)대로 PR #1222에서 비-dist 변경을 분리해줘"
- Track C/D: A·B 머지 후 §4/§5 지정

## 9. 기각 확정 (재조사 불필요, 실측 근거)

larger runner(개인 계정 불가 + 수집 100s는 워커당 직렬), conftest 분리/`-p no:`/collect_ignore(효과 0), testmon(공유 DB fixture false-green), PR에서 --cov 제거(sysmon과 택1, sysmon 적용됨).
