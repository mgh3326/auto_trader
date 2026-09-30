// Task 890 PR A round 2 finding B1: the loss-cut second-click button is an
// approval control like approve/deny, so it obeys the same page gates
// (actions_enabled and the row's expiry). Adapted from the round-2 tester's
// counterexample; the frozen contract test is left unchanged.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApprovalInboxPanel } from "../trader/ApprovalInboxPanel";
import type { TraderApprovalItem } from "../trader/types";

const ID = "11111111-2222-3333-4444-555555555555";
const NOW = new Date("2026-09-30T01:00:00Z");
const calls: Array<{ url: string; method: string }> = [];
let listActionsEnabled = true;

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

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW);
  calls.length = 0;
  listActionsEnabled = true;
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
        return json({
          as_of: NOW.toISOString(),
          actions_enabled: true,
          loss_cut_actions_enabled: true,
          item: {
            ...lossCutItem(),
            card_kind: "loss_cut_confirmation",
            actionable: false,
            block_reason: "not_human_card",
          },
        });
      }
      return json({
        as_of: NOW.toISOString(),
        actions_enabled: listActionsEnabled,
        loss_cut_actions_enabled: listActionsEnabled,
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
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));

  await waitFor(() =>
    expect(within(row).queryByTestId(`loss-cut-confirm-${ID}`)).toBeNull(),
  );
  expect(within(row).queryByTestId(`approve-${ID}`)).toBeNull();
  expect(within(row).queryByTestId(`deny-${ID}`)).toBeNull();
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
});
