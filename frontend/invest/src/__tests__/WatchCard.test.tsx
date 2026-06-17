import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { WatchCard } from "../desktop/stock-detail/WatchCard";
import type { WatchAlertRow } from "../types/watches";

function watch(overrides: Partial<WatchAlertRow> = {}): WatchAlertRow {
  return {
    alert_uuid: "alert-1",
    source_report_uuid: "report-1",
    market: "kr",
    symbol: "005930",
    symbol_name: "삼성전자",
    target_kind: "asset",
    metric: "price_below",
    operator: "below",
    threshold: "70000",
    threshold_high: null,
    status: "active",
    valid_until: "2026-06-18T03:00:00Z",
    intent: "buy_review",
    action_mode: "notify_only",
    rationale: "진입가 근접 시 분할 매수 검토",
    trigger_checklist: [],
    max_action: {},
    current_price: "69800",
    proximity_band: "hit",
    last_event: null,
    near_expiry: true,
    ...overrides,
  };
}

describe("WatchCard", () => {
  test("shows loading state when watches is undefined", () => {
    render(<WatchCard watches={undefined} />);
    expect(screen.getByText("불러오는 중입니다…")).toBeInTheDocument();
  });

  test("shows empty state when no watches", () => {
    render(<WatchCard watches={[]} />);
    expect(screen.getByText("등록된 감시가 없습니다.")).toBeInTheDocument();
  });

  test("renders an active price watch with condition, proximity and current price", () => {
    render(<WatchCard watches={[watch()]} />);
    // condition: "가격 ₩70,000 이하"
    expect(screen.getByText(/가격 ₩70,000 이하/)).toBeInTheDocument();
    // status + proximity + near-expiry pills
    expect(screen.getByText("감시중")).toBeInTheDocument();
    expect(screen.getByText("도달")).toBeInTheDocument();
    expect(screen.getByText("임박")).toBeInTheDocument();
    // current price formatted for KR
    expect(screen.getByText("₩69,800")).toBeInTheDocument();
    expect(screen.getByText("진입가 근접 시 분할 매수 검토")).toBeInTheDocument();
  });

  test("renders a between-operator zone condition with both thresholds", () => {
    render(
      <WatchCard
        watches={[
          watch({
            metric: "price",
            operator: "between",
            threshold: "70000",
            threshold_high: "80000",
            proximity_band: null,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/가격 ₩70,000 ~ ₩80,000/)).toBeInTheDocument();
  });

  test("renders triggered watch with last event outcome", () => {
    render(
      <WatchCard
        watches={[
          watch({
            status: "triggered",
            proximity_band: null,
            current_price: null,
            last_event: {
              event_uuid: "evt-1",
              outcome: "notified",
              current_value: "69000",
              created_at: "2026-06-17T03:00:00Z",
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText("발화됨")).toBeInTheDocument();
    expect(screen.getByText(/notified/)).toBeInTheDocument();
  });
});
