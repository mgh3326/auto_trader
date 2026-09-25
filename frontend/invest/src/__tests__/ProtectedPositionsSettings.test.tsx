import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import {
  ProtectedPositionSaveError,
  type ProtectedPositionsResponse,
  type ProtectionPreview,
} from "../api/protectedPositions";
import { ProtectedPositionsSettingsContent } from "../pages/ProtectedPositionsSettingsRoute";

const preview: ProtectionPreview = {
  account_scope: "kis_live",
  market: "kr",
  symbol: "005930",
  before_protected_quantity: "5",
  after_protected_quantity: "2",
  broker_held: "10",
  broker_sellable: "8",
  before_headroom: "3",
  headroom: "6",
  state: "covered",
  broker_observed_at: "2026-09-26T00:00:00+00:00",
};

const data: ProtectedPositionsResponse = {
  can_edit: true,
  positions: [{
    account_scope: "kis_live",
    market: "kr",
    symbol: "005930",
    name: "삼성전자",
    protected_quantity: "5",
    broker_held: "10",
    broker_sellable: "8",
    headroom: "3",
    state: "covered",
    mode: "off",
    broker_observed_at: "2026-09-26T00:00:00+00:00",
    read_error: null,
    revision: 2,
    latest_revision: null,
    history_url: "/invest/api/settings/protected-positions/kis_live/kr/005930/history",
  }],
};

const { saveProtectedPosition } = vi.hoisted(() => ({ saveProtectedPosition: vi.fn() }));

vi.mock("../api/protectedPositions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/protectedPositions")>();
  return { ...actual, saveProtectedPosition };
});

beforeEach(() => {
  saveProtectedPosition.mockReset();
});

test("off or shadow rows keep the non-enforcing warning persistent", () => {
  render(<ProtectedPositionsSettingsContent data={data} onReload={async () => {}} />);

  expect(screen.getByTestId("protection-not-enforced-banner")).toHaveTextContent("보호 미강제 — 표시·기록만");
  expect(screen.getAllByText("보호 미강제 — 표시·기록만").length).toBeGreaterThanOrEqual(2);
});

test("confirmation dialog foregrounds tactical sellable change and starts on cancel", async () => {
  saveProtectedPosition.mockRejectedValueOnce(
    new ProtectedPositionSaveError(409, { error: "confirm_required", preview }),
  );
  const user = userEvent.setup();
  render(<ProtectedPositionsSettingsContent data={data} onReload={async () => {}} />);

  await user.clear(screen.getByLabelText("005930 보호 수량"));
  await user.type(screen.getByLabelText("005930 보호 수량"), "2");
  await user.type(screen.getByLabelText("005930 변경 사유"), "전술 매도 전 확인");
  await user.click(screen.getByTestId("protected-position-preview"));

  const dialog = await screen.findByTestId("protected-position-confirm-dialog");
  expect(screen.getByTestId("tactical-sellable-change")).toHaveTextContent("전술 매도 가능 수량이 3 → 6 로 바뀝니다");
  const cancel = screen.getByRole("button", { name: "취소 · 다시 확인" });
  await waitFor(() => expect(cancel).toHaveFocus());
  expect(dialog).toHaveTextContent("신선 보유 H");
  expect(screen.getByLabelText("감소 보호 종목코드 재입력")).toBeInTheDocument();
});

test("stale save response gives an accessible reload-oriented error", async () => {
  saveProtectedPosition.mockRejectedValueOnce(
    new ProtectedPositionSaveError(409, { error: "stale_form", message: "stale" }),
  );
  const user = userEvent.setup();
  render(<ProtectedPositionsSettingsContent data={data} onReload={async () => {}} />);

  await user.type(screen.getByLabelText("005930 변경 사유"), "동시 변경 확인");
  await user.click(screen.getByTestId("protected-position-preview"));

  expect(await screen.findByRole("alert")).toHaveTextContent("양식이 오래되었습니다");
});

test("non-admin screen is read-only", () => {
  render(<ProtectedPositionsSettingsContent data={{ ...data, can_edit: false }} onReload={async () => {}} />);

  expect(screen.getByText("읽기 전용입니다. 관리자만 보호 수량을 변경할 수 있습니다.")).toBeInTheDocument();
  expect(screen.queryByTestId("protected-position-preview")).not.toBeInTheDocument();
});
