# CIO Scout Report Quality Gate — Runbook

Owner: CIO · 관련 설계: [ROB-170](/ROB/issues/ROB-170) ([plan](/ROB/issues/ROB-170#document-plan)) / [ROB-172](/ROB/issues/ROB-172) / [ROB-197](/ROB/issues/ROB-197)

Scout Report 수신 시 CIO가 수동으로 G1~G6 sweep 하지 않도록 **자동 체크리스트** + reopen 결정 플로우.

## 1. Heartbeat 시작 시 실행

```bash
# 로컬 파일
uv run python scripts/cio_quality_gate.py path/to/scout_report.md

# 표준입력 (Scout comment 본문 복붙)
uv run python scripts/cio_quality_gate.py --stdin < scout.md

# Paperclip 이슈에서 직접 (가장 큰 comment = Scout Report 가정)
uv run python scripts/cio_quality_gate.py --paperclip-issue ROB-158
```

Exit code:
- `0` — 모든 gate pass
- `1` — soft-gate만 fail (ACCEPT-WITH-FLAG)
- `2` — hard-gate 위반 (REOPEN)

`--json` 플래그로 머신리더블 출력 (CI/CD·Paperclip automation 편입 시 사용).

## 2. Decision Flow

```
Scout Report 수신
  ↓
cio_quality_gate.py 실행
  ↓
┌─────────────────────────────────────────────────────┐
│ exit 2 (hard-gate fail)                             │
│   → Scout 이슈에 §7.2 reopen 코멘트 (스크립트가 자동 초안) │
│   → CIO 해당 heartbeat에서는 보드-facing 진행 중단       │
│   → Scout 재제출 대기                                  │
├─────────────────────────────────────────────────────┤
│ exit 1 (soft-gate만 fail)                           │
│   → CIO가 근거와 함께 채택 가능 (ACCEPT-WITH-FLAG)     │
│   → 최종안 본문에 soft-gate 위반 한계 명시 필수         │
│   → Investment Reviewer 의무 호출 (hard gate 편입 사례) │
├─────────────────────────────────────────────────────┤
│ exit 0 (ACCEPT)                                      │
│   → 그대로 TC briefing 흐름 진입                        │
└─────────────────────────────────────────────────────┘
```

CIO는 스크립트 결과를 **추가 판단 재료**로 사용한다. 다음은 스크립트가 잡지 못할 수 있으므로 CIO가 별도 확인:

- sector/테마 집중도 (5% hard gate는 CEO layer)
- 직전 heartbeat 대비 thesis 변경 일관성
- 예외적으로 강한 fundamental catalyst로 과열 지표 정당화 가능한 경우 (§3.3 Tier A→C 승격)

## 3. Gate 정의 요약

| key | label | severity | 스크립트 검출 방식 |
|---|---|---|---|
| G1 | Depth | hard | 후보별 same-depth-check (`#7 AND #1~#6·#8 중 6개+`) 적용. fail 1건 이상이면 hit |
| G2 | Grouped rejection | soft | `전원/모두 ... microcap/SPAC/REIT/avoid` 패턴 + 근접한 개별 6자리 code 수가 부족할 때 hit |
| G3 | Tool failure disclosure | hard | `schema mismatch`, `rate limit`, `fallback`, `retry` 등 signal 검출 + `### 제한사항` 섹션 부재 |
| G4 | Execution path | hard | `[신규]` 후보 row에서 execution cell이 bare `KIS`/`Toss` (qualifier 없음)이면 hit |
| G5 | DCA vs 신규 비교 | soft | 후보 문맥에 `대비/우위/열위/vs/비교` 중 어느 것도 없으면 hit |
| G6 | Budget reality | hard | `get_cash_balance` 호출 흔적 없음, **또는** 주문안 총액 > 예수금 × 1.5 인데 disclosure 없음 |

Hard-gate 위반 = reopen 필수. Soft-gate 위반 = CIO 재량 채택 가능 + 한계 명시.

### 3.1 per-candidate same-depth-check 공식 (§3.2 plan)

```
pass                := (#7 기록) AND (#1~#6, #8 중 6개 이상 기록)
pass (avoid-simp.)  := #1 + #2 부분 기록 + avoid 사유 + grouped 여부 검토
fail                := 위 조건 불충족
```

8 항목: (1) Source (2) Quote (3) Indicators (4) S/R (5) News (6) Fundamental/consensus (7) Execution path (8) DCA 비교.

## 4. Reopen 코멘트 템플릿 (§7.2)

스크립트가 hard-gate fail 시 자동 초안을 출력한다. 그대로 복사 → Scout 이슈에 붙여넣고 `재요청 범위`만 실제 후보명으로 다듬는다.

```markdown
## Scout reopen 요청 — same-depth gate 위반

- 위반 gate: {violations}
- 구체 사항:
  - G1 Depth: fail 후보: 삼성전기 (fail), 삼성SDI (fail)
  - G6 Budget reality: get_cash_balance 호출: 없음 / disclosure: 있음
- 재요청 범위: 위반 gate에서 지적된 후보 deep-dive + 누락된 disclosure 보충
- 기대 산출물: §3 checklist 기준 `same-depth-check = pass` Scout Report v2
```

## 5. Soft-gate 채택 시 최종안 flag 문구

Soft-gate만 위반한 상태로 CIO가 채택하는 경우, 보드-facing 최종안 요약에 한 줄 한계 명시:

```markdown
> same-depth status: PARTIAL — G2 grouped rejection exception 검증 부족
> (오버솔드 11종 중 3종만 개별 분해, 나머지 grouped로 처리).
> 실질 영향 낮음 (메인보드 + 유동성 기준으로 후보 생성 자체가 0건에 수렴).
```

## 6. ROB-158 검증 기대치

스크립트를 과거 Scout Report([ROB-158](/ROB/issues/ROB-158))에 돌리면 다음이 hit (smoke test: `uv run pytest tests/test_cio_quality_gate.py -v`):

- **G1 Depth (hard)** — 13건 fail (ROB-158이 Scout Report v1 구조라 각 row에 news/fundamental/비교 서브라인이 embedded 되어 있지 않음. 최악의 fail은 삼성전기(009150), 삼성SDI(006400) — RSI + 목표가만 기록)
- **G4 Execution path (hard)** — Krafton(259960), LG이노텍(011070) 신규 후보 execution cell이 bare `KIS` (qualifier 없음)
- **G6 Budget reality (hard)** — `get_cash_balance` 호출 흔적 없음 + 주문안 총액 ~₩14.7M (Tier 1+2 합산)

Soft-gate (경고만):
- **G2 Grouped rejection (soft)** — `오버솔드 상위 전원 ... (SKonec·Alpha AI·JR GLOBAL REIT 등)` 개별 분해 부족

스크립트 exit code: `2` (REOPEN).

### v1 → v2 format 주의

현행 스크립트의 G1 pass 공식은 v2 템플릿([ROB-170](/ROB/issues/ROB-170) §6.2) 기준 per-candidate 서브라인이 각 항목(news/fundamental/DCA 비교 등)을 명시한다는 전제로 동작한다. ROB-158처럼 v1 14컬럼 단일 테이블 구조에서는 news/fundamental/비교가 row 밖 narrative에 있어 per-row 파서가 감지하지 못한다 → **v1 format 입력에서는 G1 over-report가 정상**이다. v2 마이그레이션([ROB-170](/ROB/issues/ROB-170) §9 #2) 이후 재실측하면 실제 fail 건수로 수렴할 것.

## 7. 한계 / 추후 보강

- 파서는 휴리스틱 기반이다. 표 구조가 Scout AGENTS.md v2 템플릿([ROB-170](/ROB/issues/ROB-170) §6.2)과 크게 다르면 false positive / negative 발생 가능 → [ROB-172](/ROB/issues/ROB-172) 후속 실행 후 실제 heartbeat에서 검증하며 튜닝.
- G6 예수금 실수령값이 Report 본문에 없고 외부 state에서만 알 수 있는 경우 `--cash` CLI arg로 주입 지원 예정.
- 스크립트 false positive로 CIO가 수동 override한 사례는 본 runbook §5 flag 문구에 이력으로 남겨 튜닝 feedback loop에 사용.
