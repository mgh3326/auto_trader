import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeJournalPlaceholder } from "../../components/CommitteeJournalPlaceholder";
import type { CommitteeJournalPlaceholder as JournalType } from "../../api/types";

describe("CommitteeJournalPlaceholder", () => {
  it("renders journal uuid and notes", () => {
    const journal: JournalType = {
      journal_uuid: "11111111-1111-4111-8111-111111111111",
      notes: "Reviewed AAPL preopen — wait for support retest.",
    };

    render(<CommitteeJournalPlaceholder journalPlaceholder={journal} />);

    expect(screen.getByText("기록 / 사후 검토")).toBeInTheDocument();
    expect(
      screen.getByText("11111111-1111-4111-8111-111111111111"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Reviewed AAPL preopen — wait for support retest."),
    ).toBeInTheDocument();
  });

  it("renders nothing if both fields are missing", () => {
    const journal: JournalType = { journal_uuid: null, notes: null };
    const { container } = render(
      <CommitteeJournalPlaceholder journalPlaceholder={journal} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when journal is null", () => {
    const { container } = render(
      <CommitteeJournalPlaceholder journalPlaceholder={null} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
