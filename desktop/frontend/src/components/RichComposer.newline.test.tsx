import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import type { AppState, ServerEvent } from "../api/types";
import { RichComposer } from "./RichComposer";
import { AppProvider } from "../state/AppContext";

const apiMock = vi.hoisted(() => {
  let eventHandler: ((event: ServerEvent) => void) | null = null;
  const getState = vi.fn<() => Promise<AppState>>();
  const connectEvents = vi.fn((handler: (event: ServerEvent) => void) => {
    eventHandler = handler;
    return { close: vi.fn() } as unknown as EventSource;
  });
  const getChatHistory = vi.fn(async () => ({ turns: [], start: 0, total: 0 }));
  const getCompletionCatalog = vi.fn(async () => ({ skills: [], mcpTools: [], mcpPrompts: [] }));
  return {
    getState, connectEvents, getChatHistory, getCompletionCatalog,
    emit(event: ServerEvent) { eventHandler?.(event); },
    reset() { eventHandler = null; getState.mockReset(); getChatHistory.mockClear(); },
  };
});

vi.mock("../api/client", () => ({
  ApiClient: class {
    port = ""; token = "";
    getState = apiMock.getState;
    connectEvents = apiMock.connectEvents;
    getChatHistory = apiMock.getChatHistory;
    getCompletionCatalog = apiMock.getCompletionCatalog;
    constructor() {
      return new Proxy(this, {
        get: (target, prop) =>
          prop in target ? (target as never)[prop] : (async () => ({})) as never,
      });
    }
  },
}));

function buildState(): AppState {
  return {
    app: { name: "Code Wood", version: "test" },
    workspace: { id: "ws-1", name: "Workspace", root: "D:/workspace", workDirectory: "D:/workspace" },
    workspaces: [{ id: "ws-1", name: "Workspace", root: "D:/workspace", active: true, isDefault: false }],
    chats: [{ index: 0, id: "chat-1", name: "Chat 1", messageCount: 0, active: true, running: false, planMode: false, model: "provider/model" }],
    activeChatId: "chat-1",
    model: { current: "provider/model", available: ["provider/model"], ready: true, reasoningEffort: "", reasoningEfforts: [] },
    language: "en",
    executionPolicy: "default",
  };
}

describe("RichComposer newline handling", () => {
  const originalConsole = { ...console };
  beforeEach(() => {
    console.error = vi.fn();
    console.warn = vi.fn();
    console.log = vi.fn();
  });
  afterEach(() => {
    Object.assign(console, originalConsole);
  });

  it("preserves a native Shift+Enter <div> block boundary as a newline", async () => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
    const onChange = vi.fn();
    const utils = render(
      <AppProvider>
        <RichComposer segments={[{ kind: "text", value: "line1" }]} onChange={onChange} onSubmit={() => {}} />
      </AppProvider>,
    );
    const root = utils.container.querySelector(".rich-composer-editor") as HTMLElement;
    // macOS Shift+Enter inserts a nested <div> block node at the caret, which
    // sits just after the last text node ("line1").
    const block = document.createElement("div");
    block.textContent = "line2";
    root.appendChild(block);
    // A no-op input round-trips the DOM through readSegmentsFromDom (onChange).
    fireEvent.input(root, { type: "input", data: "x", inputType: "insertText" });
    // The block boundary must be flattened to "\n" in the model, joined with the
    // surrounding text nodes in DOM order.
    const calls = onChange.mock.calls;
    const lastSeen = calls.length ? calls[calls.length - 1][0] : [];
    const joined = lastSeen.map((s: any) => s.value).join("");
    expect(joined).toContain("line1\nline2");
  });

  it("cut across multi-line <div> blocks deletes exactly the selected range", async () => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
    const onChange = vi.fn();
    const utils = render(
      <AppProvider>
        <RichComposer segments={[{ kind: "text", value: "" }]} onChange={onChange} onSubmit={() => {}} />
      </AppProvider>,
    );
    const root = utils.container.querySelector(".rich-composer-editor") as HTMLElement;
    await act(async () => { await new Promise((r) => window.setTimeout(r, 5)); });
    // macOS renders each line as its own <div> block; select the middle line's
    // contents ("line2") so the offset math must account for the block boundaries.
    root.innerHTML = "";
    const d1 = document.createElement("div");
    d1.textContent = "line1";
    const d2 = document.createElement("div");
    d2.textContent = "line2";
    const d3 = document.createElement("div");
    d3.textContent = "line3";
    root.appendChild(d1);
    root.appendChild(d2);
    root.appendChild(d3);
    root.appendChild(document.createTextNode(""));
    const range = document.createRange();
    range.setStart(d2.firstChild as Node, 0);
    range.setEnd(d2.firstChild as Node, 5);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    await act(async () => {
      fireEvent.cut(root, { clipboardData: { setData: vi.fn(), getData: () => "" } as any });
    });
    const lastSeen = onChange.mock.calls.length
      ? onChange.mock.calls[onChange.mock.calls.length - 1][0]
      : [];
    const joined = lastSeen.map((s: any) => s.value).join("");
    // Only "line2" is removed; the surrounding blocks keep their newlines, so
    // the middle line collapses to a blank line (both boundaries remain).
    expect(joined).toBe("line1\n\nline3");
  });
});
