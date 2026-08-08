import { describe, expect, it } from "vitest";
import { applySearchHighlights } from "./ChatView";

describe("applySearchHighlights", () => {
  it("wraps every keyword occurrence in <mark class=\"search-term\">", () => {
    const root = document.createElement("div");
    root.innerHTML = "修复搜索框的搜索体验";
    applySearchHighlights(root, ["搜索"]);
    const marks = root.querySelectorAll("mark.search-term");
    expect(marks.length).toBe(2);
    expect(marks[0].textContent).toBe("搜索");
    expect(marks[1].textContent).toBe("搜索");
    expect(root.textContent).toBe("修复搜索框的搜索体验");
  });

  it("merges overlapping CJK unigram/bigram matches into one mark", () => {
    const root = document.createElement("div");
    root.innerHTML = "搜索框";
    applySearchHighlights(root, ["搜", "索", "搜索"]);
    const marks = root.querySelectorAll("mark.search-term");
    expect(marks.length).toBe(1);
    expect(marks[0].textContent).toBe("搜索");
  });

  it("matches ASCII case-insensitively", () => {
    const root = document.createElement("div");
    root.innerHTML = "Use Python and PYTHON";
    applySearchHighlights(root, ["python"]);
    expect(root.querySelectorAll("mark.search-term").length).toBe(2);
  });

  it("skips code blocks and UI chrome", () => {
    const root = document.createElement("div");
    root.innerHTML =
      '<div class="answer">搜索框 <pre><code>搜索框</code></pre></div>' +
      '<button type="button">搜索框</button>';
    applySearchHighlights(root, ["搜索"]);
    expect(root.querySelectorAll("mark.search-term").length).toBe(1);
    expect(root.querySelector("pre code")?.textContent).toBe("搜索框");
    expect(root.querySelector("button mark.search-term")).toBeNull();
  });

  it("is idempotent and never nests marks", () => {
    const root = document.createElement("div");
    root.innerHTML = "搜索框";
    applySearchHighlights(root, ["搜索"]);
    applySearchHighlights(root, ["搜索"]);
    const marks = root.querySelectorAll("mark.search-term");
    expect(marks.length).toBe(1);
    expect(root.querySelectorAll("mark mark.search-term").length).toBe(0);
    expect(root.textContent).toBe("搜索框");
  });

  it("does nothing for empty keyword lists", () => {
    const root = document.createElement("div");
    root.innerHTML = "搜索框";
    applySearchHighlights(root, []);
    expect(root.querySelectorAll("mark.search-term").length).toBe(0);
  });
});
