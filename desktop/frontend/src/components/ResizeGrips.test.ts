import { describe, expect, it } from "vitest";
import { getMinWindowWidth } from "./ResizeGrips";

describe("getMinWindowWidth", () => {
  it("keeps the base minimum while both sidebars are open", () => {
    expect(getMinWindowWidth(true, true)).toBe(960);
  });

  it("removes only the closed sidebar minimum", () => {
    expect(getMinWindowWidth(false, true)).toBe(720);
    expect(getMinWindowWidth(true, false)).toBe(720);
  });

  it("allows the content-only window minimum when both sidebars are closed", () => {
    expect(getMinWindowWidth(false, false)).toBe(480);
  });
});
