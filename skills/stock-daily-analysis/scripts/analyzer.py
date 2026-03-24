# -*- coding: utf-8 -*-
"""
?????? - ?????

????????????????
?????????
"""

import logging
import argparse
import json
from typing import List, Dict, Any, Optional

# ????
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ????
# Support both invocation modes:
# 1) python -m scripts.analyzer
# 2) python <skill_root>/scripts/analyzer.py
try:
    from scripts.data_fetcher import (
        get_daily_data,
        get_realtime_quote,
        get_stock_name,
        build_quote_from_snapshot,
    )
    from scripts.trend_analyzer import StockTrendAnalyzer
    from scripts.ai_analyzer import AIAnalyzer
    from scripts.notifier import AnalysisReport, format_analysis_report, format_dashboard_report
except ModuleNotFoundError:
    from data_fetcher import (
        get_daily_data,
        get_realtime_quote,
        get_stock_name,
        build_quote_from_snapshot,
    )
    from trend_analyzer import StockTrendAnalyzer
    from ai_analyzer import AIAnalyzer
    from notifier import AnalysisReport, format_analysis_report, format_dashboard_report


DEFAULT_DAYS = 60


def analyze_stock(code: str, quote_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    ??????
    
    Args:
        code: ???? (? '600519', 'AAPL', '00700')
        
    Returns:
        ???????????
    """
    logger.info(f"??????: {code}")
    
    # ??????
    quote = build_quote_from_snapshot(code, quote_snapshot)
    name = (quote.name if quote else None) or get_stock_name(code)
    
    # ??????
    df = get_daily_data(code, days=DEFAULT_DAYS, quote_snapshot=quote_snapshot)
    
    if df is None or df.empty:
        logger.error(f"???? {code} ???")
        return {
            'code': code,
            'name': name,
            'error': '??????',
            'technical_indicators': {},
            'ai_analysis': {'operation_advice': '????', 'sentiment_score': 0}
        }
    
    # ????
    analyzer = StockTrendAnalyzer()
    trend_result = analyzer.analyze(df, code)
    
    # ??????????????
    quote = get_realtime_quote(code, quote_snapshot=quote_snapshot)
    if quote:
        name = quote.name or name
    
    # AI ??? Smart Shell ???skill ????????
    ai_analyzer = AIAnalyzer()
    ai_result = ai_analyzer.analyze(code, name, trend_result.to_dict())
    
    # ????
    result = {
        'code': code,
        'name': name,
        'technical_indicators': trend_result.to_dict(),
        'ai_analysis': ai_result
    }
    
    logger.info(f"{code} ???????: {ai_result.get('sentiment_score', trend_result.signal_score)}")
    return result


def analyze_stocks(codes: List[str], quote_by_code: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    ??????
    
    Args:
        codes: ??????
        
    Returns:
        ??????
    """
    results = []
    for code in codes:
        try:
            snap = (quote_by_code or {}).get(code)
            result = analyze_stock(code, quote_snapshot=snap)
            results.append(result)
        except Exception as e:
            logger.error(f"?? {code} ???: {e}")
            results.append({
                'code': code,
                'name': code,
                'error': str(e),
                'ai_analysis': {'operation_advice': '????', 'sentiment_score': 0}
            })
    
    return results


def print_analysis(codes: List[str]) -> None:
    """
    ?????????
    
    Args:
        codes: ??????
    """
    results = analyze_stocks(codes)
    
    # ??????????
    reports = []
    for result in results:
        if 'error' not in result:
            try:
                from scripts.notifier import create_report_from_result
            except ModuleNotFoundError:
                from notifier import create_report_from_result
            report = create_report_from_result(result)
            reports.append(report)
    
    if reports:
        print("\n" + format_dashboard_report(reports))
        
        # ???????????
        for report in reports:
            print("\n" + format_analysis_report(report))
    else:
        print("????????")


def _parse_codes_arg(raw_codes: List[str]) -> List[str]:
    """Parse stock codes from argv tokens and comma-separated values."""
    out: List[str] = []
    for item in raw_codes:
        for code in item.split(","):
            code = code.strip()
            if code:
                out.append(code)
    # keep order while removing duplicates
    seen = set()
    uniq: List[str] = []
    for c in out:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


def _load_quote_by_code(
    quote_json: Optional[str],
    quote_file: Optional[str],
    codes: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Accept two shapes:
    1) { "688795": { ...snapshot... }, "600519": { ... } }
    2) { ...snapshot... }  # single stock; will map to first code
    """
    raw = None
    if quote_file:
        with open(quote_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    elif quote_json:
        try:
            raw = json.loads(quote_json)
        except json.JSONDecodeError:
            # PowerShell users often pass single-quoted dict-like strings.
            import ast

            raw = ast.literal_eval(quote_json)
    else:
        return {}

    if not isinstance(raw, dict):
        raise ValueError("quote payload must be JSON object")

    first_code = codes[0] if codes else None
    if first_code and "price" in raw:
        return {first_code: raw}

    out: Dict[str, Dict[str, Any]] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            out[str(k).strip()] = v
    return out


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
        help="Path to injected quote snapshot JSON file",
    )
    args = parser.parse_args()

    codes = _parse_codes_arg(args.codes) if args.codes else ["600519"]
    quote_by_code = _load_quote_by_code(
        quote_json=args.quote_json or None,
        quote_file=args.quote_file or None,
        codes=codes,
    )
    results = analyze_stocks(codes, quote_by_code=quote_by_code)

    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    # Convert to report format and print
    reports = []
    for result in results:
        if "error" not in result:
            try:
                from scripts.notifier import create_report_from_result
            except ModuleNotFoundError:
                from notifier import create_report_from_result

            reports.append(create_report_from_result(result))

    if reports:
        print("\n" + format_dashboard_report(reports))
        for report in reports:
            print("\n" + format_analysis_report(report))
    else:
        print("????????")
    return 0


# ????
if __name__ == "__main__":
    raise SystemExit(main())
