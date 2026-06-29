import { describe, it, expect } from "vitest";
import {
  toggleTabVisibility,
  type RightPanelPrefs,
} from "./rightPanelTabs";

const base: RightPanelPrefs = { visible: ["dashboard"], active: "dashboard" };

describe("rightPanelTabs", () => {
  it("adds a non-mandatory tab and keeps canonical order", () => {
    const next = toggleTabVisibility(base, "browser");
    expect(next.visible).toEqual(["dashboard", "browser"]);
  });

  it("activates a newly shown tab", () => {
    const next = toggleTabVisibility(base, "browser");
    expect(next.active).toBe("browser");
  });

  it("removes a visible non-mandatory tab", () => {
    const withBrowser: RightPanelPrefs = {
      visible: ["dashboard", "browser"],
      active: "browser",
    };
    const next = toggleTabVisibility(withBrowser, "browser");
    expect(next.visible).toEqual(["dashboard"]);
    // Active falls back to a still-visible tab.
    expect(next.active).toBe("dashboard");
  });

  it("never hides the mandatory dashboard tab", () => {
    const next = toggleTabVisibility(base, "dashboard");
    expect(next.visible).toContain("dashboard");
  });

  it("always keeps at least one visible tab", () => {
    let prefs: RightPanelPrefs = { visible: ["dashboard", "browser"], active: "dashboard" };
    prefs = toggleTabVisibility(prefs, "browser");
    expect(prefs.visible.length).toBeGreaterThanOrEqual(1);
    // dashboard can't be toggled off, so we stay at >=1.
    prefs = toggleTabVisibility(prefs, "dashboard");
    expect(prefs.visible.length).toBeGreaterThanOrEqual(1);
  });
});
