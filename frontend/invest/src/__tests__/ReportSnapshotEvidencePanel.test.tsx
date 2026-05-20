// ROB-275 — ReportSnapshotEvidencePanel tests.
//
// Critical regression guard: mounting the panel must NOT trigger any
// payload-detail fetch. Only the bundle-list endpoint is fetched eagerly;
// detail fetches happen after a row click.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ReportSnapshotEvidencePanel } from "../components/investment-reports/ReportSnapshotEvidencePanel";

const originalFetch = global.fetch;

interface FetchResponseInit {
  status?: number;
  ok?: boolean;
  json: () => Promise<unknown>;
}

function makeResponse(payload: unknown, status: number = 200): FetchResponseInit {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
  };
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
});

function mockBundleAndDetail() {
  const bundleResponse = {
    bundle: {
      bundle_uuid: "bundle-1",
      purpose: "rob275_smoke",
      market: "kr",
      account_scope: "kis_live",
      policy_version: "intraday_action_report_v1",
      status: "partial",
      as_of: "2026-05-20T11:00:00Z",
      coverage_summary: {},
      freshness_summary: { overall: "partial" },
      created_at: "2026-05-20T11:00:00Z",
    },
    items: [
      {
        snapshot_uuid: "snap-required",
        role: "required",
        snapshot_kind: "portfolio",
        source_kind: "manual",
        market: "kr",
        symbol: null,
        account_scope: "kis_live",
        freshness_status: "fresh",
        as_of: "2026-05-20T11:00:00Z",
        valid_until: null,
        source_table: null,
        source_id: null,
        source_uri: null,
        payload_size_bytes: 128,
      },
      {
        snapshot_uuid: "snap-optional",
        role: "optional",
        snapshot_kind: "market",
        source_kind: "domain_ref",
        market: "kr",
        symbol: null,
        account_scope: null,
        freshness_status: "soft_stale",
        as_of: "2026-05-20T10:00:00Z",
        valid_until: null,
        source_table: "market_quote_snapshots",
        source_id: 42,
        source_uri: "market_quote_snapshots:abc",
        payload_size_bytes: 4096,
      },
    ],
    unavailable_sources: { naver_remote_debug: "blocked" },
    source_conflicts: null,
    legacy_no_snapshot: false,
  };
  const detailResponse = {
    snapshot_uuid: "snap-required",
    role: "required",
    snapshot_kind: "portfolio",
    source_kind: "manual",
    market: "kr",
    symbol: null,
    account_scope: "kis_live",
    source_table: null,
    source_id: null,
    source_uri: null,
    freshness_status: "fresh",
    as_of: "2026-05-20T11:00:00Z",
    valid_until: null,
    source_timestamps_json: {},
    coverage_json: {},
    errors_json: {},
    payload_json: { cash_krw: 1_000_000 },
  };
  (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(
    (url: RequestInfo | URL) => {
      const u = typeof url === "string" ? url : url.toString();
      if (u.includes("/snapshot-bundle")) {
        return Promise.resolve(makeResponse(bundleResponse));
      }
      if (u.includes("/snapshots/")) {
        return Promise.resolve(makeResponse(detailResponse));
      }
      return Promise.resolve(makeResponse({}, 404));
    },
  );
}

describe("ReportSnapshotEvidencePanel", () => {
  it("mounts without triggering any snapshot detail fetch", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("snapshot-evidence-panel")).toBeInTheDocument(),
    );

    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    const detailCalls = fetchMock.mock.calls.filter((args) => {
      const u = typeof args[0] === "string" ? args[0] : String(args[0]);
      return u.includes("/snapshots/");
    });
    expect(detailCalls).toHaveLength(0);
  });

  it("groups items by role and renders unavailable_sources in a separate section", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-evidence-role-required"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId("snapshot-evidence-role-optional"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("snapshot-evidence-role-fallback"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("snapshot-evidence-unavailable-sources"),
    ).toBeInTheDocument();
    // source_conflicts was null → section is not rendered.
    expect(
      screen.queryByTestId("snapshot-evidence-source-conflicts"),
    ).not.toBeInTheDocument();
  });

  it("fetches the detail payload on row click and renders the drawer", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    const row = await screen.findByTestId(
      "snapshot-evidence-row-snap-required",
    );
    await act(async () => {
      fireEvent.click(row);
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-payload-drawer"),
      ).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-payload-json"),
      ).toHaveTextContent(/cash_krw/),
    );
  });

  it("renders a legacy message when the report has no snapshot bundle", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      makeResponse({
        bundle: null,
        items: [],
        unavailable_sources: null,
        source_conflicts: null,
        legacy_no_snapshot: true,
      }),
    );

    render(<ReportSnapshotEvidencePanel reportUuid="legacy-uuid" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-evidence-panel-legacy"),
      ).toBeInTheDocument(),
    );
    // No payload fetch on legacy reports either.
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    const detailCalls = fetchMock.mock.calls.filter((args) => {
      const u = typeof args[0] === "string" ? args[0] : String(args[0]);
      return u.includes("/snapshots/");
    });
    expect(detailCalls).toHaveLength(0);
  });
});
