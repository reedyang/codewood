# -*- coding: utf-8 -*-
"""
Notification and output formatting module.
Formats analysis reports and outputs the results.
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    """Analysis report data structure."""
    code: str
    name: str
    sentiment_score: int
    trend_prediction: str
    operation_advice: str
    decision_type: str
    confidence_level: str
    technical_summary: Dict[str, Any]
    ai_analysis: Optional[str] = None
    risk_warning: str = ""
    buy_reason: str = ""
    support_levels: List[float] = None
    resistance_levels: List[float] = None
    
    def __post_init__(self):
        if self.support_levels is None:
            self.support_levels = []
        if self.resistance_levels is None:
            self.resistance_levels = []


def format_analysis_report(report: AnalysisReport) -> str:
    """
    Format an analysis report as plain text.

    Args:
        report: analysis report data

    Returns:
        Formatted report text
    """
    lines = [
        f"{'='*50}",
        f"📊 {report.name} ({report.code}) Analysis Report",
        f"{'='*50}",
        "",
        f"【Core Summary】",
        f"  Recommendation: {report.operation_advice}",
        f"  Trend outlook: {report.trend_prediction}",
        f"  Sentiment score: {report.sentiment_score}/100",
        f"  Confidence: {report.confidence_level}",
        "",
        f"【Technical Analysis】",
    ]
    
    # Technical indicators
    tech = report.technical_summary
    if 'current_price' in tech:
        lines.append(f"  Current price: {tech.get('current_price', 'N/A')}")
    
    if 'ma5' in tech:
        lines.append(f"  MA5: {tech.get('ma5', 'N/A'):.2f} (bias: {tech.get('bias_ma5', 0):+.2f}%)")
    if 'ma10' in tech:
        lines.append(f"  MA10: {tech.get('ma10', 'N/A'):.2f} (bias: {tech.get('bias_ma10', 0):+.2f}%)")
    if 'ma20' in tech:
        lines.append(f"  MA20: {tech.get('ma20', 'N/A'):.2f}")
    
    if 'trend_status' in tech:
        lines.append(f"  Trend status: {tech.get('trend_status', 'N/A')}")
    
    if 'volume_status' in tech:
        lines.append(f"  Volume status: {tech.get('volume_status', 'N/A')}")
    
    if 'macd_status' in tech:
        lines.append(f"  MACD: {tech.get('macd_status', 'N/A')}")
    
    if 'rsi_status' in tech:
        lines.append(f"  RSI: {tech.get('rsi_status', 'N/A')}")
    
    lines.append("")
    
    # Support and resistance levels
    if report.support_levels:
        lines.append(f"【Support Levels】")
        for level in report.support_levels[:3]:
            lines.append(f"  - {level:.2f}")
        lines.append("")
    
    if report.resistance_levels:
        lines.append(f"【Resistance Levels】")
        for level in report.resistance_levels[:3]:
            lines.append(f"  - {level:.2f}")
        lines.append("")
    
    # Buy reasons
    if report.buy_reason:
        lines.append(f"【Buy Reasons】")
        lines.append(f"  {report.buy_reason}")
        lines.append("")
    
    # Risk warnings
    if report.risk_warning:
        lines.append(f"【Risk Warnings】")
        lines.append(f"  {report.risk_warning}")
        lines.append("")
    
    # AI analysis
    if report.ai_analysis:
        lines.append(f"【AI Analysis】")
        lines.append(f"  {report.ai_analysis}")
        lines.append("")
    
    lines.append(f"{'='*50}")
    
    return "\n".join(lines)


def format_dashboard_report(reports: List[AnalysisReport]) -> str:
    """
    Format a decision dashboard report that summarizes multiple stocks.

    Args:
        reports: list of analysis reports

    Returns:
        Formatted dashboard report
    """
    if not reports:
        return "No analysis reports available"
    
    # Summary counts
    buy_count = sum(1 for r in reports if r.decision_type == 'buy')
    hold_count = sum(1 for r in reports if r.decision_type == 'hold')
    sell_count = sum(1 for r in reports if r.decision_type == 'sell')
    
    lines = [
        f"{'='*60}",
        f"📊 Stock Analysis Decision Dashboard",
        f"{'='*60}",
        "",
        f"Number of stocks analyzed: {len(reports)}",
        f"🟢 Buy: {buy_count}  🟡 Hold: {hold_count}  🔴 Sell: {sell_count}",
        "",
        f"{'='*60}",
    ]
    
    for report in reports:
        emoji = "🟢" if report.decision_type == 'buy' else "🟡" if report.decision_type == 'hold' else "🔴"
        lines.append(f"{emoji} {report.name} ({report.code})")
        lines.append(f"   Recommendation: {report.operation_advice} | Score: {report.sentiment_score}/100")
        lines.append(f"   Trend: {report.trend_prediction}")
        
        # Add key technical indicators
        tech = report.technical_summary
        key_info = []
        
        if 'bias_ma5' in tech:
            key_info.append(f"Bias: {tech['bias_ma5']:+.1f}%")
        if 'macd_status' in tech:
            key_info.append(f"MACD: {tech['macd_status']}")
        
        if key_info:
            lines.append(f"   Key indicators: {' | '.join(key_info)}")
        
        lines.append("")
    
    lines.append(f"{'='*60}")
    
    return "\n".join(lines)


def create_report_from_result(result: Dict[str, Any]) -> AnalysisReport:
    """
    Create a report object from an analysis result dictionary.

    Args:
        result: analysis result dictionary

    Returns:
        AnalysisReport object
    """
    technical = result.get('technical_indicators', {})
    ai_result = result.get('ai_analysis', {})
    
    # Determine decision type
    advice = ai_result.get('operation_advice', 'hold')
    if advice in ['buy', 'add_position', 'strong_buy']:
        decision_type = 'buy'
    elif advice in ['sell', 'reduce_position', 'strong_sell']:
        decision_type = 'sell'
    else:
        decision_type = 'hold'
    
    return AnalysisReport(
        code=result.get('code', ''),
        name=result.get('name', ''),
        sentiment_score=ai_result.get('sentiment_score', 50),
        trend_prediction=ai_result.get('trend_prediction', 'sideways'),
        operation_advice=advice,
        decision_type=decision_type,
        confidence_level=ai_result.get('confidence_level', 'medium'),
        technical_summary=technical,
        ai_analysis=ai_result.get('analysis_summary', ''),
        risk_warning=ai_result.get('risk_warning', ''),
        buy_reason=ai_result.get('buy_reason', ''),
        support_levels=technical.get('support_levels', []),
        resistance_levels=technical.get('resistance_levels', []),
    )


def print_report(report: AnalysisReport) -> None:
    """Print the analysis report to the console."""
    print(format_analysis_report(report))


def print_dashboard(reports: List[AnalysisReport]) -> None:
    """Print the decision dashboard to the console."""
    print(format_dashboard_report(reports))
