import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ManualCashResponse, ManualCashView } from "../api/manualCash";
import { ManualCashSaveError } from "../api/manualCash";
import { ManualCashSettingsContent } from "../pages/ManualCashSettingsRoute";

const saveMock = vi.fn();

vi.mock("../api/manualCash", async () => {
  const actual = await vi.importActual<typeof import("../api/manualCash")>("../api/manualCash");
  return { ...actual, saveManualCash: (...args: unknown[]) => saveMock(...args) };
});

afterEach(() => {
  saveMock.mockReset();
});

const LIMITS = {
  max_amount_krw: 10_000_000_000,
  max_accounts: 20,
  name_max_len: 40,
  confirm_change_ratio: "0.5",
};

function view(overrides: Partial<ManualCashView> = {}): ManualCashView {
  return {
    present: true,
    amount: 10_000_000,
    amount_valid: true,
    accounts: [
      { name: "토스 파킹", amount: 6_000_000 },
      { name: "CMA", amount: 4_000_000 },
    ],
    source: "operator_confirmed",
    confirmed_at: "2026-09-24T01:00:00+00:00",
    updated_at: "2026-09-24T01:00:00+00:00",
    stale: false,
    stale_at: "2026-09-27T01:00:00+00:00",
    stale_after_hours: 72,
    ...overrides,
  };
}

function data(v: ManualCashView = view(), canEdit = true): ManualCashResponse {
  return { manual_cash: v, limits: LIMITS, can_edit: canEdit };
}

function renderContent(d: ManualCashResponse = data(), onSaved = vi.fn()) {
  render(<ManualCashSettingsContent data={d} onSaved={onSaved} />);
  return { onSaved };
}

function amountInput(index: number) {
  return screen.getByRole("textbox", { name: `계좌 ${index} 금액` });
}

