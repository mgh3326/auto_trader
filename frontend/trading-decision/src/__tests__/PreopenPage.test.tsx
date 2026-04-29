import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PreopenPage from "../pages/PreopenPage";
import {
  makePreopenFailOpen,
  makePreopenLinkedSession,
  makePreopenResponse,
} from "../test/fixtures/preopen";
import { mockFetch } from "../test/server";

const PREOPEN_URL = "/trading/api/preopen/latest?market_scope=kr";
const CREATE_URL = "/trading/api/decisions/from-research-run";

describe("PreopenPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders loading then fail-open banner with advisory_skipped_reason and no CTA", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenFailOpen())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/No preopen research run available/i)).toBeInTheDocument();
    expect(screen.getByText(/no_open_preopen_run/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create decision session/i })).toBeNull();
  });

  it("renders run summary, candidates, reconciliations from fixture", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    // Symbol appears in both candidates and reconciliations tables
    expect(await screen.findAllByText("005930")).toHaveLength(2);
    expect(screen.getByText("near_fill")).toBeInTheDocument();
    expect(screen.getByText(/Morning scan/)).toBeInTheDocument();
  });

  it("clicking Create decision session calls api with correct args and navigates", async () => {
    const user = userEvent.setup();
    const sessionUuid = "sess-aaaa-1111-2222-333333333333";

    const { calls } = mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
      [CREATE_URL]: () =>
        new Response(
          JSON.stringify({
            session_uuid: sessionUuid,
            session_url: `/trading/decisions/sessions/${sessionUuid}`,
            status: "open",
            advisory_skipped_reason: null,
            warnings: [],
          }),
          { status: 201 },
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    // Wait for page to load
    expect(await screen.findByRole("button", { name: /create decision session/i })).toBeInTheDocument();

    // First click triggers confirm prompt
    await user.click(screen.getByRole("button", { name: /create decision session/i }));
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();

    // Second click (confirm) submits
    await user.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => {
      const postCall = calls.find((c) => c.method === "POST");
      expect(postCall).toBeDefined();
      const body = JSON.parse(postCall?.body ?? "{}");
      expect(body.selector.run_uuid).toBe("run-1111-2222-3333-444444444444");
      expect(body.include_tradingagents).toBe(false);
      expect(body.notes).toBe("Created from preopen dashboard");
    });
  });

  it("hides Create decision session when a linked session already exists", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              linked_sessions: [makePreopenLinkedSession()],
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("link", { name: /open session/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /create decision session/i }),
    ).toBeNull();
  });

  it("surfaces ApiError detail (research_run_has_no_candidates) inline without throwing", async () => {
    const user = userEvent.setup();

    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
      [CREATE_URL]: () =>
        new Response(
          JSON.stringify({ detail: "research_run_has_no_candidates" }),
          { status: 422 },
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });
    await screen.findByRole("button", { name: /create decision session/i });

    // First click → confirm
    await user.click(screen.getByRole("button", { name: /create decision session/i }));
    // Second click → submit
    await user.click(screen.getByRole("button", { name: /confirm/i }));

    expect(
      await screen.findByText(/research_run_has_no_candidates/i),
    ).toBeInTheDocument();
  });
});
