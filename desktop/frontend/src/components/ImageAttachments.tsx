import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon } from "./Icon";

/** A pasted clipboard bitmap pending in the composer. ``path`` is the saved
 *  on-disk file (sent to the model as an inline reference); ``dataUrl`` is the
 *  in-memory bitmap used for the thumbnail before send. */
export interface PastedImage {
  path: string;
  name: string;
  dataUrl: string;
}

/** Full-screen overlay showing a single image, dismissable via the top-right
 *  close button, an Esc keypress, or a backdrop click. */
export function ImageLightbox({
  src,
  alt,
  onClose,
}: {
  src: string;
  alt?: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="image-lightbox"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <button
        type="button"
        className="image-lightbox-close"
        aria-label="Close"
        onClick={onClose}
      >
        <Icon name="win-close" size={18} />
      </button>
      <img
        className="image-lightbox-img"
        src={src}
        alt={alt || ""}
        onClick={(e) => e.stopPropagation()}
      />
    </div>
  );
}

interface ThumbProps {
  src: string;
  alt: string;
  onEnlarge: () => void;
  /** Context-menu items; when empty the right-click menu is suppressed. */
  menuItems?: (close: () => void) => MenuItem[];
  /** When set, a quick-delete "x" button is shown at the top-right corner. */
  onRemove?: () => void;
}

/** A single clickable image thumbnail with an optional right-click menu and an
 *  optional top-right quick-delete button. */
export function ImageThumb({ src, alt, onEnlarge, menuItems, onRemove }: ThumbProps) {
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  return (
    <>
      <span className="attachment-thumb-wrap">
        <button
          type="button"
          className="attachment-thumb"
          title={alt}
          onClick={onEnlarge}
          onContextMenu={(e) => {
            if (!menuItems) {
              return;
            }
            e.preventDefault();
            setMenu({ x: e.clientX, y: e.clientY });
          }}
        >
          <img src={src} alt={alt} />
        </button>
        {onRemove && (
          <button
            type="button"
            className="attachment-thumb-remove"
            aria-label="Remove image"
            title="Remove"
            onClick={(e) => {
              e.stopPropagation();
              onRemove();
            }}
          >
            <Icon name="win-close" size={10} />
          </button>
        )}
      </span>
      {menu && menuItems && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems(() => setMenu(null))}
          onClose={() => setMenu(null)}
        />
      )}
    </>
  );
}

/** Copy an image (given by a data URL or fetchable URL) to the clipboard as a
 *  PNG. Best-effort: silently no-ops when the clipboard API is unavailable. */
async function copyImageToClipboard(src: string): Promise<void> {
  try {
    if (!navigator.clipboard || !window.ClipboardItem) {
      return;
    }
    const res = await fetch(src);
    const blob = await res.blob();
    // ClipboardItem wants a stable image type; coerce non-png to png is not
    // possible without a canvas round-trip, so fall back to the original type.
    const type = blob.type || "image/png";
    await navigator.clipboard.write([new ClipboardItem({ [type]: blob })]);
  } catch {
    // Ignore — clipboard write can be blocked by focus / permissions.
  }
}

/** The row of pending image thumbnails shown above the composer input. */
export function AttachmentStrip({
  images,
  onRemove,
}: {
  images: readonly PastedImage[];
  onRemove: (path: string) => void;
}) {
  const [enlarged, setEnlarged] = useState<PastedImage | null>(null);
  if (images.length === 0) {
    return null;
  }
  return (
    <>
      <div className="composer-attachments">
        {images.map((img) => (
          <ImageThumb
            key={img.path}
            src={img.dataUrl}
            alt={img.name || "image"}
            onEnlarge={() => setEnlarged(img)}
            onRemove={() => onRemove(img.path)}
            menuItems={(close) => [
              {
                id: "copy",
                label: "Copy",
                onSelect: () => {
                  void copyImageToClipboard(img.dataUrl);
                  close();
                },
              },
              {
                id: "delete",
                label: "Delete",
                danger: true,
                onSelect: () => {
                  onRemove(img.path);
                  close();
                },
              },
            ]}
          />
        ))}
      </div>
      {enlarged && (
        <ImageLightbox
          src={enlarged.dataUrl}
          alt={enlarged.name}
          onClose={() => setEnlarged(null)}
        />
      )}
    </>
  );
}

/** A thumbnail for an image referenced by on-disk path inside a sent message.
 *  Loads via the backend ``/chat-image`` route and supports enlarge + copy. */
export function SentImageThumb({ path }: { path: string }) {
  const { chatImageUrl } = useApp();
  const [enlarged, setEnlarged] = useState(false);
  const src = chatImageUrl(path);
  const name = path.split(/[\\/]/).pop() || "image";
  return (
    <>
      <ImageThumb
        src={src}
        alt={name}
        onEnlarge={() => setEnlarged(true)}
        menuItems={(close) => [
          {
            id: "copy",
            label: "Copy",
            onSelect: () => {
              void copyImageToClipboard(src);
              close();
            },
          },
        ]}
      />
      {enlarged && (
        <ImageLightbox src={src} alt={name} onClose={() => setEnlarged(false)} />
      )}
    </>
  );
}
