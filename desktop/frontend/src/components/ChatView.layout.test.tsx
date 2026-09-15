import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { liveTurnToHistoryTurn } from "./ChatView";
import type { HistoryRound, Turn, TurnRound } from "../api/types";

const startChatFromCompactSummaryMock = vi.hoisted(() => vi.fn());

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    t: (key: string) => {
      const translations: Record<string, string> = {
        "activity.thoughtFor": "Thought for",
        "activity.working": "Working...",
        "activity.thinking": "Thinking",
        "thinking.show": "Thinking",
        "msg.copy": "Copy",
        "msg.newFromCompact": "New chat from summary",
      };
      return translations[key] ?? key;
    },
    startChatFromCompactSummary: startChatFromCompactSummaryMock,
  }),
}));

import { groupLiveRounds, HistoryRoundDetailView, LiveRoundView, orderTranscriptEntries, RoundShell, TurnView } from "./ChatView";
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

  it("mounts expanded and auto-collapses (animated) after the settle delay", () => {
    vi.useFakeTimers();
    try {
      render(
        <RoundShell
          timerText="Worked for 4s"
          running={false}
          showTimer={true}
          autoExpand={true}
          autoCollapseDelayMs={900}
          detailsNode={<div>details</div>}
          textNode={null}
        />,
      );

      // The just-settled turn shows the expanded "Worked for" body first.
      expect(screen.getByText("details")).toBeTruthy();

      // After the hold delay the close animation starts; the body is still
      // mounted while it animates closed.
      act(() => {
        vi.advanceTimersByTime(900);
      });
      expect(screen.getByText("details")).toBeTruthy();

      // Once the close animation would have finished, the body unmounts.
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(screen.queryByText("details")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the shell collapsed when it mounts already settled", () => {
    render(
      <RoundShell
        timerText="Worked for 4s"
        running={false}
        showTimer={true}
        autoExpand={false}
        detailsNode={<div>details</div>}
        textNode={null}
      />,
    );

    expect(screen.queryByText("details")).toBeNull();
  });
});

