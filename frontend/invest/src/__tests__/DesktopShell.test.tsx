import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopShell } from "../desktop/DesktopShell";

test("renders left/center/right slots", () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
      <DesktopShell
        left={<div>L</div>}
        center={<div>C</div>}
        right={<div>R</div>}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
  expect(screen.getByText("L")).toBeInTheDocument();
  expect(screen.getByText("C")).toBeInTheDocument();
  expect(screen.getByText("R")).toBeInTheDocument();
});
