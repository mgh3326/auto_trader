import { InvestmentReportBundleContent } from "../../components/investment-reports/InvestmentReportBundleContent";
import { InvestmentReportsContent } from "../../components/investment-reports/InvestmentReportsContent";
import { MobileShell } from "../../mobile/MobileShell";

export function MobileInvestmentReportsPage() {
  return (
    <MobileShell title="투자 리포트">
      <InvestmentReportsContent compact />
    </MobileShell>
  );
}

export function MobileInvestmentReportBundlePage() {
  return (
    <MobileShell title="리포트 상세">
      <InvestmentReportBundleContent compact />
    </MobileShell>
  );
}