describe("TurnView settle animation", () => {
  it("shows the settled \"Worked for\" shell expanded, then auto-collapses it", () => {
    vi.useFakeTimers();
    try {
      const runningTurn: Turn = {
        id: 1,
        userText: "hi",
        rounds: [
          {
            id: 11,
            waitStartedAt: 1000,
            waitEndedAt: 5000,
            segments: [
              {
                id: 111,
                kind: "step",
                text: "\\uE004• Ran request_skill_prompt codex-usage\\uE005",
              },
            ],
          },
        ],
        startedAt: 1000,
        endedAt: null,
      };
      const handlers = { onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() };

      const { rerender } = render(
        <TurnView turn={runningTurn} now={2000} negIndex={-1} handlers={handlers} />,
      );

      // Task settles: the live turn gets an endedAt.
      rerender(
        <TurnView
          turn={{ ...runningTurn, endedAt: 20000 }}
          now={20000}
          negIndex={-1}
          handlers={handlers}
        />,
      );

      // The "Worked for" block is shown expanded first (tool row visible).
      expect(screen.getByText(/Ran request_skill_prompt/)).toBeTruthy();

      // After the settle hold delay + close animation it collapses away.
      act(() => {
        vi.advanceTimersByTime(900);
      });
      // (Effects flush at the end of each act(), so the unmount timer is
      // scheduled on the next advance — advance again past the close animation.)
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(screen.queryByText(/Ran request_skill_prompt/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders settled turns collapsed when they mount already ended", () => {
    const handlers = { onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() };
    render(
      <TurnView
        turn={{
          id: 2,
          userText: "hi",
          rounds: [
            {
              id: 21,
              waitStartedAt: 1000,
              waitEndedAt: 5000,
              segments: [
                {
                  id: 211,
                  kind: "step",
                  text: "\\uE004• Ran request_skill_prompt codex-usage\\uE005",
                },
              ],
            },
          ],
          startedAt: 1000,
          endedAt: 20000,
        }}
        now={20000}
        negIndex={-1}
        handlers={handlers}
      />,
    );

    // Already-settled turns (history reload) stay collapsed — no animation.
    expect(screen.queryByText(/Ran request_skill_prompt/)).toBeNull();
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

  it("shows a hover-only copy button on a shell row that copies the full command", () => {
    const writeText = vi.fn();
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <StepsView
        text={[
          "\uE004• Ran git status --short\uE005",
          "\uE00Agit status --short\uE00B",
          "\uE000some output\uE001",
        ].join("\n")}
      />,
    );

    const copyBtn = screen.getByTitle("Copy");
    expect(copyBtn).toBeTruthy();
    fireEvent.click(copyBtn);
    expect(writeText).toHaveBeenCalledWith("git status --short");
  });

  it("renders the copy button on a shell row without captured output", () => {
    const writeText = vi.fn();
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <StepsView
        text={[
          "\uE004• Ran git status\uE005",
          "\uE00Agit status\uE00B",
        ].join("\n")}
      />,
    );

    const copyBtn = screen.getByTitle("Copy");
    expect(copyBtn).toBeTruthy();
    fireEvent.click(copyBtn);
    expect(writeText).toHaveBeenCalledWith("git status");
  });
});

describe("TurnView streaming thinking collapse reporting", () => {
  it("reports collapsed streaming thinking to the ancestor, lifting the hold on expand", () => {
    const handlers = { onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() };
    const onStreamingThinkingCollapsedChange = vi.fn();
    const runningTurn: Turn = {
      id: 7,
      userText: "hi",
      rounds: [
        {
          id: 71,
          waitStartedAt: 1000,
          waitEndedAt: null,
          thinkingText: "reasoning in progress",
          thinkingStartedAt: 1000,
          thinkingEndedAt: null,
          segments: [],
        },
      ],
      startedAt: 1000,
      endedAt: null,
    };

    render(
      <TurnView
        turn={runningTurn}
        now={2000}
        negIndex={-1}
        handlers={handlers}
        onStreamingThinkingCollapsedChange={onStreamingThinkingCollapsedChange}
      />,
    );

    // Collapsed by default while streaming → the ancestor is told to hold
    // off auto-scrolling the transcript to the bottom on its stream ticks.
    expect(onStreamingThinkingCollapsedChange).toHaveBeenLastCalledWith(true);

    // Expanding the thought lifts the hold (it now occupies real height).
    fireEvent.click(screen.getByRole("button", { name: /Thinking/ }));
    expect(onStreamingThinkingCollapsedChange).toHaveBeenLastCalledWith(false);

    // Collapsing it again re-engages the hold.
    fireEvent.click(screen.getByRole("button", { name: /Thinking/ }));
    expect(onStreamingThinkingCollapsedChange).toHaveBeenLastCalledWith(true);
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

const compactRound = (overrides: Partial<TurnRound> = {}): TurnRound => ({
  id: 1,
  waitStartedAt: 1000,
  waitEndedAt: 1100,
  segments: [],
  compactNoticeTitle: "Context compacted",
  compactNoticeBody: "formatted summary",
  ...overrides,
});

const answerRound = (overrides: Partial<TurnRound> = {}): TurnRound => ({
  id: 2,
  waitStartedAt: 1200,
  waitEndedAt: 1300,
  segments: [{ id: 3, kind: "answer", text: "最终回答" }],
  ...overrides,
});

describe("TurnView compact notice placement", () => {
  it("renders a streaming compact summary between its user entry and the continuation", () => {
    render(
      <TurnView
        turn={{
          id: 9,
          userText: "最新一条用户消息",
          rounds: [compactRound({ compactNoticeStage: "stream" }), answerRound()],
          startedAt: 900,
          endedAt: null,
        }}
        now={1400}
        negIndex={-1}
        handlers={{ onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() }}
      />,
    );

    const user = screen.getByText("最新一条用户消息");
    const summary = screen.getByText("formatted summary");
    const answer = screen.getByText("最终回答");
    // The summary belongs to this turn and sits between the user entry and the
    // post-compaction continuation.
    expect(user.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(summary.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("keeps a compact summary above a settled turn's output", () => {
    render(
      <TurnView
        turn={{
          id: 10,
          userText: "已结束任务的消息",
          rounds: [compactRound(), answerRound()],
          startedAt: 1000,
          endedAt: 2000,
        }}
        now={3000}
        negIndex={-1}
        handlers={{ onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() }}
      />,
    );

    const summary = screen.getByText("formatted summary");
    const user = screen.getByText("已结束任务的消息");
    const answer = screen.getByText("最终回答");
    expect(user.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(summary.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("offers a new-chat action on a completed compact summary", () => {
    render(
      <TurnView
        turn={{
          id: 11,
          userText: "已完成任务的消息",
          rounds: [compactRound()],
          startedAt: 1000,
          endedAt: 2000,
        }}
        now={3000}
        negIndex={-1}
        handlers={{ onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() }}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "New chat from summary" }),
    );
    expect(startChatFromCompactSummaryMock).toHaveBeenCalledWith(
      "Context compacted",
      "formatted summary",
    );
  });

  it("hides the new-chat action while a summary is still streaming", () => {
    render(
      <TurnView
        turn={{
          id: 12,
          userText: "正在流式摘要的消息",
          rounds: [compactRound({ compactNoticeStage: "stream" })],
          startedAt: 1000,
          endedAt: null,
        }}
        now={1100}
        negIndex={-1}
        handlers={{ onCopy: vi.fn(), onFork: vi.fn(), onEdit: vi.fn() }}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "New chat from summary" }),
    ).toBeNull();
  });
});

describe("orderTranscriptEntries", () => {
  it("keeps the persisted history order and appends the live turns after it", () => {
    const entries = orderTranscriptEntries(
      [
        { userText: "第一条用户消息", timestamp: "2026-08-04 12:00:00", rounds: [] },
        {
          userText: "第二条用户消息",
          timestamp: "2026-08-04 12:01:00",
          rounds: [compactRound({ waitEndedAt: 1100 }) as unknown as HistoryRound],
        },
      ],
      [{
        id: 1,
        userText: "第三条用户消息",
        rounds: [],
        startedAt: new Date("2026-08-04T11:59:00").getTime(),
        endedAt: null,
      }],
    );

    // The live turn's client clock must not reorder it ahead of persisted turns
    // (server second-resolution timestamps vs. client milliseconds).
    expect(entries.map((entry) => entry.turn.userText)).toEqual([
      "第一条用户消息",
      "第二条用户消息",
      "第三条用户消息",
    ]);
  });

  it("does not reorder two persisted turns whose timestamps tie", () => {
    const entries = orderTranscriptEntries(
      [
        { userText: "先前", timestamp: "2026-08-05 12:00:00", rounds: [] },
        { userText: "随后", timestamp: "2026-08-05 12:00:00", rounds: [] },
      ],
      [],
    );

    expect(entries.map((entry) => entry.turn.userText)).toEqual(["先前", "随后"]);
  });
});

describe("groupLiveRounds background grouping", () => {
  it("keeps an active background round in its own group so later Wait lines are not swallowed", () => {
    const now = Date.now();
    const groups = groupLiveRounds([
      {
        id: 1,
        waitStartedAt: now,
        waitEndedAt: null,
        bgTaskId: "call_1",
        bgTaskEnded: false,
        segments: [
          {
            id: 2,
            kind: "step",
            // Background shell round: prompt line + OPEN command-output block
            // (no \uE001 END sentinel) — the live suffix keeps it open until the
            // task finishes.
            text:
              "\uE004• Ran in background echo hi\uE005\n" +
              "\uE000Background task started (id=call_1)...",
          },
        ],
      },
      {
        id: 3,
        waitStartedAt: now,
        waitEndedAt: null,
        // A blocking tool's feedback line arrives while the background task is
        // still running. It is routed into its own round by appendSegment; the
        // renderer must NOT merge it into the open background block.
        segments: [
          {
            id: 4,
            kind: "step",
            text:
              "\uE004\x1b[38;2;19;161;14m•\x1b[0m \x1b[1mWait\x1b[0m \x1b[94m(seconds=30)\x1b[0m\uE005",
          },
        ],
      },
    ]);

    expect(groups.filter((g) => g.kind === "tool")).toHaveLength(2);
    const bgGroup = groups.find(
      (g) => g.kind === "tool" && g.rounds.some((r) => r.bgTaskId === "call_1"),
    );
    const waitGroup = groups.find(
      (g) =>
        g.kind === "tool" &&
        g.rounds.some((r) => r.segments.some((s) => s.text.includes("Wait"))),
    );
    expect(bgGroup && bgGroup.kind === "tool" ? bgGroup.rounds : []).toHaveLength(1);
    expect(waitGroup && waitGroup.kind === "tool" ? waitGroup.rounds : []).toHaveLength(1);
    // The Wait line must not share a group with the still-open background block.
    expect(bgGroup).not.toBe(waitGroup);
  });
});
