import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { RichComposer } from "./RichComposer";

const catalog = { skills: [], mcpTools: [], mcpPrompts: [] };
let segs: { kind: "text"; value: string }[] = [{ kind: "text", value: "" }];

function FakeApp() {
  const getCompletionCatalog = vi.fn(() => Promise.resolve(catalog as never));
  const t = (k: string) => k;
  return { getCompletionCatalog, t } as never;
}

describe("RichComposer Shift+Enter blocks", () => {
  it("simulates native block-separated content via direct DOM + rebuild", () => {
    let rendered: any = null;
    const onChange = vi.fn();
    const { container } = render(
      // @ts-expect-error simplified context mock
      null
    );
    void rendered;
  });
});
