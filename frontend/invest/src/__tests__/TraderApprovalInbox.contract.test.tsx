// Task 890 PR A -- FROZEN CONTRACT (builder-owned). Implementers must make
// these pass WITHOUT editing this file. It pins which endpoint every /trader
// button calls, the loss-cut two-click, when buttons must not exist, and the
// protected-quantity form's use of the existing PUT path.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { TraderPage } from "../trader/TraderPage";
import type {
  TraderApprovalDetailResponse,
  TraderApprovalInboxResponse,
  TraderApprovalItem,
} from "../trader/types";

type Call = { url: string; method: string; headers: Record<string, string>; body: unknown };

const fetchMock = vi.fn();
const calls: Call[] = [];
let routes: Array<[(url: string, method: string) => boolean, (body: unknown) => Response | Promise<Response>]> = [];

const ID = "11111111-2222-3333-4444-555555555555";
const LC = "99999999-8888-7777-6666-555555555555";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function item(overrides: Partial<TraderApprovalItem> = {}): TraderApprovalItem {
  return {
    proposal_id: ID,
    symbol: "005930",
    market: "equity_kr",
    account_mode: "kis_live",
    broker_account_id: null,
    side: "buy",
    order_type: "limit",
    action: "place",
    exit_intent: null,
    requires_two_step: false,
    card_kind: "manual",
    lifecycle_state: "proposed",
    rungs: [
      {
        rung_index: 0,
        side: "buy",
        quantity: "3",
        limit_price: "65000",
        notional: "195000",
        state: "pending_approval",
        broker_order_id: null,
        filled_qty: null,
        void_reason: null,
        distance_pct: "-4.41",
      },
    ],
    total_quantity: "3",
    total_notional: "195000",
    distance_pct: "-4.41",
    distance_price_asof: "2026-09-30T00:59:00+00:00",
    tier: "support_reserve_net",
    caveats: ["auto:per_order_cap_exceeded"],
    valid_until: "2026-09-30T02:00:00+00:00",
    expires_in_seconds: 3600,
    approved_at: null,
    approved_by_channel: null,
    commit_lease_active: false,
    actionable: true,
    block_reason: null,
    ...overrides,
  };
}

function inbox(
  items: TraderApprovalItem[],
  flags: Partial<Pick<TraderApprovalInboxResponse, "actions_enabled" | "loss_cut_actions_enabled">> = {},
): TraderApprovalInboxResponse {
  return {
    as_of: "2026-09-30T01:00:00+00:00",
    actions_enabled: true,
    loss_cut_actions_enabled: true,
    count: items.length,
    items,
    ...flags,
  };
}

const protectedPositions = {
  can_edit: true,
  positions: [
    {
      account_scope: "kis_live",
      market: "kr",
      symbol: "005930",
      name: "삼성전자",
      protected_quantity: "1",
      broker_held: "12",
      broker_sellable: "11",
      headroom: "10",
      state: "covered",
      mode: "shadow",
      broker_observed_at: "2026-09-30T00:58:00+00:00",
      read_error: null,
      revision: 4,
      latest_revision: null,
      history_url: "/invest/api/settings/protected-positions/kis_live/kr/005930/history",
    },
  ],
};

function route(
  match: (url: string, method: string) => boolean,
  respond: (body: unknown) => Response | Promise<Response>,
) {
  routes.unshift([match, respond]);
}

function serveInbox(body: TraderApprovalInboxResponse) {
  route((u, m) => m === "GET" && u === "/trading/api/trader/approvals", () => jsonResponse(body));
}

function nth<T>(values: T[], index: number): T {
  const value = values[index];
  if (value === undefined) throw new Error(`missing element ${index}`);
  return value;
}

function mutations(): Call[] {
  return calls.filter((c) => c.method !== "GET");
}

