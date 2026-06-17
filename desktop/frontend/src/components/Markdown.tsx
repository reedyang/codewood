import { createElement, type ReactNode } from "react";

// Compact, dependency-free Markdown renderer. It mirrors the structure the
// terminal highlights (headings, emphasis, inline/fenced code, lists, quotes,
// links) so the model reply is colored in the GUI without a heavy dependency.
// All text flows through React children, so it is escaped by default.

const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)|(~~[^~]+~~)|(\[[^\]]+\]\([^)]+\))/g;

function safeHref(url: string): string | null {
  const u = url.trim();
  return /^(https?:|mailto:)/i.test(u) ? u : null;
}

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = new RegExp(INLINE_RE);
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      nodes.push(text.slice(last, m.index));
    }
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (tok.startsWith("`")) {
      nodes.push(
        <code key={k} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      nodes.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("~~")) {
      nodes.push(<del key={k}>{tok.slice(2, -2)}</del>);
    } else if (tok.startsWith("*") || tok.startsWith("_")) {
      nodes.push(<em key={k}>{tok.slice(1, -1)}</em>);
    } else if (tok.startsWith("[")) {
      const mm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      const href = mm ? safeHref(mm[2]) : null;
      if (mm && href) {
        nodes.push(
          <a key={k} href={href} target="_blank" rel="noreferrer noopener">
            {mm[1]}
          </a>,
        );
      } else {
        nodes.push(tok);
      }
    }
    last = re.lastIndex;
  }
  if (last < text.length) {
    nodes.push(text.slice(last));
  }
  return nodes;
}

export function MarkdownText({ text }: { text: string }): ReactNode {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line.trim())) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        buf.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      blocks.push(
        <pre key={key++} className="md-pre">
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length, 6);
      blocks.push(
        createElement(
          `h${level}`,
          { key: key++, className: "md-h" },
          renderInline(heading[2], `h${key}`),
        ),
      );
      i++;
      continue;
    }

    if (/^\s*([-*_])(\s*\2){2,}\s*$/.test(line)) {
      blocks.push(<hr key={key++} className="md-hr" />);
      i++;
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        const content = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "");
        items.push(<li key={items.length}>{renderInline(content, `li${key}-${items.length}`)}</li>);
        i++;
      }
      blocks.push(
        ordered ? (
          <ol key={key++} className="md-list">
            {items}
          </ol>
        ) : (
          <ul key={key++} className="md-list">
            {items}
          </ul>
        ),
      );
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={key++} className="md-quote">
          {renderInline(buf.join(" "), `q${key}`)}
        </blockquote>,
      );
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i].trim()) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i])
    ) {
      para.push(lines[i]);
      i++;
    }
    const inlineNodes: ReactNode[] = [];
    para.forEach((p, idx) => {
      if (idx > 0) {
        inlineNodes.push(<br key={`br-${key}-${idx}`} />);
      }
      inlineNodes.push(...renderInline(p, `p${key}-${idx}`));
    });
    blocks.push(
      <p key={key++} className="md-p">
        {inlineNodes}
      </p>,
    );
  }

  return <div className="md">{blocks}</div>;
}
