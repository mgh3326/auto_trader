import { InvestmentReportBundleContent } from "../../components/investment-reports/InvestmentReportBundleContent";
import { InvestmentReportsContent } from "../../components/investment-reports/InvestmentReportsContent";
import { DesktopShell } from "../../desktop/DesktopShell";
import { useViewport } from "../../hooks/useViewport";
import {
  MobileInvestmentReportBundlePage,
  MobileInvestmentReportsPage,
} from "../mobile/MobileInvestmentReportsPage";

export function DesktopInvestmentReportsPage() {
  return <DesktopShell center={<InvestmentReportsContent />} />;
}

export function DesktopInvestmentReportBundlePage() {
  return <DesktopShell center={<InvestmentReportBundleContent />} />;
}

export function InvestmentReportsRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? (
    <MobileInvestmentReportsPage />
  ) : (
    <DesktopInvestmentReportsPage />
  );
}

export function InvestmentReportBundleRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? (
    <MobileInvestmentReportBundlePage />
  ) : (
    <DesktopInvestmentReportBundlePage />
  );
}
