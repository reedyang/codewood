import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const useAppMock = vi.fn();

vi.mock("../state/AppContext", () => ({
  useApp: () => useAppMock(),
}));

import { DashboardContent } from "./DashboardPanel";

function renderDashboard(state: Record<string, unknown>) {
  useAppMock.mockReturnValue({
    state,
    t: (key: string) => key,
  });
  return render(<DashboardContent />);
}

describe("DashboardPanel context usage", () => {
  beforeEach(() => {
    useAppMock.mockReset();
  });

  it("renders window, used tokens, usage percent and per-part breakdown", () => {
    renderDashboard({
      contextUsage: {
        percent: 12,
        tokens: 15360,
        window: 128000,
        parts: [
          { key: "system", tokens: 8000 },
          { key: "tools", tokens: 4000 },
          { key: "history", tokens: 3360 },
        ],
      },
    });

    expect(screen.getByText("128,000")).toBeTruthy();
    expect(screen.getByText("15,360")).toBeTruthy();
    expect(screen.getByText("12.0%")).toBeTruthy();
    expect(screen.getByText("dashboard.part.system")).toBeTruthy();
    expect(screen.getByText("dashboard.part.tools")).toBeTruthy();
    expect(screen.getByText("dashboard.part.history")).toBeTruthy();
    expect(screen.getByText("6.3%")).toBeTruthy();
    expect(screen.getByText("3.1%")).toBeTruthy();
    expect(screen.getByText("2.6%")).toBeTruthy();
  });

  it("renders the breakdown in fixed order with history last", () => {
    renderDashboard({
      contextUsage: {
        percent: 50,
        tokens: 1000,
        window: 2000,
        parts: [
          { key: "history", tokens: 900 },
          { key: "tools", tokens: 100 },
          { key: "system", tokens: 100 },
        ],
      },
    });
    const labels = screen
      .getAllByText(/^dashboard\.part\./, { selector: ".context-usage-part-label" })
      .map((el) => el.textContent);
    expect(labels).toEqual([
      "dashboard.part.system",
      "dashboard.part.tools",
      "dashboard.part.history",
    ]);
  });

  it("renders zero-token rows for empty parts (no skills / no MCP)", () => {
    renderDashboard({
      contextUsage: {
        percent: 1,
        tokens: 100,
        window: 128000,
        parts: [
          { key: "system", tokens: 100 },
          { key: "tools", tokens: 0 },
          { key: "skills", tokens: 0 },
          { key: "mcp", tokens: 0 },
          { key: "history", tokens: 0 },
        ],
      },
    });

    expect(screen.getByText("dashboard.part.skills")).toBeTruthy();
    expect(screen.getByText("dashboard.part.mcp")).toBeTruthy();
  });

  it("renders an empty state when no context usage data exists (new chat)", () => {
    renderDashboard({});
    expect(screen.getByText("dashboard.context")).toBeTruthy();
    expect(screen.getByText("dashboard.contextEmpty")).toBeTruthy();
  });
});
