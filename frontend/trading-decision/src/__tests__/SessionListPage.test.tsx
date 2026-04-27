import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import SessionListPage from "../pages/SessionListPage";
import { makeSessionList } from "../test/fixtures";
import { mockFetch } from "../test/server";

describe("SessionListPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows empty state", async () => {
    mockFetch({
      "/trading/api/decisions": () =>
        new Response(JSON.stringify(makeSessionList({ sessions: [], total: 0 }))),
    });

    render(<SessionListPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText("No decision sessions yet.")).toBeInTheDocument();
  });

  it("shows session rows with proposal counters", async () => {
    mockFetch({
      "/trading/api/decisions": () =>
        new Response(JSON.stringify(makeSessionList())),
    });

    render(<SessionListPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText("Momentum rebalance")).toBeInTheDocument();
    expect(screen.getAllByText("3")).toHaveLength(2);
  });

  it("status filter refetches", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions": () =>
        new Response(JSON.stringify(makeSessionList())),
      "/trading/api/decisions?limit=50&offset=0&status=open": () =>
        new Response(JSON.stringify(makeSessionList())),
    });
    render(<SessionListPage />, { wrapper: MemoryRouter });
    await screen.findByText("Momentum rebalance");

    await userEvent.selectOptions(screen.getByLabelText("Status filter"), "open");

    await waitFor(() => expect(calls.length).toBeGreaterThan(1));
  });

  it("links rows to detail route", async () => {
    mockFetch({
      "/trading/api/decisions": () =>
        new Response(JSON.stringify(makeSessionList())),
    });
    render(<SessionListPage />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("link", { name: /Momentum rebalance/ })).toHaveAttribute(
      "href",
      "/sessions/session-1",
    );
  });
});
