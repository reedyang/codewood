import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    t: (key: string) => {
      const translations: Record<string, string> = {
        "activity.toolCalls": "Called {count} tools",
        "activity.thoughtFor": "Thought for",
        "activity.working": "Working...",
        "activity.thinking": "Thinking",
        "activity.collapse": "Collapse",
        "thinking.show": "Thinking",
      };
      return translations[key] ?? key;
    },
  }),
}));

import { HistoryRoundDetailView, LiveRoundView, RoundShell } from "./ChatView";
import { StepsView } from "./Steps";

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

  it("renders text before the completed tool summary when both are present", () => {
    render(
      <HistoryRoundDetailView
        round={{
          waitSeconds: 9,
          text: "先给用户一段说明",
          tools: "\uE004• Ran npx ccusage codex\uE005",
        }}
      />,
    );

    const text = screen.getByText("先给用户一段说明");
    const tools = screen.getByText("Called 1 tools");

    expect(
      text.compareDocumentPosition(tools) & Node.DOCUMENT_POSITION_FOLLOWING,
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

describe("LiveRoundView", () => {
  it("renders an earlier answer-only live round as settled text", () => {
    render(
      <LiveRoundView
        round={{
          id: 1,
          waitStartedAt: 0,
          waitEndedAt: null,
          segments: [{ id: 1, kind: "answer", text: "先给用户一段说明" }],
        }}
        now={4000}
        forceSettled={true}
      />,
    );

    expect(screen.getByText("先给用户一段说明")).toBeTruthy();
    expect(screen.queryByText("Working... (4s)")).toBeNull();
  });
});

describe("StepsView", () => {
  it("keeps the sub-agent session affordance when control text separates the two states", () => {
    render(
      <StepsView
        text={[
          "\uE004• Exploring...\uE005",
          "\uE008sa_123\uE009",
          "\r\u001b[2K",
          "\uE004• Explored for 41.3s\uE005",
        ].join("\n")}
      />,
    );

    expect(screen.queryByText("Exploring...")).toBeNull();
    expect(screen.getByText("Explored for 41.3s")).toBeTruthy();
    expect(screen.getByTitle("View sub-agent session")).toBeTruthy();
  });
});
