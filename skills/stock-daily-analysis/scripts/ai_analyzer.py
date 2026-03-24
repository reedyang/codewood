# -*- coding: utf-8 -*-
"""
AI 分析模块 - Smart Shell 对接模式
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class AIAnalyzer:
    """AI 分析器 - 由 Smart Shell 托管推理，不再直接调用外部 LLM"""

    def __init__(self):
        logger.info("stock-daily-analysis 使用 Smart Shell 托管分析模式（内部 LLM 已停用）")

    def analyze(self, code: str, name: str, technical_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于技术指标生成结构化分析，由 Smart Shell 统一承接后续语言推理。
        """
        del code, name  # Reserved for future cross-symbol context enhancement.
        return self._default_analysis_from_tech(technical_data)

    def _default_analysis_from_tech(self, tech: Dict[str, Any]) -> Dict[str, Any]:
        """基于技术面的默认分析"""
        score = int(tech.get("signal_score", 50))
        buy_signal = tech.get("buy_signal", "观望")
        trend_status = tech.get("trend_status", "震荡")
        reasons = tech.get("signal_reasons", [])
        risks = tech.get("risk_factors", [])

        summary_parts = []
        if trend_status:
            summary_parts.append(f"趋势: {trend_status}")
        if reasons:
            summary_parts.append(reasons[0])

        return {
            "sentiment_score": score,
            "trend_prediction": trend_status,
            "operation_advice": buy_signal,
            "confidence_level": "高" if score >= 70 else "中" if score >= 50 else "低",
            "analysis_summary": " | ".join(summary_parts)[:120],
            "buy_reason": ", ".join(reasons),
            "risk_warning": " | ".join(risks),
            "target_price": "",
            "stop_loss": "",
        }
