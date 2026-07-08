import { render, screen } from "@testing-library/react";
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

import { HistoryRoundDetailView } from "./ChatView";

describe("HistoryRoundDetailView", () => {
  it("renders thought before the tool summary when both are present", () => {
    render(
      <HistoryRoundDetailView
        round={{
          waitSeconds: 9,
          thinking: "hidden reasoning",
          tools: "\uE004• Request skill prompt (skill_id=codex-usage)\uE005",
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
