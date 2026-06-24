import { describe, it, expect } from "vitest";
import { addTab, removeTab, selectTab, type ConsoleTabState } from "./consoleTabs";

const empty: ConsoleTabState = { tabs: [], activeId: "" };
const tab = (id: string): { id: string; title: string; kind: string } => ({
  id,
  title: `t-${id}`,
  kind: "cmd",
});

describe("consoleTabs", () => {
  it("adds a tab and makes it active", () => {
    const s = addTab(empty, tab("a"));
    expect(s.tabs.map((t) => t.id)).toEqual(["a"]);
    expect(s.activeId).toBe("a");
  });

  it("keeps appending and activating the newest", () => {
    let s = addTab(empty, tab("a"));
    s = addTab(s, tab("b"));
    expect(s.tabs.map((t) => t.id)).toEqual(["a", "b"]);
    expect(s.activeId).toBe("b");
  });

  it("removing the active tab falls back to the last remaining", () => {
    let s = addTab(addTab(addTab(empty, tab("a")), tab("b")), tab("c"));
    s = removeTab(s, "c");
    expect(s.tabs.map((t) => t.id)).toEqual(["a", "b"]);
    expect(s.activeId).toBe("b");
  });

  it("removing a non-active tab keeps the active id", () => {
    let s = addTab(addTab(empty, tab("a")), tab("b"));
    s = selectTab(s, "a");
    s = removeTab(s, "b");
    expect(s.activeId).toBe("a");
  });

  it("removing the last tab clears the active id", () => {
    let s = addTab(empty, tab("a"));
    s = removeTab(s, "a");
    expect(s.tabs).toEqual([]);
    expect(s.activeId).toBe("");
  });

  it("selecting an unknown id is a no-op", () => {
    const s = addTab(empty, tab("a"));
    expect(selectTab(s, "zzz")).toBe(s);
  });
});
