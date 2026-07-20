import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MarkdownText } from "./Markdown";

// Guards the emphasis boundary rule: `_x_` / `*x*` only italicize when the
// char immediately outside each delimiter is NOT a letter, digit, or
// underscore. This keeps snake_case / `a*b*c` identifiers from being eaten as
// emphasis (mirrors cli/core/text_output_renderer.py).

describe("Markdown emphasis boundary rule", () => {
  it("does NOT italicize snake_case identifiers", () => {
    const { container } = render(<MarkdownText text={"shell, read, project_context_search, memory_search"} />);
    expect(container.querySelectorAll("em").length).toBe(0);
    expect(container.textContent).toContain("project_context_search");
  });

  it("does NOT italicize a*b*c style identifiers", () => {
    const { container } = render(<MarkdownText text={"foo a*b*c bar"} />);
    expect(container.querySelectorAll("em").length).toBe(0);
    expect(container.textContent).toContain("a*b*c");
  });

  it("does NOT italicize a_b_c with letters adjacent to the delimiters", () => {
    const { container } = render(<MarkdownText text={"value a_b_c end"} />);
    expect(container.querySelectorAll("em").length).toBe(0);
    expect(container.textContent).toContain("a_b_c");
  });

  it("still bolds __x__ surrounded by spaces", () => {
    const { container } = render(<MarkdownText text={"word __x__ word"} />);
    const strongs = container.querySelectorAll("strong");
    expect(strongs.length).toBe(1);
    expect(strongs[0].textContent).toBe("x");
  });

  it("still italicizes _x_ surrounded by spaces", () => {
    const { container } = render(<MarkdownText text={"word _x_ word"} />);
    const ems = container.querySelectorAll("em");
    expect(ems.length).toBe(1);
    expect(ems[0].textContent).toBe("x");
  });

  it("still italicizes *x* surrounded by spaces", () => {
    const { container } = render(<MarkdownText text={"word *x* word"} />);
    const ems = container.querySelectorAll("em");
    expect(ems.length).toBe(1);
    expect(ems[0].textContent).toBe("x");
  });

  it("still bolds **x** surrounded by spaces", () => {
    const { container } = render(<MarkdownText text={"word **x** word"} />);
    const strongs = container.querySelectorAll("strong");
    expect(strongs.length).toBe(1);
    expect(strongs[0].textContent).toBe("x");
  });
});
