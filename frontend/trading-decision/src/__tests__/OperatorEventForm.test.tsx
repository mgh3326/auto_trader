import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import OperatorEventForm from "../components/OperatorEventForm";

describe("OperatorEventForm", () => {
  it("submits operator_market_event with current session_uuid and trimmed source_text", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(
      screen.getByLabelText(/source text/i),
      "  OpenAI earnings missed  ",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "user",
        event_type: "operator_market_event",
        session_uuid: "session-1",
        source_text: "OpenAI earnings missed",
        severity: 2,
        confidence: 50,
      }),
    );
  });

  it("blocks submit when source_text is empty", async () => {
    const onSubmit = vi.fn();
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /source text is required/i,
    );
  });

  it("parses comma-separated affected symbols", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    await userEvent.type(
      screen.getByLabelText(/affected symbols/i),
      "MSFT, NVDA ,  AAPL",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        affected_symbols: ["MSFT", "NVDA", "AAPL"],
      }),
    );
  });

  it("clears the textarea after a successful submit", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    const textarea = screen.getByLabelText(
      /source text/i,
    ) as HTMLTextAreaElement;
    await userEvent.type(textarea, "abc");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(textarea.value).toBe("");
  });

  it("surfaces an error and keeps the form intact when submit fails", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      detail: "validation failed",
    });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    const textarea = screen.getByLabelText(
      /source text/i,
    ) as HTMLTextAreaElement;
    await userEvent.type(textarea, "abc");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/validation failed/i);
    expect(textarea.value).toBe("abc");
  });

  it("clamps severity to 1..5 and confidence to 0..100", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    const severity = screen.getByLabelText(/severity/i) as HTMLInputElement;
    await userEvent.clear(severity);
    await userEvent.type(severity, "9");
    const confidence = screen.getByLabelText(
      /confidence/i,
    ) as HTMLInputElement;
    await userEvent.clear(confidence);
    await userEvent.type(confidence, "150");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 5, confidence: 100 }),
    );
  });
});
