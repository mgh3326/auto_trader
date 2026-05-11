import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { NewsTabs, NEWS_TABS, PRIMARY_NEWS_TABS, SECONDARY_NEWS_TABS } from "../components/news/NewsTabs";

describe("NewsTabs", () => {
  test("renders Toss-style primary tabs only in the visible row", () => {
    render(<NewsTabs value="top" onChange={vi.fn()} />);

    expect(screen.getByTestId("tab-holdings")).toHaveTextContent("보유주식");
    expect(screen.getByTestId("tab-watchlist")).toHaveTextContent("관심주식");
    expect(screen.getByTestId("tab-top")).toHaveTextContent("주요뉴스");
    expect(screen.getByTestId("tab-latest")).toHaveTextContent("최신뉴스");
    expect(screen.getByTestId("tab-hot")).toHaveTextContent("급상승뉴스");
    expect(screen.getByTestId("tab-research")).toHaveTextContent("리서치");

    expect(screen.queryByTestId("tab-kr")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tab-us")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tab-crypto")).not.toBeInTheDocument();
  });

  test("keeps secondary tab definitions available without showing them by default", () => {
    expect(PRIMARY_NEWS_TABS.map((t) => t.key)).toEqual([
      "holdings",
      "watchlist",
      "top",
      "latest",
      "hot",
      "research",
    ]);
    expect(SECONDARY_NEWS_TABS.map((t) => t.key)).toEqual(["kr", "us", "crypto"]);
    expect(NEWS_TABS.map((t) => t.key)).toEqual([
      "holdings",
      "watchlist",
      "top",
      "latest",
      "hot",
      "research",
      "kr",
      "us",
      "crypto",
    ]);
  });

  test("emits selected primary tab", async () => {
    const onChange = vi.fn();
    render(<NewsTabs value="top" onChange={onChange} variant="pill-row" />);

    await userEvent.click(screen.getByTestId("tab-latest"));

    expect(onChange).toHaveBeenCalledWith("latest");
  });
});
