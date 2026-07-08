import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    t: (key: string) => {
      const translations: Record<string, string> = {
        "activity.toolCalls": "Called {count} tools",
        "activity.thoughtFor": "Thought for",
        "activity.collapse": "Collapse",
        "thinking.show": "Thinking",
      };
      return translations[key] ?? key;
    },
  }),
}));

import { HistoryRoundDetailView, RoundShell } from "./ChatView";

describe("HistoryRoundDetailView", () => {
  it("renders thought before the completed tool summary when both are present", () => {
    render(
      <HistoryRoundDetailView
        round={{
          waitSeconds: 9,
          thinking: "hidden reasoning",
          tools: "\uE004• Read hello.py\uE005",
        }}
      />,
    );

    const thought = screen.getByText("Thought for 9s");
    const tools = screen.getByText("Called 1 tools");

    expect(
      thought.compareDocumentPosition(tools) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });
});

describe("RoundShell", () => {
  it("shows the tool description when collapsed and Working when expanded", () => {
    render(
      <RoundShell
        timerText="Read hello.py"
        expandedTimerText="Working..."
        running={true}
        showTimer={true}
        autoExpand={false}
        detailsNode={<div>details</div>}
        textNode={null}
      />,
    );

    expect(screen.getByText("Read hello.py")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Read hello.py" }));

    expect(screen.getByText("Working...")).toBeTruthy();
    expect(screen.getByText("details")).toBeTruthy();
  });
});
