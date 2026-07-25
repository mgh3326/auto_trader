// ROB-692 — on-demand deterministic recommendation card on the stock detail
// page. Idle CTA -> click -> result render; R:R chip only when trade_setup
// is present; card renders nothing for crypto (unsupported market).

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { RecommendationCard } from "../desktop/stock-detail/RecommendationCard";
import type { StockDetailRecommendationResponse } from "../types/stockDetail";

function reco(
  overrides: Partial<StockDetailRecommendationResponse> = {},
): StockDetailRecommendationResponse {
  return {
    market: "us",
    symbol: "AAPL",
    name: "Apple Inc.",
    as_of: "2026-07-04T09:00:00+09:00",
    current_price: 100,
    action: "buy",
    confidence: "high",
    rsi14: 28.5,
    reasoning: "RSI 28.5 (oversold)",
    insufficient_inputs: [],
    buy_zones: [{ price: 95, type: "support", reasoning: "Support at 95" }],
    sell_targets: [{ price: 120, type: "resistance", reasoning: "Resistance at 120" }],
    stop_loss: 90,
    trade_setup: {
      direction: "long",
      entry: "100.0",
      stop: "90.0",
      target: "120.0",
      risk_pct: "10.00",
      reward_pct: "20.00",
      rr_ratio: "2.00",
    },
    ...overrides,
  };
}

describe("RecommendationCard (ROB-692)", () => {
  test("renders nothing for crypto market", () => {
    const { container } = render(
      <RecommendationCard market="crypto" data={undefined} loading={false} error={undefined} onLoad={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  test("idle state shows a CTA button and no result", () => {
    render(<RecommendationCard market="us" data={undefined} loading={false} error={undefined} onLoad={vi.fn()} />);
    expect(screen.getByText("추천 실행 / R:R 보기")).toBeInTheDocument();
    expect(screen.queryByText("매수")).not.toBeInTheDocument();
  });

  test("clicking the CTA calls onLoad", async () => {
    const user = userEvent.setup();
    const onLoad = vi.fn();
    render(<RecommendationCard market="us" data={undefined} loading={false} error={undefined} onLoad={onLoad} />);
    await user.click(screen.getByText("추천 실행 / R:R 보기"));
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  test("loading state shows a loading message", () => {
    render(<RecommendationCard market="us" data={undefined} loading={true} error={undefined} onLoad={vi.fn()} />);
    expect(screen.getByText("불러오는 중입니다…")).toBeInTheDocument();
  });

  test("error state shows the error and a retry button", () => {
    render(<RecommendationCard market="us" data={undefined} loading={false} error="network down" onLoad={vi.fn()} />);
    expect(screen.getByText(/network down/)).toBeInTheDocument();
    expect(screen.getByText("다시 시도")).toBeInTheDocument();
  });

  test("renders action/confidence/zones/stop_loss and the R:R chip when trade_setup is present", () => {
    render(<RecommendationCard market="us" data={reco()} loading={false} error={undefined} onLoad={vi.fn()} />);
    expect(screen.getByText("매수")).toBeInTheDocument();
    expect(screen.getByText(/신뢰도 높음/)).toBeInTheDocument();
    expect(screen.getAllByText(/RSI 28.5/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Support at 95/)).toBeInTheDocument();
    expect(screen.getByText(/Resistance at 120/)).toBeInTheDocument();
    expect(screen.getByText("롱")).toBeInTheDocument();
    expect(screen.getByText(/손익비 R:R 2\.00/)).toBeInTheDocument();
  });

  test("omits the R:R chip when trade_setup is null (e.g. sell/hold recommendation)", () => {
    render(
      <RecommendationCard
        market="us"
        data={reco({ action: "sell", trade_setup: null })}
        loading={false}
        error={undefined}
        onLoad={vi.fn()}
      />,
    );
    expect(screen.getByText("매도")).toBeInTheDocument();
    expect(screen.queryByText("롱")).not.toBeInTheDocument();
    expect(screen.queryByText("숏")).not.toBeInTheDocument();
    expect(screen.queryByText(/손익비/)).not.toBeInTheDocument();
  });

  test("shows the insufficient_inputs note when present", () => {
    render(
      <RecommendationCard
        market="us"
        data={reco({ insufficient_inputs: ["price", "consensus"] })}
        loading={false}
        error={undefined}
        onLoad={vi.fn()}
      />,
    );
    expect(screen.getByText(/데이터 부족: price, consensus/)).toBeInTheDocument();
  });
});
