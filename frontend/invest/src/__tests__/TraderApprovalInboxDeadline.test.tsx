// Task 890 PR A round 5 finding: a list response that arrives late restarted
// the row countdown. The deadline model anchors every relative expiry (list
// rows and detail reads) to the moment its request was SENT, keeps one
// deadline per row as the minimum of all observations (never extended, and a
// locally expired row stays expired) and fences list responses so an older
// list never replaces a newer one. List, detail and POST responses are held
// and released in a chosen order.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
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
  listResponse = () => listBody([item(2)]);
  detailExpiresIn = 2;
  document.cookie = "csrftoken=test-csrf";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const kind: Kind = method === "POST" ? "post" : url.endsWith(`/approvals/${ID}`) ? "detail" : "list";
      // The body is computed when the request is sent, like a server snapshot.
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

async function release(h: Held) {
  await act(async () => {
    h.release();
  });
}

async function startHeldReload(): Promise<Held> {
  hold.list = true;
  const before = held.length;
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
  await waitFor(() => expect(held).toHaveLength(before + 1));
  hold.list = false;
  return held[before]!;
}

test("a list snapshot sent before the first click but delivered after expiry does not restore the confirm button", async () => {
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);
  const staleList = await startHeldReload();

  await act(async () => {
    fireEvent.click(within(row).getByTestId(`approve-${ID}`));
  });
  await waitFor(() => expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled());

  await advance(2000);
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");

  hold.detail = true; // the reload's detail re-read stays in flight
  await release(staleList);

  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
  expect(posts).toHaveLength(1);
});

test("a stale list delivered while the first POST is pending across expiry does not re-render approve or deny", async () => {
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);
  const staleList = await startHeldReload();

  hold.post = true;
  await act(async () => {
    fireEvent.click(within(row).getByTestId(`approve-${ID}`));
  });
  await waitFor(() => expect(held.some((h) => h.kind === "post")).toBe(true));

  await advance(2000);
  expect(controls(row)).toEqual([]);

  await release(staleList);

  expect(controls(row)).toEqual([]);
  expect(posts).toHaveLength(1);
});

test("a detail read's expiry is anchored to when it was sent, not when it arrived", async () => {
  listResponse = () => listBody([item(100)]);
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);
  detailExpiresIn = 100;
  await act(async () => {
    fireEvent.click(within(row).getByTestId(`approve-${ID}`));
  });
  await waitFor(() => expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled());

  // Sent at T0+10s, the server says 5 s remain; the response lands 4 s later.
  await advance(10_000);
  hold.detail = true;
  detailExpiresIn = 5;
  fireEvent.click(within(row).getByRole("button", { name: "상태 새로고침" }));
  await waitFor(() => expect(held).toHaveLength(1));
  await advance(4000);
  await release(held[0]!);
  expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled();

  // True deadline is T0+15s (sent time + 5 s), not T0+19s (arrival + 5 s).
  await advance(1000);
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
});

test("a later response reporting more time left never extends a row's deadline", async () => {
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  await advance(1000);
  listResponse = () => listBody([item(60)]);
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
  await waitFor(() => expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("0:01"));

  await advance(1000);
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
});

test("a row that expired locally stays expired even if the clock moves back", async () => {
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  await advance(2000);
  expect(controls(row)).toEqual([]);

  await act(async () => {
    vi.setSystemTime(T0);
    vi.advanceTimersByTime(1000);
  });
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
});

test("a list row without an expiry is treated as already expired", async () => {
  listResponse = () => listBody([item(null)]);
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
});

// Overlapping list reads happen when the mount effect runs twice
// (React StrictMode): the older list must not replace the newer one.
test("an older list response landing after a newer one does not replace it", async () => {
  hold.list = true;
  let n = 0;
  const OTHER = "99999999-8888-7777-6666-555555555555";
  listResponse = () => {
    n += 1;
    return n === 1
      ? listBody([item(100)])
      : listBody([item(100), item(100, { proposal_id: OTHER, symbol: "MSFT" })]);
  };
  render(
    <StrictMode>
      <ApprovalInboxPanel />
    </StrictMode>,
  );
  await waitFor(() => expect(held.filter((h) => h.kind === "list")).toHaveLength(2));
  const [older, newer] = held.filter((h) => h.kind === "list");

  await release(newer!);
  await screen.findByTestId(`approval-row-${OTHER}`);
  await release(older!);

  expect(screen.getByTestId(`approval-row-${OTHER}`)).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "승인 대기 (2)" })).toBeInTheDocument();
});

test("the first list delivered late counts its expiry from when it was sent", async () => {
  hold.list = true;
  render(<ApprovalInboxPanel />);
  await waitFor(() => expect(held).toHaveLength(1));
  hold.list = false;

  // Sent at T0 with 2 s left, delivered 1.5 s later.
  await advance(1500);
  await release(held[0]!);
  const row = await screen.findByTestId(`approval-row-${ID}`);
  expect(controls(row)).toEqual(["approve", "deny"]);

  await advance(500);
  expect(controls(row)).toEqual([]);
  expect(within(row).getByTestId(`expires-${ID}`)).toHaveTextContent("만료");
});

test("a click's post-action detail read counts its expiry from when it was sent", async () => {
  listResponse = () => listBody([item(100)]);
  render(<ApprovalInboxPanel />);
  const row = await screen.findByTestId(`approval-row-${ID}`);

  // The first click's own detail read is sent at T0 (server: 5 s left) and lands 4 s later.
  hold.detail = true;
  detailExpiresIn = 5;
  await act(async () => {
    fireEvent.click(within(row).getByTestId(`approve-${ID}`));
  });
  await waitFor(() => expect(held).toHaveLength(1));
  await advance(4000);
  await release(held[0]!);
  expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled();

  await advance(1000);
  expect(controls(row)).toEqual([]);
});
