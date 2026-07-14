import { describe, it, expect } from "vitest";
import {
  resolveToolOutputLang,
  SyntaxOutput,
} from "./SyntaxOutput";

describe("resolveToolOutputLang", () => {
  it("highlights read output by line-number format (English UI)", () => {
    const body = "Read src/foo.py [offset=0, limit=2000]";
    const payload = "1: def main():\n2:     pass\n";
    const res = resolveToolOutputLang(body, payload);
    expect(res.lang).toBe("python");
    expect(res.lineNumbers).toBe(true);
  });

  it("highlights read output with a localized label", () => {
    const body = "读取 src/bar.ts [offset=0]";
    const payload = "1: const x = 1;\n2: export {};\n";
    const res = resolveToolOutputLang(body, payload);
    expect(res.lang).toBe("typescript");
    expect(res.lineNumbers).toBe(true);
  });

  it("highlights shell file reads without line numbers", () => {
    const body = "Ran cat src/foo.js";
    const payload = "const a = 1;\nconsole.log(a);\n";
    const res = resolveToolOutputLang(body, payload);
    expect(res.lang).toBe("javascript");
    expect(res.lineNumbers).toBe(false);
  });

  it("skips non-code files", () => {
    const body = "Read notes.txt";
    const payload = "1: hello\n";
    expect(resolveToolOutputLang(body, payload).lang).toBe("");
  });

  it("skips shell output of non-read commands", () => {
    const body = "Ran git status";
    const payload = "On branch main\n";
    expect(resolveToolOutputLang(body, payload).lang).toBe("");
  });

  it("skips directories (no code extension)", () => {
    const body = "Read src";
    const payload = "1: file.py\n";
    expect(resolveToolOutputLang(body, payload).lang).toBe("");
  });
});

describe("SyntaxOutput", () => {
  it("returns null (fallback) when no language resolves", () => {
    const node = SyntaxOutput({ text: "1: x\n", lang: "", lineNumbers: true });
    expect(node).toBeNull();
  });

  it("returns null for ANSI-colored payloads (preserve terminal colors)", () => {
    const node = SyntaxOutput({
      text: "\x1b[31mconst x = 1;\x1b[0m",
      lang: "javascript",
      lineNumbers: false,
    });
    expect(node).toBeNull();
  });
});
