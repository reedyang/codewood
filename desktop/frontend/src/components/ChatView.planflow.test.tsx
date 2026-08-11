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
