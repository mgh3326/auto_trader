import { useSearchParams } from "react-router-dom";

export type PortfolioTab = "holdings" | "signals" | "sellHistory" | "buyHistory" | "currentOrders" | "watchAlerts" | "retrospectives";

export const PORTFOLIO_TABS: { key: PortfolioTab; label: string }[] = [
  { key: "holdings", label: "보유 현황" },
  { key: "signals", label: "시그널" },
  { key: "sellHistory", label: "매도 이력" },
  { key: "buyHistory", label: "매수 이력" },
  { key: "currentOrders", label: "현재 주문" },
  { key: "watchAlerts", label: "감시" },
  { key: "retrospectives", label: "회고" },
];

function parsePortfolioTab(value: string | null): PortfolioTab {
  return value === "signals" || value === "sellHistory" || value === "buyHistory" || value === "currentOrders" || value === "watchAlerts" || value === "retrospectives"
    ? value
    : "holdings";
}

export function usePortfolioTabSearchParam(): [PortfolioTab, (tab: PortfolioTab) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = parsePortfolioTab(searchParams.get("tab"));

  const setActiveTab = (tab: PortfolioTab) => {
    setSearchParams((next) => {
      const params = new URLSearchParams(next);
      if (tab === "holdings") params.delete("tab");
      else params.set("tab", tab);
      return params;
    }, { replace: true });
  };

  return [activeTab, setActiveTab];
}
