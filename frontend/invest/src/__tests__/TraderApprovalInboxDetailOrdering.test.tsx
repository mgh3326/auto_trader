// Task 890 PR A round 4 finding B1: detail reads for one row can overlap
// (the post-action read, the list reload's re-read and the row state refresh).
// Only the latest read's outcome -- success or failure -- may be applied, so a
// slow older "still confirming" response can never restore a withdrawn
// second-click button. Every detail GET here is held and released in a chosen
// order. Detail shapes mirror the real core (see
// TraderApprovalInboxLossCutConfirm.test.tsx).
//
// The first click's own post-action read cannot be the older read: no other
// detail read can start while it is in flight (the row has no token yet and
// its state-refresh button appears only after the action completes).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApprovalInboxPanel } from "../trader/ApprovalInboxPanel";
import type { TraderApprovalDetailResponse, TraderApprovalItem } from "../trader/types";

const ID = "11111111-2222-3333-4444-555555555555";
const NOW = new Date("2026-09-30T01:00:00Z");

type Shape = "confirming" | "denied" | "finished";
type Outcome = Shape | "missing" | "network";
interface HeldRead {
  resolve: (res: Response) => void;
  reject: (err: unknown) => void;
}

const posts: string[] = [];
let listed = true;
let holdReads = false;
let held: HeldRead[] = [];
let confirmReason = "approved";

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
    valid_until: "2026-09-30T02:00:00Z",
    expires_in_seconds: 3600,
    approved_at: null,
    approved_by_channel: null,
    commit_lease_active: false,
    actionable: true,
    block_reason: null,
  };
}

