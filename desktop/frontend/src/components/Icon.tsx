import type { CSSProperties } from "react";
import panel from "../assets/icons/panel.svg";
import newChat from "../assets/icons/new-chat.svg";
import folder from "../assets/icons/folder.svg";
import folderOpen from "../assets/icons/folder-open.svg";
import pin from "../assets/icons/pin.svg";
import gear from "../assets/icons/gear.svg";
import shield from "../assets/icons/shield.svg";
import plus from "../assets/icons/plus.svg";
import send from "../assets/icons/send.svg";
import chevron from "../assets/icons/chevron.svg";
import search from "../assets/icons/search.svg";
import check from "../assets/icons/check.svg";
import sun from "../assets/icons/sun.svg";
import moon from "../assets/icons/moon.svg";
import monitor from "../assets/icons/monitor.svg";
import info from "../assets/icons/info.svg";
import dots from "../assets/icons/dots.svg";
import stop from "../assets/icons/stop.svg";
import winMin from "../assets/icons/win-min.svg";
import winMax from "../assets/icons/win-max.svg";
import winRestore from "../assets/icons/win-restore.svg";
import winClose from "../assets/icons/win-close.svg";

const SOURCES = {
  panel,
  "new-chat": newChat,
  folder,
  "folder-open": folderOpen,
  pin,
  gear,
  shield,
  plus,
  send,
  chevron,
  search,
  check,
  sun,
  moon,
  monitor,
  info,
  dots,
  stop,
  "win-min": winMin,
  "win-max": winMax,
  "win-restore": winRestore,
  "win-close": winClose,
} as const;

export type IconName = keyof typeof SOURCES;

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

/**
 * Renders a file-based SVG icon as a CSS mask so it inherits the current text
 * color (theme-aware) without being drawn at runtime in code.
 */
export function Icon({ name, size = 16, className }: IconProps) {
  // Quote the URL: Vite inlines SVGs as data URIs whose attributes use single
  // quotes, which would make an unquoted url() token invalid (dropping the mask).
  const url = `url("${SOURCES[name]}")`;
  const style: CSSProperties = {
    width: size,
    height: size,
    WebkitMaskImage: url,
    maskImage: url,
  };
  return <span className={`icon ${className ?? ""}`} style={style} aria-hidden="true" />;
}
