import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { DiffPreview, langFromPath } from "./DiffPreview";
import type { DiffRow } from "../api/types";

// Example frontend unit test: covers the apply_patch diff preview component
// and its language-inference helper. Serves as the template for further
// frontend tests. These files are excluded from the production tsconfig and
// are never imported by the app entry, so they do not ship in the bundle.

describe("langFromPath", () => {
  it("infers and normalizes common extensions", () => {
    expect(langFromPath("src/main.tsx")).toBe("typescript");
    expect(langFromPath("a/b/util.py")).toBe("python");
    expect(langFromPath("index.html")).toBe("xml");
    expect(langFromPath("Cargo/lib.rs")).toBe("rust");
  });

  it("returns empty string when there is no usable extension", () => {
    expect(langFromPath("Makefile")).toBe("");
    expect(langFromPath("")).toBe("");
    expect(langFromPath(undefined)).toBe("");
  });
});

describe("DiffPreview", () => {
  const rows: DiffRow[] = [
    { type: "context", oldNo: 1, newNo: 1, oldText: "def main():", newText: "def main():" },
    { type: "change", oldNo: 2, newNo: 2, oldText: 'print("a")', newText: 'print("b")' },
    { type: "context", oldNo: 3, newNo: 3, oldText: "    return", newText: "    return" },
  ];

  it("renders nothing when there are no rows", () => {
    const { container } = render(<DiffPreview rows={[]} lang="python" />);
    expect(container.querySelector(".diff-preview")).toBeNull();
  });

  it("renders both sides for every row in the side-by-side layout", () => {
    // jsdom has no ResizeObserver, so the component keeps its wide default.
    const { container } = render(<DiffPreview rows={rows} lang="python" />);
    expect(container.querySelector(".diff-side-by-side")).not.toBeNull();

    const diffRows = container.querySelectorAll(".diff-side-by-side .diff-row");
    expect(diffRows.length).toBe(rows.length);

    // Context and change rows must populate BOTH the old and new cells — this
    // is the regression guard for "right side missing on unchanged lines".
    for (const row of diffRows) {
      const oldCell = row.querySelector(".diff-cell-old .diff-code");
      const newCell = row.querySelector(".diff-cell-new .diff-code");
      expect(oldCell?.textContent ?? "").not.toBe("");
      expect(newCell?.textContent ?? "").not.toBe("");
    }
  });

  it("marks changed cells with del/add classes and leaves context plain", () => {
    const { container } = render(<DiffPreview rows={rows} lang="python" />);
    const allRows = container.querySelectorAll(".diff-side-by-side .diff-row");

    const changeRow = allRows[1];
    expect(changeRow.querySelector(".diff-cell-old.diff-del")).not.toBeNull();
    expect(changeRow.querySelector(".diff-cell-new.diff-add")).not.toBeNull();

    const contextRow = allRows[0];
    expect(contextRow.querySelector(".diff-cell-old.diff-del")).toBeNull();
    expect(contextRow.querySelector(".diff-cell-new.diff-add")).toBeNull();
  });

  it("renders an omitted-row marker without code cells", () => {
    const omittedRows: DiffRow[] = [
      { type: "omitted", oldNo: null, newNo: null, oldText: "... omitted 4 lines ...", newText: "... omitted 4 lines ..." },
    ];
    const { container } = render(<DiffPreview rows={omittedRows} lang="python" />);
    expect(container.querySelector(".diff-row-omitted")).not.toBeNull();
    expect(container.querySelector(".diff-omitted-text")?.textContent).toContain("omitted 4 lines");
  });
});
