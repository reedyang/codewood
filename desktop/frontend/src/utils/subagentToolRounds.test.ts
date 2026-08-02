import { describe, expect, it } from "vitest";
import {
  buildFallbackToolRound,
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

  it("shows the searched files (include) in the grep fallback round detail", () => {
    const round = buildFallbackToolRoundsFromRaw([
      {
        tool: "grep",
        args: {
          limit: 80,
          path: "desktop/frontend/src/state",
          pattern: "newChat|draftMode",
          include: "AppContext.tsx",
        },
        output: "1: match",
      },
    ], { lang: "en" })[0];

    expect(round).toContain("Grep");
    expect(round).toContain("desktop/frontend/src/state/AppContext.tsx");
  });

  it("escapes sentinel characters inside tool output so the block stays closed", () => {
    const round = buildFallbackToolRoundsFromRaw([
      {
        tool: "shell",
        args: { command: "npx vitest run x.test.ts" },
        output: "line1\n\uE004• Grep \u001b[0m x\uE005\n\uE0001: match\uE001\nrest",
      },
    ], { lang: "en" })[0];

    // The payload sentinels must be escaped so the parser sees exactly one
    // output-begin and one output-end marker (the wrapper's own).
    expect(round.split("\uE000").length - 1).toBe(1);
    expect(round.split("\uE001").length - 1).toBe(1);
    expect(round).toContain("\\uE004");
    expect(round).toContain("\\uE005");
    expect(round).toContain("\\uE000");
    expect(round).toContain("\\uE001");
    expect(round).toContain("line1");
    expect(round).toContain("rest");
  });

  it("escapes sentinels in the error fallback text too", () => {
    const round = buildFallbackToolRound(
      "shell",
      { command: "echo hi" },
      "",
      "",
      { lang: "en", errText: "boom \uE001 boom" },
    );
    expect(round.split("\uE001").length - 1).toBe(1);
    expect(round).toContain("boom \\uE001 boom");
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
