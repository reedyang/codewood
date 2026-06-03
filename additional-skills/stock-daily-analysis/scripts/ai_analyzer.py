# -*- coding: utf-8 -*-
"""
AI analysis module: produces a structured technical summary for the upstream agent or host to reason over in natural language.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class AIAnalyzer:
    """Structured technical analysis. It does not call an external LLM directly and is used after the host injects context."""

    def __init__(self):
        pass

    def analyze(self, code: str, name: str, technical_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a structured analysis from technical indicators for later interpretation by the host together with market data.
        """
        del code, name  # Reserved for future cross-symbol context enhancement.
        return self._default_analysis_from_tech(technical_data)

    def _default_analysis_from_tech(self, tech: Dict[str, Any]) -> Dict[str, Any]:
        """Default analysis based on technical data."""
        score = int(tech.get("signal_score", 50))
        buy_signal = tech.get("buy_signal", "hold")
        trend_status = tech.get("trend_status", "sideways")
        reasons = tech.get("signal_reasons", [])
        risks = tech.get("risk_factors", [])

        summary_parts = []
        if trend_status:
            summary_parts.append(f"Trend: {trend_status}")
        if reasons:
            summary_parts.append(reasons[0])

        return {
            "sentiment_score": score,
            "trend_prediction": trend_status,
            "operation_advice": buy_signal,
            "confidence_level": "high" if score >= 70 else "medium" if score >= 50 else "low",
            "analysis_summary": " | ".join(summary_parts)[:120],
            "buy_reason": ", ".join(reasons),
            "risk_warning": " | ".join(risks),
            "target_price": "",
            "stop_loss": "",
        }
