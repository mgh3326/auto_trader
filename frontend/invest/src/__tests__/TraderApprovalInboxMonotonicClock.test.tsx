// Task 1232 (#890 PR A follow-up): the deadline model measures elapsed time
// on a monotonic clock, not the device wall clock. A backwards wall-clock
// correction while a row is unexpired must not keep approve/deny/confirm
// enabled at the true deadline, and a forwards jump must not expire a row
// early. The clock is injectable: tests substitute a controllable source.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApprovalInboxPanel } from "../trader/ApprovalInboxPanel";
import type { TraderApprovalItem } from "../trader/types";

const ID = "11111111-2222-3333-4444-555555555555";
const T0 = new Date("2026-09-30T01:00:00Z").getTime();

type Kind = "list" | "detail" | "post";
interface Held {
  kind: Kind;
  url: string;
  release: () => void;
}

const posts: string[] = [];
const held: Held[] = [];
const hold: Record<Kind, boolean> = { list: false, detail: false, post: false };

function item(expiresIn: number | null, overrides: Partial<TraderApprovalItem> = {}): TraderApprovalItem {
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
    valid_until: null,
    expires_in_seconds: expiresIn,
    approved_at: null,
    approved_by_channel: null,
    commit_lease_active: false,
    actionable: true,
    block_reason: null,
    ...overrides,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function listBody(items: TraderApprovalItem[]) {
  return { as_of: new Date(T0).toISOString(), actions_enabled: true, loss_cut_actions_enabled: true, count: items.length, items };
}

/** The real core's detail after a web first click (confirmation step). */
function confirmingDetail(expiresIn: number) {
  return {
    as_of: new Date(T0).toISOString(),
    actions_enabled: true,
    loss_cut_actions_enabled: true,
    item: item(expiresIn, { card_kind: "loss_cut_confirmation", actionable: false, block_reason: "not_human_card" }),
  };
}

let listResponse: () => unknown;
let detailExpiresIn: number;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(T0);
  posts.length = 0;
  held.length = 0;
  hold.list = hold.detail = hold.post = false;
  listResponse = () => listBody([item(5)]);
  detailExpiresIn = 5;
  document.cookie = "csrftoken=test-csrf";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const kind: Kind = method === "POST" ? "post" : url.endsWith(`/approvals/${ID}`) ? "detail" : "list";
      let body: Response;
      if (kind === "post") {
        posts.push(url);
        body = url.endsWith("/approve")
          ? json({ handled: true, reason: "loss_cut_confirmation_required", proposal_id: ID, confirmation_token: "tok" })
          : json({ handled: false, reason: "EXPIRED", proposal_id: ID });
      } else if (kind === "detail") {
        body = json(confirmingDetail(detailExpiresIn));
      } else {
        body = json(listResponse());
      }
      if (!hold[kind]) return body;
      return await new Promise<Response>((resolve) => {
        held.push({ kind, url, release: () => resolve(body) });
      });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function controls(row: HTMLElement): string[] {
  return ["approve", "deny", "loss-cut-confirm"].filter(
    (kind) => within(row).queryByTestId(`${kind}-${ID}`) !== null,
  );
}

async function advance(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

/** A device wall-clock correction: moves the fake Date, not elapsed time. */
async function moveWall(delta: number) {
  await act(async () => {
    vi.setSystemTime(Date.now() + delta);
  });
}

test.each(["base", "confirmation", "pending POST"] as const)(
  "a backwards wall-clock correction before expiry cannot expose %s controls at the true deadline",
  async (stage) => {
    if (stage === "pending POST") hold.post = true;
    render(<ApprovalInboxPanel />);
    const row = await screen.findByTestId(`approval-row-${ID}`);
    if (stage !== "base") {
      await act(async () => {
        fireEvent.click(within(row).getByTestId(`approve-${ID}`));
      });
      if (stage === "confirmation") {
        await waitFor(() => expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled());
      }
    }
    const postsBefore = posts.length;

    await advance(2000);
    expect(controls(row).length).toBeGreaterThan(0);
    await moveWall(-500); // The device clock jumps back; elapsed time does not.
    await advance(3000);

    // The monotonic clock reached the true deadline (5 s elapsed) regardless
    // of the wall clock reading 4.5 s.
    expect(controls(row)).toEqual([]);
    expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
    expect(posts).toHaveLength(postsBefore);
  },
);

test("a forwards wall-clock jump does not expire a row early", async () => {
  listResponse = () => listBody([item(60)]);
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  await advance(1000);
  await moveWall(60_000); // Device clock jumps an hour past the deadline.
  await advance(1000);

  expect(controls(row)).toEqual(["approve", "deny"]);
  expect(within(row).getByTestId(`expires-${ID}`)).not.toHaveTextContent("만료");
});

test("the injected clock drives the deadline model instead of the interval tick", async () => {
  let mono = 0;
  render(<ApprovalInboxPanel clock={() => mono} />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  // Sixty seconds of ticks with a frozen injected clock: nothing expires.
  await advance(60_000);
  expect(controls(row)).toEqual(["approve", "deny"]);

  // The injected source reaching the deadline expires the row on the next tick.
  mono = 5000;
  await advance(1000);
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
  expect(posts).toHaveLength(0);
});
