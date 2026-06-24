/** Pure helpers for the embedded-console tab list.
 *
 * The ConsolePanel keeps an ordered list of open console tabs and a single
 * active id. These helpers centralize the add/remove + active-fallback rules so
 * they can be unit-tested without React.
 */

export interface ConsoleTab {
  id: string;
  title: string;
  kind: string;
}

export interface ConsoleTabState {
  tabs: ConsoleTab[];
  activeId: string;
}

/** Append a tab and make it active. */
export function addTab(state: ConsoleTabState, tab: ConsoleTab): ConsoleTabState {
  return { tabs: [...state.tabs, tab], activeId: tab.id };
}

/** Remove a tab; if it was active, fall back to the last remaining tab. */
export function removeTab(state: ConsoleTabState, id: string): ConsoleTabState {
  const tabs = state.tabs.filter((t) => t.id !== id);
  let activeId = state.activeId;
  if (id === state.activeId) {
    activeId = tabs.length ? tabs[tabs.length - 1].id : "";
  }
  return { tabs, activeId };
}

/** Select an existing tab (no-op when the id is unknown). */
export function selectTab(state: ConsoleTabState, id: string): ConsoleTabState {
  if (!state.tabs.some((t) => t.id === id)) {
    return state;
  }
  return { ...state, activeId: id };
}
