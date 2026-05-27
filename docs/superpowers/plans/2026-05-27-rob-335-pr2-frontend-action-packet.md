# ROB-335 PR2 — `/invest/reports` ActionPacket 프론트 surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR1이 추가한 `action_packet` read-model payload를 `/invest/reports` 리포트 상세 화면에 "오늘의 보유 액션 / 신규 후보 / 리스크 / 데이터 부족" 4헤더 + sub-verdict chip으로 렌더한다.

**Architecture:** ROB-322 `ReviewSectionsView`와 동일한 view-layer 패턴. 백엔드 snake_case `action_packet` JSON → `normalizeActionPacket`로 camelCase 변환 → `ActionPacketView` 컴포넌트가 `Pill` 칩으로 sub-verdict를 표시. 기존 `review_sections` surface를 대체하지 않고 그 아래에 additive로 마운트. ActionPacket이 없으면(legacy/비-intraday) 렌더 안 함.

**Tech Stack:** React + TypeScript, Vitest + @testing-library/react (jsdom), 인라인 CSS + CSS 변수, 수기 TS 타입(OpenAPI 생성 아님). 설계 근거: `docs/superpowers/specs/2026-05-27-rob-335-intraday-action-cycle-design.md` §3.6. 백엔드 payload: PR #985 (`app/schemas/investment_reports.py` `ActionPacket`/`ActionPacketEntry`/`DataGapEntry`).

---

## Scope & 선행 조건

