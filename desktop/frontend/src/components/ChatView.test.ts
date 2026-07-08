import { describe, expect, it } from "vitest";
import { countToolCalls, getLastToolPromptBody } from "./Steps";
import {
  groupLiveRounds,
  hasPendingInvisibleRound,
  shouldShowPendingWorking,
  splitCompletedTurn,
} from "./ChatView";

describe("countToolCalls", () => {
  it("counts prompt rows in a rendered tool block", () => {
    const prompt = "\uE004• Ran echo hi\uE005";
    const text = `${prompt}\n${prompt}\n`;
    expect(countToolCalls(text)).toBe(2);
  });
});

describe("getLastToolPromptBody", () => {
  it("returns the last prompt description without ANSI codes", () => {
    const first = "\uE004• \x1b[32mRequest skill prompt (skill_id=codex-usage)\x1b[0m\uE005";
    const second = "\uE004• \x1b[36mRead hello.py\x1b[0m\uE005";
    const text = `${first}\n${second}\n`;

    expect(getLastToolPromptBody(text)).toBe("Read hello.py");
  });
});

describe("groupLiveRounds", () => {
  it("ignores empty rounds between tool rounds", () => {
    const rounds = [
      {
        id: 1,
        waitStartedAt: 10,
        waitEndedAt: 20,
        segments: [{ id: 1, kind: "step", text: "\uE004• Read hello.py\uE005" }],
      },
      {
        id: 2,
        waitStartedAt: 20,
        waitEndedAt: 30,
        segments: [],
      },
      {
        id: 3,
        waitStartedAt: 30,
        waitEndedAt: 40,
        segments: [{ id: 2, kind: "step", text: "\uE004• Read world.py\uE005" }],
      },
      {
        id: 4,
        waitStartedAt: 40,
        waitEndedAt: 50,
        segments: [{ id: 3, kind: "answer", text: "最终答案" }],
      },
    ] as Parameters<typeof groupLiveRounds>[0];

    const groups = groupLiveRounds(rounds);

    expect(groups).toHaveLength(2);
    expect(groups[0]?.kind).toBe("tool");
    if (groups[0]?.kind === "tool") {
      expect(groups[0].rounds).toHaveLength(2);
    }
    expect(groups[1]?.kind).toBe("other");
  });

  it("keeps a tool round with thinking in the tool group", () => {
    const rounds = [
      {
        id: 1,
        waitStartedAt: 10,
        waitEndedAt: 20,
        thinkingText: "hidden reasoning",
        thinkingStartedAt: 10,
        thinkingEndedAt: 14,
        segments: [{ id: 1, kind: "step", text: "\uE004• Read hello.py\uE005" }],
      },
      {
        id: 2,
        waitStartedAt: 20,
        waitEndedAt: 30,
        segments: [{ id: 2, kind: "step", text: "\uE004• Read world.py\uE005" }],
      },
    ] as Parameters<typeof groupLiveRounds>[0];

    const groups = groupLiveRounds(rounds);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.kind).toBe("tool");
    if (groups[0]?.kind === "tool") {
      expect(groups[0].rounds).toHaveLength(2);
    }
  });
});

describe("splitCompletedTurn", () => {
  it("moves the final answer-only round outside the worked-for block", () => {
    const turn = {
      rounds: [
        { waitSeconds: 2, tools: "tool block" },
        {
          waitSeconds: 4,
          text: "最终答案第一段\n\n- 项目符号",
        },
      ],
    } as Parameters<typeof splitCompletedTurn>[0];

    const layout = splitCompletedTurn(turn);

    expect(layout.detailRounds).toHaveLength(2);
    expect(layout.detailRounds[1]?.text).toBe("最终答案第一段\n\n- 项目符号");
    expect(layout.finalAnswerText).toContain("最终答案第一段");
    expect(layout.workedForSeconds).toBe(6);
  });

  it("still extracts the final answer when the last round carries thinking", () => {
    const turn = {
      rounds: [
        { waitSeconds: 3, tools: "tool block" },
        {
          waitSeconds: 5,
          thinking: "hidden reasoning",
          text: "答案正文",
        },
      ],
    } as Parameters<typeof splitCompletedTurn>[0];

    const layout = splitCompletedTurn(turn);

    expect(layout.detailRounds).toHaveLength(2);
    expect(layout.finalAnswerText).toBe("答案正文");
    expect(layout.workedForSeconds).toBe(8);
  });

  it("keeps a final round with tools inside the worked-for block", () => {
    const turn = {
      rounds: [
        { waitSeconds: 3, tools: "tool block" },
        {
          waitSeconds: 5,
          tools: "more tools",
          text: "答案正文",
        },
      ],
    } as Parameters<typeof splitCompletedTurn>[0];

    const layout = splitCompletedTurn(turn);

    expect(layout.detailRounds).toHaveLength(2);
    expect(layout.finalAnswerText).toBe("答案正文");
    expect(layout.workedForSeconds).toBe(8);
  });
});

describe("shouldShowPendingWorking", () => {
  it("detects a hidden running round that should keep the latest live group active", () => {
    const turn = {
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 10,
          waitEndedAt: 20,
          segments: [{ id: 1, kind: "step", text: "Read hello.py" }],
        },
        {
          id: 2,
          waitStartedAt: 30,
          waitEndedAt: null,
          segments: [],
        },
      ],
    } as Parameters<typeof hasPendingInvisibleRound>[0];

    expect(hasPendingInvisibleRound(turn)).toBe(true);
  });

  it("shows Working while the newest round is still empty and running", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [],
        },
      ],
    } as Parameters<typeof shouldShowPendingWorking>[0];

    expect(shouldShowPendingWorking(turn)).toBe(true);
  });

  it("hides the placeholder once the round has visible content", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [{ id: 1, kind: "step", text: "Read hello.py" }],
        },
      ],
    } as Parameters<typeof shouldShowPendingWorking>[0];

    expect(shouldShowPendingWorking(turn)).toBe(false);
  });

  it("does not show the placeholder when a live group is already visible", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [],
        },
      ],
    } as Parameters<typeof shouldShowPendingWorking>[0];

    expect(shouldShowPendingWorking(turn, true)).toBe(false);
  });
});
