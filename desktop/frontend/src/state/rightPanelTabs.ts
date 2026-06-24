/**
 * Right-panel tab preferences (GUI-local, persisted in localStorage).
 *
 * The right panel is a tab container. Each tab has a stable id; the user picks
 * which tabs are visible via the "+" menu. Visibility and the active tab are
 * presentation-only state, so they live in localStorage (like uiPrefs) rather
 * than the backend domain model.
 */

export type RightPanelTabId = "todos" | "browser";

/** All known tabs in display order. "todos" is always first and is the
 *  mandatory tab (it can never be hidden to an empty tab bar). */
export const RIGHT_PANEL_TABS: readonly RightPanelTabId[] = ["todos", "browser"];

/** Tabs that must always remain visible (cannot be toggled off). */
export const MANDATORY_TABS: readonly RightPanelTabId[] = ["todos"];

export interface RightPanelPrefs {
  visible: RightPanelTabId[];
  active: RightPanelTabId;
}

const STORAGE_KEY = "codewood.rightPanelTabs";

const DEFAULT_PREFS: RightPanelPrefs = {
  visible: ["todos"],
  active: "todos",
};

function isTabId(value: unknown): value is RightPanelTabId {
  return (
    typeof value === "string" &&
    (RIGHT_PANEL_TABS as readonly string[]).includes(value)
  );
}

function sanitize(parsed: Partial<RightPanelPrefs>): RightPanelPrefs {
  const visibleRaw = Array.isArray(parsed.visible) ? parsed.visible : [];
  // Keep canonical order and dedupe; always force the mandatory tabs in.
  const visibleSet = new Set<RightPanelTabId>();
  for (const id of MANDATORY_TABS) {
    visibleSet.add(id);
  }
  for (const v of visibleRaw) {
    if (isTabId(v)) {
      visibleSet.add(v);
    }
  }
  const visible = RIGHT_PANEL_TABS.filter((id) => visibleSet.has(id));
  let active: RightPanelTabId = isTabId(parsed.active)
    ? parsed.active
    : visible[0];
  if (!visible.includes(active)) {
    active = visible[0];
  }
  return { visible, active };
}

export function loadRightPanelPrefs(): RightPanelPrefs {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { ...DEFAULT_PREFS, visible: [...DEFAULT_PREFS.visible] };
    }
    return sanitize(JSON.parse(raw) as Partial<RightPanelPrefs>);
  } catch {
    return { ...DEFAULT_PREFS, visible: [...DEFAULT_PREFS.visible] };
  }
}

export function saveRightPanelPrefs(prefs: RightPanelPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage may be unavailable; tab prefs simply won't persist.
  }
}

/** Toggle a tab's visibility. Mandatory tabs cannot be hidden, and the result
 *  always keeps at least one visible tab. Returns sanitized prefs (with a
 *  valid active tab). */
export function toggleTabVisibility(
  prefs: RightPanelPrefs,
  id: RightPanelTabId,
): RightPanelPrefs {
  if ((MANDATORY_TABS as readonly string[]).includes(id)) {
    return prefs;
  }
  const currentlyVisible = prefs.visible.includes(id);
  const nextVisible = currentlyVisible
    ? prefs.visible.filter((t) => t !== id)
    : [...prefs.visible, id];
  return sanitize({ visible: nextVisible, active: prefs.active });
}
