// ROB-318 Phase 3 (PR-C) — ReportDiagnosticsPanel render tests.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReportDiagnosticsPanel } from "../components/investment-reports/ReportDiagnosticsPanel";
import type { SnapshotReportDiagnostics } from "../types/investmentReports";

describe("ReportDiagnosticsPanel", () => {
  it("returns null when diagnostics is null (legacy report)", () => {
    const { container } = render(<ReportDiagnosticsPanel diagnostics={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("returns null when diagnostics is undefined", () => {
    const { container } = render(
      <ReportDiagnosticsPanel diagnostics={undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("returns null when no sub-field carries anything to show", () => {
    const { container } = render(
      <ReportDiagnosticsPanel diagnostics={{}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the quality grade badge with coverage", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      report_quality_summary: {
        grade: "high_confidence",
        fresh_coverage_pct: 100,
      },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
    const grade = screen.getByTestId("report-diagnostics-grade");
    expect(grade).toHaveTextContent("리포트 품질: 높음");
    expect(grade).toHaveTextContent("신선도 100%");
  });

  it("renders why_no_action with kind label + backend reason_ko", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      why_no_action: {
        kind: "data_insufficient",
        blocking_sources: ["portfolio"],
        reason_ko: "데이터 부족 — portfolio 확인 불가로 매수/매도 권고 보류",
      },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
    const why = screen.getByTestId("report-diagnostics-why");
    expect(why).toHaveAttribute("data-kind", "data_insufficient");
    expect(why).toHaveTextContent("데이터 부족");
    expect(why).toHaveTextContent("매수/매도 권고 보류");
  });

  it("renders degraded source chips with reason_code label, hides fresh sources", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      data_sufficiency_by_source: {
        portfolio: { status: "unavailable", reason_code: "user_id_missing" },
        market: { status: "fresh" },
      },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
    const chip = screen.getByTestId("report-diagnostics-source-portfolio");
    expect(chip).toHaveTextContent("포지션");
    expect(chip).toHaveTextContent("확인 불가");
    expect(chip).toHaveTextContent("사용자 미지정");
    // fresh source must not render a chip
    expect(screen.queryByTestId("report-diagnostics-source-market")).toBeNull();
  });

  it("falls back to raw reason_code when unmapped", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      data_sufficiency_by_source: {
        journal: { status: "hard_stale", reason_code: "weird_code" },
      },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
    const chip = screen.getByTestId("report-diagnostics-source-journal");
    expect(chip).toHaveTextContent("weird_code");
  });
});
