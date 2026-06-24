import { describe, expect, it } from "vitest";
import {
  IMG_CLOSE,
  IMG_OPEN,
  appendImageRefs,
  encodeImageRef,
  hasImageRef,
  parseImageRefs,
  stripImageRefs,
} from "./imageRefs";

const tok = (p: string) => `${IMG_OPEN}${p}${IMG_CLOSE}`;

describe("encodeImageRef", () => {
  it("wraps a path in the sentinel pair", () => {
    expect(encodeImageRef("/a/b.png")).toBe(tok("/a/b.png"));
  });

  it("strips sentinels and newlines from the path", () => {
    expect(encodeImageRef(`/a\n${IMG_OPEN}b.png`)).toBe(tok("/ab.png"));
  });

  it("returns empty string for a blank path", () => {
    expect(encodeImageRef("   ")).toBe("");
  });
});

describe("appendImageRefs", () => {
  it("appends image tokens after the body", () => {
    const out = appendImageRefs("hello", ["/a.png", "/b.png"]);
    expect(out).toBe(`hello\n${tok("/a.png")}\n${tok("/b.png")}`);
  });

  it("returns the body unchanged when there are no paths", () => {
    expect(appendImageRefs("hello", [])).toBe("hello");
  });

  it("emits only tokens when the body is empty", () => {
    expect(appendImageRefs("", ["/a.png"])).toBe(tok("/a.png"));
  });
});

describe("parseImageRefs", () => {
  it("splits text and image segments in order", () => {
    const text = `before ${tok("/a.png")} after`;
    expect(parseImageRefs(text)).toEqual([
      { kind: "text", text: "before " },
      { kind: "image", path: "/a.png" },
      { kind: "text", text: " after" },
    ]);
  });

  it("returns a single text segment when there are no tokens", () => {
    expect(parseImageRefs("plain")).toEqual([{ kind: "text", text: "plain" }]);
  });

  it("handles consecutive image tokens", () => {
    const text = `${tok("/a.png")}${tok("/b.png")}`;
    expect(parseImageRefs(text)).toEqual([
      { kind: "image", path: "/a.png" },
      { kind: "image", path: "/b.png" },
    ]);
  });
});

describe("hasImageRef / stripImageRefs", () => {
  it("detects image references", () => {
    expect(hasImageRef(`x ${tok("/a.png")}`)).toBe(true);
    expect(hasImageRef("x")).toBe(false);
  });

  it("strips image references leaving trimmed prose", () => {
    expect(stripImageRefs(`hi\n${tok("/a.png")}`)).toBe("hi");
  });
});
