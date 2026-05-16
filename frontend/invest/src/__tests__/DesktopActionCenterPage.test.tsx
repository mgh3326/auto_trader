import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { DesktopActionCenterPage } from "../pages/desktop/DesktopActionCenterPage";
import { useActionCenter } from "../hooks/useActionCenter";

vi.mock("../hooks/useActionCenter", () => ({ useActionCenter: vi.fn() }));
vi.mock("../desktop/RightRemotePanel", () => ({ RightRemotePanel: () => <div data-testid="right-remote-panel" /> }));

const readyState = {
  state: {
    status: "ready" as const,
    reports: {
      reports: [
        {
          reportUuid: "report-2",
          reportType: "daily",
          market: "crypto",
          accountScope: "upbit-live",
          createdByProfile: "analyst",
          status: "published",
          summary: "최신 코인 액션 리포트",
          riskSummary: "긴 리스크 문구도 카드 안에서 줄바꿈되어야 합니다.",
          dataFreshness: {},
          coverage: {},
          sourcePolicy: [],
          safetyNotes: [],
          createdAt: "2026-05-16T00:00:00Z",
          publishedAt: "2026-05-16T00:01:00Z",
          validUntil: null,
          stageResults: [],
          candidates: [],
        },
        {
          reportUuid: "report-1",
          reportType: "daily",
          market: "kr",
          accountScope: "kis-live",
          createdByProfile: "analyst",
          status: "published",
          summary: "삼성전자 후보와 현금 여력 점검",
          riskSummary: "이벤트 리스크가 남아 있습니다.",
          dataFreshness: {
            accountFeasibility: "확인 불가",
            marketLiquidity: "2026-05-15T09:10:00+09:00",
          },
          coverage: {
            accountFeasibility: "확인 불가",
            eventRisk: "degraded",
          },
          sourcePolicy: ["KIS live is account/order authority"],
          safetyNotes: ["이 화면은 주문 실행이 아닙니다."],
          createdAt: "2026-05-15T00:00:00Z",
          publishedAt: "2026-05-15T00:01:00Z",
          validUntil: null,
          stageResults: [
            {
              stageKey: "account_feasibility",
              source: "kis_live",
              status: "unavailable",
              freshnessAt: null,
              unavailableReason: "확인 불가",
              warnings: ["계좌 여력 확인 필요"],
            },
          ],
          candidates: [],
        },
      ],
      unavailableLabel: "확인 불가",
    },
    candidates: {
      candidates: [
        {
          candidateUuid: "cand-2",
          reportUuid: "report-2",
          symbol: "KRW-ONDO",
          market: "crypto",
          side: "sell",
          actionType: "stop_exit_sell",
          quantity: "88.80994671",
          quantityPct: null,
          limitPrice: "541",
          notional: "48046",
          currency: "KRW",
          priority: 1,
          confidence: 0.78,
          thesis: "최신 리포트 후보만 표시되어야 합니다.",
          riskNotes: ["지정가만 사용"],
          verification: {
            accountFeasibility: "manual check",
            liquidity: "spread checked",
            eventNewsRisk: "low",
          },
          blockingReasons: [],
          approvalStatus: "awaiting_approval",
          approvalType: "manual",
          executionState: "not_submitted",
          createdAt: "2026-05-16T00:02:00Z",
          validUntil: null,
        },
        {
          candidateUuid: "cand-1",
          reportUuid: "report-1",
          symbol: "005930",
          market: "kr",
          side: "buy",
          actionType: "buy_candidate",
          quantity: null,
          quantityPct: 12.5,
          limitPrice: null,
          notional: null,
          currency: "KRW",
          priority: 8,
          confidence: 0.72,
          thesis: "메모리 업황 개선과 수급 회복 기대",
          riskNotes: ["정규장 유동성 확인 필요", "뉴스 리스크 확인 필요"],
          verification: {
            accountFeasibility: "확인 불가",
            liquidity: "확인 불가",
            eventNewsRisk: "확인 불가",
          },
          blockingReasons: ["계좌 여력 확인 필요"],
          approvalStatus: "awaiting_approval",
          approvalType: "manual",
          executionState: "not_submitted",
          createdAt: "2026-05-15T00:02:00Z",
          validUntil: null,
        },
      ],
      unavailableLabel: "확인 불가",
    },
  },
  reload: vi.fn(),
};

function wrap(ui: React.ReactElement) {
  return <MemoryRouter basename="/invest" initialEntries={["/invest/action-center"]}>{ui}</MemoryRouter>;
}

beforeEach(() => {
  vi.mocked(useActionCenter).mockReturnValue(readyState);
});

test("renders analyst reports, candidate queue, and unavailable markers", () => {
  render(wrap(<DesktopActionCenterPage />));

  expect(screen.getByRole("heading", { name: "액션 센터" })).toBeInTheDocument();
  expect(screen.getByText("최신 코인 액션 리포트")).toBeInTheDocument();
  expect(screen.queryByText("삼성전자 후보와 현금 여력 점검")).not.toBeInTheDocument();
  expect(screen.getByText("KRW-ONDO")).toBeInTheDocument();
  expect(screen.queryByText("005930")).not.toBeInTheDocument();
  expect(screen.getByText("88.80994671")).toBeInTheDocument();
  expect(screen.getByText("541")).toBeInTheDocument();
  expect(screen.getByText(/정규장 확인 필요/)).toBeInTheDocument();
});

test("keeps approval controls read-only and separates approval from execution state", () => {
  render(wrap(<DesktopActionCenterPage />));

  expect(screen.getByText("승인 상태: awaiting_approval")).toBeInTheDocument();
  expect(screen.getByText("실행 상태: not_submitted")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "승인 기록은 수동 처리" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "거절 기록은 수동 처리" })).toBeDisabled();
  expect(screen.getByText(/의사결정\/승인 대기 자료이며 주문 실행이 아닙니다/)).toBeInTheDocument();
});

test("renders loading and error states without hiding non-execution copy", () => {
  vi.mocked(useActionCenter).mockReturnValueOnce({ state: { status: "loading" }, reload: vi.fn() });
  const { rerender } = render(wrap(<DesktopActionCenterPage />));
  expect(screen.getByText(/액션 센터 데이터를 불러오는 중/)).toBeInTheDocument();
  expect(screen.getAllByText(/주문 실행이 아닙니다/).length).toBeGreaterThanOrEqual(1);

  vi.mocked(useActionCenter).mockReturnValueOnce({ state: { status: "error", message: "boom" }, reload: vi.fn() });
  rerender(wrap(<DesktopActionCenterPage />));
  expect(screen.getByText(/액션 센터 데이터를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "재시도" })).toBeInTheDocument();
});
