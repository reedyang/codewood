import { describe, expect, it } from "vitest";
import type { DiffRow } from "../api/types";
import { normalizeDiffRows } from "./FileChangeDetails";

describe("normalizeDiffRows", () => {
  it("converts stale change rows with only new content into add rows", () => {
    const rows: DiffRow[] = [
      { type: "change", oldNo: null, newNo: 1823, oldText: "", newText: "// added line" },
      { type: "change", oldNo: null, newNo: 1824, oldText: "", newText: "// another added line" },
    ];

    const normalized = normalizeDiffRows(rows);

    expect(normalized).toEqual([
      { type: "add", oldNo: null, newNo: 1823, oldText: "", newText: "// added line" },
      { type: "add", oldNo: null, newNo: 1824, oldText: "", newText: "// another added line" },
    ]);
  });

  it("converts stale change rows with only old content into del rows", () => {
    const rows: DiffRow[] = [
      { type: "change", oldNo: 10, newNo: null, oldText: "removed", newText: "" },
    ];

    const normalized = normalizeDiffRows(rows);

    expect(normalized).toEqual([
      { type: "del", oldNo: 10, newNo: null, oldText: "removed", newText: "" },
    ]);
  });
});
