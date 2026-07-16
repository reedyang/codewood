import { describe, expect, it } from "vitest";
import {
  buildFallbackToolRoundFromCall,
  buildFallbackToolRoundsFromRaw,
  getSubAgentMessageToolRounds,
} from "./subagentToolRounds";

describe("subagentToolRounds", () => {
  it("builds a read fallback round with path detail and output", () => {
    const rounds = buildFallbackToolRoundsFromRaw([
      {
        tool: "read",
        args: { path: "cli/subagents/executor.py", offset: 0, limit: 2000 },
        output: "1: test output",
      },
    ], { lang: "en" });

    expect(rounds).toHaveLength(1);
    expect(rounds[0]).toContain("Read");
    expect(rounds[0]).toContain("cli/subagents/executor.py");
    expect(rounds[0]).toContain("1: test output");
    expect(rounds[0]).toContain("\uE004");
    expect(rounds[0]).toContain("\uE000");
    expect(rounds[0]).toContain("\u001b[38;2;19;161;14m•");
    expect(rounds[0]).toContain("\u001b[38;2;97;175;239mcli/subagents/executor.py");
  });

  it("parses OpenAI-style tool_calls arguments for fallback rendering", () => {
    const round = buildFallbackToolRoundFromCall({
      function: {
        name: "read",
        arguments: "{\"path\":\"cli/tools/run_subagent.py\",\"limit\":50}",
      },
    }, "", { lang: "en" });

    expect(round).toContain("Read");
    expect(round).toContain("cli/tools/run_subagent.py");
    expect(round).toContain("[limit=50]");
  });

  it("prefers structured raw rounds over bare tool call names", () => {
    const rounds = getSubAgentMessageToolRounds({
      role: "assistant",
      content: "",
      tool_calls: [{ name: "read", args: { path: "ignored.py" } }],
      _tool_rounds_raw: [
        {
          tool: "read",
          args: { path: "actual.py" },
          output: "42: answer",
        },
      ],
    }, { lang: "en" });

    expect(rounds).toHaveLength(1);
    expect(rounds[0]).toContain("Read");
    expect(rounds[0]).toContain("actual.py");
    expect(rounds[0]).toContain("42: answer");
    expect(rounds[0]).not.toContain("ignored.py");
  });
});
