import { render, screen, fireEvent } from "@testing-library/react";
import { FileChangeList } from "./FileChangeList";
import { FileChangeDetails } from "./FileChangeDetails";
import type { FileChangeSummary, DiffRow } from "../api/types";
import { translate } from "../i18n";

const t = (key: string, params?: Record<string, string | number>) => translate("en", key, params);

const patch: DiffRow[] = [
      { type: "context", oldNo: 1, newNo: 1, oldText: 'def main():', newText: 'def main():' },
      { type: "context", oldNo: 2, newNo: 2, oldText: '    print("Alice...")', newText: '    print("Alice...")' },
      { type: "context", oldNo: 3, newNo: 3, oldText: '    print("x")', newText: '    print("x")' },
      { type: "context", oldNo: 4, newNo: 4, oldText: '    print("y")', newText: '    print("y")' },
      { type: "context", oldNo: 5, newNo: 5, oldText: '    print("z")', newText: '    print("z")' },
      { type: "context", oldNo: 6, newNo: 6, oldText: '    print("w")', newText: '    print("w")' },
      { type: "context", oldNo: 7, newNo: 7, oldText: '    print("v")', newText: '    print("v")' },
      { type: "context", oldNo: 8, newNo: 8, oldText: '    print("u")', newText: '    print("u")' },
      { type: "context", oldNo: 9, newNo: 9, oldText: '    print("t")', newText: '    print("t")' },
      { type: "del", oldNo: 10, newNo: null, oldText: '    print("Hello, world!")', newText: '' },
      { type: "add", oldNo: null, newNo: 10, oldText: '', newText: '    print("中文翻译")' },
      { type: "context", oldNo: 11, newNo: 11, oldText: '    print("a")', newText: '    print("a")' },
      { type: "context", oldNo: 12, newNo: 12, oldText: '    print("b")', newText: '    print("b")' },
      { type: "context", oldNo: 13, newNo: 13, oldText: '    print("c")', newText: '    print("c")' },
      { type: "context", oldNo: 14, newNo: 14, oldText: '    print("d")', newText: '    print("d")' },
      { type: "context", oldNo: 15, newNo: 15, oldText: '    print("e")', newText: '    print("e")' },
      { type: "context", oldNo: 16, newNo: 16, oldText: '    print("f")', newText: '    print("f")' },
      { type: "context", oldNo: 17, newNo: 17, oldText: '    print("g")', newText: '    print("g")' },
      { type: "context", oldNo: 18, newNo: 18, oldText: '    print("h")', newText: '    print("h")' },
    ];

const summary: FileChangeSummary = {
  totalFiles: 1,
  totalAdded: 1,
  totalDeleted: 1,
  files: [
    {
      filePath: "C:/test/helloworld.py",
      changeType: "modify",
      source: "apply_patch",
      timestamp: new Date().toISOString(),
      addedLines: 1,
      deletedLines: 1,
      patch,
    },
  ],
};

describe("FileChangeList", () => {
  it("renders header with counts", () => {
    render(<FileChangeList summary={summary} t={t} />);
    expect(screen.getByText(t("fileChange.header", { count: 1 }))).toBeTruthy();
    // Header stats (+1/-1) plus per-file stats both exist
    expect(screen.getAllByText("+1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("-1").length).toBeGreaterThan(0);
  });

  it("expands to show details and expands unmodified lines", () => {
    render(<FileChangeList summary={summary} t={t} />);
    const header = screen.getByText("helloworld.py");
    fireEvent.click(header);
    // Both unmodified blocks (before + after change) should show the expand toggle
    const expand = screen.getAllByText(t("fileChange.expand"));
    expect(expand.length).toBe(2);
    // Click the collapsed one to expand
    fireEvent.click(expand[1]);
    expect(screen.getAllByText(t("fileChange.expand")).length).toBeGreaterThan(0);
  });
});
