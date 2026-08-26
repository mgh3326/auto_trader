import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExternalCashDeclareConflict } from "../api/fundingAdvisory";
import { ExternalCashPanel } from "../pages/FundingRoute";
import type {
  ExternalCashDeclaration,
  ExternalCashForm,
  ExternalCashHeadsView,
  ExternalCashHistoryView,
} from "../types/fundingAdvisory";
import { EXTERNAL_CASH_NO_AUTO_ADD_NOTICE } from "../types/fundingAdvisory";

const HEAD: ExternalCashDeclaration = {
  declaration_id: "head-zero",
  owner_user_id: 7,
  location_key: "parking_primary",
  display_label: "파킹통장",
  currency: "KRW",
  amount: "0",
  as_of: "2026-08-20T06:30:00Z",
  fresh_until: "2026-08-21T06:30:00Z",
  source_note: "운영자 선언",
  declared_by_user_id: 7,
  origin: "invest_ui",
  supersedes_declaration_id: null,
  idempotency_key: "funding-ui:zero",
  recorded_at: "2026-08-20T06:30:00Z",
};

const CURRENT: ExternalCashHeadsView = {
  heads: [
    {
      status: "fresh",
      amount_status: "known",
      current: HEAD,
      route_fundable_amount: "0",
      verification_badge: "운영자 선언 · 시스템 검증 불가",
      warning_code: null,
    },
  ],
  count: 1,
  notice: EXTERNAL_CASH_NO_AUTO_ADD_NOTICE,
};

const HISTORY: ExternalCashHistoryView = {
  declarations: [HEAD],
  count: 1,
};

const FORM: ExternalCashForm = {
  owner_user_id: 7,
  as_of: "2026-08-20T07:30:00Z",
  as_of_fixed: true,
  creates_money_movement: false,
  idempotency_key: "funding-ui:form",
  notice: EXTERNAL_CASH_NO_AUTO_ADD_NOTICE,
  currencies: ["KRW", "USD"],
  default_location_key: "parking_primary",
  default_display_label: "파킹통장",
  default_currency: "KRW",
  default_amount: "0",
  default_source_note: "운영자 선언",
  expected_head_declaration_id: "head-zero",
  heads: [
    {
      location_key: "parking_primary",
      display_label: "파킹통장",
      currency: "KRW",
      amount: "0",
      as_of: "2026-08-20T06:30:00Z",
      status: "fresh",
      expected_head_declaration_id: "head-zero",
    },
  ],
};

const declareMock = vi.fn();

vi.mock("../api/fundingAdvisory", async () => {
  const actual = await vi.importActual<typeof import("../api/fundingAdvisory")>("../api/fundingAdvisory");
  return {
    ...actual,
    declareExternalCash: (...args: unknown[]) => declareMock(...args),
  };
});

afterEach(() => {
  declareMock.mockReset();
});

function renderPanel({
  current = CURRENT,
  history = HISTORY,
  form = FORM,
  onSaved = async () => undefined,
}: {
  current?: ExternalCashHeadsView;
  history?: ExternalCashHistoryView;
  form?: ExternalCashForm | null;
  onSaved?: () => Promise<void>;
} = {}) {
  return render(
    <MemoryRouter>
      <ExternalCashPanel current={current} history={history} form={form} onSaved={onSaved} />
    </MemoryRouter>,
  );
}

describe("ExternalCashPanel self-service", () => {
  it("shows the zero parking head, notice, frozen as-of, and history table", () => {
    renderPanel();

    expect(screen.getByTestId("external-cash-notice")).toHaveTextContent(EXTERNAL_CASH_NO_AUTO_ADD_NOTICE);
    expect(screen.getByTestId("external-cash-card-parking_primary")).toHaveTextContent("파킹통장");
    expect(screen.getByTestId("external-cash-card-parking_primary")).toHaveTextContent("0");
    expect(screen.getByTestId("external-cash-as-of")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("2026-08-15T08:20:00+09:00")).toBeNull();
    expect(screen.getByTestId("external-cash-history-table")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "통화" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "신규 위치" })).toBeInTheDocument();
  });

  it("appends a new declaration from the zero head", async () => {
    const user = userEvent.setup();
    declareMock.mockResolvedValue({ ...HEAD, declaration_id: "head-next", amount: "1500000" });
    const onSaved = vi.fn(async () => undefined);
    renderPanel({ onSaved });

    await user.clear(screen.getByRole("textbox", { name: "금액 (KRW)" }));
    await user.type(screen.getByRole("textbox", { name: "금액 (KRW)" }), "1500000");
    await user.click(screen.getByRole("button", { name: "선언 저장 · 돈 이동 아님" }));

    await waitFor(() => expect(declareMock).toHaveBeenCalledWith(
      expect.objectContaining({
        amount: "1500000",
        as_of: "2026-08-20T07:30:00Z",
        expected_head_declaration_id: "head-zero",
        location_key: "parking_primary",
      }),
    ));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("shows the new head after a 409 stale submit", async () => {
    const user = userEvent.setup();
    declareMock.mockRejectedValue(
      new ExternalCashDeclareConflict({
        error: "expected_head_conflict",
        message: "expected declaration head does not match current head",
        current_head: { ...HEAD, declaration_id: "head-new", amount: "640000" },
      }),
    );
    renderPanel();

    await user.click(screen.getByRole("button", { name: "선언 저장 · 돈 이동 아님" }));

    expect(await screen.findByTestId("external-cash-conflict")).toHaveTextContent("다른 곳에서 선언이 갱신되었습니다");
    expect(screen.getByTestId("external-cash-conflict-head")).toHaveTextContent("640,000");
  });

  it("hides the write form for a non-admin session", () => {
    renderPanel({ form: null });
    expect(screen.queryByTestId("external-cash-form")).toBeNull();
    expect(screen.getByText("관리자만 새 선언을 append할 수 있습니다.")).toBeInTheDocument();
  });
});
