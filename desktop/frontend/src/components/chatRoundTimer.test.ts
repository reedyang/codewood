import { describe, expect, it } from "vitest";
import { shouldShowRoundTimer } from "./chatRoundTimer";

describe("shouldShowRoundTimer", () => {
  it("hides Working while a thinking-only round is still streaming", () => {
    expect(
      shouldShowRoundTimer({
        running: true,
        hasTools: false,
        hasAnswer: false,
        thinkingRunning: true,
      }),
    ).toBe(false);
  });

  it("shows the timer once tool output exists", () => {
    expect(
      shouldShowRoundTimer({
        running: true,
        hasTools: true,
        hasAnswer: false,
        thinkingRunning: false,
      }),
    ).toBe(true);
  });

  it("keeps the live timer for answer-only rounds", () => {
    expect(
      shouldShowRoundTimer({
        running: true,
        hasTools: false,
        hasAnswer: true,
        thinkingRunning: false,
      }),
    ).toBe(true);
  });

  it("hides the timer once the round is no longer running", () => {
    expect(
      shouldShowRoundTimer({
        running: false,
        hasTools: false,
        hasAnswer: false,
        thinkingRunning: false,
      }),
    ).toBe(false);
  });
});