function detail(shape: Shape): TraderApprovalDetailResponse {
  const confirming: TraderApprovalItem = {
    ...lossCutItem(),
    card_kind: "loss_cut_confirmation",
    actionable: false,
    block_reason: "not_human_card",
  };
  const approved = { approved_at: "2026-09-30T01:00:01Z", approved_by_channel: "web" };
  const items: Record<Shape, TraderApprovalItem> = {
    confirming,
    denied: {
      ...confirming,
      ...approved,
      lifecycle_state: "rejected",
      block_reason: "terminal",
      rungs: confirming.rungs.map((r) => ({ ...r, state: "rejected" })),
    },
    finished: {
      ...confirming,
      ...approved,
      commit_lease_active: true,
      rungs: confirming.rungs.map((r) => ({ ...r, state: "acked", broker_order_id: "B-1" })),
    },
  };
  return {
    as_of: NOW.toISOString(),
    actions_enabled: true,
    loss_cut_actions_enabled: true,
    item: items[shape],
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW);
  posts.length = 0;
  listed = true;
  holdReads = false;
  held = [];
  confirmReason = "approved";
  document.cookie = "csrftoken=test-csrf";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "POST") {
        posts.push(url);
        if (url.endsWith("/approve")) {
          return json({
            handled: true,
            reason: "loss_cut_confirmation_required",
            proposal_id: ID,
            confirmation_token: "test-confirmation-token",
          });
        }
        return json({ handled: true, reason: confirmReason, proposal_id: ID });
      }
      if (url.endsWith(`/approvals/${ID}`)) {
        if (!holdReads) return json(detail("confirming"));
        return await new Promise<Response>((resolve, reject) => {
          held.push({ resolve, reject });
        });
      }
      // After the first click the real list no longer lists the row.
      return json({
        as_of: NOW.toISOString(),
        actions_enabled: true,
        loss_cut_actions_enabled: true,
        count: listed ? 1 : 0,
        items: listed ? [lossCutItem()] : [],
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
  await waitFor(() => expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled());
  expect(posts).toHaveLength(1);
  listed = false;
  holdReads = true;
  return row;
}

type Source = "reload" | "state";

async function startRead(row: HTMLElement, source: Source): Promise<HeldRead> {
  const before = held.length;
  if (source === "reload") {
    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
  } else {
    fireEvent.click(within(row).getByRole("button", { name: "상태 새로고침" }));
  }
  await waitFor(() => expect(held).toHaveLength(before + 1));
  return held[before]!;
}

async function settle(read: HeldRead, outcome: Outcome) {
  await act(async () => {
    if (outcome === "network") read.reject(new TypeError("network down"));
    else if (outcome === "missing") read.resolve(json({ detail: "proposal_not_found" }, 404));
    else read.resolve(json(detail(outcome)));
  });
}

function controls(row: HTMLElement): string[] {
  return ["approve", "deny", "loss-cut-confirm"].filter(
    (kind) => within(row).queryByTestId(`${kind}-${ID}`) !== null,
  );
}

const SOURCES: Source[] = ["reload", "state"];
const WITHDRAWALS = ["denied", "missing", "network"] as const;

test.each(
  SOURCES.flatMap((older) =>
    SOURCES.flatMap((newer) => WITHDRAWALS.map((outcome) => [older, newer, outcome] as const)),
  ),
)(
  "an older %s read answering 'confirming' after a newer %s read answered %s keeps the button withdrawn",
  async (older, newer, outcome) => {
    const row = await firstClick();

    const oldRead = await startRead(row, older);
    const newRead = await startRead(row, newer);
    await settle(newRead, outcome);
    await waitFor(() => expect(controls(row)).toEqual([]));

    await settle(oldRead, "confirming");

    expect(controls(row)).toEqual([]);
    expect(posts).toHaveLength(1);
  },
);

// Positive controls: the newest read still answers "confirming", so an older
// withdrawal or failure landing afterwards must not remove the button either.
test.each(
  SOURCES.flatMap((older) =>
    SOURCES.flatMap((newer) => WITHDRAWALS.map((outcome) => [older, newer, outcome] as const)),
  ),
)(
  "an older %s read answering %s after a newer %s 'confirming' read keeps the button",
  async (older, newer, outcome) => {
    const row = await firstClick();

    const oldRead = await startRead(row, older);
    const newRead = await startRead(row, newer);
    await settle(newRead, "confirming");
    await settle(oldRead, outcome);

    expect(within(row).getByTestId(`loss-cut-confirm-${ID}`)).toBeEnabled();
  },
);

// The confirm click's post-action read is newer than any read started before
// the click; an older read landing afterwards must not replace its result.
test.each(SOURCES)(
  "an older %s read landing after the confirm click's post-action read does not replace it",
  async (older) => {
    const row = await firstClick();
    const oldRead = await startRead(row, older);

    await act(async () => {
      fireEvent.click(within(row).getByTestId(`loss-cut-confirm-${ID}`));
    });
    await waitFor(() => expect(held).toHaveLength(2));
    await settle(held[1]!, "finished");
    await waitFor(() =>
      expect(within(row).getByTestId(`broker-state-${ID}-0`)).toHaveAttribute("data-state", "acked"),
    );

    await settle(oldRead, "confirming");

    expect(within(row).getByTestId(`broker-state-${ID}-0`)).toHaveAttribute("data-state", "acked");
    expect(controls(row)).toEqual([]);
    expect(posts).toHaveLength(2);
  },
);

test.each(SOURCES)(
  "an older %s read landing after the confirm click's failed post-action read does not restore detail",
  async (older) => {
    confirmReason = "loss_cut_confirmation_expired";
    const row = await firstClick();
    const oldRead = await startRead(row, older);

    await act(async () => {
      fireEvent.click(within(row).getByTestId(`loss-cut-confirm-${ID}`));
    });
    await waitFor(() => expect(held).toHaveLength(2));
    await settle(held[1]!, "network");
    await waitFor(() =>
      expect(within(row).getByTestId(`approval-result-${ID}`)).toHaveAttribute(
        "data-reason",
        "loss_cut_confirmation_expired",
      ),
    );
    expect(within(row).queryByTestId(`broker-state-${ID}-0`)).toBeNull();

    await settle(oldRead, "confirming");

    expect(within(row).queryByTestId(`broker-state-${ID}-0`)).toBeNull();
    expect(controls(row)).toEqual([]);
    expect(posts).toHaveLength(2);
  },
);

// The confirm click's post-action read is itself the older read when a list
// reload or state refresh starts while it is in flight: its late outcome must
// not replace the newer detail.
test.each(
  SOURCES.flatMap((newer) => (["confirming", "network"] as const).map((late) => [newer, late] as const)),
)(
  "the confirm click's post-action read answering late does not replace a newer %s read (%s)",
  async (newer, late) => {
    const row = await firstClick();

    await act(async () => {
      fireEvent.click(within(row).getByTestId(`loss-cut-confirm-${ID}`));
    });
    await waitFor(() => expect(held).toHaveLength(1));
    const actionRead = held[0]!;
    const newRead = await startRead(row, newer);
    await settle(newRead, "finished");
    await waitFor(() =>
      expect(within(row).getByTestId(`broker-state-${ID}-0`)).toHaveAttribute("data-state", "acked"),
    );

    await settle(actionRead, late);

    await waitFor(() =>
      expect(within(row).getByTestId(`approval-result-${ID}`)).toHaveAttribute("data-reason", "approved"),
    );
    expect(within(row).getByTestId(`broker-state-${ID}-0`)).toHaveAttribute("data-state", "acked");
    expect(controls(row)).toEqual([]);
    expect(posts).toHaveLength(2);
  },
);
