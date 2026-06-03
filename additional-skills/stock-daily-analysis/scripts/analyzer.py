# -*- coding: utf-8 -*-
"""
Stock analysis entrypoint and CLI wrapper.

This module consumes injected market snapshots, computes technical indicators,
and formats analysis outputs for terminal usage.
"""

import argparse
import json
import locale
import logging
import sys
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Support two invocation modes:
# 1) python -m scripts.analyzer
# 2) python <skill_root>/scripts/analyzer.py
try:
    from scripts.data_fetcher import (
        build_quote_from_snapshot,
        get_daily_data,
        get_realtime_quote,
        get_stock_name,
    )
    from scripts.trend_analyzer import StockTrendAnalyzer
    from scripts.ai_analyzer import AIAnalyzer
    from scripts.notifier import format_analysis_report, format_dashboard_report
except ModuleNotFoundError:
    from data_fetcher import (
        build_quote_from_snapshot,
        get_daily_data,
        get_realtime_quote,
        get_stock_name,
    )
    from trend_analyzer import StockTrendAnalyzer
    from ai_analyzer import AIAnalyzer
    from notifier import format_analysis_report, format_dashboard_report


DEFAULT_DAYS = 60


def analyze_stock(code: str, quote_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Analyze one stock and return normalized result payload."""
    logger.info("Starting stock analysis: %s", code)

    quote = build_quote_from_snapshot(code, quote_snapshot)
    name = (quote.name if quote else None) or get_stock_name(code)

    df = get_daily_data(code, days=DEFAULT_DAYS, quote_snapshot=quote_snapshot)
    if df is None or df.empty:
        logger.error("Failed to retrieve data for %s", code)
        return {
            "code": code,
            "name": name,
            "error": "data_fetch_failed",
            "technical_indicators": {},
            "ai_analysis": {"operation_advice": "hold", "sentiment_score": 0},
        }

    analyzer = StockTrendAnalyzer()
    trend_result = analyzer.analyze(df, code)

    latest_quote = get_realtime_quote(code, quote_snapshot=quote_snapshot)
    if latest_quote:
        name = latest_quote.name or name

    ai_analyzer = AIAnalyzer()
    ai_result = ai_analyzer.analyze(code, name, trend_result.to_dict())

    result = {
        "code": code,
        "name": name,
        "technical_indicators": trend_result.to_dict(),
        "ai_analysis": ai_result,
    }
    logger.info(
        "%s analysis completed, sentiment score: %s",
        code,
        ai_result.get("sentiment_score", trend_result.signal_score),
    )
    return result


def analyze_stocks(
    codes: List[str],
    quote_by_code: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Analyze multiple stocks and return result list."""
    results: List[Dict[str, Any]] = []
    for code in codes:
        try:
            snap = (quote_by_code or {}).get(code)
            results.append(analyze_stock(code, quote_snapshot=snap))
        except Exception as exc:
            logger.error("Analysis failed for %s: %s", code, exc)
            results.append(
                {
                    "code": code,
                    "name": code,
                    "error": str(exc),
                    "ai_analysis": {"operation_advice": "hold", "sentiment_score": 0},
                }
            )
    return results


def print_analysis(codes: List[str]) -> None:
    """Print formatted dashboard and per-stock reports."""
    results = analyze_stocks(codes)
    reports = []
    for result in results:
        if "error" in result:
            continue
        try:
            from scripts.notifier import create_report_from_result
        except ModuleNotFoundError:
            from notifier import create_report_from_result
        reports.append(create_report_from_result(result))

    if not reports:
        print("No analysis results to display")
        return

    print("\n" + format_dashboard_report(reports))
    for report in reports:
        print("\n" + format_analysis_report(report))


def _parse_codes_arg(raw_codes: List[str]) -> List[str]:
    """Parse stock codes from argv tokens and comma-separated values."""
    out: List[str] = []
    for item in raw_codes:
        for code in item.split(","):
            code = code.strip()
            if code:
                out.append(code)

    seen = set()
    uniq: List[str] = []
    for code in out:
        if code not in seen:
            uniq.append(code)
            seen.add(code)
    return uniq


def _load_quote_by_code(
    quote_json: Optional[str],
    quote_file: Optional[str],
    codes: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Accept two payload forms:
    1) { "688795": { ...snapshot... }, "600519": { ... } }
    2) { ...snapshot... }  # single stock, mapped to first input code
    """
    raw = None
    if quote_file:
        with open(quote_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    elif quote_json:
        try:
                raw = json.loads(quote_json)
        except json.JSONDecodeError:
            # PowerShell may pass single-quoted dict-like strings.
            import ast
            try:
                raw = ast.literal_eval(quote_json)
            except Exception:
                # Last-resort recovery for heavily escaped or quote-lost payloads
                # such as {\688795\:{\name\:\Moore Threads\,\price\:552.0}}.
                compact = quote_json.replace("\\", "").strip()
                try:
                    raw = json.loads(compact)
                except Exception:
                    import yaml

                    raw = yaml.safe_load(compact)
    else:
        return {}

    if not isinstance(raw, dict):
        raise ValueError("quote payload must be JSON object")

    first_code = codes[0] if codes else None
    if first_code and "price" in raw:
        return {first_code: raw}

    out: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key).strip()] = value
    return out


def _extract_quote_json_from_argv(argv: List[str]) -> Optional[str]:
    """
    Recover --quote-json payload from raw argv.

    This is a defensive parser for shells (notably PowerShell) where complex
    quoted JSON may be split into multiple argv tokens unexpectedly.
    """
    for idx, token in enumerate(argv):
        if token != "--quote-json":
            continue
        payload_parts: List[str] = []
        j = idx + 1
        while j < len(argv):
            cur = argv[j]
            if cur.startswith("--"):
                break
            payload_parts.append(cur)
            j += 1
        if not payload_parts:
            return ""
        return " ".join(payload_parts).strip()
    return None


def _decode_stdin_text(raw: bytes) -> str:
    """
    Decode piped stdin bytes.

    Upstream Python scripts usually emit UTF-8. On Windows, ``cmd.exe`` / ``echo``
    may emit the system ANSI/OEM code page (e.g. GBK for Chinese locales), which
    breaks ``sys.stdin.read()`` (UTF-8 by default). Read ``sys.stdin.buffer``
    and try multiple decodings.
    """
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    pref = (locale.getpreferredencoding(False) or "").strip()
    for enc in (pref, "gbk", "gb18030", "cp936"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def main() -> int:
    """CLI entrypoint for direct skill execution."""
    parser = argparse.ArgumentParser(
        description="Stock daily analysis (A/H/US) with technical indicators"
    )
    parser.add_argument(
        "codes",
        nargs="*",
        help="Stock codes, supports space-separated or comma-separated values",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print raw analysis results as JSON",
    )
    parser.add_argument(
        "--quote-json",
        type=str,
        default="",
        help="Injected quote snapshot JSON from upstream skill orchestration",
    )
    parser.add_argument(
        "--quote-file",
        type=str,
        default="",
        help="Path to injected quote snapshot JSON file (avoid if pipe/stdin is available)",
    )
    parser.add_argument(
        "--quote-stdin",
        action="store_true",
        dest="quote_stdin",
        help="Read quote snapshot JSON from stdin (e.g. piped from stock-data fetch script)",
    )
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.debug("Ignored unknown argv tokens: %s", unknown)

    codes = _parse_codes_arg(args.codes) if args.codes else ["600519"]

    quote_file = (args.quote_file or "").strip() or None
    quote_json: Optional[str] = None

    if args.quote_stdin:
        quote_json = _decode_stdin_text(sys.stdin.buffer.read()).strip()
        quote_file = None
    elif quote_file is None:
        # Prefer a payload reconstructed from raw argv when available.
        # It is more robust under shell quoting differences.
        recovered_quote_json = _extract_quote_json_from_argv(sys.argv[1:])
        quote_json = (
            recovered_quote_json
            if recovered_quote_json is not None
            else (args.quote_json or None)
        )
        if quote_json == "":
            quote_json = None

    quote_by_code = _load_quote_by_code(
        quote_json=quote_json,
        quote_file=quote_file,
        codes=codes,
    )
    results = analyze_stocks(codes, quote_by_code=quote_by_code)

    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    reports = []
    for result in results:
        if "error" in result:
            continue
        try:
            from scripts.notifier import create_report_from_result
        except ModuleNotFoundError:
            from notifier import create_report_from_result
        reports.append(create_report_from_result(result))

    if not reports:
        print("No analysis results to display")
        return 0

    print("\n" + format_dashboard_report(reports))
    for report in reports:
        print("\n" + format_analysis_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
