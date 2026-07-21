import { useState, useMemo, useEffect, useRef } from "react";
import { FileChangeRecord, DiffRow } from "../api/types";
import { Code, langFromPath } from "./DiffPreview";

interface FileChangeDetailsProps {
  file: FileChangeRecord;
  t: (key: string, params?: Record<string, string | number>) => string;
}

const LINES_PER_CHUNK = 20;
const SIDE_BY_SIDE_MIN_WIDTH = 760;

export function FileChangeDetails({ file, t }: FileChangeDetailsProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [wide, setWide] = useState(true);
  const [visibleUnmodified, setVisibleUnmodified] = useState<Record<number, number>>({});

  const patch = file.patch || [];
  const lang = useMemo(() => langFromPath(file.filePath), [file.filePath]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const apply = (width: number) => setWide(width >= SIDE_BY_SIDE_MIN_WIDTH);
    apply(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) apply(entry.contentRect.width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const blocks = useMemo(() => {
    const result: Array<{
      type: "change" | "unmodified";
      rows: DiffRow[];
      startIndex: number;
    }> = [];

    let currentUnmodified: DiffRow[] = [];
    let currentChange: DiffRow[] = [];
    let startIndex = 0;

    const flushChange = () => {
      if (currentChange.length > 0) {
        result.push({ type: "change", rows: currentChange, startIndex });
        currentChange = [];
      }
    };

    const flushUnmodified = () => {
      if (currentUnmodified.length > 0) {
        result.push({ type: "unmodified", rows: currentUnmodified, startIndex });
        currentUnmodified = [];
      }
    };

    for (let i = 0; i < patch.length; i++) {
      const row = patch[i];
      if (row.type === "context") {
        flushChange();
        if (currentUnmodified.length === 0) {
          startIndex = i;
        }
        currentUnmodified.push(row);
      } else {
        flushUnmodified();
        if (currentChange.length === 0) {
          startIndex = i;
        }
        currentChange.push(row);
      }
    }

    flushChange();
    flushUnmodified();

    return result;
  }, [patch]);

  const getVisibleCount = (blockIndex: number) => visibleUnmodified[blockIndex] || 0;

  const expandUnmodified = (blockIndex: number) => {
    setVisibleUnmodified((prev) => ({
      ...prev,
      [blockIndex]: (prev[blockIndex] || 0) + LINES_PER_CHUNK,
    }));
  };

  const renderChangeBlockSideBySide = (block: { rows: DiffRow[] }, blockIndex: number) => (
    <div key={blockIndex} className="diff-preview">
      <div className="diff-side-by-side">
        {block.rows.map((row, rowIndex) => {
          if (row.type === "omitted") {
            return (
              <div key={`${blockIndex}-${rowIndex}`} className="diff-row diff-row-omitted">
                <span className="diff-omitted-text">{row.oldText}</span>
              </div>
            );
          }
          const leftCls = row.type === "del" || row.type === "change" ? "diff-del" : "";
          const rightCls = row.type === "add" || row.type === "change" ? "diff-add" : "";
          return (
            <div key={`${blockIndex}-${rowIndex}`} className="diff-row">
              <div className={`diff-cell diff-cell-old ${leftCls}`}>
                <span className="diff-lineno">{row.oldNo ?? ""}</span>
                <Code text={row.oldText} lang={lang} />
              </div>
              <div className={`diff-cell diff-cell-new ${rightCls}`}>
                <span className="diff-lineno">{row.newNo ?? ""}</span>
                <Code text={row.newText} lang={lang} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );

  const renderChangeBlockInline = (block: { rows: DiffRow[] }, blockIndex: number) => (
    <div key={blockIndex} className="diff-preview">
      <div className="diff-inline">
        {block.rows.map((row, rowIndex) => {
          if (row.type === "omitted") {
            return (
              <div key={`${blockIndex}-${rowIndex}`} className="diff-iline diff-row-omitted">
                <span className="diff-omitted-text">{row.oldText}</span>
              </div>
            );
          }
          if (row.type === "context") {
            return (
              <div key={`${blockIndex}-${rowIndex}`} className="diff-iline">
                <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
                <span className="diff-sign"> </span>
                <Code text={row.newText || row.oldText} lang={lang} />
              </div>
            );
          }
          return (
            <>
              {row.type === "del" || row.type === "change" ? (
                <div key={`${blockIndex}-${rowIndex}-del`} className="diff-iline diff-del">
                  <span className="diff-lineno">{row.oldNo ?? ""}</span>
                  <span className="diff-sign">-</span>
                  <Code text={row.oldText} lang={lang} />
                </div>
              ) : null}
              {row.type === "add" || row.type === "change" ? (
                <div key={`${blockIndex}-${rowIndex}-add`} className="diff-iline diff-add">
                  <span className="diff-lineno">{row.newNo ?? ""}</span>
                  <span className="diff-sign">+</span>
                  <Code text={row.newText} lang={lang} />
                </div>
              ) : null}
            </>
          );
        })}
      </div>
    </div>
  );

  const renderUnmodifiedBlockSideBySide = (block: { rows: DiffRow[] }, blockIndex: number) => {
    const totalRows = block.rows.length;
    const visibleCount = getVisibleCount(blockIndex);

    if (visibleCount === 0) {
      const topN = Math.min(3, totalRows);
      const bottomN = Math.min(3, totalRows - topN);
      const topRows = block.rows.slice(0, topN);
      const bottomRows = bottomN > 0 ? block.rows.slice(totalRows - bottomN) : [];
      const middleHidden = totalRows - topN - bottomN;

      if (middleHidden <= 0) {
        return (
          <div key={blockIndex} className="diff-preview">
            <div className="diff-side-by-side">
              {block.rows.map((row, rowIndex) => (
                <div key={`${blockIndex}-${rowIndex}`} className="diff-row">
                  <div className="diff-cell diff-cell-old">
                    <span className="diff-lineno">{row.oldNo ?? ""}</span>
                    <Code text={row.oldText} lang={lang} />
                  </div>
                  <div className="diff-cell diff-cell-new">
                    <span className="diff-lineno">{row.newNo ?? ""}</span>
                    <Code text={row.newText} lang={lang} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      }

      return (
        <div key={blockIndex} className="diff-preview">
          <div className="diff-side-by-side">
            {topRows.map((row, rowIndex) => (
              <div key={`${blockIndex}-t${rowIndex}`} className="diff-row">
                <div className="diff-cell diff-cell-old">
                  <span className="diff-lineno">{row.oldNo ?? ""}</span>
                  <Code text={row.oldText} lang={lang} />
                </div>
                <div className="diff-cell diff-cell-new">
                  <span className="diff-lineno">{row.newNo ?? ""}</span>
                  <Code text={row.newText} lang={lang} />
                </div>
              </div>
            ))}
          </div>
          <div className="diff-unmodified-toggle" onClick={() => expandUnmodified(blockIndex)}>
            <span className="diff-unmodified-count">{t("fileChange.linesHidden", { count: middleHidden })}</span>
          </div>
          <div className="diff-side-by-side">
            {bottomRows.map((row, rowIndex) => (
              <div key={`${blockIndex}-b${rowIndex}`} className="diff-row">
                <div className="diff-cell diff-cell-old">
                  <span className="diff-lineno">{row.oldNo ?? ""}</span>
                  <Code text={row.oldText} lang={lang} />
                </div>
                <div className="diff-cell diff-cell-new">
                  <span className="diff-lineno">{row.newNo ?? ""}</span>
                  <Code text={row.newText} lang={lang} />
                </div>
              </div>
            ))}
          </div>
        </div>
      );
    }

    const visibleRows = block.rows.slice(0, visibleCount);
    const remainingHidden = totalRows - visibleCount;

    return (
      <div key={blockIndex} className="diff-preview">
        <div className="diff-side-by-side">
          {visibleRows.map((row, rowIndex) => (
            <div key={`${blockIndex}-${rowIndex}`} className="diff-row">
              <div className="diff-cell diff-cell-old">
                <span className="diff-lineno">{row.oldNo ?? ""}</span>
                <Code text={row.oldText} lang={lang} />
              </div>
              <div className="diff-cell diff-cell-new">
                <span className="diff-lineno">{row.newNo ?? ""}</span>
                <Code text={row.newText} lang={lang} />
              </div>
            </div>
          ))}
        </div>
        {remainingHidden > 0 && (
          <div className="diff-unmodified-toggle" onClick={() => expandUnmodified(blockIndex)}>
            <span className="diff-unmodified-count">{t("fileChange.linesRemaining", { count: remainingHidden })}</span>
          </div>
        )}
      </div>
    );
  };

  const renderUnmodifiedBlockInline = (block: { rows: DiffRow[] }, blockIndex: number) => {
    const totalRows = block.rows.length;
    const visibleCount = getVisibleCount(blockIndex);

    if (visibleCount === 0) {
      const topN = Math.min(3, totalRows);
      const bottomN = Math.min(3, totalRows - topN);
      const topRows = block.rows.slice(0, topN);
      const bottomRows = bottomN > 0 ? block.rows.slice(totalRows - bottomN) : [];
      const middleHidden = totalRows - topN - bottomN;

      if (middleHidden <= 0) {
        return (
          <div key={blockIndex} className="diff-preview">
            <div className="diff-inline">
              {block.rows.map((row, rowIndex) => (
                <div key={`${blockIndex}-${rowIndex}`} className="diff-iline">
                  <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
                  <span className="diff-sign"> </span>
                  <Code text={row.newText || row.oldText} lang={lang} />
                </div>
              ))}
            </div>
          </div>
        );
      }

      return (
        <div key={blockIndex} className="diff-preview">
          <div className="diff-inline">
            {topRows.map((row, rowIndex) => (
              <div key={`${blockIndex}-t${rowIndex}`} className="diff-iline">
                <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
                <span className="diff-sign"> </span>
                <Code text={row.newText || row.oldText} lang={lang} />
              </div>
            ))}
          </div>
          <div className="diff-unmodified-toggle" onClick={() => expandUnmodified(blockIndex)}>
            <span className="diff-unmodified-count">{t("fileChange.linesHidden", { count: middleHidden })}</span>
          </div>
          <div className="diff-inline">
            {bottomRows.map((row, rowIndex) => (
              <div key={`${blockIndex}-b${rowIndex}`} className="diff-iline">
                <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
                <span className="diff-sign"> </span>
                <Code text={row.newText || row.oldText} lang={lang} />
              </div>
            ))}
          </div>
        </div>
      );
    }

    const visibleRows = block.rows.slice(0, visibleCount);
    const remainingHidden = totalRows - visibleCount;

    return (
      <div key={blockIndex} className="diff-preview">
        <div className="diff-inline">
          {visibleRows.map((row, rowIndex) => (
            <div key={`${blockIndex}-${rowIndex}`} className="diff-iline">
              <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
              <span className="diff-sign"> </span>
              <Code text={row.newText || row.oldText} lang={lang} />
            </div>
          ))}
        </div>
        {remainingHidden > 0 && (
          <div className="diff-unmodified-toggle" onClick={() => expandUnmodified(blockIndex)}>
            <span className="diff-unmodified-count">{t("fileChange.linesRemaining", { count: remainingHidden })}</span>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="file-change-details" ref={containerRef}>
      {blocks.map((block, blockIndex) => {
        if (block.type === "change") {
          return wide
            ? renderChangeBlockSideBySide(block, blockIndex)
            : renderChangeBlockInline(block, blockIndex);
        } else {
          return wide
            ? renderUnmodifiedBlockSideBySide(block, blockIndex)
            : renderUnmodifiedBlockInline(block, blockIndex);
        }
      })}
    </div>
  );
}