describe("ManualCashSettingsContent", () => {
  it("shows rows from the operator's saved breakdown, total, last-updated, source and fresh rule", () => {
    renderContent();
    expect(screen.getAllByTestId("manual-cash-row")).toHaveLength(2);
    expect(screen.getByTestId("manual-cash-total")).toHaveTextContent("10,000,000원");
    expect(screen.getByTestId("manual-cash-current-amount")).toHaveTextContent("10,000,000원");
    expect(screen.getByTestId("manual-cash-updated-at")).not.toHaveTextContent("—");
    expect(screen.getByTestId("manual-cash-source")).toHaveTextContent("운영자 확인");
    expect(screen.getByTestId("manual-cash-fresh")).toHaveTextContent("72시간");
    expect(screen.getByTestId("manual-cash-fresh")).toHaveTextContent("deployment_cap 파킹 항이 0");
  });

  it("warns loudly when stale — matching the server rule (excluded, parking term 0)", () => {
    renderContent(data(view({ stale: true })));
    const banner = screen.getByTestId("manual-cash-stale");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent("0으로 반영 중");
    expect(banner).toHaveTextContent("stale_treated_as_zero");
    expect(banner).toHaveTextContent("total_orderable_krw");
  });

  it("does not prefill amounts when nothing is stored", () => {
    renderContent(data(view({ present: false, amount: null, amount_valid: false, accounts: [], source: null, updated_at: null, stale: true, stale_at: null })));
    expect(screen.getByTestId("manual-cash-missing")).toHaveTextContent("absent_treated_as_zero");
    expect(screen.getAllByTestId("manual-cash-row")).toHaveLength(1);
    expect(amountInput(1)).toHaveValue("");
    expect(screen.getByTestId("manual-cash-save")).toBeDisabled();
  });

  it("flags a legacy total with no breakdown and an unmarked source", () => {
    renderContent(data(view({ accounts: [], source: null })));
    expect(screen.getByTestId("manual-cash-no-breakdown")).toBeInTheDocument();
    expect(screen.getByTestId("manual-cash-source")).toHaveTextContent("출처 표시 없음");
    expect(amountInput(1)).toHaveValue("");
  });

  it.each(["-1", "abc", "1.5", "1e3", "NaN", "Infinity", "10000000001"])(
    "blocks save for invalid amount %s",
    async (raw) => {
      const user = userEvent.setup();
      renderContent();
      await user.clear(amountInput(1));
      await user.type(amountInput(1), raw);
      expect(screen.getByTestId("manual-cash-save")).toBeDisabled();
      expect(screen.getByTestId("manual-cash-total")).toHaveTextContent("—");
    },
  );

  it("adds and removes rows and recomputes the total", async () => {
    const user = userEvent.setup();
    renderContent();
    await user.click(screen.getByRole("button", { name: "+ 계좌 추가" }));
    await user.type(screen.getByRole("textbox", { name: "계좌 3 이름" }), "새 계좌");
    await user.type(amountInput(3), "1,000,000");
    expect(screen.getByTestId("manual-cash-total")).toHaveTextContent("11,000,000원");
    await user.click(screen.getByRole("button", { name: "계좌 1 삭제" }));
    expect(screen.getByTestId("manual-cash-total")).toHaveTextContent("5,000,000원");
  });

  it("saves a change within 50% directly, sending integers without separators", async () => {
    const user = userEvent.setup();
    saveMock.mockResolvedValue(data(view({ amount: 15_000_000 })));
    const { onSaved } = renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "11,000,000"); // 10M → 15M = exactly +50%
    await user.click(screen.getByTestId("manual-cash-save"));
    expect(screen.queryByTestId("manual-cash-confirm-dialog")).toBeNull();
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledWith({
      accounts: [
        { name: "토스 파킹", amount: 11_000_000 },
        { name: "CMA", amount: 4_000_000 },
      ],
      expected_updated_at: "2026-09-24T01:00:00+00:00",
      confirm_large_change: false,
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("requires the confirm dialog for a >50% jump and cancelling writes nothing", async () => {
    const user = userEvent.setup();
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "96000000"); // typo: 10M → 100M
    await user.click(screen.getByTestId("manual-cash-save"));

    const dialog = screen.getByTestId("manual-cash-confirm-dialog");
    expect(within(dialog).getByTestId("manual-cash-confirm-current")).toHaveTextContent("10,000,000원");
    expect(within(dialog).getByTestId("manual-cash-confirm-next")).toHaveTextContent("100,000,000원");
    expect(within(dialog).getByTestId("manual-cash-confirm-ratio")).toHaveTextContent("+900.0%");
    expect(saveMock).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "취소 · 다시 확인" }));
    expect(screen.queryByTestId("manual-cash-confirm-dialog")).toBeNull();
    expect(saveMock).not.toHaveBeenCalled();
  });

  it("focuses cancel so a stray Enter never confirms a typo, traps Tab, and restores focus", async () => {
    const user = userEvent.setup();
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "96000000{Enter}"); // Enter submits the form
    const dialog = screen.getByTestId("manual-cash-confirm-dialog");
    const cancel = within(dialog).getByRole("button", { name: "취소 · 다시 확인" });
    const confirm = within(dialog).getByTestId("manual-cash-confirm-save");
    expect(cancel).toHaveFocus();

    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus(); // wrapped, focus stays inside the dialog
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await user.tab({ shift: true });
    expect(cancel).toHaveFocus();

    await user.keyboard("{Enter}"); // a second Enter lands on cancel
    expect(screen.queryByTestId("manual-cash-confirm-dialog")).toBeNull();
    expect(saveMock).not.toHaveBeenCalled();
    expect(amountInput(1)).toHaveFocus(); // focus returns to where it was
  });

  it("closes on Escape without saving", async () => {
    const user = userEvent.setup();
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "96000000");
    await user.click(screen.getByTestId("manual-cash-save"));
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("manual-cash-confirm-dialog")).toBeNull();
    expect(saveMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("manual-cash-save")).toHaveFocus();
  });

  it("sends confirm_large_change=true only after the explicit confirm", async () => {
    const user = userEvent.setup();
    saveMock.mockResolvedValue(data(view({ amount: 100_000_000 })));
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "96000000");
    await user.click(screen.getByTestId("manual-cash-save"));
    await user.click(screen.getByTestId("manual-cash-confirm-save"));
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    expect(saveMock.mock.calls[0]?.[0]).toMatchObject({ confirm_large_change: true });
  });

  it("opens the confirm dialog when the server says confirm_required", async () => {
    const user = userEvent.setup();
    saveMock.mockRejectedValueOnce(new ManualCashSaveError(409, { error: "confirm_required" }));
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "7000000");
    await user.click(screen.getByTestId("manual-cash-save"));
    await waitFor(() => expect(screen.getByTestId("manual-cash-confirm-dialog")).toBeInTheDocument());
  });

  it("tells the operator to reload on a stale form", async () => {
    const user = userEvent.setup();
    saveMock.mockRejectedValueOnce(new ManualCashSaveError(409, { error: "stale_form" }));
    renderContent();
    await user.clear(amountInput(1));
    await user.type(amountInput(1), "7000000");
    await user.click(screen.getByTestId("manual-cash-save"));
    await waitFor(() => expect(screen.getByTestId("manual-cash-stale-form")).toBeInTheDocument());
  });

  it("is read-only for non-admins", () => {
    renderContent(data(view(), false));
    expect(screen.queryByTestId("manual-cash-save")).toBeNull();
    expect(amountInput(1)).toBeDisabled();
  });
});
