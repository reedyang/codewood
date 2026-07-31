import { describe, expect, it } from "vitest";
import { countToolCalls, getLastToolPromptBody } from "./Steps";
import {
  groupModelsByProvider,
  groupLiveRounds,
  getLiveTurnDisplayState,
  hasPendingInvisibleRound,
  shouldShowPendingWorking,
  shouldShowStreamingWorkingForRound,
  splitCompletedTurn,
  toolTextHasVisibleOutput,
} from "./ChatView";

describe("countToolCalls", () => {
  it("counts prompt rows in a rendered tool block", () => {
    const prompt = "\uE004• Ran echo hi\uE005";
    const text = `${prompt}\n${prompt}\n`;
    expect(countToolCalls(text)).toBe(2);
  });

  it("collapses a repainted failed tool prompt into one row", () => {
    const greenPrompt = "\uE004\x1b[38;2;19;161;14m•\x1b[0m Read hello.py\uE005";
    const redPrompt = "\uE004\x1b[38;2;197;15;31m•\x1b[0m Read hello.py\uE005";
    const text = [
      greenPrompt,
      "\uE000hello output\uE001",
      `\x1b7\x1b[2A\r\x1b[2K${redPrompt}\x1b8`,
    ].join("\n");

    expect(countToolCalls(text)).toBe(1);
    expect(getLastToolPromptBody(text)).toBe("Read hello.py");
  });
});

describe("toolTextHasVisibleOutput", () => {
  it("treats command output and diff previews as visible tool output", () => {
    expect(toolTextHasVisibleOutput("\uE004• Read x.py\uE005")).toBe(false);
    expect(toolTextHasVisibleOutput("\uE004• Read x.py\uE005\uE000body\uE001")).toBe(true);
    expect(toolTextHasVisibleOutput("\uE004• Edit x.py\uE005\uE006{\"file\":\"x.py\",\"diffRows\":[]}\uE007")).toBe(true);
  });

  it("lets running tool rounds stop spinning without looking idle", () => {
    const hasVisibleOutput = toolTextHasVisibleOutput(
      "\uE004• Edit x.py\uE005\uE006{\"file\":\"x.py\",\"diffRows\":[{\"type\":\"add\"}]}\uE007",
    );

    expect(hasVisibleOutput).toBe(true);
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

  it("keeps a tool round with thinking separate from the tool group", () => {
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

    expect(groups).toHaveLength(2);
    expect(groups[0]?.kind).toBe("other");
    expect(groups[1]?.kind).toBe("tool");
    if (groups[1]?.kind === "tool") {
      expect(groups[1].rounds).toHaveLength(1);
      expect(groups[1].rounds[0]?.id).toBe(2);
    }
  });

  it("merges consecutive tool-only rounds into one group regardless of settled/running state", () => {
    const rounds = [
      {
        id: 1,
        waitStartedAt: 10,
        waitEndedAt: 20,
        segments: [{ id: 1, kind: "step", text: "\uE004• Read hello.py\uE005\uE000body\uE001" }],
      },
      {
        id: 2,
        waitStartedAt: 21,
        waitEndedAt: null,
        segments: [{ id: 2, kind: "step", text: "\uE004• Read world.py\uE005" }],
      },
    ] as Parameters<typeof groupLiveRounds>[0];

    const groups = groupLiveRounds(rounds);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.kind).toBe("tool");
    if (groups[0]?.kind === "tool") {
      expect(groups[0].rounds).toHaveLength(2);
      expect(groups[0].rounds[0]?.id).toBe(1);
      expect(groups[0].rounds[1]?.id).toBe(2);
    }
  });
});

describe("groupModelsByProvider", () => {
  it("splits on the first slash and keeps later slashes in the model name", () => {
    const groups = groupModelsByProvider([
      "openai/gpt-4o",
      "vendor/family/model/v2",
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toEqual({
      provider: "openai",
      items: [{ selector: "openai/gpt-4o", name: "gpt-4o" }],
    });
    expect(groups[1]).toEqual({
      provider: "vendor",
      items: [{ selector: "vendor/family/model/v2", name: "family/model/v2" }],
    });
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

  it("shows Working while answer text is still streaming", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [{ id: 1, kind: "answer", text: "正在输出正文" }],
        },
      ],
    } as Parameters<typeof shouldShowPendingWorking>[0];

    expect(shouldShowPendingWorking(turn)).toBe(true);
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

  it("still shows Working when only a settled tool group is visible and the newest round is empty", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: 30,
          segments: [{ id: 1, kind: "step", text: "\uE004• Edit x.py\uE005\uE000error\uE001" }],
        },
        {
          id: 2,
          waitStartedAt: 31,
          waitEndedAt: null,
          segments: [],
        },
      ],
    } as Parameters<typeof shouldShowPendingWorking>[0];

    expect(shouldShowPendingWorking(turn)).toBe(true);
  });
});

describe("shouldShowStreamingWorkingForRound", () => {
  it("shows Working for a running answer round", () => {
    const round = {
      waitEndedAt: null,
      thinkingText: "",
      segments: [{ id: 1, kind: "answer", text: "流式正文" }],
    } as Parameters<typeof shouldShowStreamingWorkingForRound>[0];

    expect(shouldShowStreamingWorkingForRound(round)).toBe(true);
  });

  it("keeps Thinking-only rounds from showing Working", () => {
    const round = {
      waitEndedAt: null,
      thinkingText: "hidden reasoning",
      segments: [],
    } as Parameters<typeof shouldShowStreamingWorkingForRound>[0];

    expect(shouldShowStreamingWorkingForRound(round)).toBe(false);
  });
});

describe("getLiveTurnDisplayState", () => {
  it("hides Working while Thinking is active", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          thinkingText: "hidden reasoning",
          segments: [],
        },
      ],
    } as Parameters<typeof getLiveTurnDisplayState>[0];

    expect(getLiveTurnDisplayState(turn).showWorking).toBe(false);
  });

  it("hides Working while a tool prompt is still waiting for output", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [{ id: 1, kind: "step", text: "\uE004• Edit x.py\uE005" }],
        },
      ],
    } as Parameters<typeof getLiveTurnDisplayState>[0];

    expect(getLiveTurnDisplayState(turn).showWorking).toBe(false);
  });

  it("shows Working for an empty running round", () => {
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
    } as Parameters<typeof getLiveTurnDisplayState>[0];

    expect(getLiveTurnDisplayState(turn).showWorking).toBe(true);
  });

  it("keeps global Working hidden when tool output is visible because the tool row owns it", () => {
    const turn = {
      startedAt: 10,
      endedAt: null,
      rounds: [
        {
          id: 1,
          waitStartedAt: 20,
          waitEndedAt: null,
          segments: [{ id: 1, kind: "step", text: "\uE004• Edit x.py\uE005\uE006{}\uE007" }],
        },
      ],
    } as Parameters<typeof getLiveTurnDisplayState>[0];

    expect(getLiveTurnDisplayState(turn).showWorking).toBe(false);
  });
});
