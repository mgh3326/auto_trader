import type { ScreenerInvestorFlowChip } from "../../types/screener";

const TONE_CLASS: Record<ScreenerInvestorFlowChip["tone"], string> = {
  double_buy: "investor-flow-chip--double-buy",
  double_sell: "investor-flow-chip--double-sell",
  foreign_buy: "investor-flow-chip--foreign-buy",
  foreign_sell: "investor-flow-chip--foreign-sell",
  institution_buy: "investor-flow-chip--institution-buy",
  institution_sell: "investor-flow-chip--institution-sell",
  neutral: "investor-flow-chip--neutral",
};

export function InvestorFlowChip({ chip }: { chip: ScreenerInvestorFlowChip }) {
  const title = chip.snapshotDate ? `${chip.label} (${chip.snapshotDate})` : chip.label;
  return (
    <span
      className={`investor-flow-chip ${TONE_CLASS[chip.tone]} investor-flow-chip--${chip.dataState}`}
      title={title}
      data-testid="investor-flow-chip"
    >
      {chip.label}
    </span>
  );
}
