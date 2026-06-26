import { type CSSProperties, type ReactNode } from "react";

// SGR-only ANSI renderer. The backend forwards step output with its color
// runs intact (cursor/erase escapes are dropped server-side) so the GUI can
// reproduce the terminal's coloring — most importantly the green/red
// success-or-failure bullet before "Ran" / "You ran".

const SGR_RE = /\x1b\[([0-9;]*)m/g;

// 16-color palette tuned to read well on both themes.
const BASIC_FG: Record<number, string> = {
  30: "#3b4252",
  31: "#cd3131",
  32: "#13a10e",
  33: "#b58900",
  34: "#2472c8",
  35: "#bc3fbc",
  36: "#11a8cd",
  37: "#cfd2d6",
  90: "#8a8f98",
  91: "#f14c4c",
  92: "#23d18b",
  93: "#c9a300",
  94: "#3b8eea",
  95: "#d670d6",
  96: "#29b8db",
  97: "#ffffff",
};

interface Style {
  color?: string;
  fontWeight?: number;
  fontStyle?: string;
  textDecoration?: string;
  opacity?: number;
}

function applyCodes(style: Style, codes: number[]): Style {
  const next: Style = { ...style };
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i];
    if (c === 0) {
      next.color = undefined;
      next.fontWeight = undefined;
      next.fontStyle = undefined;
      next.textDecoration = undefined;
      next.opacity = undefined;
    } else if (c === 1) {
      next.fontWeight = 700;
    } else if (c === 2) {
      next.opacity = 0.7;
    } else if (c === 3) {
      next.fontStyle = "italic";
    } else if (c === 4) {
      next.textDecoration = "underline";
    } else if (c === 22) {
      next.fontWeight = undefined;
      next.opacity = undefined;
    } else if (c === 23) {
      next.fontStyle = undefined;
    } else if (c === 24) {
      next.textDecoration = undefined;
    } else if (c === 39) {
      next.color = undefined;
    } else if (c === 38) {
      const mode = codes[i + 1];
      if (mode === 2) {
        next.color = `rgb(${codes[i + 2] || 0},${codes[i + 3] || 0},${codes[i + 4] || 0})`;
        i += 4;
      } else if (mode === 5) {
        i += 2;
      }
    } else if (c === 48) {
      const mode = codes[i + 1];
      i += mode === 2 ? 4 : mode === 5 ? 2 : 0;
    } else if (BASIC_FG[c]) {
      next.color = BASIC_FG[c];
    }
  }
  return next;
}

export function AnsiText({
  text,
  onPathPreview,
}: {
  text: string;
  onPathPreview?: (path: string) => void;
}): ReactNode {
  const nodes: ReactNode[] = [];
  let style: Style = {};
  let last = 0;
  let key = 0;
  const re = new RegExp(SGR_RE);
  let m: RegExpExecArray | null;

  const push = (chunk: string, s: Style) => {
    if (!chunk) {
      return;
    }
    // Preview-path link: if a handler is registered and the chunk wraps
    // ``(path=...)`` (the detail suffix from _tool_action_detail), render
    // the path value as a clickable anchor inside the styled span.
    if (onPathPreview) {
      const pm = chunk.match(/^([\s\S]*?)\(path=([^)]+)\)([\s\S]*)$/);
      if (pm) {
        const linkStyle: CSSProperties = {
          ...(s as CSSProperties),
          textDecoration: "underline",
          cursor: "pointer",
          color: "#3584e4",
        };
        const pushSimple = (txt: string, st: Style) => {
          if (!txt) {
            return;
          }
          nodes.push(
            <span key={key++} style={st as CSSProperties}>
              {txt}
            </span>,
          );
        };
        pushSimple(pm[1] + "(path=", s);
        nodes.push(
          <a
            key={key++}
            href="#"
            style={linkStyle}
            onClick={(e) => {
              e.preventDefault();
              onPathPreview(pm[2]);
            }}
          >
            {pm[2]}
          </a>,
        );
        pushSimple(")" + pm[3], s);
        return;
      }
    }
    const hasStyle = Object.values(s).some((v) => v !== undefined);
    nodes.push(
      hasStyle ? (
        <span key={key++} style={s as CSSProperties}>
          {chunk}
        </span>
      ) : (
        <span key={key++}>{chunk}</span>
      ),
    );
  };

  while ((m = re.exec(text)) !== null) {
    push(text.slice(last, m.index), style);
    const codes = m[1] === "" ? [0] : m[1].split(";").map((x) => parseInt(x, 10) || 0);
    style = applyCodes(style, codes);
    last = re.lastIndex;
  }
  push(text.slice(last), style);
  return <>{nodes}</>;
}
