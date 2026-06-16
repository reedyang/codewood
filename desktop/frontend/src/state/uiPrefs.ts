/**
 * GUI-local UI preferences persisted in localStorage.
 *
 * Pin/Archive are presentation-only state for the desktop GUI; they are not
 * part of the backend domain model and therefore never affect the TUI.
 */

const STORAGE_KEY = "codewood.uiPrefs";

export interface UiPrefs {
  pinnedWorkspaceIds: string[];
  pinnedChatIds: string[];
  archivedChatIds: string[];
}

const EMPTY: UiPrefs = {
  pinnedWorkspaceIds: [],
  pinnedChatIds: [],
  archivedChatIds: [],
};

function sanitizeIds(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const out: string[] = [];
  for (const item of value) {
    if (typeof item === "string" && item) {
      out.push(item);
    }
  }
  return out;
}

export function loadUiPrefs(): UiPrefs {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { ...EMPTY };
    }
    const parsed = JSON.parse(raw) as Partial<UiPrefs>;
    return {
      pinnedWorkspaceIds: sanitizeIds(parsed.pinnedWorkspaceIds),
      pinnedChatIds: sanitizeIds(parsed.pinnedChatIds),
      archivedChatIds: sanitizeIds(parsed.archivedChatIds),
    };
  } catch {
    return { ...EMPTY };
  }
}

export function saveUiPrefs(prefs: UiPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage may be unavailable; pin/archive simply won't persist.
  }
}

/** Toggle an id within a list, returning a new array. */
export function toggleId(list: string[], id: string): string[] {
  if (!id) {
    return list;
  }
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}
