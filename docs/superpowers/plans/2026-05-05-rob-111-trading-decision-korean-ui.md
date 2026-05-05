# ROB-111 Trading Decision Korean UI / ko-KR Locale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `frontend/trading-decision` Korean-by-default — translate static UI chrome, switch date/number formatting to `ko-KR`, and replace raw enum/token/JSON exposure with Korean display labels — without changing API payloads, DB rows, or TradingAgents free-text output.

**Architecture:** Add a single lightweight display-only label layer at `src/i18n/` (no external i18n library). Centralize all enum→Korean maps in `ko.ts` and reusable helpers (`labelOrToken`, `labelOrderSide`, `labelOperatorToken`, formatter wrappers) in `formatters.ts`. Translate hardcoded UI strings inline at call sites; pull all enum/token labels from the central maps so each token maps to exactly one Korean display string. Keep API payloads, enum values, symbols, venue IDs, source keys, and backend schemas unchanged.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, Testing Library, react-router-dom 7. No new runtime dependencies.

**Reference:** Linear issue [ROB-111](https://linear.app/mgh3326/issue/ROB-111/auto-trader-react-한국어-ui메타데이터-표시-및-ko-kr-locale-정리)

---

## Operating Rules (read before any task)

- **Working directory for every command:** `frontend/trading-decision/` (the React workspace).
- **Commit cadence:** one focused commit per task. Each commit must keep `npm run typecheck` and `npm test` green.
- **Branch:** `feature/ROB-111-trading-decision-korean-ui` from `main` (created by the worktree skill before execution).
- **Never** add an i18n runtime dependency, change API enum values, mutate DB, or modify backend code.
- **Token-shaped fallbacks** (e.g. unknown `source_profile` strings, raw `safety_scope`) must still be readable: if no Korean label exists, use the existing `formatOperatorToken`-style snake-to-space helper rather than printing the raw token.
- **Do not** translate user-supplied free-text fields: `notes`, `market_brief`'s arbitrary string values, `proposal.original_rationale`, `proposal.original_payload` free text, `user_note`, news article titles/summaries, news source feed names. Their UI labels (the `<dt>` etc. surrounding them) DO get translated.
- **Co-author trailer for commits:** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

## File Structure

**Create:**
- `src/i18n/ko.ts` — all enum/status/token → Korean string maps, grouped by domain.
- `src/i18n/formatters.ts` — `labelOrToken`, `labelOrderSide`, `labelOperatorToken`, `formatDateTimeKo`, `formatDecimalKo` wrappers.
- `src/i18n/index.ts` — barrel re-export so callers do `import { ... } from "../i18n"`.
- `src/__tests__/i18n.formatters.test.ts` — formatter helper tests.

**Modify (display-only):**
- `src/format/datetime.ts` — default `locale = "ko-KR"`.
- `src/format/decimal.ts` — default `locale = "ko-KR"`.
- `src/format/percent.ts` — leave numeric formatting alone (it's locale-independent), but add `nullDash = "—"` consistency only if needed (no behavior change required).
- `src/components/StatusBadge.tsx`
- `src/components/WarningChips.tsx`
- `src/components/ReconciliationBadge.tsx`
- `src/components/NxtVenueBadge.tsx`
- `src/components/ReadinessStatusBadge.tsx`
- `src/components/MarketBriefPanel.tsx`
- `src/components/ProposalResponseControls.tsx`
- `src/components/ProposalRow.tsx`
- `src/components/ProposalAdjustmentEditor.tsx`
- `src/components/OutcomeMarkForm.tsx`
- `src/components/ReconciliationDecisionSupportPanel.tsx`
- `src/components/NewsReadinessSection.tsx`
- `src/components/MarketNewsBriefingSection.tsx`
- `src/components/CommitteeEvidenceArtifacts.tsx`
- `src/pages/SessionListPage.tsx`
- `src/pages/SessionDetailPage.tsx`
- `src/pages/PreopenPage.tsx`

**Tests to update (existing, assertions only):**
- `src/__tests__/SessionListPage.test.tsx`
- `src/__tests__/SessionDetailPage.test.tsx`
- `src/__tests__/PreopenPage.test.tsx`
- `src/__tests__/ProposalResponseControls.test.tsx`
- `src/__tests__/ProposalRow.test.tsx`
- `src/__tests__/ProposalAdjustmentEditor.test.tsx`
- `src/__tests__/OutcomeMarkForm.test.tsx`
- `src/__tests__/OutcomesPanel.test.tsx`
- `src/__tests__/WarningChips.test.tsx`
- `src/__tests__/ReconciliationBadge.test.tsx`
- `src/__tests__/ReconciliationDecisionSupportPanel.test.tsx`
- `src/__tests__/NxtVenueBadge.test.tsx`
- `src/__tests__/format.decimal.test.ts`

**Untouched (Korean already, or out of scope):** `OperatorEventForm`, `ExecutionReviewPanel`, `LinkedActionsPanel`, `OutcomesPanel`, `OriginalVsAdjustedSummary`, `StrategyEventTimeline`, `AnalyticsMatrix`, `Committee*` components other than `CommitteeEvidenceArtifacts` (their existing surface is mostly headings — translate only obvious chrome, not committee free-text payloads). Verify each at end-of-plan; only translate if they expose hardcoded English UI chrome to the user.

---

## Task 1: Bootstrap the `src/i18n/` label layer

**Files:**
- Create: `frontend/trading-decision/src/i18n/ko.ts`
- Create: `frontend/trading-decision/src/i18n/formatters.ts`
- Create: `frontend/trading-decision/src/i18n/index.ts`
- Test: `frontend/trading-decision/src/__tests__/i18n.formatters.test.ts`

- [ ] **Step 1: Write the failing formatter test**

Create `src/__tests__/i18n.formatters.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  labelOperatorToken,
  labelOrToken,
  labelOrderSide,
} from "../i18n/formatters";

describe("labelOrToken", () => {
  it("returns the Korean label when the key is known", () => {
    const map = { open: "진행 중", closed: "종료" } as const;
    expect(labelOrToken(map, "open")).toBe("진행 중");
  });

  it("falls back to the formatted token when the key is unknown", () => {
    const map = { open: "진행 중" } as const;
    expect(labelOrToken(map, "needs_review")).toBe("needs review");
  });

  it("returns the dash placeholder for null/undefined", () => {
    const map = { open: "진행 중" } as const;
    expect(labelOrToken(map, null)).toBe("—");
    expect(labelOrToken(map, undefined)).toBe("—");
  });
});

describe("labelOrderSide", () => {
  it("translates buy/sell", () => {
    expect(labelOrderSide("buy")).toBe("매수");
    expect(labelOrderSide("sell")).toBe("매도");
  });

  it("returns dash for none/null", () => {
    expect(labelOrderSide("none")).toBe("—");
    expect(labelOrderSide(null)).toBe("—");
  });
});

describe("labelOperatorToken", () => {
  it("converts snake_case tokens to spaced text", () => {
    expect(labelOperatorToken("paper_plumbing_smoke")).toBe(
      "paper plumbing smoke",
    );
  });

  it("returns dash for null/empty", () => {
    expect(labelOperatorToken(null)).toBe("—");
    expect(labelOperatorToken("")).toBe("—");
  });
});
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd frontend/trading-decision
npm test -- i18n.formatters
```

Expected: FAIL — module `../i18n/formatters` not resolved.

- [ ] **Step 3: Create `src/i18n/ko.ts` with the central label maps**

```ts
import type {
  ActionKind,
  CommitteeAccountMode,
  ExecutionAccountMode,
  ExecutionReviewStageStatus,
  ExecutionSource,
  InstrumentType,
  OutcomeHorizon,
  PreopenArtifactReadinessStatus,
  PreopenArtifactStatus,
  PreopenNewsReadinessStatus,
  PreopenPaperApprovalBridgeStatus,
  PreopenPaperApprovalCandidateStatus,
  PreopenQaCheckSeverity,
  PreopenQaCheckStatus,
  PreopenQaConfidence,
  PreopenQaEvaluatorStatus,
  PreopenQaGrade,
  ProposalKind,
  SessionStatus,
  Side,
  TrackKind,
  UserResponseValue,
  WorkflowStatus,
} from "../api/types";
import type {
  CandidateKind,
  NxtClassification,
  ReconciliationStatus,
} from "../api/reconciliation";

export const SESSION_STATUS_LABEL: Record<SessionStatus, string> = {
  open: "진행 중",
  closed: "종료",
  archived: "보관됨",
};

export const USER_RESPONSE_LABEL: Record<UserResponseValue, string> = {
  pending: "대기",
  accept: "수락",
  partial_accept: "부분 수락",
  modify: "수정",
  defer: "보류",
  reject: "거절",
};

export const RESPONSE_BUTTON_LABEL: Record<
  "accept" | "partial_accept" | "modify" | "defer" | "reject",
  string
> = {
  accept: "수락",
  partial_accept: "부분 수락",
  modify: "수정",
  defer: "보류",
  reject: "거절",
};

export const SIDE_LABEL: Record<Side, string> = {
  buy: "매수",
  sell: "매도",
  none: "—",
};

export const PROPOSAL_KIND_LABEL: Record<ProposalKind, string> = {
  trim: "축소",
  add: "추가 매수",
  enter: "신규 진입",
  exit: "청산",
  pullback_watch: "되돌림 관찰",
  breakout_watch: "돌파 관찰",
  avoid: "회피",
  no_action: "무행동",
  other: "기타",
};

export const ACTION_KIND_LABEL: Record<ActionKind, string> = {
  live_order: "실주문",
  paper_order: "모의주문",
  watch_alert: "감시 알림",
  no_action: "무행동",
  manual_note: "수기 메모",
};

export const TRACK_KIND_LABEL: Record<TrackKind, string> = {
  accepted_live: "수락(실주문)",
  accepted_paper: "수락(모의)",
  rejected_counterfactual: "거절 대조",
  analyst_alternative: "분석가 대안",
  user_alternative: "사용자 대안",
};

export const OUTCOME_HORIZON_LABEL: Record<OutcomeHorizon, string> = {
  "1h": "1시간",
  "4h": "4시간",
  "1d": "1일",
  "3d": "3일",
  "7d": "7일",
  final: "최종",
};

export const INSTRUMENT_TYPE_LABEL: Record<InstrumentType, string> = {
  equity_kr: "국내주식",
  equity_us: "해외주식",
  crypto: "암호화폐",
  forex: "외환",
  index: "지수",
};

export const ACCOUNT_MODE_LABEL: Record<CommitteeAccountMode, string> = {
  kis_mock: "KIS 모의",
  alpaca_paper: "Alpaca Paper",
  kis_live: "KIS 실계좌",
  db_simulated: "DB 시뮬레이션",
};

export const EXECUTION_ACCOUNT_MODE_LABEL: Record<ExecutionAccountMode, string> = {
  kis_live: "KIS 실계좌",
  kis_mock: "KIS 모의",
  alpaca_paper: "Alpaca Paper",
  db_simulated: "DB 시뮬레이션",
};

export const EXECUTION_SOURCE_LABEL: Record<ExecutionSource, string> = {
  preopen: "장전",
  watch: "감시",
  manual: "수기",
  websocket: "실시간",
  reconciler: "조정",
};

export const EXECUTION_REVIEW_STAGE_STATUS_LABEL: Record<
  ExecutionReviewStageStatus,
  string
> = {
  ready: "준비 완료",
  degraded: "주의",
  unavailable: "미사용",
  skipped: "건너뜀",
  pending: "대기",
};

export const WORKFLOW_STATUS_LABEL: Record<WorkflowStatus, string> = {
  created: "생성됨",
  evidence_generating: "근거 수집 중",
  evidence_ready: "근거 준비됨",
  debate_ready: "토론 준비됨",
  trader_draft_ready: "트레이더 초안 준비",
  risk_review_ready: "리스크 리뷰 준비",
  auto_approved: "자동 승인",
  preview_ready: "프리뷰 준비",
  journal_ready: "기록 준비",
  completed: "완료",
  failed_evidence: "근거 실패",
  failed_trader_draft: "트레이더 초안 실패",
  failed_risk_review: "리스크 리뷰 실패",
  preview_blocked: "프리뷰 차단",
};

export const RECONCILIATION_STATUS_LABEL: Record<ReconciliationStatus, string> = {
  maintain: "유지",
  near_fill: "체결 임박",
  too_far: "괴리 큼",
  chasing_risk: "추격 위험",
  data_mismatch: "데이터 불일치",
  kr_pending_non_nxt: "국내 브로커 전용",
  unknown_venue: "거래소 알 수 없음",
  unknown: "알 수 없음",
};

export const NXT_CLASSIFICATION_LABEL: Record<NxtClassification, string> = {
  buy_pending_at_support: "매수 대기(지지선 근접)",
  buy_pending_too_far: "매수 대기(괴리 큼)",
  buy_pending_actionable: "매수 대기(실행 가능)",
  sell_pending_near_resistance: "매도 대기(저항선 근접)",
  sell_pending_too_optimistic: "매도 대기(낙관 과다)",
  sell_pending_actionable: "매도 대기(실행 가능)",
  non_nxt_pending_ignore_for_nxt: "비-NXT 대기",
  holding_watch_only: "보유 감시",
  data_mismatch_requires_review: "데이터 불일치 검토 필요",
  unknown: "알 수 없음",
};

export const CANDIDATE_KIND_LABEL: Record<CandidateKind, string> = {
  pending_order: "대기 주문",
  holding: "보유",
  screener_hit: "스크리너 적중",
  proposed: "제안됨",
  other: "기타",
};

export const NEWS_READINESS_LABEL: Record<PreopenNewsReadinessStatus, string> = {
  ready: "정상",
  stale: "오래됨",
  unavailable: "미사용",
};

export const ARTIFACT_STATUS_LABEL: Record<PreopenArtifactStatus, string> = {
  unavailable: "미사용",
  draft: "초안",
  ready: "준비 완료",
  degraded: "주의",
};

export const ARTIFACT_READINESS_LABEL: Record<
  PreopenArtifactReadinessStatus,
  string
> = {
  ready: "준비 완료",
  stale: "오래됨",
  unavailable: "미사용",
  partial: "일부",
};

export const PAPER_APPROVAL_STATUS_LABEL: Record<
  PreopenPaperApprovalBridgeStatus,
  string
> = {
  available: "사용 가능",
  warning: "주의",
  blocked: "차단됨",
  unavailable: "미사용",
};

export const PAPER_APPROVAL_CANDIDATE_STATUS_LABEL: Record<
  PreopenPaperApprovalCandidateStatus,
  string
> = {
  available: "사용 가능",
  warning: "주의",
  unavailable: "미사용",
};

export const QA_STATUS_LABEL: Record<PreopenQaEvaluatorStatus, string> = {
  ready: "준비 완료",
  needs_review: "검토 필요",
  unavailable: "미사용",
  skipped: "건너뜀",
};

export const QA_CHECK_STATUS_LABEL: Record<PreopenQaCheckStatus, string> = {
  pass: "통과",
  warn: "주의",
  fail: "실패",
  unknown: "알 수 없음",
  skipped: "건너뜀",
};

export const QA_SEVERITY_LABEL: Record<PreopenQaCheckSeverity, string> = {
  info: "정보",
  low: "낮음",
  medium: "보통",
  high: "높음",
};

export const QA_GRADE_LABEL: Record<PreopenQaGrade, string> = {
  excellent: "매우 우수",
  good: "양호",
  watch: "주의",
  poor: "미흡",
  unavailable: "미사용",
};

export const QA_CONFIDENCE_LABEL: Record<PreopenQaConfidence, string> = {
  high: "높음",
  medium: "보통",
  low: "낮음",
  unavailable: "미사용",
};

export const VENUE_LABEL: Record<string, string> = {
  upbit: "Upbit",
  alpaca_paper: "Alpaca Paper",
  kis: "KIS",
  kis_mock: "KIS 모의",
  kis_live: "KIS 실계좌",
};

export const WARNING_TOKEN_LABEL: Record<string, string> = {
  missing_quote: "시세 누락",
  stale_quote: "시세 오래됨",
  missing_orderbook: "호가 누락",
  missing_support_resistance: "지지/저항선 미사용",
  missing_kr_universe: "국내 유니버스 누락",
  non_nxt_venue: "비-NXT 거래소",
  unknown_venue: "거래소 알 수 없음",
  unknown_side: "방향 알 수 없음",
};

export const SAFETY_SCOPE_LABEL: Record<string, string> = {
  preview_only_confirm_false_no_broker_submit:
    "브로커 제출 없는 preview 전용",
  advisory_only: "자문 전용",
};

export const PURPOSE_LABEL: Record<string, string> = {
  paper_plumbing_smoke: "페이퍼 배관 점검",
  alpha_candidate_review: "알파 후보 리뷰",
};

export const COMMON = {
  dash: "—",
  loading: "불러오는 중…",
  saving: "저장 중…",
  retry: "다시 시도",
  cancel: "취소",
  refresh: "새로고침",
  next: "다음",
  previous: "이전",
  all: "전체",
  yes: "예",
  no: "아니오",
  unknown: "알 수 없음",
  rawData: "원본 데이터 보기",
  somethingWentWrong: "오류가 발생했습니다. 다시 시도해 주세요.",
} as const;
```

- [ ] **Step 4: Create `src/i18n/formatters.ts`**

```ts
import { COMMON } from "./ko";

export function labelOrToken<K extends string>(
  map: Readonly<Record<K, string>>,
  key: K | string | null | undefined,
): string {
  if (key === null || key === undefined || key === "") return COMMON.dash;
  const known = (map as Record<string, string>)[key];
  if (known !== undefined) return known;
  return formatToken(key);
}

export function labelOperatorToken(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return COMMON.dash;
  return formatToken(value);
}

export function labelOrderSide(side: string | null | undefined): string {
  if (side === "buy") return "매수";
  if (side === "sell") return "매도";
  return COMMON.dash;
}

export function labelYesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return COMMON.dash;
  return value ? COMMON.yes : COMMON.no;
}

function formatToken(raw: string): string {
  return raw.replace(/_/g, " ");
}
```

- [ ] **Step 5: Create `src/i18n/index.ts` barrel**

```ts
export * from "./ko";
export * from "./formatters";
```

- [ ] **Step 6: Run the formatter test, confirm it passes**

```bash
npm test -- i18n.formatters
```

Expected: PASS (3 + 2 + 2 cases).

- [ ] **Step 7: Run typecheck and full suite**

```bash
npm run typecheck
npm test
```

Expected: PASS. New files have no consumers yet, so no regressions.

- [ ] **Step 8: Commit**

```bash
git add src/i18n src/__tests__/i18n.formatters.test.ts
git commit -m "$(cat <<'EOF'
feat(trading-decision): add Korean label/formatter i18n layer (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Switch default formatter locale to ko-KR

**Files:**
- Modify: `frontend/trading-decision/src/format/datetime.ts`
- Modify: `frontend/trading-decision/src/format/decimal.ts`
- Test: `frontend/trading-decision/src/__tests__/format.decimal.test.ts` (existing)
- Create: `frontend/trading-decision/src/__tests__/format.datetime.test.ts`

- [ ] **Step 1: Inspect existing decimal test to see what to keep**

```bash
cat src/__tests__/format.decimal.test.ts
```

Expected: locale-coupled assertions you'll need to update.

- [ ] **Step 2: Write a failing datetime test**

Create `src/__tests__/format.datetime.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { formatDateTime } from "../format/datetime";

describe("formatDateTime", () => {
  it("returns dash for null/undefined", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
  });

  it("returns the original string when not a valid date", () => {
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });

  it("formats ISO timestamps in ko-KR by default", () => {
    const result = formatDateTime("2026-05-05T10:30:00Z");
    expect(result).toMatch(/2026/);
    expect(result.length).toBeGreaterThan(0);
    // Korean default produces something like "2026. 5. 5. 오후 7:30"
    // Match a Korean month/day separator or AM/PM marker.
    expect(result).toMatch(/\.|오전|오후/);
  });
});
```

- [ ] **Step 3: Run the test to confirm it fails**

```bash
npm test -- format.datetime
```

Expected: FAIL — last assertion fails because default is `en-US`.

- [ ] **Step 4: Update `src/format/datetime.ts` default locale**

Replace its body with:

```ts
export function formatDateTime(
  iso: string | null | undefined,
  locale = "ko-KR",
): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
```

- [ ] **Step 5: Update `src/format/decimal.ts` default locale**

Replace with:

```ts
export function formatDecimal(
  s: string | null | undefined,
  locale = "ko-KR",
  opts: Intl.NumberFormatOptions = { maximumFractionDigits: 8 },
): string {
  if (s === null || s === undefined) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return new Intl.NumberFormat(locale, opts).format(n);
}
```

- [ ] **Step 6: Update existing `src/__tests__/format.decimal.test.ts`**

Read the existing assertions; convert any literal `en-US` formatted strings (e.g. `"1,234.56"`) to their `ko-KR` equivalents (digits and grouping are identical for thousands but assertions that pass an explicit `"en-US"` should be left alone). Adjust only the assertions that depended on the default locale.

If a test does `expect(formatDecimal("1234.5")).toBe("1,234.5")`, leave it — `ko-KR` produces the same comma grouping for that case. If a test depended on locale-specific formatting that diverges, replace with `formatDecimal("1234.5", "en-US")` to keep that case explicit, and add a parallel `ko-KR` case.

- [ ] **Step 7: Run format tests, confirm green**

```bash
npm test -- format
```

Expected: PASS.

- [ ] **Step 8: Run full suite to catch downstream breakage**

```bash
npm test
```

Expected: There may be a small number of test failures in components asserting English-formatted dates — note them. Do not fix here; subsequent tasks will rewrite those assertions when translating each component.

- [ ] **Step 9: Commit**

```bash
git add src/format/datetime.ts src/format/decimal.ts src/__tests__/format.decimal.test.ts src/__tests__/format.datetime.test.ts
git commit -m "$(cat <<'EOF'
feat(trading-decision): default ko-KR locale in datetime/decimal formatters (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Translate StatusBadge, ReconciliationBadge, NxtVenueBadge, ReadinessStatusBadge

**Files:**
- Modify: `src/components/StatusBadge.tsx`
- Modify: `src/components/ReconciliationBadge.tsx`
- Modify: `src/components/NxtVenueBadge.tsx`
- Modify: `src/components/ReadinessStatusBadge.tsx`
- Test: `src/__tests__/ReconciliationBadge.test.tsx` (existing)
- Test: `src/__tests__/NxtVenueBadge.test.tsx` (existing)

- [ ] **Step 1: Update `StatusBadge.tsx` to render Korean label**

Replace file with:

```tsx
import type { SessionStatus, UserResponseValue } from "../api/types";
import { SESSION_STATUS_LABEL, USER_RESPONSE_LABEL } from "../i18n";
import styles from "./StatusBadge.module.css";

interface StatusBadgeProps {
  value: SessionStatus | UserResponseValue;
}

function labelFor(value: SessionStatus | UserResponseValue): string {
  if (value in SESSION_STATUS_LABEL) {
    return SESSION_STATUS_LABEL[value as SessionStatus];
  }
  return USER_RESPONSE_LABEL[value as UserResponseValue];
}

export default function StatusBadge({ value }: StatusBadgeProps) {
  return (
    <span className={`${styles.badge} ${styles[value]}`}>{labelFor(value)}</span>
  );
}
```

- [ ] **Step 2: Update `ReconciliationBadge.tsx` to use central map**

Replace file with:

```tsx
import type { ReconciliationStatus } from "../api/reconciliation";
import { RECONCILIATION_STATUS_LABEL } from "../i18n";
import styles from "./ReconciliationBadge.module.css";

interface Props {
  value: ReconciliationStatus | null;
}

export default function ReconciliationBadge({ value }: Props) {
  if (value === null) return null;
  const label = RECONCILIATION_STATUS_LABEL[value];
  return (
    <span
      aria-label={`조정 상태: ${label}`}
      className={`${styles.badge} ${styles[value]}`}
    >
      {label}
    </span>
  );
}
```

- [ ] **Step 3: Update `NxtVenueBadge.tsx` Korean labels**

Edit the four `badgeLabel` literals and the `aria-label` prefix:

| English | Korean |
|--|--|
| `"NXT review needed"` | `"NXT 검토 필요"` |
| `"Non-NXT (KR broker)"` | `"비-NXT (국내 브로커)"` |
| `"NXT eligibility unknown"` | `"NXT 자격 알 수 없음"` |
| `"NXT actionable"` | `"NXT 실행 가능"` |
| `"NXT not actionable"` | `"NXT 실행 불가"` |
| `"NXT venue: ..."` (aria) | `"NXT 거래소: ..."` |

- [ ] **Step 4: Update `ReadinessStatusBadge.tsx` to import central map**

Replace its `LABELS` constant with the imported `NEWS_READINESS_LABEL`:

```tsx
import type { PreopenNewsReadinessStatus } from "../api/types";
import { NEWS_READINESS_LABEL } from "../i18n";
import styles from "./ReadinessStatusBadge.module.css";

export interface ReadinessStatusBadgeProps {
  status: PreopenNewsReadinessStatus;
}

export default function ReadinessStatusBadge({
  status,
}: ReadinessStatusBadgeProps) {
  return (
    <span
      className={`${styles.badge} ${styles[status]}`}
      data-status={status}
      role="status"
    >
      {NEWS_READINESS_LABEL[status]}
    </span>
  );
}
```

- [ ] **Step 5: Update `ReconciliationBadge.test.tsx` assertions**

Open `src/__tests__/ReconciliationBadge.test.tsx`. Replace any English label assertions with their Korean equivalents from `RECONCILIATION_STATUS_LABEL` (e.g. `"Maintain"` → `"유지"`, `"Near fill"` → `"체결 임박"`). Replace the aria-label prefix `"Reconciliation status:"` with `"조정 상태:"`.

- [ ] **Step 6: Update `NxtVenueBadge.test.tsx` assertions**

Replace English badge text matches with the Korean equivalents from Step 3.

- [ ] **Step 7: Run targeted tests**

```bash
npm test -- ReconciliationBadge NxtVenueBadge
```

Expected: PASS.

- [ ] **Step 8: Run full suite**

```bash
npm test
npm run typecheck
```

Expected: typecheck PASS. Some unrelated tests (SessionListPage/SessionDetailPage etc. that rendered StatusBadge) may now fail; do not fix them — they will be addressed in their own tasks.

- [ ] **Step 9: Commit**

```bash
git add src/components/StatusBadge.tsx src/components/ReconciliationBadge.tsx src/components/NxtVenueBadge.tsx src/components/ReadinessStatusBadge.tsx src/__tests__/ReconciliationBadge.test.tsx src/__tests__/NxtVenueBadge.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate StatusBadge / Reconciliation / NXT / Readiness badges (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Translate WarningChips

**Files:**
- Modify: `src/components/WarningChips.tsx`
- Test: `src/__tests__/WarningChips.test.tsx` (existing)

- [ ] **Step 1: Replace `FRIENDLY` with the central map**

Edit `src/components/WarningChips.tsx` to:

```tsx
import { WARNING_TOKEN_LABEL } from "../i18n";
import { labelOperatorToken } from "../i18n/formatters";
import styles from "./WarningChips.module.css";

interface Props {
  tokens: string[];
}

const TOKEN_RE = /^[a-z][a-z0-9_]{0,63}$/;

function labelFor(token: string): string {
  return WARNING_TOKEN_LABEL[token] ?? labelOperatorToken(token);
}

export default function WarningChips({ tokens }: Props) {
  const safe = tokens.filter((t) => TOKEN_RE.test(t));
  if (safe.length === 0) return null;
  return (
    <ul aria-label="경고" className={styles.list}>
      {safe.map((token) => (
        <li
          aria-label={`경고: ${labelFor(token)}`}
          className={styles.chip}
          key={token}
        >
          {labelFor(token)}
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 2: Update `WarningChips.test.tsx` assertions**

Read the file, replace English label / aria-label assertions with the Korean equivalents (`"경고"` for the list aria-label, `"경고: 시세 누락"` style for chip aria-labels, `"시세 누락"` for chip text, etc.).

- [ ] **Step 3: Run tests**

```bash
npm test -- WarningChips
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/components/WarningChips.tsx src/__tests__/WarningChips.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate WarningChips labels (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Translate ProposalResponseControls

**Files:**
- Modify: `src/components/ProposalResponseControls.tsx`
- Test: `src/__tests__/ProposalResponseControls.test.tsx` (existing)

- [ ] **Step 1: Update button labels and aria-label**

Replace the `buttons` array and the `aria-label` in the `<div>`:

```tsx
import type { RespondAction, UserResponseValue } from "../api/types";
import { RESPONSE_BUTTON_LABEL } from "../i18n";

interface ProposalResponseControlsProps {
  currentResponse: UserResponseValue;
  isSubmitting: boolean;
  onSimpleResponse: (response: "accept" | "reject" | "defer") => void;
  onOpenAdjust: (response: "modify" | "partial_accept") => void;
}

const buttons: Array<{ value: RespondAction; kind: "simple" | "adjust" }> = [
  { value: "accept", kind: "simple" },
  { value: "partial_accept", kind: "adjust" },
  { value: "modify", kind: "adjust" },
  { value: "defer", kind: "simple" },
  { value: "reject", kind: "simple" },
];

export default function ProposalResponseControls({
  currentResponse,
  isSubmitting,
  onSimpleResponse,
  onOpenAdjust,
}: ProposalResponseControlsProps) {
  return (
    <div className="response-controls" aria-label="제안 응답 컨트롤">
      {buttons.map((button) => (
        <button
          aria-pressed={currentResponse === button.value}
          className={currentResponse === button.value ? "btn btn-primary" : "btn"}
          disabled={isSubmitting}
          key={button.value}
          onClick={() => {
            if (button.value === "modify" || button.value === "partial_accept") {
              onOpenAdjust(button.value);
            } else {
              onSimpleResponse(button.value);
            }
          }}
          type="button"
        >
          {RESPONSE_BUTTON_LABEL[button.value]}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Update test assertions**

In `src/__tests__/ProposalResponseControls.test.tsx`, replace English button names with Korean equivalents:

```tsx
for (const name of ["수락", "부분 수락", "수정", "보류", "거절"]) {
  expect(screen.getByRole("button", { name })).toBeInTheDocument();
}
```

Replace all subsequent `getByRole("button", { name: "Accept" })` with `name: "수락"`, `"Modify"` with `"수정"`, etc. matching the Korean labels.

- [ ] **Step 3: Run tests**

```bash
npm test -- ProposalResponseControls
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/components/ProposalResponseControls.tsx src/__tests__/ProposalResponseControls.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate ProposalResponseControls (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Translate ProposalAdjustmentEditor

**Files:**
- Modify: `src/components/ProposalAdjustmentEditor.tsx`
- Test: `src/__tests__/ProposalAdjustmentEditor.test.tsx` (existing)

- [ ] **Step 1: Translate field labels, error messages, button text**

Edit `src/components/ProposalAdjustmentEditor.tsx` so the `specs` array uses Korean labels:

```ts
const specs: FieldSpec[] = [
  { label: "수량", userKey: "user_quantity", originalKey: "original_quantity" },
  { label: "수량 비율(%)", userKey: "user_quantity_pct", originalKey: "original_quantity_pct", percent: true },
  { label: "금액", userKey: "user_amount", originalKey: "original_amount", nonNegative: true },
  { label: "가격", userKey: "user_price", originalKey: "original_price", nonNegative: true },
  { label: "트리거 가격", userKey: "user_trigger_price", originalKey: "original_trigger_price", nonNegative: true },
  { label: "임계 비율(%)", userKey: "user_threshold_pct", originalKey: "original_threshold_pct", percent: true },
];
```

Update the inline error strings:

| English | Korean |
|--|--|
| `${spec.label} must be a decimal string.` | `${spec.label}은(는) 소수 문자열이어야 합니다.` |
| `${spec.label} must be greater than or equal to 0.` | `${spec.label}은(는) 0 이상이어야 합니다.` |
| `${spec.label} must be between 0 and 100.` | `${spec.label}은(는) 0 이상 100 이하이어야 합니다.` |
| `Enter at least one adjusted numeric value.` | `조정된 숫자 값을 하나 이상 입력해 주세요.` |
| `Something went wrong. Try again.` | use `COMMON.somethingWentWrong` |

Update the `<span>Note</span>` label to `<span>메모</span>`. Update the buttons:

```tsx
<button className="btn btn-primary" disabled={isSubmitting} type="submit">
  {response === "partial_accept" ? "부분 수락 저장" : "수정 저장"}
</button>
<button className="btn btn-ghost" disabled={isSubmitting} onClick={onCancel} type="button">
  취소
</button>
```

Add `import { COMMON } from "../i18n";` at the top and replace the fallback error string.

- [ ] **Step 2: Update test assertions**

In `src/__tests__/ProposalAdjustmentEditor.test.tsx`, replace any English label / button / error matchers with the Korean equivalents above.

- [ ] **Step 3: Run tests**

```bash
npm test -- ProposalAdjustmentEditor
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/components/ProposalAdjustmentEditor.tsx src/__tests__/ProposalAdjustmentEditor.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate ProposalAdjustmentEditor (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Translate OutcomeMarkForm

**Files:**
- Modify: `src/components/OutcomeMarkForm.tsx`
- Test: `src/__tests__/OutcomeMarkForm.test.tsx` (existing)

- [ ] **Step 1: Use central track/horizon labels and translate UI chrome**

Edit `src/components/OutcomeMarkForm.tsx`:

```tsx
import { COMMON, OUTCOME_HORIZON_LABEL, TRACK_KIND_LABEL } from "../i18n";
```

Translate UI strings:

| English | Korean |
|--|--|
| `aria-label="Record outcome mark"` | `aria-label="결과 마크 기록"` |
| `Track` (label) | `트랙` |
| `Horizon` (label) | `기간` |
| `Counterfactual` (label) | `대조군` |
| `— select —` (option) | `— 선택 —` |
| `Price at mark` | `마크 시점 가격` |
| `e.g. 118000000` (placeholder) | `예: 118000000` |
| `PnL %` | `손익(%)` |
| `PnL amount` | `손익 금액` |
| `optional` (placeholder) | `선택` |
| `price_at_mark must be a non-negative number` | `마크 시점 가격은 0 이상의 숫자여야 합니다` |
| `accepted_live must not have a counterfactual selected` | `accepted_live 트랙은 대조군을 선택할 수 없습니다` |
| `counterfactual is required for this track` | `이 트랙에서는 대조군이 필요합니다` |
| `Could not record outcome mark.` | `결과 마크를 기록할 수 없습니다.` |
| `Saving...` | `COMMON.saving` |
| `Record mark` (submit button) | `마크 기록` |

Use `TRACK_KIND_LABEL[t]` and `OUTCOME_HORIZON_LABEL[h]` for `<option>` text:

```tsx
{TRACKS.map((t) => (
  <option key={t} value={t}>
    {TRACK_KIND_LABEL[t]}
  </option>
))}
{HORIZONS.map((h) => (
  <option key={h} value={h}>
    {OUTCOME_HORIZON_LABEL[h]}
  </option>
))}
```

Counterfactual `<option>` text becomes:

```tsx
{`#${c.id} · 기준가 ${c.baseline_price}`}
```

- [ ] **Step 2: Update test assertions**

In `src/__tests__/OutcomeMarkForm.test.tsx`, swap labels/options/error strings to the Korean equivalents from step 1. The form aria-label `"Record outcome mark"` → `"결과 마크 기록"`.

- [ ] **Step 3: Run tests**

```bash
npm test -- OutcomeMarkForm OutcomesPanel
npm run typecheck
```

Expected: PASS. Update `OutcomesPanel.test.tsx` only if it asserts strings produced by `TRACK_KIND_LABEL` or horizon literals; otherwise leave it alone.

- [ ] **Step 4: Commit**

```bash
git add src/components/OutcomeMarkForm.tsx src/__tests__/OutcomeMarkForm.test.tsx
git diff --cached --stat
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate OutcomeMarkForm (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Translate ProposalRow

**Files:**
- Modify: `src/components/ProposalRow.tsx`
- Test: `src/__tests__/ProposalRow.test.tsx` (existing)

- [ ] **Step 1: Translate static UI in ProposalRow**

Edit `src/components/ProposalRow.tsx`:

Update imports:

```tsx
import {
  COMMON,
  PROPOSAL_KIND_LABEL,
  SIDE_LABEL,
} from "../i18n";
```

Update the `valuePairs` constant labels to Korean (these are the `<dt>` labels rendered by `ValueList`):

```tsx
const valuePairs = [
  ["수량", "original_quantity", "user_quantity"],
  ["수량 비율(%)", "original_quantity_pct", "user_quantity_pct"],
  ["금액", "original_amount", "user_amount"],
  ["가격", "original_price", "user_price"],
  ["트리거 가격", "original_trigger_price", "user_trigger_price"],
  ["임계 비율(%)", "original_threshold_pct", "user_threshold_pct"],
] as const;
```

Update the chip / heading text:

```tsx
<span className={styles.chip}>{SIDE_LABEL[proposal.side]}</span>
<span className={styles.chip}>{PROPOSAL_KIND_LABEL[proposal.proposal_kind]}</span>
```

Translate static strings:

| English | Korean |
|--|--|
| `<h3>Original</h3>` | `<h3>원본</h3>` |
| `<h3>Your decision</h3>` | `<h3>내 결정</h3>` |
| `<h3>Crypto paper workflow</h3>` | `<h3>암호화폐 모의 워크플로우</h3>` |
| `Session is archived. You can no longer respond.` | `세션이 보관되었습니다. 더 이상 응답할 수 없습니다.` |
| `Something went wrong. Try again.` | `COMMON.somethingWentWrong` |
| `Current quote estimate needed` | `현재 시세 추정이 필요합니다` |
| `Non-NXT pending order — KR broker routing only. Review before deciding; recording a response on this row does not place or cancel a broker order.` | `비-NXT 대기 주문 — 국내 브로커 라우팅 전용. 결정 전에 검토하세요. 이 행의 응답 기록은 브로커 주문을 제출하거나 취소하지 않습니다.` |
| `Accept records this decision only; it does not send a live trade.` | `수락은 결정만 기록합니다. 실주문을 전송하지 않습니다.` |
| `<summary>Record outcome mark</summary>` | `<summary>결과 마크 기록</summary>` |
| `aria-label="Outcome marks"` | `aria-label="결과 마크"` |

Update `formatProposalValue` 's `Amount` branch — the label is now Korean, so adjust:

```ts
function isMissingSellAmount(label: string, value: string, proposal: ProposalDetail) {
  return label === "금액" && proposal.side === "sell" && Number(value) === 0 && proposal.original_price === null;
}

function formatProposalValue(label: string, value: string, proposal: ProposalDetail) {
  if (isMissingSellAmount(label, value, proposal)) {
    return "현재 시세 추정이 필요합니다";
  }
  return `${formatDecimal(value)}${
    label === "금액" && proposal.original_currency
      ? ` ${proposal.original_currency}`
      : ""
  }`;
}
```

Update `summaryPairs` similarly (it currently uses the same `valuePairs` labels, which are now Korean — no further changes needed for keys).

- [ ] **Step 2: Update test assertions**

In `src/__tests__/ProposalRow.test.tsx`, replace English string matchers with the Korean equivalents above. Likely matches: `"Original"`, `"Your decision"`, `"Quantity"`, side / kind labels (`"buy"` → `"매수"`, etc.), the safety note, and the non-NXT warning.

- [ ] **Step 3: Run tests**

```bash
npm test -- ProposalRow
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/components/ProposalRow.tsx src/__tests__/ProposalRow.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate ProposalRow chrome and value labels (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Translate ReconciliationDecisionSupportPanel

**Files:**
- Modify: `src/components/ReconciliationDecisionSupportPanel.tsx`
- Test: `src/__tests__/ReconciliationDecisionSupportPanel.test.tsx` (existing)

- [ ] **Step 1: Translate `<dt>` labels and aria-label**

Edit the panel:

| English | Korean |
|--|--|
| `aria-label="Reconciliation decision support"` | `aria-label="조정 의사결정 지원"` |
| `Pending side` | `대기 방향` |
| `Pending price` | `대기 가격` |
| `Pending qty` | `대기 수량` |
| `Pending order` | `대기 주문` |
| `Live quote` | `실시간 시세` |
| `Gap to current` | `현재가 대비 괴리` |
| `Distance to fill` | `체결까지 거리` |
| `Nearest support` | `가까운 지지선` |
| `Nearest resistance` | `가까운 저항선` |
| `Bid/ask spread` | `매수/매도 스프레드` |

Use `labelOrderSide` for the side value: `<Item label="대기 방향" value={labelOrderSide(side)} />`. Add the import.

- [ ] **Step 2: Update test assertions**

Open `src/__tests__/ReconciliationDecisionSupportPanel.test.tsx`, replace English `<dt>` labels with the Korean equivalents.

- [ ] **Step 3: Run tests**

```bash
npm test -- ReconciliationDecisionSupportPanel
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/components/ReconciliationDecisionSupportPanel.tsx src/__tests__/ReconciliationDecisionSupportPanel.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate ReconciliationDecisionSupportPanel (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Translate MarketBriefPanel + hide raw JSON behind 원본 데이터 보기

**Files:**
- Modify: `src/components/MarketBriefPanel.tsx`

- [ ] **Step 1: Replace English UI chrome and reuse central reconciliation map**

Edit the file:

```tsx
import {
  COMMON,
  RECONCILIATION_STATUS_LABEL,
  NXT_CLASSIFICATION_LABEL,
} from "../i18n";
import { labelOrToken } from "../i18n/formatters";
import styles from "./MarketBriefPanel.module.css";
```

Remove the local `RECON_LABEL` and `NXT_LABEL`. Use `RECONCILIATION_STATUS_LABEL` and `NXT_CLASSIFICATION_LABEL` from i18n instead. For unknown keys in `nxt_summary` / `reconciliation_summary`, render `labelOrToken(MAP, key)` so unrecognized backend tokens show a readable form rather than raw `snake_case`.

Translate the visible chrome:

| English | Korean |
|--|--|
| `<summary>Market brief</summary>` | `<summary>시장 브리핑</summary>` |
| `Research run:` | `리서치 실행:` |
| ` · refreshed ` | ` · 갱신 ` (use `formatDateTime(summary.refreshed_at)` instead of raw ISO) |
| `Counts:` | `건수:` |
| `candidates` | `후보` |
| `reconciliations` | `조정` |
| `Reconciliation summary` | `조정 요약` |
| `NXT summary` | `NXT 요약` |
| `Snapshot warnings:` | `스냅샷 경고:` |
| `Source warnings:` | `소스 경고:` |

When `summary` is `null` but `brief` is non-null (the current `else if (brief)` branch), wrap the raw JSON inside a nested `<details>` whose summary is `COMMON.rawData`:

```tsx
{summary ? (
  <div className={styles.summary}>{/* ...localized fields... */}</div>
) : brief ? (
  <details>
    <summary>{COMMON.rawData}</summary>
    <pre>{JSON.stringify(brief, null, 2)}</pre>
  </details>
) : null}
```

When `summary` is non-null, append a sibling `<details><summary>{COMMON.rawData}</summary><pre>...</pre></details>` so operators can still inspect raw payload, but it is no longer the primary display.

- [ ] **Step 2: Run typecheck and existing tests**

There is no dedicated `MarketBriefPanel.test.tsx`, but `SessionDetailPage.test.tsx` may render this; that test gets updated in Task 11.

```bash
npm run typecheck
npm test -- MarketBriefPanel
```

Expected: typecheck PASS. The `MarketBriefPanel` filename match returns no test file — that's fine.

- [ ] **Step 3: Commit**

```bash
git add src/components/MarketBriefPanel.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate MarketBriefPanel and gate raw JSON behind 원본 데이터 보기 (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Translate SessionListPage

**Files:**
- Modify: `src/pages/SessionListPage.tsx`
- Test: `src/__tests__/SessionListPage.test.tsx` (existing)

- [ ] **Step 1: Translate UI chrome and use central session-status options**

Edit `src/pages/SessionListPage.tsx`:

```tsx
import {
  COMMON,
  SESSION_STATUS_LABEL,
  WORKFLOW_STATUS_LABEL,
  ACCOUNT_MODE_LABEL,
} from "../i18n";
import { labelOrToken } from "../i18n/formatters";
```

Translate strings:

| English | Korean |
|--|--|
| `<h1>Decision inbox</h1>` | `<h1>의사결정함</h1>` |
| `Status filter` (visible label + aria-label) | `상태 필터` |
| `<option value="">All</option>` | `<option value="">{COMMON.all}</option>` |
| Status `<option>` text | `{SESSION_STATUS_LABEL[status]}` (replace `open`/`closed`/`archived` literals) |
| `Refresh` | `{COMMON.refresh}` |
| `Something went wrong. Try again.` | `{COMMON.somethingWentWrong}` |
| `No decision sessions yet.` | `아직 의사결정 세션이 없습니다.` |
| `<th>Generated</th>` | `생성 시각` |
| `<th>Profile</th>` | `프로필` |
| `<th>Strategy</th>` | `전략` |
| `<th>Scope</th>` | `범위` |
| `<th>Status</th>` | `상태` |
| `<th>Workflow</th>` | `워크플로우` |
| `<th>Account</th>` | `계정` |
| `<th>Proposals</th>` | `제안` |
| `<th>Pending</th>` | `대기` |
| `Previous` | `{COMMON.previous}` |
| `Next` | `{COMMON.next}` |

Replace the workflow-status mini cell so it renders `labelOrToken(WORKFLOW_STATUS_LABEL, session.workflow_status)` instead of `replace(/_/g, " ").toUpperCase()`. Replace `account_mode` cell with `labelOrToken(ACCOUNT_MODE_LABEL, session.account_mode)`.

Update market_scope rendering: replace `session.market_scope ?? "—"` with `session.market_scope ? session.market_scope.toUpperCase() : COMMON.dash` (preserves "KR"/"US"/"CRYPTO" tokens which are operator identifiers).

- [ ] **Step 2: Update SessionListPage test assertions**

Edit `src/__tests__/SessionListPage.test.tsx`. Replace:
- `"No decision sessions yet."` → `"아직 의사결정 세션이 없습니다."`
- `getByLabelText("Status filter")` → `getByLabelText("상태 필터")`

The test fixture's strategy name (`"Momentum rebalance"`) is fixture data, not UI chrome — leave it alone.

- [ ] **Step 3: Run tests and typecheck**

```bash
npm test -- SessionListPage
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/pages/SessionListPage.tsx src/__tests__/SessionListPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate SessionListPage (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Translate SessionDetailPage

**Files:**
- Modify: `src/pages/SessionDetailPage.tsx`
- Test: `src/__tests__/SessionDetailPage.test.tsx` (existing)

- [ ] **Step 1: Translate UI chrome and use central workflow status map**

Edit `src/pages/SessionDetailPage.tsx`:

```tsx
import { COMMON, WORKFLOW_STATUS_LABEL } from "../i18n";
import { labelOrToken } from "../i18n/formatters";
```

Translate strings:

| English | Korean |
|--|--|
| `Session not found` (ErrorView) | `세션을 찾을 수 없습니다` |
| `<h1>Session not found</h1>` | `<h1>세션을 찾을 수 없습니다</h1>` |
| `Back to inbox` (Link x2) | `의사결정함으로 돌아가기` |
| `Something went wrong. Try again.` | `{COMMON.somethingWentWrong}` |
| `all markets` | `전체 시장` |
| `<strong>Workflow Status:</strong>` | `<strong>워크플로우 상태:</strong>` |
| `aria-label="Committee artifacts"` | `aria-label="위원회 산출물"` |
| `aria-label="Analytics"` | `aria-label="분석"` |
| `<h2>Outcome analytics</h2>` | `<h2>결과 분석</h2>` |
| `Loading analytics...` | `분석을 불러오는 중...` |
| `aria-label="Strategy events"` | `aria-label="전략 이벤트"` |
| `<h2>Strategy events</h2>` | `<h2>전략 이벤트</h2>` |
| `Loading strategy events...` | `전략 이벤트를 불러오는 중...` |
| `Session not found for strategy events.` | `전략 이벤트용 세션을 찾을 수 없습니다.` |
| `aria-label="Proposals"` | `aria-label="제안"` |
| `${pending_count} of ${proposals_count} pending` | ``${pending_count}/${proposals_count} 대기 중`` |

Replace the workflow-status display with:

```tsx
<span className={styles.workflowStatus}>
  {labelOrToken(WORKFLOW_STATUS_LABEL, data.workflow_status)}
</span>
```

- [ ] **Step 2: Update SessionDetailPage test assertions**

Replace English UI matches with Korean equivalents from Step 1. Match the new pending-count format (`"X/Y 대기 중"` instead of `"X of Y pending"`).

- [ ] **Step 3: Run tests and typecheck**

```bash
npm test -- SessionDetailPage useDecisionSession
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/pages/SessionDetailPage.tsx src/__tests__/SessionDetailPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate SessionDetailPage (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Translate NewsReadinessSection

**Files:**
- Modify: `src/components/NewsReadinessSection.tsx`

- [ ] **Step 1: Translate UI chrome**

Translate strings:

| English | Korean |
|--|--|
| `aria-label="News readiness"` | `aria-label="뉴스 준비도"` |
| `<h2>News readiness</h2>` | `<h2>뉴스 준비도</h2>` |
| `News readiness lookup failed. Treat this preopen as if news is unavailable.` | `뉴스 준비도 조회에 실패했습니다. 뉴스를 미사용으로 간주하세요.` |
| `<dt>Latest run</dt>` | `최근 실행` |
| `<dt>Latest article</dt>` | `최근 기사` |
| `<dt>Freshness window</dt>` | `신선도 기준` |
| ` min` | `분` |
| `News is older than ${X} min — verify before acting.` | ``뉴스가 ${X}분 이상 경과했습니다. 행동 전에 확인하세요.`` |
| `News pipeline did not report a recent successful run.` | `뉴스 파이프라인의 최근 실행 성공 기록이 없습니다.` |
| `aria-label="News source counts"` | `aria-label="뉴스 소스 건수"` |
| `No source counts available.` | `소스 건수가 없습니다.` |
| `<h3>Source coverage</h3>` | `<h3>소스 커버리지</h3>` |
| Coverage table headers | `Source` → `소스`, `Status` → `상태`, `Expected` → `예상`, `Stored` → `저장됨`, `24h` → `24시간`, `Latest article` → `최근 기사` |
| `Latest articles (...)` | `최근 기사 (...)` |
| `No recent articles to preview.` | `미리 볼 최근 기사가 없습니다.` |

For the coverage table `<td>{source.status}</td>`, leave the raw status (it is a backend-provided string mirroring `PreopenNewsSourceCoverage.status`); add a comment-free pass-through with a fallback through `labelOperatorToken` only if the displayed value still appears as `snake_case` after manual smoke-checking.

- [ ] **Step 2: Run targeted tests**

```bash
npm test -- NewsReadinessSection
npm run typecheck
```

Expected: typecheck PASS. There is no `NewsReadinessSection.test.tsx`; PreopenPage tests cover its rendering and are updated in Task 15.

- [ ] **Step 3: Commit**

```bash
git add src/components/NewsReadinessSection.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate NewsReadinessSection (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Translate MarketNewsBriefingSection

**Files:**
- Modify: `src/components/MarketNewsBriefingSection.tsx`

- [ ] **Step 1: Translate UI chrome (preserve article titles/summaries verbatim)**

Translate strings:

| English | Korean |
|--|--|
| `aria-label="Market news briefing"` | `aria-label="시장 뉴스 브리핑"` |
| `<h2>Market news briefing</h2>` | `<h2>시장 뉴스 브리핑</h2>` |
| `No market news briefing available yet.` | `아직 시장 뉴스 브리핑이 없습니다.` |
| `Market-aware sections from recent news, filtered before trading review.` | `최근 뉴스에서 시장 관련 섹션을 추출해 트레이딩 리뷰 전에 필터링했습니다.` |
| `aria-label="Market news briefing summary"` | `aria-label="시장 뉴스 브리핑 요약"` |
| `No high-signal briefing sections found.` | `시그널이 강한 브리핑 섹션이 없습니다.` |
| `${N} items` | ``${N}건`` |
| `No articles in this section.` | `이 섹션에는 기사가 없습니다.` |
| `Filtered noise: ${N}` | ``필터링된 노이즈: ${N}`` |
| `Show top excluded articles (${N})` | ``상위 제외 기사 보기 (${N})`` |
| `Score ${N}` | ``점수 ${N}`` |
| `Terms: ${...}` | ``매칭 키워드: ${...}`` |

For `summaryChips`, translate the chip key text:

```tsx
const SUMMARY_CHIP_LABEL: Record<SummaryKey, string> = {
  included: "포함",
  excluded: "제외",
  sections: "섹션",
  uncategorized: "미분류",
};
// in JSX:
<span>{SUMMARY_CHIP_LABEL[key]}</span>
```

Preserve article `title` / `summary` / `source` / `feed_source` text — those come from external content.

- [ ] **Step 2: Run typecheck**

```bash
npm run typecheck
npm test -- MarketNewsBriefingSection PreopenPage
```

Expected: typecheck PASS. PreopenPage tests are updated in the next task.

- [ ] **Step 3: Commit**

```bash
git add src/components/MarketNewsBriefingSection.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate MarketNewsBriefingSection (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Translate PreopenPage

**Files:**
- Modify: `src/pages/PreopenPage.tsx`
- Test: `src/__tests__/PreopenPage.test.tsx` (existing)

This file is large (620 lines) and contains three local sub-components: `PreopenBriefingArtifactSection`, `PreopenQaEvaluatorPanel`, and `PreopenPaperApprovalBridgeSection`. Translate each in turn, then the main component.

- [ ] **Step 1: Update imports and remove local label maps**

Add imports:

```tsx
import {
  ARTIFACT_READINESS_LABEL,
  ARTIFACT_STATUS_LABEL,
  COMMON,
  PAPER_APPROVAL_CANDIDATE_STATUS_LABEL,
  PAPER_APPROVAL_STATUS_LABEL,
  QA_CHECK_STATUS_LABEL,
  QA_CONFIDENCE_LABEL,
  QA_GRADE_LABEL,
  QA_SEVERITY_LABEL,
  QA_STATUS_LABEL,
  SAFETY_SCOPE_LABEL,
  PURPOSE_LABEL,
  VENUE_LABEL,
} from "../i18n";
import { labelOperatorToken, labelOrToken } from "../i18n/formatters";
```

Delete the local `QA_STATUS_LABEL` and `PAPER_APPROVAL_STATUS_LABEL` constants (use the imports). Replace the local `formatOperatorToken` helper with the imported `labelOperatorToken` (semantically identical: snake → spaced text + dash for null/empty).

- [ ] **Step 2: Translate PreopenBriefingArtifactSection**

| English | Korean |
|--|--|
| `aria-label="Preopen briefing artifact"` | `aria-label="장전 브리핑 산출물"` |
| `<h2>Preopen briefing</h2>` | `<h2>장전 브리핑</h2>` |
| `Artifact ${status}` | ``산출물 ${labelOrToken(ARTIFACT_STATUS_LABEL, artifact.status)}`` |
| `News brief: ${artifact.news_summary}` | ``뉴스 요약: ${artifact.news_summary}`` |
| `aria-label="Preopen artifact risk notes"` | `aria-label="장전 산출물 리스크 노트"` |

For each readiness card, render `labelOrToken(ARTIFACT_READINESS_LABEL, item.status)` instead of raw `item.status`.

For each section card, render `labelOrToken(ARTIFACT_STATUS_LABEL, section.status)` instead of raw `section.status`.

- [ ] **Step 3: Translate PreopenQaEvaluatorPanel**

| English | Korean |
|--|--|
| `aria-label="Preopen QA evaluator"` | `aria-label="장전 QA 평가"` |
| `<h2>QA evaluator</h2>` | `<h2>QA 평가</h2>` |
| `${qa.source} · ${qa.overall.grade} · confidence ${qa.overall.confidence}` | ``${qa.source} · ${labelOrToken(QA_GRADE_LABEL, qa.overall.grade)} · 신뢰도 ${labelOrToken(QA_CONFIDENCE_LABEL, qa.overall.confidence)}`` |
| `QA ${status}` | ``QA ${labelOrToken(QA_STATUS_LABEL, qa.status)}`` |
| `Overall score: ${X}` | ``전체 점수: ${X}`` |
| `aria-label="QA blocking reasons"` | `aria-label="QA 차단 사유"` |
| `aria-label="QA warnings"` | `aria-label="QA 경고"` |
| Check status/severity (`${status} · ${severity}`) | ``${labelOrToken(QA_CHECK_STATUS_LABEL, check.status)} · ${labelOrToken(QA_SEVERITY_LABEL, check.severity)}`` |

- [ ] **Step 4: Translate PreopenPaperApprovalBridgeSection**

Translate the static safety note and chrome:

| English | Korean |
|--|--|
| `aria-label="Paper approval preview"` | `aria-label="모의 승인 미리보기"` |
| `<h2>Paper approval preview</h2>` | `<h2>모의 승인 미리보기</h2>` |
| `${bridge.market_scope ?? "unknown market"}` | ``${bridge.market_scope ? bridge.market_scope.toUpperCase() : "시장 알 수 없음"}`` |
| `${eligible_count} eligible / ${candidate_count} candidates` | ``${eligible_count}건 사용 가능 / ${candidate_count}건 후보`` |
| `Preview ${statusLabel}` | ``미리보기 ${labelOrToken(PAPER_APPROVAL_STATUS_LABEL, bridge.status)}`` |
| Safety note text | `Advisory-only preview. Execution is not allowed from this screen. Explicit operator approval is required before any Alpaca Paper submit; this card does not submit or cancel paper orders.` → `자문 전용 미리보기. 이 화면에서는 실행할 수 없습니다. Alpaca Paper 제출 전에는 명시적인 운영자 승인이 필요하며, 이 카드는 모의 주문을 제출하거나 취소하지 않습니다.` |
| `aria-label="Paper approval blocking reasons"` | `aria-label="모의 승인 차단 사유"` |
| `aria-label="Paper approval warnings"` | `aria-label="모의 승인 경고"` |

For each candidate `<dl>`:

| English | Korean |
|--|--|
| `<dt>Signal source</dt>` | `<dt>시그널 소스</dt>` |
| `<dt>Execution venue</dt>` | `<dt>실행 거래소</dt>` |
| `<dt>Asset class</dt>` | `<dt>자산 클래스</dt>` |
| `<dt>Workflow</dt>` | `<dt>워크플로우</dt>` |
| `Purpose: ${X}` | ``목적: ${labelOrToken(PURPOSE_LABEL, candidate.purpose)}`` |
| `Preview payload: ${X}` | ``미리보기 페이로드: ${X}`` |
| `${candidate.symbol} approval copy` (aria-label) | ``${candidate.symbol} 승인 안내`` |
| `${candidate.symbol} paper approval warnings` (aria-label) | ``${candidate.symbol} 모의 승인 경고`` |
| `No paper approval preview candidates are currently available.` | `현재 사용 가능한 모의 승인 미리보기 후보가 없습니다.` |

Replace `formatVenueLabel` to use the central `VENUE_LABEL`:

```tsx
function formatVenueLabel(venue: string | null, symbol: string | null): string {
  const venueLabel = venue
    ? (VENUE_LABEL[venue] ?? labelOperatorToken(venue))
    : COMMON.dash;
  return [venueLabel, symbol].filter(Boolean).join(" ");
}
```

Render candidate status with `labelOrToken(PAPER_APPROVAL_CANDIDATE_STATUS_LABEL, candidate.status)`.

- [ ] **Step 5: Translate the main PreopenPage component**

| English | Korean |
|--|--|
| `<h1>Preopen briefing</h1>` (both branches) | `<h1>장전 브리핑</h1>` |
| `<strong>No preopen research run available</strong>` | `<strong>장전 리서치 실행이 없습니다</strong>` |
| `Reason: ${X}` | ``사유: ${X}`` |
| `Generated: ${formatDateTime(...)}` | ``생성 시각: ${formatDateTime(...)}`` |
| `${data.market_scope.toUpperCase()}` | (keep — operator identifier) |
| `Advisory ${used/not used}` | ``자문 ${data.advisory_used ? "사용" : "미사용"}`` |
| `Advisory notice: ${X}` | ``자문 안내: ${X}`` |
| `aria-label="Source warnings"` | `aria-label="소스 경고"` |
| `<h2>Candidates (${N})</h2>` | ``<h2>후보 (${N})</h2>`` |
| Candidates table headers | `Symbol` → `종목`, `Side` → `방향`, `Kind` → `종류`, `Confidence` → `신뢰도`, `Price` → `가격`, `Qty` → `수량`, `Rationale` → `근거` |
| `<span>{c.side}</span>` | `<span>{labelOrderSide(c.side)}</span>` (import from `../i18n/formatters`) |
| `<h2>Pending reconciliations (${N})</h2>` | ``<h2>대기 중인 조정 항목 (${N})</h2>`` |
| Reconciliation table headers | `Symbol` → `종목`, `Classification` → `분류`, `NXT class` → `NXT 분류`, `Actionable` → `실행 가능`, `Gap %` → `괴리율`, `Summary` → `요약` |
| `Yes` / `No` | `예` / `아니오` (use `labelYesNo` from `../i18n/formatters`) |
| `<h2>Linked decision sessions</h2>` | `<h2>연결된 의사결정 세션</h2>` |
| `Open session` | `세션 열기` |
| `Confirm create decision session?` | `의사결정 세션을 생성할까요?` |
| `Creating…` | `생성 중…` |
| `Create decision session` (fallback if no `artifactCta.label`) | `의사결정 세션 생성` |
| `Cancel` | `{COMMON.cancel}` |
| `Failed to create decision session.` | `의사결정 세션 생성에 실패했습니다.` |
| `Something went wrong. Try again.` (state.message default) | `{COMMON.somethingWentWrong}` |

For the reconciliation classification cell, render `labelOrToken(RECONCILIATION_STATUS_LABEL, r.classification)` (import `RECONCILIATION_STATUS_LABEL`). For the NXT class cell, render `r.nxt_classification ? labelOrToken(NXT_CLASSIFICATION_LABEL, r.nxt_classification) : COMMON.dash`.

For the candidate kind cell, render `labelOrToken(CANDIDATE_KIND_LABEL, c.candidate_kind)` (import `CANDIDATE_KIND_LABEL`).

For confidence display, keep `${c.confidence}%` (numeric). For price, keep `${c.proposed_price} ${c.currency}` (numeric + ISO currency code).

For `linked_sessions[i].status`, the backend status string is a known controlled vocabulary mirroring `SessionStatus`; render `labelOrToken(SESSION_STATUS_LABEL, s.status)` and import `SESSION_STATUS_LABEL`.

- [ ] **Step 6: Update PreopenPage test assertions**

In `src/__tests__/PreopenPage.test.tsx`, sweep English UI string assertions and replace with the Korean equivalents from Steps 2–5. Pay special attention to:
- `"Preopen briefing"` → `"장전 브리핑"`
- `"Candidates"` / `"Pending reconciliations"` headings → Korean
- Side cell `"buy"` → `"매수"` if asserted directly (the test fixture decides what to render)
- Paper approval preview status `"Available"` → `"사용 가능"`
- QA `"Ready"` → `"준비 완료"`

Do not change literal API token assertions in test fixtures (e.g. `paper_approval_bridge.status: "available"`).

- [ ] **Step 7: Run tests and typecheck**

```bash
npm test -- PreopenPage
npm run typecheck
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/pages/PreopenPage.tsx src/__tests__/PreopenPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate PreopenPage chrome and structured metadata (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Translate CommitteeEvidenceArtifacts (and audit other Committee components)

**Files:**
- Modify: `src/components/CommitteeEvidenceArtifacts.tsx`
- Audit (modify only if hardcoded English chrome found): `src/components/CommitteeExecutionPreview.tsx`, `CommitteeJournalPlaceholder.tsx`, `CommitteePortfolioApproval.tsx`, `CommitteeResearchDebate.tsx`, `CommitteeRiskReview.tsx`, `CommitteeTraderDraft.tsx`, `CommitteeWorkflowTransition.tsx`

The committee components mostly render free-text payload fields (`summary`, `notes`, `rationale`) which we explicitly do NOT translate. Headings and field labels DO get translated.

- [ ] **Step 1: Translate CommitteeEvidenceArtifacts headings**

Edit `src/components/CommitteeEvidenceArtifacts.tsx`:

| English | Korean |
|--|--|
| `<h3>Committee Evidence</h3>` | `<h3>위원회 근거</h3>` |
| `<h4>Technical Analysis</h4>` | `<h4>기술적 분석</h4>` |
| `<h4>News Analysis</h4>` | `<h4>뉴스 분석</h4>` |
| `Confidence: ${X}%` | ``신뢰도: ${X}%`` |

Also render the `evidence.on_chain_analysis` block (it is in the type but missing here today). Add an `<h4>온체인 분석</h4>` block paralleling the others; if `evidence.on_chain_analysis` is `null`, omit the block. (Verify: the `CommitteeEvidence` type has `on_chain_analysis: CommitteeAnalysisSub | null`.)

- [ ] **Step 2: Audit other committee components**

Open each file and grep for hardcoded English `<h1>` through `<h4>`, `<button>`, and aria-label literals:

```bash
grep -nE '"[A-Z][A-Za-z ]+"' src/components/Committee*.tsx
```

For each match that is user-visible UI chrome (not a payload key, type literal, or className), translate to Korean. If a file already renders only Korean or only payload data, leave it alone.

Likely translations across these:

- `CommitteeWorkflowTransition`: button labels like `"Start"`, `"Approve"`, `"Auto Approve"`, `"Submit"`, `"Block"` → contextual Korean.
- `CommitteeRiskReview` headings/verdict labels.
- `CommitteeTraderDraft` action labels (use existing `CommitteeTraderAction` enum mapping if convenient — or inline mapping).

If a component file shows English-only chrome that requires more than a couple of swaps, isolate the translation to the headings and obvious button text only; do not refactor structure.

- [ ] **Step 3: Run committee component tests**

```bash
npm test -- components/Committee
npm run typecheck
```

Expected: PASS. If existing committee tests assert English headings, update those assertions to the Korean equivalents you used.

- [ ] **Step 4: Commit**

```bash
git add src/components/Committee*.tsx src/__tests__/components/Committee*.test.tsx
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate committee component chrome (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Final verification and ko-KR conformance sweep

**Files:** none (verification only).

- [ ] **Step 1: Run full test suite**

```bash
cd frontend/trading-decision
npm test
```

Expected: PASS (no failures).

- [ ] **Step 2: Run typecheck**

```bash
npm run typecheck
```

Expected: PASS.

- [ ] **Step 3: Run production build**

```bash
npm run build
```

Expected: PASS.

- [ ] **Step 4: Search for residual English UI chrome**

Run a sweep that surfaces any remaining English-looking strings in JSX text or aria-labels:

```bash
grep -RIn --include='*.tsx' -E '>[A-Z][a-zA-Z ]{2,}<' src/components src/pages | grep -v '\.test\.tsx' | grep -v '__tests__'
grep -RIn --include='*.tsx' 'aria-label="[A-Z]' src/components src/pages | grep -v '\.test\.tsx' | grep -v '__tests__'
```

For each hit, decide:
- It's UI chrome → translate inline (small additional commit if needed).
- It's a test fixture / sample text / external content (article title, source name, URL) → leave alone.
- It's an operator identifier (e.g. "KR", "US", "Alpaca Paper") → leave alone.

If you find any chrome that still needs translating, fix it and commit:

```bash
git add -p
git commit -m "$(cat <<'EOF'
feat(trading-decision): translate residual UI chrome (ROB-111)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Verify locale defaults still flow**

```bash
grep -RIn 'en-US' src/format src/components src/pages
```

Expected: zero matches except in tests that explicitly pass `"en-US"` to assert opt-in behavior.

- [ ] **Step 6: Smoke-check the dev build in a browser**

```bash
npm run dev
```

In the browser, exercise the golden paths:
1. `/` (Decision inbox) — verify Korean column headers, status filter, pagination.
2. `/sessions/<uuid>` for a fixture session — verify proposal labels, response buttons (수락/수정/...), date format.
3. `/preopen` — verify briefing chrome, QA panel, paper approval preview Korean labels, raw JSON tucked under `원본 데이터 보기`.

Confirm:
- Dates render as `2026. 5. 5. 오후 7:30` style (ko-KR `dateStyle: "medium"` + `timeStyle: "short"`).
- Numbers render with comma grouping (`1,234,567`).
- Status badges display Korean (`진행 중`, `종료`, `보관됨`, `대기`, `수락`, etc.).
- Raw JSON is hidden behind `원본 데이터 보기` `<details>`.

If any panel still shows raw English chrome, treat it as a Step 4 finding and patch.

- [ ] **Step 7: Push branch and open the PR**

```bash
git push -u origin feature/ROB-111-trading-decision-korean-ui
gh pr create --base main --title "feat(trading-decision): Korean UI + ko-KR locale (ROB-111)" --body "$(cat <<'EOF'
## Summary
- Adds `src/i18n/` Korean label/formatter layer (no runtime i18n dep).
- Switches `formatDateTime` and `formatDecimal` defaults to `ko-KR`.
- Translates static UI chrome, status/action/token labels, and structured metadata across SessionList, SessionDetail, Preopen, Proposal, Outcome, Reconciliation, Warning, News, MarketBrief, and Committee components.
- Hides raw JSON payloads behind `원본 데이터 보기` when known structured fields can be displayed.

## Scope (per ROB-111)
- Display-only. No API/DB/scheduler/broker side effects.
- Existing DB free-text (`notes`, `market_brief`'s arbitrary string values, `original_rationale`, `original_payload` free text, news titles) is **not** translated.
- TradingAgents output language is **not** changed.

## Test plan
- [ ] `npm run typecheck` (frontend/trading-decision)
- [ ] `npm test`
- [ ] `npm run build`
- [ ] Manual smoke: `/`, `/sessions/<uuid>`, `/preopen` — Korean labels, ko-KR formatting, raw JSON gated behind `원본 데이터 보기`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Acceptance Criteria Coverage

| Criterion | Covered by |
|--|--|
| Korean static UI copy across list/detail/preopen and shared components | Tasks 3–16 |
| Default date/number formatting uses `ko-KR` | Task 2 |
| Raw enum/status/action tokens not shown directly when a Korean label is available | Tasks 1, 3, 5, 7, 11, 12, 15, 16 |
| Known structured metadata (`market_brief`, safety scope, source profile, workflow) shown with Korean labels/descriptions | Tasks 1, 10, 15 |
| Raw JSON gated behind `원본 데이터 보기` | Task 10 (`MarketBriefPanel`) |
| Existing DB free-text values unchanged and not bulk translated | Operating Rules + Task 8/16 (translate UI chrome only) |
| Existing tests updated and passing | Tasks 3–16 + Task 17 verification |
| `npm run typecheck`, `npm test`, `npm run build` pass | Task 17 |
| No broker/order/DB/scheduler/trading side effects | Operating Rules + display-only edits throughout |
