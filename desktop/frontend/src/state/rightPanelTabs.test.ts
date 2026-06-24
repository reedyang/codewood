import { describe, it, expect } from "vitest";
import {
  toggleTabVisibility,
  type RightPanelPrefs,
} from "./rightPanelTabs";

const base: RightPanelPrefs = { visible: ["todos"], active: "todos" };

describe("rightPanelTabs", () => {
  it("adds a non-mandatory tab and keeps canonical order", () => {
    const next = toggleTabVisibility(base, "browser");
    expect(next.visible).toEqual(["todos", "browser"]);
  });

  it("removes a visible non-mandatory tab", () => {
    const withBrowser: RightPanelPrefs = {
      visible: ["todos", "browser"],
      active: "browser",
    };
    const next = toggleTabVisibility(withBrowser, "browser");
    expect(next.visible).toEqual(["todos"]);
    // Active falls back to a still-visible tab.
    expect(next.active).toBe("todos");
  });

  it("never hides the mandatory todos tab", () => {
    const next = toggleTabVisibility(base, "todos");
    expect(next.visible).toContain("todos");
  });

  it("always keeps at least one visible tab", () => {
    let prefs: RightPanelPrefs = { visible: ["todos", "browser"], active: "todos" };
    prefs = toggleTabVisibility(prefs, "browser");
    expect(prefs.visible.length).toBeGreaterThanOrEqual(1);
    // todos can't be toggled off, so we stay at >=1.
    prefs = toggleTabVisibility(prefs, "todos");
    expect(prefs.visible.length).toBeGreaterThanOrEqual(1);
  });
});
