import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api/analysisArtifacts";
import ItemArtifactLinks from "../components/investment-reports/ItemArtifactLinks";

afterEach(() => {
  vi.restoreAllMocks();
});

const meta = (over = {}) => ({
  id: 1,
  artifact_uuid: "u1",
  market: "us" as const,
  kind: "support_resistance_map" as const,
  title: "SR map",
  symbols: ["NVDA"],
  as_of: "2026-07-01T00:00:00Z",
  valid_until: null,
  session_label: null,
  correlation_id: "live:kis_live:xyz",
  account_scope: null,
  content_hash: null,
  version: 1,
  readiness_label: "ready_for_order_review" as const,
  is_stale: false,
  created_by: "claude" as const,
  created_at: "2026-07-01T00:00:00Z",
  ...over,
});

const renderC = (props: Parameters<typeof ItemArtifactLinks>[0]) =>
  render(
    <MemoryRouter>
      <ItemArtifactLinks {...props} />
    </MemoryRouter>,
  );

describe("ItemArtifactLinks", () => {
  it("fetches by correlationIds and labels '이 판단이 인용한 분석' when ids present", async () => {
    const spy = vi.spyOn(api, "fetchArtifacts").mockResolvedValue({
      success: true,
      count: 1,
      filters: {} as never,
      artifacts: [meta()],
    });
    renderC({
      symbol: "NVDA",
      market: "us",
      correlationIds: ["live:kis_live:xyz"],
    });
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({
          market: "us",
          correlationIds: ["live:kis_live:xyz"],
        }),
      ),
    );
    expect(screen.getByText(/이 판단이 인용한 분석/)).toBeInTheDocument();
    expect(screen.getByText("SR map")).toBeInTheDocument();
  });

  it("falls back to symbol fetch and labels '이 종목 최근' when no correlationIds", async () => {
    const spy = vi.spyOn(api, "fetchArtifacts").mockResolvedValue({
      success: true,
      count: 1,
      filters: {} as never,
      artifacts: [meta()],
    });
    renderC({ symbol: "NVDA", market: "us", correlationIds: [] });
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ market: "us", symbol: "NVDA" }),
      ),
    );
    expect(spy.mock.calls[0]?.[0]).not.toHaveProperty("correlationIds");
    expect(screen.getByText(/이 종목 최근/)).toBeInTheDocument();
  });

  it("shows empty state when fetch returns no artifacts", async () => {
    vi.spyOn(api, "fetchArtifacts").mockResolvedValue({
      success: true,
      count: 0,
      filters: {} as never,
      artifacts: [],
    });
    renderC({ symbol: "NVDA", market: "us", correlationIds: [] });
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(screen.getByText("관련 아티팩트 없음")).toBeInTheDocument(),
    );
  });

  it("renders nothing when there is neither a symbol nor correlationIds", () => {
    const { container } = renderC({
      symbol: null,
      market: "us",
      correlationIds: [],
    });
    expect(container).toBeEmptyDOMElement();
  });
});
