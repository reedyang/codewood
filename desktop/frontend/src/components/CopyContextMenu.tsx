import {
  useCallback,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { useApp } from "../state/AppContext";
import { ContextMenu } from "./ContextMenu";

/**
 * Right-click menu for surfaces where the user selects text by mouse (chat
 * transcript, message input box).
 *
 * With a selection the native menu is replaced by a Copy item (plus Cut when
 * ``onCut`` is provided — editable surfaces). When ``onPaste`` is provided and
 * there is NO selection, a single Paste item is shown instead; without a
 * selection and without a paste handler the native menu is left alone so the
 * browser's normal actions (translate, inspect…) keep working.
 */
export function useCopyContextMenu({
  onPaste,
  onCut,
}: {
  onPaste?: () => void;
  onCut?: () => void;
} = {}) {
  const { t } = useApp();
  const [menu, setMenu] = useState<{
    x: number;
    y: number;
    hasSelection: boolean;
  } | null>(null);

  const onContextMenu = useCallback((e: ReactMouseEvent) => {
    const sel = window.getSelection();
    const scoped =
      !!sel?.anchorNode && e.currentTarget.contains(sel.anchorNode);
    const hasSelection =
      !!sel &&
      !sel.isCollapsed &&
      !!sel.toString().trim() &&
      scoped;
    if (!hasSelection && !onPaste) {
      return;
    }
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, hasSelection });
  }, [onPaste]);

  const copySelection = useCallback(() => {
    const text = window.getSelection()?.toString() ?? "";
    if (text) {
      void navigator.clipboard?.writeText(text);
    }
  }, []);

  const pasteFromMenu = useCallback(() => {
    onPaste?.();
  }, [onPaste]);

  const cutFromMenu = useCallback(() => {
    onCut?.();
  }, [onCut]);

  const menuNode: ReactNode = menu ? (
    <ContextMenu
      x={menu.x}
      y={menu.y}
      items={(() => {
        if (!menu.hasSelection) {
          return [{ id: "paste", label: t("msg.paste"), onSelect: pasteFromMenu }];
        }
        const items = [{ id: "copy", label: t("msg.copy"), onSelect: copySelection }];
        if (onCut) {
          items.push({ id: "cut", label: t("msg.cut"), onSelect: cutFromMenu });
        }
        return items;
      })()}
      onClose={() => setMenu(null)}
    />
  ) : null;

  return { onContextMenu, menuNode };
}
