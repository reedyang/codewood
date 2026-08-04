import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { liveTurnToHistoryTurn } from "./ChatView";
import type { Turn } from "../api/types";

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    t: (key: string) => {
      const translations: Record<string, string> = {
        "activity.thoughtFor": "Thought for",
        "activity.working": "Working...",
        "activity.thinking": "Thinking",
        "thinking.show": "Thinking",
      };
      return translations[key] ?? key;
    },
  }),
}));

import { HistoryRoundDetailView, LiveRoundView, orderTranscriptEntries, RoundShell, TurnView } from "./ChatView";
import { StepsView } from "./Steps";

describe("HistoryRoundDetailView", () => {
  it("renders thought before the tool steps when both are present", () => {
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
    const tool = screen.getByText("Read hello.py");

    expect(
      thought.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it("renders text when no tools are present", () => {
    render(
      <HistoryRoundDetailView
        round={{
          waitSeconds: 9,
          text: "先给用户一段说明",
        }}
      />,
    );

    expect(screen.getByText("先给用户一段说明")).toBeTruthy();
  });

  it("renders the visible reply between thought and tool steps when all three are present", () => {
    // Regression: a round carrying thinking + a visible reply + tool steps must
    // show the reply after the thought block and before the tool steps (it was
    // previously dropped entirely when tools were present).
    render(
      <HistoryRoundDetailView
        round={{
          waitSeconds: 9,
          thinking: "hidden reasoning",
          text: "我先为您寻找并读取 `helloworld.py` 的内容。",
          tools: "\uE004• Read hello.py\uE005",
        }}
      />,
    );

    const answer = screen.getByText((content) =>
      content.includes("我先为您寻找并读取"),
    );
    const thought = screen.getByText("Thought for 9s");
    const tool = screen.getByText("Read hello.py");
    expect(answer).toBeTruthy();
    expect(
      thought.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(
      answer.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it("does not auto-scroll expanded thinking to the bottom", () => {
    const scrollTopSets: number[] = [];
    const scrollHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
    const scrollTop = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop");
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        return 123;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTop", {
      configurable: true,
      get() {
        return 0;
      },
      set(value) {
        scrollTopSets.push(Number(value));
      },
    });
    try {
      render(
        <HistoryRoundDetailView
          round={{
            waitSeconds: 9,
            thinking: "第一行\n第二行",
          }}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Thought for 9s" }));

      expect(screen.getByText((content) => content.includes("第一行"))).toBeTruthy();
      expect(document.querySelector(".thinking-scroll")).toBeTruthy();
      expect(scrollTopSets).not.toContain(123);
    } finally {
      if (scrollHeight) {
        Object.defineProperty(HTMLElement.prototype, "scrollHeight", scrollHeight);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollHeight;
      }
      if (scrollTop) {
        Object.defineProperty(HTMLElement.prototype, "scrollTop", scrollTop);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTop;
      }
    }
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

  it("replaces a topic-bearing explore running row with the completed row", () => {
    render(
      <StepsView
        text={[
          "\uE004• Exploring sub-agent architecture...\uE005",
          "\uE008sa_123\uE009",
          "\r\u001b[2K",
          "\uE004• Explored sub-agent architecture for 41.3s\uE005",
        ].join("\n")}
      />,
    );

    expect(screen.queryByText("Exploring sub-agent architecture...")).toBeNull();
    expect(screen.getByText("Explored sub-agent architecture for 41.3s")).toBeTruthy();
    expect(screen.getByTitle("View sub-agent session")).toBeTruthy();
  });
});

describe("liveTurnToHistoryTurn", () => {
  it("maps a settled live turn (tool rounds + final answer) into the history shape so it collapses into \"Worked for\"", () => {
    const live: Turn = {
      id: 1,
      userText: "查看我的codex用量",
      rounds: [
        {
          id: 11,
          waitStartedAt: 1000,
          waitEndedAt: 5000,
          segments: [
            { id: 111, kind: "step", text: "\uE004• Ran request_skill_prompt codex-usage\uE005" },
          ],
        },
        {
          id: 12,
          waitStartedAt: 5000,
          waitEndedAt: 15000,
          segments: [
            { id: 121, kind: "step", text: "\uE004• Ran shell npx ccusage codex\uE005" },
          ],
        },
        {
          id: 13,
          waitStartedAt: 15000,
          waitEndedAt: 20000,
          thinkingText: "hidden reasoning",
          segments: [
            { id: 131, kind: "answer", text: "您的 Codex 总用量约为 8.18 亿 tokens" },
          ],
        },
      ],
      startedAt: 1000,
      endedAt: 20000,
    };

    const history = liveTurnToHistoryTurn(live);

    expect(history.userText).toBe("查看我的codex用量");
    expect(history.rounds).toHaveLength(3);
    expect(history.rounds[0].tools).toContain("Ran request_skill_prompt");
    expect(history.rounds[1].tools).toContain("Ran shell npx ccusage codex");
    // Final answer round becomes the round whose text feeds the "final answer".
    expect(history.rounds[2].text).toContain("8.18 亿 tokens");
    expect(history.rounds[2].thinking).toBe("hidden reasoning");
    // Elapsed time is carried over so the "Worked for" timer is correct.
    expect(history.rounds[0].waitSeconds).toBe(4);
    expect(history.rounds[1].waitSeconds).toBe(10);
  });
});

describe("TurnView compact notice placement", () => {
  it("keeps a streamed compact summary after the triggering live user entry", () => {
    render(
      <TurnView
        turn={{
          id: 9,
          userText: "最新一条用户消息",
          rounds: [],
          startedAt: 1000,
          endedAt: null,
        }}
        now={1100}
        negIndex={-1}
        handlers={{ onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() }}
        compactNotice={{
          title: "Compacting context",
          body: "streamed compact summary",
          text: "Compacting context",
          stage: "stream",
          anchorTurnId: 9,
        }}
      />,
    );

    const user = screen.getByText("最新一条用户消息");
    const summary = screen.getByText("streamed compact summary");
    expect(
      user.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });
});

describe("orderTranscriptEntries", () => {
  it("keeps an older settled live turn ahead of a newer persisted history turn", () => {
    const entries = orderTranscriptEntries(
      [{
        userText: "本轮用户消息",
        timestamp: "2026-08-04 12:01:00",
        rounds: [],
      }],
      [{
        id: 1,
        userText: "前一轮用户消息",
        rounds: [],
        startedAt: new Date("2026-08-04T12:00:00").getTime(),
        endedAt: new Date("2026-08-04T12:00:30").getTime(),
      }],
    );

    expect(entries.map((entry) => entry.turn.userText)).toEqual([
      "前一轮用户消息",
      "本轮用户消息",
    ]);
  });
});
