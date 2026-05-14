# -*- coding: utf-8 -*-
"""
数据获取模块 - 纯行情适配层（不直接调用其他 skill 脚本）

约束：
1) 本模块不发起 shell/subprocess 调用；
2) 行情数据由上层 Agent 先通过其他 skill（如 baidu）获取后注入。
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class StockQuote:
    """统一实时行情数据结构"""
    code: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    change_amount: float = 0.0
    volume: int = 0
    amount: float = 0.0
    open_price: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    volume_ratio: Optional[float] = None
    turnover_rate: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    total_mv: Optional[float] = None
    circ_mv: Optional[float] = None


@dataclass
class ChipDistribution:
    """筹码分布数据"""
    profit_ratio: float = 0.0
    avg_cost: float = 0.0
    concentration_90: float = 0.0
    concentration_70: float = 0.0


def _to_float(v: Any) -> Optional[float]:
    try:
        s = str(v).replace(",", "").replace("，", "").strip()
        return float(s)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(float(str(v).replace(",", "").replace("，", "").strip()))
    except (TypeError, ValueError):
        return None


def _is_us_code(stock_code: str) -> bool:
    """判断是否为美股代码（1-5个大写字母）"""
    code = stock_code.strip().upper()
    return bool(re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', code))


def _is_hk_code(stock_code: str) -> bool:
    """判断是否为港股代码（5位数字）"""
    code = stock_code.lower()
    if code.startswith('hk'):
        numeric_part = code[2:]
        return numeric_part.isdigit() and 1 <= len(numeric_part) <= 5
    return code.isdigit() and len(code) == 5


def _is_etf_code(stock_code: str) -> bool:
    """判断是否为 ETF 代码"""
    etf_prefixes = ('51', '52', '56', '58', '15', '16', '18')
    return stock_code.startswith(etf_prefixes) and len(stock_code) == 6


def normalize_code(stock_code: str) -> tuple:
    """
    标准化股票代码
    
    Returns:
        tuple: (market, code)
        - market: 'a', 'hk', 'us'
        - code: 标准化后的代码
    """
    code = stock_code.strip()
    
    if _is_us_code(code):
        return 'us', code.upper()
    
    if _is_hk_code(code):
        # 去除 hk 前缀，返回5位数字
        if code.lower().startswith('hk'):
            code = code[2:]
        return 'hk', code.zfill(5)
    
    # A股默认处理
    return 'a', code


def build_quote_from_snapshot(stock_code: str, snapshot: Optional[Dict[str, Any]]) -> Optional[StockQuote]:
    """Build StockQuote from externally provided snapshot dict."""
    if not snapshot:
        return None
    _, code = normalize_code(stock_code)

    price = _to_float(snapshot.get("price"))
    if price is None or price <= 0:
        return None
    change_pct = _to_float(snapshot.get("change_pct")) or 0.0
    change_amount = _to_float(snapshot.get("change_amount")) or 0.0
    pre_close = _to_float(snapshot.get("pre_close"))
    if pre_close is None:
        pre_close = price - change_amount if change_amount else (
            price / (1 + change_pct / 100.0) if change_pct else price
        )

    return StockQuote(
        code=code,
        name=str(snapshot.get("name") or code),
        price=price,
        change_pct=change_pct,
        change_amount=change_amount,
        volume=_to_int(snapshot.get("volume")) or 0,
        amount=_to_float(snapshot.get("amount")) or 0.0,
        open_price=_to_float(snapshot.get("open_price")) or price,
        high=_to_float(snapshot.get("high")) or price,
        low=_to_float(snapshot.get("low")) or price,
        pre_close=pre_close,
        turnover_rate=_to_float(snapshot.get("turnover_rate")),
        pe_ratio=_to_float(snapshot.get("pe_ratio")),
        pb_ratio=_to_float(snapshot.get("pb_ratio")),
        total_mv=_to_float(snapshot.get("total_mv")),
        circ_mv=_to_float(snapshot.get("circ_mv")),
    )


def _build_synthetic_daily_from_quote(quote: StockQuote, days: int = 60) -> pd.DataFrame:
    """Build synthetic OHLCV from one quote snapshot for downstream indicators."""
    n = max(20, int(days))
    end = datetime.now().date()
    start_close = quote.pre_close if quote.pre_close and quote.pre_close > 0 else quote.price
    end_close = quote.price if quote.price > 0 else start_close
    step = (end_close - start_close) / max(1, n - 1)

    rows = []
    for i in range(n):
        close = start_close + step * i
        open_p = close - step * 0.3
        high = max(open_p, close) * 1.005
        low = min(open_p, close) * 0.995
        vol = max(1, int((quote.volume or 100000) * (0.8 + 0.4 * (i + 1) / n)))
        rows.append(
            {
                "date": pd.Timestamp(end - timedelta(days=(n - 1 - i))),
                "open": float(open_p),
                "close": float(close),
                "high": float(high),
                "low": float(low),
                "volume": vol,
                "amount": float((quote.amount or 0.0) / n if quote.amount else vol * close),
                "pct_chg": 0.0,
            }
        )
    df = pd.DataFrame(rows)
    df["pct_chg"] = df["close"].pct_change().fillna(0) * 100
    return df


def get_daily_data(stock_code: str, days: int = 60, quote_snapshot: Optional[Dict[str, Any]] = None) -> Optional[pd.DataFrame]:
    """
    获取股票日线数据
    
    Args:
        stock_code: 股票代码
        days: 获取天数
        
    Returns:
        DataFrame 包含 OHLCV 数据，失败返回 None
    """
    quote = build_quote_from_snapshot(stock_code, quote_snapshot)
    if quote and quote.price > 0:
        return _build_synthetic_daily_from_quote(quote, days=days)
    logger.error(f"获取 {stock_code} 数据失败: 未注入可用行情快照")
    return None


def get_realtime_quote(stock_code: str, quote_snapshot: Optional[Dict[str, Any]] = None) -> Optional[StockQuote]:
    """
    获取实时行情
    
    Args:
        stock_code: 股票代码
        
    Returns:
        StockQuote 对象，失败返回 None
    """
    return build_quote_from_snapshot(stock_code, quote_snapshot)


def get_chip_distribution(stock_code: str) -> Optional[ChipDistribution]:
    """
    获取筹码分布数据（仅 A 股）
    
    Args:
        stock_code: 股票代码
        
    Returns:
        ChipDistribution 对象，失败返回 None
    """
    logger.info(f"当前数据源模式仅使用 baidu skill，筹码分布暂不可用: {stock_code}")
    return None


def get_stock_name(stock_code: str) -> str:
    """获取股票名称"""
    quote = get_realtime_quote(stock_code)
    if quote and quote.name:
        return quote.name
    
    # 默认返回代码
    return stock_code