beforeEach(() => {
  calls.length = 0;
  routes = [];
  document.cookie = "csrftoken=csrf-abc";
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body ?? null;
    calls.push({ url, method, headers, body });
    for (const [match, respond] of routes) if (match(url, method)) return respond(body);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  route((u, m) => m === "GET" && u === "/invest/api/settings/protected-positions", () => jsonResponse(protectedPositions));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function row(id = ID) {
  const panel = await screen.findByTestId("panel-approval-inbox");
  return within(panel).findByTestId(`approval-row-${id}`);
}

// ---------------------------------------------------------------- list ----

test("inbox row shows symbol, account, qty, price, distance, tier, caveats, time left", async () => {
  serveInbox(inbox([item()]));
  render(<TraderPage />);
  const r = await row();
  expect(r).toHaveTextContent("005930");
  expect(r).toHaveTextContent("kis_live");
  expect(r).toHaveTextContent("65,000");
  expect(r).toHaveTextContent("-4.41%");
  expect(r).toHaveTextContent("support_reserve_net");
  expect(r).toHaveTextContent("per_order_cap_exceeded");
  expect(within(r).getByTestId(`expires-${ID}`)).toHaveTextContent(/\d/);
  const qty = within(r).getByTestId(`qty-${ID}`);
  expect(qty).toHaveTextContent("3");
});

test("list GET is the /trading/api/trader/approvals read", async () => {
  serveInbox(inbox([]));
  render(<TraderPage />);
  await screen.findByTestId("panel-approval-inbox");
  await waitFor(() =>
    expect(calls.some((c) => c.method === "GET" && c.url === "/trading/api/trader/approvals")).toBe(true),
  );
});

// ----------------------------------------------------- button endpoints ----

test("approve posts once to the existing /invest approve endpoint with CSRF", async () => {
  serveInbox(inbox([item()]));
  let release!: (r: Response) => void;
  route(
    (u, m) => m === "POST" && u === `/invest/api/approvals/${ID}/approve`,
    () => new Promise<Response>((resolve) => { release = resolve; }),
  );
  route(
    (u, m) => m === "GET" && u === `/trading/api/trader/approvals/${ID}`,
    () => jsonResponse(detail({ state: "resting", broker_order_id: "B-1" })),
  );
  const user = userEvent.setup();
  render(<TraderPage />);
  const r = await row();
  const approve = within(r).getByTestId(`approve-${ID}`);
  await user.click(approve);
  // In flight: a second click must not send a second request.
  await user.click(approve).catch(() => undefined);
  await waitFor(() => expect(mutations()).toHaveLength(1));
  const call = nth(mutations(), 0);
  expect(call.url).toBe(`/invest/api/approvals/${ID}/approve`);
  expect(call.method).toBe("POST");
  expect(call.headers["x-csrftoken"]).toBe("csrf-abc");
  expect(call.headers["idempotency-key"]).toBeTruthy();
  release(jsonResponse({ handled: true, reason: "approved", proposal_id: ID, results: ["submitted_resting"] }));

  // Result and broker acceptance state land on the same row.
  const result = await within(r).findByTestId(`approval-result-${ID}`);
  expect(result).toHaveAttribute("data-reason", "approved");
  const rung = await within(r).findByTestId(`broker-state-${ID}-0`);
  expect(rung).toHaveAttribute("data-state", "resting");
  expect(rung).toHaveTextContent("B-1");
  expect(mutations()).toHaveLength(1);
});

test("reject posts to the existing /invest deny endpoint", async () => {
  serveInbox(inbox([item()]));
  route(
    (u, m) => m === "POST" && u === `/invest/api/approvals/${ID}/deny`,
    () => jsonResponse({ handled: true, reason: "denied", proposal_id: ID, rejected_rungs: [0] }),
  );
  route(
    (u, m) => m === "GET" && u === `/trading/api/trader/approvals/${ID}`,
    () => jsonResponse(detail({ state: "rejected" })),
  );
  const user = userEvent.setup();
  render(<TraderPage />);
  const r = await row();
  await user.click(within(r).getByTestId(`deny-${ID}`));
  await waitFor(() => expect(mutations()).toHaveLength(1));
  expect(nth(mutations(), 0).url).toBe(`/invest/api/approvals/${ID}/deny`);
  expect(nth(mutations(), 0).headers["x-csrftoken"]).toBe("csrf-abc");
  const result = await within(r).findByTestId(`approval-result-${ID}`);
  expect(result).toHaveAttribute("data-reason", "denied");
});

test("a 409 processing answer is shown and never auto-retried", async () => {
  serveInbox(inbox([item()]));
  route(
    (u, m) => m === "POST" && u === `/invest/api/approvals/${ID}/approve`,
    () => jsonResponse({ detail: { error: "processing" } }, 409),
  );
  const user = userEvent.setup();
  render(<TraderPage />);
  const r = await row();
  await user.click(within(r).getByTestId(`approve-${ID}`));
  const result = await within(r).findByTestId(`approval-result-${ID}`);
  expect(result).toHaveAttribute("data-reason", "processing");
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(mutations()).toHaveLength(1);
});

// ------------------------------------------------------- loss cut 2-click ----

test("loss_cut: first click only asks; the token-bound second click confirms", async () => {
  serveInbox(
    inbox([
      item({
        proposal_id: LC,
        side: "sell",
        exit_intent: "loss_cut",
        requires_two_step: true,
        caveats: ["loss_cut_two_step"],
      }),
    ]),
  );
  route(
    (u, m) => m === "POST" && u === `/invest/api/approvals/${LC}/approve`,
    () => jsonResponse({ handled: true, reason: "loss_cut_confirmation_required", proposal_id: LC, confirmation_token: "tok-123" }),
  );
  route(
    (u, m) => m === "POST" && u === `/invest/api/approvals/${LC}/loss-cut-confirm`,
    () => jsonResponse({ handled: true, reason: "approved", proposal_id: LC, results: ["submitted_acked"] }),
  );
  route(
    (u, m) => m === "GET" && u === `/trading/api/trader/approvals/${LC}`,
    () => jsonResponse(detail({ state: "acked", broker_order_id: "T-9" }, LC)),
  );
  const user = userEvent.setup();
  render(<TraderPage />);
  const r = await row(LC);

  expect(within(r).queryByTestId(`loss-cut-confirm-${LC}`)).toBeNull();
  await user.click(within(r).getByTestId(`approve-${LC}`));
  const confirm = await within(r).findByTestId(`loss-cut-confirm-${LC}`);
  expect(mutations().map((c) => c.url)).toEqual([`/invest/api/approvals/${LC}/approve`]);

  await user.click(confirm);
  await waitFor(() => expect(mutations()).toHaveLength(2));
  const second = nth(mutations(), 1);
  expect(second.url).toBe(`/invest/api/approvals/${LC}/loss-cut-confirm`);
  expect(second.body).toEqual({ confirmation_token: "tok-123" });
  expect(second.headers["x-csrftoken"]).toBe("csrf-abc");
  const rung = await within(r).findByTestId(`broker-state-${LC}-0`);
  expect(rung).toHaveAttribute("data-state", "acked");
});

test("loss_cut approve is hidden while the loss-cut gate is off", async () => {
  serveInbox(
    inbox([item({ proposal_id: LC, exit_intent: "loss_cut", requires_two_step: true })], {
      loss_cut_actions_enabled: false,
    }),
  );
  render(<TraderPage />);
  const r = await row(LC);
  expect(within(r).queryByTestId(`approve-${LC}`)).toBeNull();
  expect(within(r).queryByTestId(`loss-cut-confirm-${LC}`)).toBeNull();
});

// ---------------------------------------------- items that get no buttons ----

test("no buttons when web approvals are disabled", async () => {
  serveInbox(inbox([item()], { actions_enabled: false, loss_cut_actions_enabled: false }));
  render(<TraderPage />);
  const r = await row();
  expect(within(r).queryByTestId(`approve-${ID}`)).toBeNull();
  expect(within(r).queryByTestId(`deny-${ID}`)).toBeNull();
});

test("no buttons for a non-actionable or already-expired item", async () => {
  const other = "abababab-abab-abab-abab-abababababab";
  serveInbox(
    inbox([
      item({ actionable: false, block_reason: "auto_approved" }),
      item({ proposal_id: other, expires_in_seconds: 0 }),
    ]),
  );
  render(<TraderPage />);
  const first = await row();
  const second = await row(other);
  for (const [r, id] of [[first, ID], [second, other]] as const) {
    expect(within(r).queryByTestId(`approve-${id}`)).toBeNull();
    expect(within(r).queryByTestId(`deny-${id}`)).toBeNull();
  }
});

test("buttons disappear when the countdown reaches zero", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-09-30T01:00:00Z"));
  serveInbox(inbox([item({ expires_in_seconds: 2 })]));
  render(<TraderPage />);
  const r = await row();
  expect(within(r).getByTestId(`approve-${ID}`)).toBeInTheDocument();
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  expect(within(r).queryByTestId(`approve-${ID}`)).toBeNull();
  expect(within(r).queryByTestId(`deny-${ID}`)).toBeNull();
});

// ------------------------------------------------ protected quantity form ----

test("protected form shows P/H/S/mode/revision and saves only through the existing PUT", async () => {
  serveInbox(inbox([]));
  const puts: unknown[] = [];
  route(
    (u, m) => m === "PUT" && u === "/invest/api/settings/protected-positions/kis_live/kr/005930",
    (body) => {
      puts.push(body);
      const b = body as { confirm_protection_change: boolean };
      if (!b.confirm_protection_change) {
        return jsonResponse(
          {
            detail: {
              error: "confirm_required",
              message: "protected quantity changes require explicit confirmation",
              preview: {
                account_scope: "kis_live",
                market: "kr",
                symbol: "005930",
                before_protected_quantity: "1",
                after_protected_quantity: "4",
                broker_held: "12",
                broker_sellable: "11",
                before_headroom: "10",
                headroom: "7",
                state: "covered",
                broker_observed_at: "2026-09-30T00:59:00+00:00",
              },
            },
          },
          409,
        );
      }
      return jsonResponse({
        position: { account_scope: "kis_live", market: "kr", symbol: "005930", protected_quantity: "4", revision: 5, action: "update" },
        idempotent_replay: false,
      });
    },
  );
  const user = userEvent.setup();
  render(<TraderPage />);
  const panel = await screen.findByTestId("panel-protected-quantity");

  await user.selectOptions(within(panel).getByTestId("pq-scope"), "kis_live");
  await user.selectOptions(within(panel).getByTestId("pq-market"), "kr");
  await user.type(within(panel).getByTestId("pq-symbol"), "005930");
  const current = await within(panel).findByTestId("pq-current");
  for (const text of ["1", "12", "11", "shadow", "4"]) expect(current).toHaveTextContent(text);

  await user.type(within(panel).getByTestId("pq-quantity"), "4");
  await user.type(within(panel).getByTestId("pq-reason"), "long-term core");
  await user.click(within(panel).getByTestId("pq-submit"));

  const confirm = await within(panel).findByTestId("pq-confirm");
  expect(puts).toHaveLength(1);
  expect(puts[0]).toMatchObject({
    protected_quantity: "4",
    reason: "long-term core",
    expected_revision: 4,
    confirm_protection_change: false,
  });
  expect(within(panel).getByTestId("pq-preview")).toHaveTextContent("7");

  await user.click(confirm);
  await waitFor(() => expect(puts).toHaveLength(2));
  expect(puts[1]).toMatchObject({ protected_quantity: "4", expected_revision: 4, confirm_protection_change: true });
  const writes = mutations();
  expect(writes.every((c) => c.method === "PUT" && c.url === "/invest/api/settings/protected-positions/kis_live/kr/005930")).toBe(true);
  expect(writes.every((c) => c.headers["x-csrftoken"] === "csrf-abc")).toBe(true);
});

test("protected form cannot submit when the session cannot edit", async () => {
  serveInbox(inbox([]));
  route(
    (u, m) => m === "GET" && u === "/invest/api/settings/protected-positions",
    () => jsonResponse({ ...protectedPositions, can_edit: false }),
  );
  render(<TraderPage />);
  const panel = await screen.findByTestId("panel-protected-quantity");
  await waitFor(() => expect(within(panel).getByTestId("pq-submit")).toBeDisabled());
});

// --------------------------------------------------------- static guard ----

const traderSources = import.meta.glob("../trader/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

test("trader sources never issue their own mutation requests", () => {
  const files = Object.entries(traderSources);
  expect(files.length).toBeGreaterThan(5);
  for (const [path, source] of files) {
    // All writes go through the two existing, CSRF-attaching clients.
    expect(source, path).not.toMatch(/method\s*:/);
    expect(source, path).not.toMatch(/\/invest\/api\/approvals/);
    expect(source, path).not.toMatch(/protected-positions\//);
    expect(source, path).not.toMatch(/XMLHttpRequest|sendBeacon|navigator\.sendBeacon/);
  }
  const all = files.map(([, source]) => source).join("\n");
  expect(all).toMatch(/mutateOrderProposalApproval/);
  expect(all).toMatch(/saveProtectedPosition/);
});

function detail(
  rung: { state: string; broker_order_id?: string | null },
  id = ID,
): TraderApprovalDetailResponse {
  const base = item({ proposal_id: id });
  return {
    as_of: "2026-09-30T01:00:05+00:00",
    actions_enabled: true,
    loss_cut_actions_enabled: true,
    item: {
      ...base,
      lifecycle_state: "submitted",
      approved_at: "2026-09-30T01:00:04+00:00",
      approved_by_channel: "web",
      actionable: false,
      block_reason: "nonce_used",
      rungs: [{ ...nth(base.rungs, 0), state: rung.state, broker_order_id: rung.broker_order_id ?? null }],
    },
  };
}
