import { describe, expect, it } from "vitest";
import { countToolCalls } from "./Steps";
import { splitCompletedTurn } from "./ChatView";

describe("countToolCalls", () => {
  it("counts prompt rows in a rendered tool block", () => {
    const prompt = "\uE004• Ran echo hi\uE005";
    const text = `${prompt}\n${prompt}\n`;
    expect(countToolCalls(text)).toBe(2);
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