- **PR2만** 다룬다. PR1(#985)이 머지된 fresh `main`에서 새 worktree/브랜치로 시작하는 것이 spec 권장 순서. (plan은 선행 작성 가능.)
- frontend vitest는 [[project_frontend_invest_vitest_threads_flaky]] 대로 `--pool=forks`로 실행. baseline 5건 pre-existing 실패 존재.
- 마이그레이션·백엔드 변경 없음 (백엔드 ActionPacket은 PR1에서 완료).

## 백엔드 payload 계약 (PR1 #985, snake_case JSON)

```jsonc
"action_packet": {
  "held_actions":  [{ "verdict": "sell_review", "symbol": "005930", "side": "sell",
                      "rationale": "...", "item_uuid": "...", "evidence_snapshot": {...} }],
  "new_buy_candidates": [{ "verdict": "buy_review", "symbol": "000660", ... }],
  "no_new_buy_reason": "국내 스크리너 스냅샷이 stale ... | null",
  "risk_reviews": [{ "verdict": "watch_only", ... }],
  "no_action_reason": { "kind": "data_insufficient", "reason_ko": "...",
                        "blocking_sources": ["portfolio"], "excluded_count": 0 } | null,
  "data_gaps_for_next_cycle": [{ "source": "portfolio", "status": "unavailable",
                                 "reason": "user_id_missing" }]
}
```

verdict 어휘(11종): `buy_review/limit_wait/no_new_buy_candidates/sell_review/trim_review/add_review/keep/no_add/watch_only/rejected/data_gap`.

## File Structure

**Create:**
- `frontend/invest/src/components/investment-reports/ActionPacketView.tsx` — 4헤더 + sub-verdict chip 렌더 컴포넌트 (자체 완결, `Pill` 재사용).
- `frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts` — `normalizeActionPacket` 단위 테스트.
- `frontend/invest/src/__tests__/ActionPacketView.test.tsx` — 컴포넌트 렌더 테스트.

**Modify:**
- `frontend/invest/src/types/investmentReports.ts` — `ActionVerdict`/`ActionPacketEntry`/`DataGapEntry`/`ActionPacket` 타입 + `InvestmentReportBundle.actionPacket` 필드.
- `frontend/invest/src/api/investmentReports.ts` — `normalizeActionPacket`(+entry) export + `fetchInvestmentReportBundle` 연결.
- `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` — `ActionPacketView` 마운트.

---

## Task 1: TS 타입 + normalizeActionPacket + fetch 연결

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts` (≈line 345-359, `ReportReviewSections`/`InvestmentReportBundle` 부근)
- Modify: `frontend/invest/src/api/investmentReports.ts` (`normalizeReviewSections` ≈190-212, `fetchInvestmentReportBundle` ≈312-341)
- Test: `frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts`

- [ ] **Step 1: 실패 테스트 작성**

```ts
// frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts
import { describe, expect, it } from "vitest";

import { normalizeActionPacket } from "../api/investmentReports";

describe("normalizeActionPacket", () => {
  it("returns null when omitted (legacy/non-intraday)", () => {
    expect(normalizeActionPacket(undefined)).toBeNull();
    expect(normalizeActionPacket(null)).toBeNull();
  });

  it("maps snake_case payload to camelCase groups", () => {
    const packet = normalizeActionPacket({
      held_actions: [
        { verdict: "sell_review", symbol: "005930", side: "sell",
          rationale: "보유 매도 검토", item_uuid: "i1", evidence_snapshot: { x: 1 } },
        { verdict: "keep", symbol: "000660", side: null,
          rationale: "유지", item_uuid: "i2", evidence_snapshot: {} },
      ],
      new_buy_candidates: [],
      no_new_buy_reason: "스크리너 stale",
      risk_reviews: [{ verdict: "watch_only", symbol: "035720", rationale: "관망",
                       item_uuid: "i3", evidence_snapshot: {} }],
      no_action_reason: { kind: "data_insufficient", reason_ko: "데이터 부족",
                          blocking_sources: ["portfolio"], excluded_count: 2 },
      data_gaps_for_next_cycle: [
        { source: "portfolio", status: "unavailable", reason: "user_id_missing" },
      ],
    });
    expect(packet).not.toBeNull();
    expect(packet!.heldActions.map((e) => e.verdict)).toEqual(["sell_review", "keep"]);
    expect(packet!.heldActions[0].itemUuid).toBe("i1");
    expect(packet!.newBuyCandidates).toEqual([]);
    expect(packet!.noNewBuyReason).toBe("스크리너 stale");
    expect(packet!.riskReviews[0].verdict).toBe("watch_only");
    expect(packet!.noActionReason!.kind).toBe("data_insufficient");
    expect(packet!.noActionReason!.blockingSources).toEqual(["portfolio"]);
    expect(packet!.dataGapsForNextCycle[0]).toEqual({
      source: "portfolio", status: "unavailable", reason: "user_id_missing",
    });
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/investmentReportsActionPacket.test.ts`
Expected: FAIL — `normalizeActionPacket` export 없음.

- [ ] **Step 3: 타입 추가 — `types/investmentReports.ts`** (`ReportReviewSections` 정의 직후, ≈line 348)

```ts
// ROB-335 — intraday ActionPacket (mirrors backend ActionPacket schema).
export type ActionVerdict =
  | "buy_review"
  | "limit_wait"
  | "no_new_buy_candidates"
  | "sell_review"
  | "trim_review"
  | "add_review"
  | "keep"
  | "no_add"
  | "watch_only"
  | "rejected"
  | "data_gap";

export interface ActionPacketEntry {
  verdict: ActionVerdict;
  symbol?: string | null;
  side?: "buy" | "sell" | null;
  rationale: string;
  itemUuid?: string | null;
  evidenceSnapshot: Record<string, unknown>;
}

export interface DataGapEntry {
  source: string;
  status?: string | null;
  reason?: string | null;
}

export interface ActionPacket {
  heldActions: ActionPacketEntry[];
  newBuyCandidates: ActionPacketEntry[];
  noNewBuyReason?: string | null;
  riskReviews: ActionPacketEntry[];
  noActionReason?: NoActionSummary | null;
  dataGapsForNextCycle: DataGapEntry[];
}
```

`InvestmentReportBundle` 인터페이스(≈line 350-359)에 필드 추가 (`reviewSections` 줄 다음):

```ts
  // ROB-335 — additive intraday ActionPacket projection. Null for legacy /
  // non-intraday reports.
  actionPacket?: ActionPacket | null;
```

- [ ] **Step 4: normalizer 추가 — `api/investmentReports.ts`** (`normalizeReviewSections` 다음, ≈line 212)

import 블록의 타입 import에 `ActionPacket`, `ActionPacketEntry`, `ActionVerdict`, `DataGapEntry` 추가. 그리고:

```ts
// ROB-335 — normalize the additive intraday ActionPacket. Null when the
// backend omits it (legacy / non-intraday reports).
function normalizeActionPacketEntry(raw: unknown): ActionPacketEntry {
  const obj = asRecord(raw);
  return {
    verdict: asString(obj.verdict, "data_gap") as ActionVerdict,
    symbol: asOptionalString(obj.symbol),
    side: asOptionalString(obj.side) as "buy" | "sell" | null,
    rationale: asString(obj.rationale, ""),
    itemUuid: asOptionalString(obj.item_uuid),
    evidenceSnapshot: asRecord(obj.evidence_snapshot),
  };
}

export function normalizeActionPacket(raw: unknown): ActionPacket | null {
  if (raw === null || raw === undefined) return null;
  const obj = asRecord(raw);

  let noActionReason: NoActionSummary | null = null;
  if (obj.no_action_reason !== null && obj.no_action_reason !== undefined) {
    const s = asRecord(obj.no_action_reason);
    noActionReason = {
      kind: asOptionalString(s.kind) as WhyNoActionKind | null,
      reasonKo: asOptionalString(s.reason_ko),
      blockingSources: asArray<string>(s.blocking_sources),
      excludedCount: asNumber(s.excluded_count, 0),
    };
  }

  return {
    heldActions: asArray(obj.held_actions).map(normalizeActionPacketEntry),
    newBuyCandidates: asArray(obj.new_buy_candidates).map(normalizeActionPacketEntry),
    noNewBuyReason: asOptionalString(obj.no_new_buy_reason),
    riskReviews: asArray(obj.risk_reviews).map(normalizeActionPacketEntry),
    noActionReason,
    dataGapsForNextCycle: asArray<Record<string, unknown>>(
      obj.data_gaps_for_next_cycle,
    ).map((g) => ({
      source: asString(g.source, ""),
      status: asOptionalString(g.status),
      reason: asOptionalString(g.reason),
    })),
  };
}
```

`fetchInvestmentReportBundle`의 `readJson<{...}>` 제네릭에 `action_packet?: unknown;` 추가하고, return 객체에 추가:

```ts
    actionPacket: normalizeActionPacket(raw.action_packet),
```

- [ ] **Step 5: 통과 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/investmentReportsActionPacket.test.ts`
Expected: PASS (2 passed).

- [ ] **Step 6: 커밋**

```bash
git add frontend/invest/src/types/investmentReports.ts frontend/invest/src/api/investmentReports.ts frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts
git commit -m "feat(rob-335): frontend ActionPacket types + normalizer (PR2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: ActionPacketView 컴포넌트 (4헤더 + sub-verdict chip)

**Files:**
- Create: `frontend/invest/src/components/investment-reports/ActionPacketView.tsx`
- Test: `frontend/invest/src/__tests__/ActionPacketView.test.tsx`

- [ ] **Step 1: 실패 테스트 작성**

```tsx
// frontend/invest/src/__tests__/ActionPacketView.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActionPacketView } from "../components/investment-reports/ActionPacketView";
import type { ActionPacket } from "../types/investmentReports";

function makePacket(overrides: Partial<ActionPacket> = {}): ActionPacket {
  return {
    heldActions: [
      { verdict: "sell_review", symbol: "005930", side: "sell",
        rationale: "보유 매도 검토", itemUuid: "i1", evidenceSnapshot: {} },
      { verdict: "keep", symbol: "000660", side: null,
        rationale: "유지", itemUuid: "i2", evidenceSnapshot: {} },
    ],
    newBuyCandidates: [],
    noNewBuyReason: "국내 스크리너 스냅샷이 stale",
    riskReviews: [
      { verdict: "watch_only", symbol: "035720", rationale: "관망",
        itemUuid: "i3", evidenceSnapshot: {} },
    ],
    noActionReason: { kind: "data_insufficient", reasonKo: "데이터 부족",
                      blockingSources: ["portfolio"], excludedCount: 0 },
    dataGapsForNextCycle: [
      { source: "portfolio", status: "unavailable", reason: "user_id_missing" },
    ],
    ...overrides,
  };
}

describe("ActionPacketView", () => {
  it("renders the four intraday headers", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByRole("heading", { name: /오늘의 보유 액션/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /신규 후보/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /리스크/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /데이터 부족/ })).toBeInTheDocument();
  });

  it("renders held verdict chips with Korean labels", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText("매도 검토")).toBeInTheDocument();
    expect(screen.getByText("유지")).toBeInTheDocument();
    expect(screen.getByText("005930")).toBeInTheDocument();
  });

  it("shows no-new-buy reason when there are no candidates", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText(/국내 스크리너 스냅샷이 stale/)).toBeInTheDocument();
  });

  it("lists data gaps with their source", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText(/portfolio/)).toBeInTheDocument();
    expect(screen.getByText(/user_id_missing/)).toBeInTheDocument();
  });

  it("renders empty-state copy when a group is empty and no reason given", () => {
    render(<ActionPacketView packet={makePacket({
      heldActions: [], newBuyCandidates: [], noNewBuyReason: null,
      riskReviews: [], noActionReason: null, dataGapsForNextCycle: [],
    })} />);
    // Each empty group shows the shared "해당 없음" placeholder.
    expect(screen.getAllByText("해당 없음").length).toBeGreaterThanOrEqual(3);
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/ActionPacketView.test.tsx`
Expected: FAIL — `ActionPacketView` 모듈 없음.

- [ ] **Step 3: 컴포넌트 구현**

```tsx
// frontend/invest/src/components/investment-reports/ActionPacketView.tsx
// ROB-335 — intraday ActionPacket surface: four headers (held / new / risk /
// data-gap) + sub-verdict chips. Pure view-layer over the bundle.actionPacket
// projection (parallel to ROB-322 ReviewSectionsView).
import { Pill, type PillTone } from "../../ds/atoms";
import type {
  ActionPacket,
  ActionPacketEntry,
  ActionVerdict,
  DataGapEntry,
  NoActionSummary,
} from "../../types/investmentReports";

const VERDICT_LABELS: Record<ActionVerdict, string> = {
  buy_review: "신규매수 검토",
  limit_wait: "지정가 대기",
  no_new_buy_candidates: "신규 후보 없음",
  sell_review: "매도 검토",
  trim_review: "축소 검토",
  add_review: "추가매수 검토",
  keep: "유지",
  no_add: "추가매수 금지",
  watch_only: "관망",
  rejected: "제외",
  data_gap: "데이터 부족",
};

const VERDICT_TONES: Record<ActionVerdict, PillTone> = {
  buy_review: "gain",
  add_review: "gain",
  limit_wait: "warn",
  no_new_buy_candidates: "paper",
  sell_review: "loss",
  trim_review: "loss",
  keep: "paper",
  no_add: "paper",
  watch_only: "accent",
  rejected: "warn",
  data_gap: "warn",
};

function EntryRow({ entry }: { entry: ActionPacketEntry }) {
  return (
    <div style={{ display: "grid", gap: 4, padding: "6px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Pill tone={VERDICT_TONES[entry.verdict]} size="sm">
          {VERDICT_LABELS[entry.verdict]}
        </Pill>
        {entry.symbol ? (
          <strong style={{ fontSize: 14 }}>{entry.symbol}</strong>
        ) : null}
      </div>
      <p style={{ margin: 0, fontSize: 13 }}>{entry.rationale}</p>
    </div>
  );
}

function PacketSection({
  title,
  entries,
  emptyReason,
}: {
  title: string;
  entries: ActionPacketEntry[];
  emptyReason?: string | null;
}) {
  return (
    <section style={{ display: "grid", gap: 6 }}>
      <h2 style={{ margin: 0, fontSize: 18 }}>
        {title} ({entries.length})
      </h2>
      {entries.length > 0 ? (
        entries.map((entry, idx) => (
          <EntryRow key={entry.itemUuid ?? `${entry.verdict}-${idx}`} entry={entry} />
        ))
      ) : (
        <p style={{ margin: 0, fontSize: 13 }}>{emptyReason ?? "해당 없음"}</p>
      )}
    </section>
  );
}

function DataGapSection({
  gaps,
  noActionReason,
}: {
  gaps: DataGapEntry[];
  noActionReason?: NoActionSummary | null;
}) {
  return (
    <section style={{ display: "grid", gap: 6 }}>
      <h2 style={{ margin: 0, fontSize: 18 }}>데이터 부족 ({gaps.length})</h2>
      {noActionReason?.reasonKo ? (
        <p style={{ margin: 0, fontSize: 13 }}>{noActionReason.reasonKo}</p>
      ) : null}
      {gaps.length > 0 ? (
        gaps.map((gap, idx) => (
          <div key={`${gap.source}-${idx}`} style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Pill tone="warn" size="sm">{gap.source}</Pill>
            <span style={{ fontSize: 13 }}>
              {[gap.status, gap.reason].filter(Boolean).join(" · ")}
            </span>
          </div>
        ))
      ) : (
        <p style={{ margin: 0, fontSize: 13 }}>해당 없음</p>
      )}
    </section>
  );
}

export function ActionPacketView({ packet }: { packet: ActionPacket }) {
  return (
    <section data-testid="action-packet" style={{ display: "grid", gap: 16 }}>
      <PacketSection title="오늘의 보유 액션" entries={packet.heldActions} />
      <PacketSection
        title="신규 후보"
        entries={packet.newBuyCandidates}
        emptyReason={packet.noNewBuyReason}
      />
      <PacketSection title="리스크" entries={packet.riskReviews} />
      <DataGapSection
        gaps={packet.dataGapsForNextCycle}
        noActionReason={packet.noActionReason}
      />
    </section>
  );
}
```

- [ ] **Step 4: 통과 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/ActionPacketView.test.tsx`
Expected: PASS (5 passed).

- [ ] **Step 5: 커밋**

```bash
git add frontend/invest/src/components/investment-reports/ActionPacketView.tsx frontend/invest/src/__tests__/ActionPacketView.test.tsx
git commit -m "feat(rob-335): ActionPacketView — four intraday headers + verdict chips (PR2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 리포트 상세 화면에 ActionPacketView 마운트

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` (import + 마운트, 리뷰/플랫 블록 ≈line 671 직후·alerts ≈line 673 직전)
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx`

- [ ] **Step 1: 실패 테스트 작성**

```tsx
// frontend/invest/src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type { ActionPacket, InvestmentReportBundle } from "../types/investmentReports";

vi.mock("../hooks/useInvestmentReportBundle", () => ({
  useInvestmentReportBundle: vi.fn(),
}));
import { useInvestmentReportBundle } from "../hooks/useInvestmentReportBundle";

const REPORT = {
  reportUuid: "00000000-0000-0000-0000-000000000001",
  reportType: "kr_morning", market: "kr", marketSession: "regular",
  accountScope: "kis_live", executionMode: "advisory_only", createdByProfile: "t",
  title: "KR", summary: "s", riskSummary: null, thesisText: null, noActionNote: null,
  marketSnapshot: {}, portfolioSnapshot: {}, previousReportUuid: null, status: "draft",
  metadata: {}, createdAt: "2026-05-27T00:00:00Z", updatedAt: "2026-05-27T00:00:00Z",
  publishedAt: null, validUntil: null, snapshotBundleUuid: null,
  snapshotPolicyVersion: null, snapshotCoverageSummary: null,
  snapshotFreshnessSummary: null, sourceConflicts: null, unavailableSources: null,
  snapshotReportDiagnostics: null,
} as InvestmentReportBundle["report"];

function makeBundle(actionPacket: ActionPacket | null): InvestmentReportBundle {
  return {
    report: REPORT, items: [], decisionsByItemUuid: {}, alerts: [], events: [],
    reviewSections: null, actionPacket,
  };
}

function renderWith(bundle: InvestmentReportBundle) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    status: "ready", bundle, error: null, reload: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={["/reports/00000000-0000-0000-0000-000000000001"]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

describe("InvestmentReportBundleContent — ActionPacket mount", () => {
  it("renders ActionPacketView when actionPacket is present", () => {
    renderWith(makeBundle({
      heldActions: [{ verdict: "keep", symbol: "005930", side: null,
        rationale: "유지", itemUuid: "i1", evidenceSnapshot: {} }],
      newBuyCandidates: [], noNewBuyReason: "신규 후보 없음",
      riskReviews: [], noActionReason: null, dataGapsForNextCycle: [],
    }));
    expect(screen.getByTestId("action-packet")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /오늘의 보유 액션/ })).toBeInTheDocument();
  });

  it("does not render ActionPacketView for legacy bundles (actionPacket null)", () => {
    renderWith(makeBundle(null));
    expect(screen.queryByTestId("action-packet")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx`
Expected: FAIL — `action-packet` testid 없음 (마운트 전).

- [ ] **Step 3: 마운트 구현**

import 추가 (파일 상단 컴포넌트 import 블록):

```tsx
import { ActionPacketView } from "./ActionPacketView";
```

`InvestmentReportBundleContent` JSX에서 리뷰/플랫 블록(≈line 648-671)과 `active watches` 섹션(≈line 673) 사이에 삽입:

```tsx
      {bundle.actionPacket ? (
        <ActionPacketView packet={bundle.actionPacket} />
      ) : null}
```

- [ ] **Step 4: 통과 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx`
Expected: PASS (2 passed).

- [ ] **Step 5: 기존 리뷰섹션 테스트 회귀 확인** (actionPacket 옵셔널이 makeBundle 깨지 않음)

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/InvestmentReportBundleContent.reviewSections.test.tsx`
Expected: PASS (기존 전부; `actionPacket` 미설정 = undefined → 마운트 안 함).

- [ ] **Step 6: 커밋**

```bash
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx
git commit -m "feat(rob-335): mount ActionPacketView on report detail surface (PR2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 전체 검증 — vitest(forks) + 타입체크/lint

**Files:** 없음 (검증 전용).

- [ ] **Step 1: 신규 + 관련 테스트 forks 실행**

Run:
```bash
cd frontend/invest && npx vitest run --pool=forks \
  src/__tests__/investmentReportsActionPacket.test.ts \
  src/__tests__/ActionPacketView.test.tsx \
  src/__tests__/InvestmentReportBundleContent.actionPacket.test.tsx \
  src/__tests__/InvestmentReportBundleContent.reviewSections.test.tsx
```
Expected: 전부 PASS.

- [ ] **Step 2: 프론트 전체 스위트 (forks)로 baseline 대비 회귀 확인**

Run: `cd frontend/invest && npx vitest run --pool=forks`
Expected: 신규 테스트 PASS + baseline 5건 pre-existing 실패([[project_frontend_invest_vitest_threads_flaky]])만 — 신규 실패 0건.

- [ ] **Step 3: 타입체크 / lint (프로젝트 스크립트 확인 후)**

`frontend/invest/package.json`의 scripts에서 typecheck/lint 명령을 확인하고 실행 (예: `npm run typecheck` 또는 `npx tsc --noEmit`, `npm run lint`). 신규 타입/컴포넌트가 통과해야 함.

- [ ] **Step 4: 로컬 시각 확인 (선택)**

[[gstack]] `/browse`로 실제 KR intraday 리포트 상세(`/invest/reports/{uuid}`)를 열어 4헤더 + verdict chip이 렌더되는지 육안 확인. (백엔드 PR1 머지 + intraday 리포트 1건 생성 선행 필요. 없으면 스킵하고 사유 기록.)

---

## Self-Review

**Spec coverage (§3.6):**
- "오늘의 보유 액션 / 신규 후보 / 리스크 / 데이터 부족" 4헤더 → Task 2 `ActionPacketView` (PacketSection×3 + DataGapSection). ✅
- sub-verdict chip → Task 2 `Pill` + `VERDICT_LABELS`/`VERDICT_TONES`. ✅
- `action_packet` payload 소비 → Task 1 타입 + `normalizeActionPacket` + fetch 연결. ✅
- ROB-275/evidence viewer 호환 유지 → additive 마운트, 기존 review/flat/alerts/events 블록 미변경 (Task 3 Step 5 회귀). ✅
- legacy/비-intraday null-safe → Task 3 `bundle.actionPacket ? ... : null` + 테스트. ✅

**Placeholder scan:** Task 4 Step 3는 프로젝트 스크립트명 확인 후 실행(명령 후보 제시), Step 4는 선행조건부 선택 단계로 명시. 그 외 전 스텝 실제 코드 포함. ✅

**Type consistency:** `ActionVerdict`(11값) ↔ `VERDICT_LABELS`/`VERDICT_TONES` 키 동일. `ActionPacket` 필드(heldActions/newBuyCandidates/noNewBuyReason/riskReviews/noActionReason/dataGapsForNextCycle)가 Task 1 타입 ↔ Task 1 normalizer ↔ Task 2 컴포넌트 ↔ Task 3 테스트 전부 일치. `NoActionSummary`(kind/reasonKo/blockingSources/excludedCount)는 기존 ROB-322 타입 재사용. `Pill` tone은 검증된 `PillTone` 값(gain/loss/warn/accent/paper)만 사용. import 경로: 컴포넌트→`../../ds/atoms`, `../../types/investmentReports` (src/components/investment-reports 기준 2단계 상위). ✅
