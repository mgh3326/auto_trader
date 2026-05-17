import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test } from "vitest";
import { PageSafetyNote } from "../components/PageSafetyNote";

beforeEach(() => {
  localStorage.clear();
});

test("renders heading, tag and bulleted items", () => {
  render(
    <PageSafetyNote
      routeId="t1"
      heading="읽기 전용 원칙"
      tag="시장 페이지"
      items={["주문·매매 API를 호출하지 않습니다.", "응답을 표시만 합니다."]}
    />,
  );
  const note = screen.getByTestId("page-safety-note");
  expect(note).toHaveAttribute("data-route-id", "t1");
  expect(screen.getByText("읽기 전용 원칙")).toBeInTheDocument();
  expect(screen.getByText("시장 페이지")).toBeInTheDocument();
  expect(screen.getByText("주문·매매 API를 호출하지 않습니다.")).toBeInTheDocument();
});

test("dismiss button persists per-route and hides the note", async () => {
  const user = userEvent.setup();
  const { unmount } = render(
    <PageSafetyNote routeId="market" heading="hello" items={["x"]} />,
  );
  expect(screen.getByTestId("page-safety-note")).toBeInTheDocument();

  await user.click(screen.getByTestId("page-safety-note-dismiss"));
  expect(screen.queryByTestId("page-safety-note")).not.toBeInTheDocument();
  expect(localStorage.getItem("invest:safety-note-dismissed:market")).toBe("1");

  unmount();
  render(<PageSafetyNote routeId="market" heading="hello" items={["x"]} />);
  expect(screen.queryByTestId("page-safety-note")).not.toBeInTheDocument();
});

test("dismiss state is scoped to its routeId", async () => {
  const user = userEvent.setup();
  localStorage.setItem("invest:safety-note-dismissed:market", "1");
  render(
    <>
      <PageSafetyNote routeId="market" heading="A" items={["a"]} />
      <PageSafetyNote routeId="coverage" heading="B" items={["b"]} />
    </>,
  );
  const visible = screen.getByTestId("page-safety-note");
  expect(visible).toHaveAttribute("data-route-id", "coverage");

  await user.click(screen.getByTestId("page-safety-note-dismiss"));
  expect(screen.queryByTestId("page-safety-note")).not.toBeInTheDocument();
});

test("non-dismissible note hides the close button", () => {
  render(<PageSafetyNote routeId="x" heading="hi" items={["a"]} dismissible={false} />);
  expect(screen.queryByTestId("page-safety-note-dismiss")).not.toBeInTheDocument();
});
