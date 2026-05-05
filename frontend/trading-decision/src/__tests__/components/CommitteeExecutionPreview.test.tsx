import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeExecutionPreview } from "../../components/CommitteeExecutionPreview";
import type { CommitteeExecutionPreview as ExecutionPreviewType } from "../../api/types";

describe("CommitteeExecutionPreview", () => {
  it("renders blocked status", () => {
    const preview: ExecutionPreviewType = {
      is_blocked: true,
      block_reason: "Insufficient balance",
      preview_payload: null,
    };

    render(<CommitteeExecutionPreview executionPreview={preview} />);

    expect(screen.getByText("실행 프리뷰")).toBeInTheDocument();
    expect(screen.getByText(/차단됨:/)).toBeInTheDocument();
    expect(screen.getByText("Insufficient balance")).toBeInTheDocument();
  });

  it("renders payload preview", () => {
    const preview: ExecutionPreviewType = {
      is_blocked: false,
      block_reason: null,
      preview_payload: { order: "buy", qty: 10 },
    };

    render(<CommitteeExecutionPreview executionPreview={preview} />);

    expect(screen.getByText(/"qty": 10/)).toBeInTheDocument();
  });

  it("renders nothing if preview is missing", () => {
    const { container } = render(<CommitteeExecutionPreview executionPreview={null} />);
    expect(container.firstChild).toBeNull();
  });
});
