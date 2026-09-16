import type { CSSProperties } from "react";
import panel from "../assets/icons/panel.svg";
import newChat from "../assets/icons/new-chat.svg";
import folder from "../assets/icons/folder.svg";
import folderOpen from "../assets/icons/folder-open.svg";
import pin from "../assets/icons/pin.svg";
import pinFilled from "../assets/icons/pin-filled.svg";
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
import copy from "../assets/icons/copy.svg";
import edit from "../assets/icons/edit.svg";
import fork from "../assets/icons/fork.svg";
import stop from "../assets/icons/stop.svg";
import winMin from "../assets/icons/win-min.svg";
import winMax from "../assets/icons/win-max.svg";
import winRestore from "../assets/icons/win-restore.svg";
import winClose from "../assets/icons/win-close.svg";
import macClose from "../assets/icons/mac-close.svg";
import macMin from "../assets/icons/mac-min.svg";
import macZoom from "../assets/icons/mac-zoom.svg";
import macZoomRestore from "../assets/icons/mac-zoom-restore.svg";
import arrowLeft from "../assets/icons/arrow-left.svg";
import arrowRight from "../assets/icons/arrow-right.svg";
import listCheck from "../assets/icons/list-check.svg";
import circle from "../assets/icons/circle.svg";
import circleSquare from "../assets/icons/circle-square.svg";
import checkCircle from "../assets/icons/check-circle.svg";
import spinner from "../assets/icons/spinner.svg";
import eye from "../assets/icons/eye.svg";
import eyeOff from "../assets/icons/eye-off.svg";
import panelRight from "../assets/icons/panel-right.svg";
import panelBottom from "../assets/icons/panel-bottom.svg";
import cube from "../assets/icons/cube.svg";
import trash from "../assets/icons/trash.svg";
import sparkles from "../assets/icons/sparkles.svg";
import wrench from "../assets/icons/wrench.svg";
import messageSquare from "../assets/icons/message-square.svg";
import paperclip from "../assets/icons/paperclip.svg";
import robot from "../assets/icons/robot.svg";
import terminal from "../assets/icons/terminal.svg";
import archive from "../assets/icons/archive.svg";
import update from "../assets/icons/update.svg";

const SOURCES = {
  panel,
  "new-chat": newChat,
  folder,
  "folder-open": folderOpen,
  pin,
  "pin-filled": pinFilled,
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
  copy,
  edit,
  fork,
  stop,
  "win-min": winMin,
  "win-max": winMax,
  "win-restore": winRestore,
  "win-close": winClose,
  "mac-close": macClose,
  "mac-min": macMin,
  "mac-zoom": macZoom,
  "mac-zoom-restore": macZoomRestore,
  "arrow-left": arrowLeft,
  "arrow-right": arrowRight,
  "list-check": listCheck,
  circle,
  "circle-square": circleSquare,
  "check-circle": checkCircle,
  spinner,
  eye,
  "eye-off": eyeOff,
  "panel-right": panelRight,
  "panel-bottom": panelBottom,
  cube,
  trash,
  sparkles,
  wrench,
  "message-square": messageSquare,
  paperclip,
  robot,
  terminal,
  archive,
  update,
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
