import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AppState, ServerEvent } from "../api/types";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!(globalThis as { ResizeObserver?: unknown }).ResizeObserver) {
  (globalThis as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
}

const apiMock = vi.hoisted(() => {
  let eventHandler: ((event: ServerEvent) => void) | null = null;
  const getState = vi.fn<() => Promise<AppState>>();
  const connectEvents = vi.fn((handler: (event: ServerEvent) => void) => {
    eventHandler = handler;
    return { close: vi.fn() } as unknown as EventSource;
  });
  const getChatHistory = vi.fn(async () => ({ turns: [], start: 0, total: 0 }));
  const listWorkspaceChats = vi.fn(async () => []);
  const newChat = vi.fn(async () => "chat-2");
  const selectChat = vi.fn(async () => true);
  const sendInput = vi.fn(async () => undefined);
  const setChatModel = vi.fn(async () => true);
  const setChatReasoning = vi.fn(async () => true);
  const savePendingInputs = vi.fn(async () => true);
  const pause = vi.fn(async () => undefined);
  const syncModelPresets = vi.fn(async () => undefined);
  const deleteChat = vi.fn(async () => true);
  const deleteWorkspace = vi.fn(async () => ({ ok: true, id: "", wasActive: false, fallbackId: "" }));
  const setPlanMode = vi.fn(async () => true);
  const answerAskMoreInfo = vi.fn(async () => undefined);
  const pasteImage = vi.fn(async () => ({ path: "x.png", name: "x.png" }));
  const saveDraftAttachment = vi.fn(async () => ({ path: "x.png", name: "x.png" }));
  const materializeDraftAttachments = vi.fn(async () => ({}));
  return {
    getState, connectEvents, getChatHistory, listWorkspaceChats, newChat, selectChat,
    sendInput, setChatModel, setChatReasoning, savePendingInputs, pause, syncModelPresets,
    deleteChat, deleteWorkspace, setPlanMode, answerAskMoreInfo, pasteImage,
    saveDraftAttachment, materializeDraftAttachments,
    emit(event: ServerEvent) {
      if (!eventHandler) throw new Error("Event handler not connected");
      eventHandler(event);
    },
    reset() {
      eventHandler = null;
      getState.mockReset();
      connectEvents.mockClear();
      getChatHistory.mockClear();
      sendInput.mockClear();
      setPlanMode.mockClear();
    },
  };
});

vi.mock("../api/client", () => ({
  ApiClient: class {
    port = "";
    token = "";
    getState = apiMock.getState;
    connectEvents = apiMock.connectEvents;
    getChatHistory = apiMock.getChatHistory;
    listWorkspaceChats = apiMock.listWorkspaceChats;
    newChat = apiMock.newChat;
    selectChat = apiMock.selectChat;
    sendInput = apiMock.sendInput;
    setChatModel = apiMock.setChatModel;
    setChatReasoning = apiMock.setChatReasoning;
    savePendingInputs = apiMock.savePendingInputs;
    pause = apiMock.pause;
    syncModelPresets = apiMock.syncModelPresets;
    deleteChat = apiMock.deleteChat;
    deleteWorkspace = apiMock.deleteWorkspace;
    setPlanMode = apiMock.setPlanMode;
    answerAskMoreInfo = apiMock.answerAskMoreInfo;
    pasteImage = apiMock.pasteImage;
    saveDraftAttachment = apiMock.saveDraftAttachment;
    materializeDraftAttachments = apiMock.materializeDraftAttachments;
    constructor(port?: string, token?: string) {
      if (port != null && token != null) {
        this.port = String(port);
        this.token = token;
      }
      return new Proxy(this, {
        get: (target, prop) =>
          prop in target ? (target as never)[prop] : (async () => ({})) as never,
      });
    }
  },
}));

import { AppProvider } from "../state/AppContext";
import { ChatView } from "./ChatView";

function buildState(overrides: Partial<AppState> = {}): AppState {
  const base: AppState = {
    app: { name: "Code Wood", version: "test" },
    workspace: { id: "ws-1", name: "Workspace", root: "D:/workspace", workDirectory: "D:/workspace" },
    workspaces: [{ id: "ws-1", name: "Workspace", root: "D:/workspace", active: true, isDefault: false }],
    chats: [{
      index: 0, id: "chat-1", name: "Chat 1", messageCount: 0, active: true,
      running: false, archived: false, planMode: false, model: "provider/model",
    }],
    activeChatId: "chat-1",
    model: { current: "provider/model", available: ["provider/model"], ready: true, reasoningEffort: "", reasoningEfforts: [] },
    language: "en",
    executionPolicy: "default",
  };
  return { ...base, ...overrides };
}

const PLAN_A = "<proposed_plan>\n# Plan A\n- step one\n- step two\n</proposed_plan>";
const PLAN_B = "<proposed_plan>\n# Plan B (revised)\n- step one\n- step two\n- step three\n</proposed_plan>";

function idleState(messageCount: number, planMode: boolean, running = false) {
  return buildState({
    chats: [{
      index: 0, id: "chat-1", name: "Chat 1", messageCount, active: true,
      running, archived: false, planMode, model: "provider/model",
    }],
  });
}

function emitTurnStart(text: string) {
  apiMock.emit({ event: "turn_start", data: { text, chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitRoundStart() {
  apiMock.emit({ event: "round_start", data: { chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitAssistant(text: string) {
  apiMock.emit({ event: "assistant", data: { text, chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitOutput(text: string) {
  apiMock.emit({ event: "output", data: { text, chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitThinking(text: string) {
  apiMock.emit({ event: "thinking", data: { text, chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitRoundEnd() {
  apiMock.emit({ event: "round_end", data: { chatId: "chat-1", workspaceId: "ws-1" } });
}
function emitIdle(state: AppState) {
  apiMock.emit({ event: "idle", data: { chatId: "chat-1", workspaceId: "ws-1", state } });
}

describe("plan execute row across a revision", () => {
  beforeEach(() => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
  });

  it("shows the execute row after a revision whose plan arrives as step (output) segments", async () => {
    render(
      <AppProvider>
        <ChatView />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Plan A turn: streamed as assistant (answer) segments with raw tags.
    act(() => {
      emitTurnStart("draft a plan");
      emitRoundStart();
      emitThinking("thinking about the plan...");
      emitAssistant("调研完成。已理清 shell 执行链路。");
      emitAssistant(PLAN_A);
      emitRoundEnd();
      emitIdle(idleState(1, false));
    });

    await waitFor(() => {
      expect(screen.queryByText("Yes, implement this plan")).toBeTruthy();
    });
    console.log("[step1] execute row visible after plan A: OK");

    await act(async () => {
      fireEvent.click(screen.getByText(/No, and tell/));
    });
    await waitFor(() => {
      expect(screen.queryByText("Yes, implement this plan")).toBeNull();
    });
    console.log("[step2] row hidden after No: OK");

    // Revision turn: plan B arrives via OUTPUT (step) events — no raw tags in
    // answer segments; history reload stays stale (empty page).
    act(() => {
      emitTurnStart("revise: rename users");
      emitRoundStart();
      emitThinking("revising the plan...");
      emitOutput("改名收到。我按一致拼写处理。");
      emitOutput(PLAN_B);
      emitRoundEnd();
      emitIdle(idleState(2, false));
    });

    console.log("[step3] revision turn ended; execute row should be visible...");
    await waitFor(
      () => {
        expect(screen.queryByText("Yes, implement this plan")).toBeTruthy();
      },
      { timeout: 5000 },
    );
    console.log("[step3] execute row visible after revision plan B: FIXED");
  }, 30000);
});

describe("transcript scroll anchoring after task completion", () => {
  beforeEach(() => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
  });

  it("keeps the viewport when the post-completion history reload lands while the user is reading earlier messages", async () => {
    let currentScrollTop = 0;
    const scrollHeightDesc = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollHeight",
    );
    const scrollTopDesc = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollTop",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        return 2000;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTop", {
      configurable: true,
      get() {
        return currentScrollTop;
      },
      set(value) {
        currentScrollTop = Number(value);
      },
    });

    try {
      // History content: a FRESH array identity on every fetch, so the
      // post-completion reload is a real "replacement" from React's view
      // (same content, new array), not a bailed-out no-op.
      apiMock.getChatHistory.mockImplementation(async () => ({
        turns: [
          {
            userText: "hello",
            rounds: [{ waitSeconds: 0, text: "hi", tools: "" }],
            timestamp: new Date().toISOString(),
          },
        ],
        start: 0,
        total: 1,
      }));

      render(
        <AppProvider>
          <ChatView />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
      await waitFor(() => expect(apiMock.getChatHistory).toHaveBeenCalled());
      await waitFor(() =>
        expect(document.querySelector(".transcript")).toBeTruthy(),
      );
      // Initial load pins the transcript to the bottom.
      expect(currentScrollTop).toBe(2000);

      // The user scrolls up to read earlier messages (away from the bottom).
      const transcript = document.querySelector(
        ".transcript",
      ) as HTMLElement;
      act(() => {
        currentScrollTop = 500;
        fireEvent.scroll(transcript);
      });

      // Task completes: the backend idle event triggers a history reload
      // that replaces the turns array.
      act(() => {
        emitIdle(idleState(1, false));
      });
      await waitFor(() => {
        expect(apiMock.getChatHistory.mock.calls.length).toBeGreaterThanOrEqual(
          2,
        );
      });

      // The reload must NOT yank the viewport back to the bottom.
      expect(currentScrollTop).toBeGreaterThanOrEqual(400);
      expect(currentScrollTop).toBeLessThan(2000);
    } finally {
      if (scrollHeightDesc) {
        Object.defineProperty(HTMLElement.prototype, "scrollHeight", scrollHeightDesc);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollHeight;
      }
      if (scrollTopDesc) {
        Object.defineProperty(HTMLElement.prototype, "scrollTop", scrollTopDesc);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTop;
      }
    }
  }, 30000);
});

describe("transcript minimap vertical centering", () => {
  beforeEach(() => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
  });

  it("centers the minimap across the transcript plus the composer dock", async () => {
    // Layout: chat-view spans 0..1056, title bar 0..44, transcript 44..800,
    // composer dock (todo list / pending tasks / input box) 800..1056.
    // Content is tall enough (scrollHeight > 3 * clientHeight) to show the
    // minimap, and there are 3 user turns so 3 minimap lines render.
    const scrollHeightDesc = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollHeight",
    );
    const clientHeightDesc = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "clientHeight",
    );
    const rectDesc = Object.getOwnPropertyDescriptor(
      Element.prototype,
      "getBoundingClientRect",
    );
    const rect = (top: number, bottom: number) => ({
      top,
      bottom,
      height: bottom - top,
      left: 0,
      right: 800,
      width: 800,
      x: 0,
      y: top,
      toJSON: () => ({}),
    });

    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        return 3000;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get() {
        return 800;
      },
    });
    Object.defineProperty(Element.prototype, "getBoundingClientRect", {
      configurable: true,
      value(this: Element) {
        const cls = String((this as HTMLElement).className || "");
        if (cls.includes("chat-view")) return rect(0, 1056);
        if (cls.includes("composer-dock")) return rect(800, 1056);
        if (cls.includes("transcript")) return rect(44, 800);
        return rect(0, 0);
      },
    });

    try {
      apiMock.getChatHistory.mockImplementation(async () => ({
        turns: [
          { userText: "one", rounds: [{ waitSeconds: 0, text: "a", tools: "" }], timestamp: new Date().toISOString() },
          { userText: "two", rounds: [{ waitSeconds: 0, text: "b", tools: "" }], timestamp: new Date().toISOString() },
          { userText: "three", rounds: [{ waitSeconds: 0, text: "c", tools: "" }], timestamp: new Date().toISOString() },
        ],
        start: 0,
        total: 3,
      }));

      render(
        <AppProvider>
          <ChatView />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
      await waitFor(() => expect(apiMock.getChatHistory).toHaveBeenCalled());
      await waitFor(() =>
        expect(document.querySelector(".transcript-minimap")).toBeTruthy(),
      );

      const minimap = document.querySelector(
        ".transcript-minimap",
      ) as HTMLElement;
      // Region = transcript top (44) .. composer dock bottom (1056) => 1012px.
      // 3 lines * 9px pitch = 27px; top = 44 + (1012 - 27) / 2 = 536.5.
      // Centering against the transcript alone would give 44 + (756-27)/2.
      expect(minimap.style.top).toBe("536.5px");
      expect(minimap.style.height).toBe("27px");
      expect(minimap.querySelectorAll(".minimap-line")).toHaveLength(3);
    } finally {
      if (scrollHeightDesc) {
        Object.defineProperty(HTMLElement.prototype, "scrollHeight", scrollHeightDesc);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollHeight;
      }
      if (clientHeightDesc) {
        Object.defineProperty(HTMLElement.prototype, "clientHeight", clientHeightDesc);
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).clientHeight;
      }
      if (rectDesc) {
        Object.defineProperty(Element.prototype, "getBoundingClientRect", rectDesc);
      } else {
        delete (Element.prototype as Partial<Element>).getBoundingClientRect;
      }
    }
  }, 30000);
});
