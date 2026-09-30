// Task 890 PR A round 2 finding B1 and its round 3 follow-up: the loss-cut
// second-click button is an approval control like approve/deny. It shows only
// while both approval gates are on, the row has not expired and the server
// detail is exactly the in-flight confirmation step. Detail shapes mirror the
// real core after a web first click, a deny, a completed confirm and expiry.
// Adapted from the round-2 tester's counterexample.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApprovalInboxPanel } from "../trader/ApprovalInboxPanel";
import type { TraderApprovalItem } from "../trader/types";

const ID = "11111111-2222-3333-4444-555555555555";
const NOW = new Date("2026-09-30T01:00:00Z");
const calls: Array<{ url: string; method: string }> = [];
let listActionsEnabled = true;
let listLossCutEnabled = true;
type DetailMode =
  | "confirming"
  | "denied"
  | "finished"
  | "approved_only"
  | "lease_only"
  | "expired"
  | "no_pending_rungs"
  | "not_published"
  | "republished"
  | "gate_off"
  | "missing";
let detailMode: DetailMode = "confirming";

function lossCutItem(): TraderApprovalItem {
  return {
    proposal_id: ID,
    symbol: "AAPL",
    market: "equity_us",
    account_mode: "toss_live",
    broker_account_id: null,
    side: "sell",
    order_type: "limit",
    action: "place",
    exit_intent: "loss_cut",
    requires_two_step: true,
    card_kind: "manual",
    lifecycle_state: "proposed",
    rungs: [
      {
        rung_index: 0,
        side: "sell",
        quantity: "1",
        limit_price: "99",
        notional: "99",
        state: "pending_approval",
        broker_order_id: null,
        filled_qty: null,
        void_reason: null,
        distance_pct: null,
      },
    ],
    total_quantity: "1",
    total_notional: "99",
    distance_pct: null,
    distance_price_asof: null,
    tier: null,
    caveats: ["loss_cut_two_step"],
    valid_until: "2026-09-30T01:00:02Z",
    expires_in_seconds: 2,
    approved_at: null,
    approved_by_channel: null,
    commit_lease_active: false,
    actionable: true,
    block_reason: null,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Detail as the real inbox service projects it after a web first click. */
function detailBody(mode: DetailMode) {
  const confirming: TraderApprovalItem = {
    ...lossCutItem(),
    card_kind: "loss_cut_confirmation",
    actionable: false,
    block_reason: "not_human_card",
  };
  const approved = { approved_at: "2026-09-30T01:00:01Z", approved_by_channel: "telegram" };
  // Each non-"confirming" shape differs from the confirmation step in the
  // field the gate reads; denied and finished are the real core's full shapes.
  const shapes: Record<DetailMode, TraderApprovalItem> = {
    confirming,
    denied: {
      ...confirming,
      ...approved,
      lifecycle_state: "rejected",
      block_reason: "terminal",
      rungs: confirming.rungs.map((r) => ({ ...r, state: "rejected" })),
    },
    finished: { ...confirming, ...approved, commit_lease_active: true },
    approved_only: { ...confirming, ...approved },
    lease_only: { ...confirming, commit_lease_active: true },
    expired: { ...confirming, expires_in_seconds: 0 },
    no_pending_rungs: {
      ...confirming,
      rungs: confirming.rungs.map((r) => ({ ...r, state: "acked" })),
    },
    not_published: { ...confirming, block_reason: "not_published" },
    republished: { ...lossCutItem(), card_kind: "reconfirm" },
    gate_off: confirming,
    missing: confirming,
  };
  const item = shapes[mode];
  return {
    as_of: NOW.toISOString(),
    actions_enabled: true,
    loss_cut_actions_enabled: mode !== "gate_off",
    item,
  };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW);
  calls.length = 0;
  listActionsEnabled = true;
  listLossCutEnabled = true;
  detailMode = "confirming";
  document.cookie = "csrftoken=test-csrf";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (method === "POST") {
        return json({
          handled: true,
          reason: "loss_cut_confirmation_required",
          proposal_id: ID,
          confirmation_token: "test-confirmation-token",
        });
      }
      if (url.endsWith(`/approvals/${ID}`)) {
        if (detailMode === "missing") return json({ detail: "proposal_not_found" }, 404);
        return json(detailBody(detailMode));
      }
      return json({
        as_of: NOW.toISOString(),
        actions_enabled: listActionsEnabled,
        loss_cut_actions_enabled: listLossCutEnabled,
        count: 1,
        items: [lossCutItem()],
      });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function firstClick() {
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);
  fireEvent.click(within(row).getByTestId(`approve-${ID}`));
  await waitFor(() =>
    expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeInTheDocument(),
  );
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  return row;
}

test("loss-cut confirmation control disappears when the proposal expires after the first click", async () => {
  const row = await firstClick();

  await act(async () => {
    vi.advanceTimersByTime(3000);
  });

  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
  expect(within(row).queryByTestId(`approve-${ID}`)).toBeNull();
  expect(within(row).queryByTestId(`deny-${ID}`)).toBeNull();
  expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull();
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
});

test("loss-cut confirmation control disappears when a refresh reports actions disabled", async () => {
  const row = await firstClick();

  listActionsEnabled = false;
  listLossCutEnabled = false;
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));

  await waitFor(() =>
    expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull(),
  );
  expect(within(row).queryByTestId(`approve-${ID}`)).toBeNull();
  expect(within(row).queryByTestId(`deny-${ID}`)).toBeNull();
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
});

test("loss-cut confirmation control disappears when a refresh turns only the loss-cut gate off", async () => {
  const row = await firstClick();

  listLossCutEnabled = false;
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));

  await waitFor(() =>
    expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull(),
  );
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
});

// Proposal state changed elsewhere (Telegram deny or confirm, expiry sweep,
// row gone): the list reload and the per-row state refresh both re-read the
// detail and withdraw the button.
const WITHDRAWN = [
  "denied",
  "finished",
  "approved_only",
  "lease_only",
  "expired",
  "no_pending_rungs",
  "not_published",
  "republished",
  "gate_off",
  "missing",
] as const;

test.each([
  ...WITHDRAWN.map((mode) => [mode, "새로고침"] as const),
  ...WITHDRAWN.map((mode) => [mode, "상태 새로고침"] as const),
])(
  "loss-cut confirmation control disappears when the detail re-read is %s (%s)",
  async (mode, refreshButton) => {
    const row = await firstClick();

    detailMode = mode;
    fireEvent.click(within(document.body).getByRole("button", { name: refreshButton }));

    await waitFor(() =>
      expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull(),
    );
    expect(within(row).queryByTestId(`approve-${ID}`)).toBeNull();
    expect(within(row).queryByTestId(`deny-${ID}`)).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  },
);

test.each(WITHDRAWN)(
  "no loss-cut confirmation control when the post-first-click detail is %s",
  async (mode) => {
    detailMode = mode;
    render(<ApprovalInboxPanel />);
    const row = await screen.findByTestId(`approval-row-${ID}`);
    fireEvent.click(within(row).getByTestId(`approve-${ID}`));

    await waitFor(() => expect(within(row).getByTestId(`approval-result-${ID}`)).toBeInTheDocument());
    expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  },
);

test("a list reload keeps the confirmation control while the detail is still confirming", async () => {
  const row = await firstClick();

  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.url.endsWith(`/approvals/${ID}`) && c.method === "GET")).toHaveLength(2),
  );

  expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeInTheDocument();
});
