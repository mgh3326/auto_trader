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

  it("renders external cross-checks in a separate 'no impact' section, not the core chip row", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      data_sufficiency_by_source: {
        portfolio: { status: "fresh" },
        // External sources also appear here today; they must NOT show as core chips.
        toss_remote_debug: { status: "unavailable", reason_code: "unavailable" },
      },
      data_quality_audit: {
        core: { status: "usable", blocking_gaps: [], fresh_coverage_pct: 100 },
        external_cross_checks: {
          toss_remote_debug: {
            status: "unavailable",
            reason_code: "unavailable",
            affects_report_generation: false,
          },
        },
        gaps: [],
      },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);

    // External source is NOT rendered as a core degraded chip.
    expect(
      screen.queryByTestId("report-diagnostics-source-toss_remote_debug"),
    ).toBeNull();

    // It appears in the dedicated external section, with the no-impact note.
    const ext = screen.getByTestId("report-diagnostics-external");
    expect(ext).toHaveTextContent("외부 교차검증");
    expect(ext).toHaveTextContent("리포트 생성에는 영향 없음");
    expect(
      screen.getByTestId("report-diagnostics-external-toss_remote_debug"),
    ).toHaveTextContent("토스증권 교차검증");
  });

  it("does not render the external section when no external cross-checks exist", () => {
    const diagnostics: SnapshotReportDiagnostics = {
      report_quality_summary: { grade: "high_confidence" },
    };
    render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
    expect(screen.queryByTestId("report-diagnostics-external")).toBeNull();
  });
});

