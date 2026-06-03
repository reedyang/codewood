# -*- coding: utf-8 -*-
"""
Trend trading analyzer - technical analysis based on trading principles

Core principles:
1. Strict entry strategy - avoid chasing strength and prioritize trade success rate
2. Trend trading - bullish MA5>MA10>MA20 alignment, trade with the trend
3. Entry preference - buy pullbacks near MA5/MA10
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class TrendStatus(Enum):
    """Trend status enum"""
    STRONG_BULL = "strong bullish"
    BULL = "bullish alignment"
    WEAK_BULL = "weak bullish"
    CONSOLIDATION = "consolidation"
    WEAK_BEAR = "weak bearish"
    BEAR = "bearish alignment"
    STRONG_BEAR = "strong bearish"


class VolumeStatus(Enum):
    """Volume status enum"""
    HEAVY_VOLUME_UP = "heavy volume rally"
    HEAVY_VOLUME_DOWN = "heavy volume drop"
    SHRINK_VOLUME_UP = "shrink-volume rally"
    SHRINK_VOLUME_DOWN = "shrink-volume pullback"
    NORMAL = "normal volume"


class BuySignal(Enum):
    """Buy signal enum"""
    STRONG_BUY = "strong buy"
    BUY = "buy"
    HOLD = "hold"
    WAIT = "wait"
    SELL = "sell"
    STRONG_SELL = "strong sell"


class MACDStatus(Enum):
    """MACD status enum"""
    GOLDEN_CROSS_ZERO = "golden cross above zero"
    GOLDEN_CROSS = "golden cross"
    BULLISH = "bullish"
    CROSSING_UP = "crossed above zero"
    CROSSING_DOWN = "crossed below zero"
    BEARISH = "bearish"
    DEATH_CROSS = "death cross"


class RSIStatus(Enum):
    """RSI status enum"""
    OVERBOUGHT = "overbought"
    STRONG_BUY = "strong buy"
    NEUTRAL = "neutral"
    WEAK = "weak"
    OVERSOLD = "oversold"


@dataclass
class TrendAnalysisResult:
    """Trend analysis result"""
    code: str
    
    # trend judgment
    trend_status: TrendStatus = TrendStatus.CONSOLIDATION
    ma_alignment: str = ""
    trend_strength: float = 0.0
    
    # moving average data
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    current_price: float = 0.0
    
    # bias
    bias_ma5: float = 0.0
    bias_ma10: float = 0.0
    bias_ma20: float = 0.0
    
    # volume analysis
    volume_status: VolumeStatus = VolumeStatus.NORMAL
    volume_ratio_5d: float = 0.0
    volume_trend: str = ""
    
    # support and resistance
    support_ma5: bool = False
    support_ma10: bool = False
    resistance_levels: List[float] = field(default_factory=list)
    support_levels: List[float] = field(default_factory=list)
    
    # MACD indicator
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_bar: float = 0.0
    macd_status: MACDStatus = MACDStatus.BULLISH
    macd_signal: str = ""
    
    # RSI indicator
    rsi_6: float = 0.0
    rsi_12: float = 0.0
    rsi_24: float = 0.0
    rsi_status: RSIStatus = RSIStatus.NEUTRAL
    rsi_signal: str = ""
    
    # buy signal
    buy_signal: BuySignal = BuySignal.WAIT
    signal_score: int = 0
    signal_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict"""
        return {
            'code': self.code,
            'trend_status': self.trend_status.value,
            'ma_alignment': self.ma_alignment,
            'trend_strength': self.trend_strength,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'ma60': self.ma60,
            'current_price': self.current_price,
            'bias_ma5': self.bias_ma5,
            'bias_ma10': self.bias_ma10,
            'bias_ma20': self.bias_ma20,
            'volume_status': self.volume_status.value,
            'volume_ratio_5d': self.volume_ratio_5d,
            'volume_trend': self.volume_trend,
            'support_ma5': self.support_ma5,
            'support_ma10': self.support_ma10,
            'buy_signal': self.buy_signal.value,
            'signal_score': self.signal_score,
            'signal_reasons': self.signal_reasons,
            'risk_factors': self.risk_factors,
            'macd_status': self.macd_status.value,
            'macd_signal': self.macd_signal,
            'rsi_status': self.rsi_status.value,
            'rsi_signal': self.rsi_signal,
        }


class StockTrendAnalyzer:
    """
    Stock trend analyzer
    
    Based on trading principles:
    1. Trend judgment - bullish MA5>MA10>MA20 alignment
    2. Bias check - do not chase strength; do not buy if the price deviates more than 5% from MA5
    3. Volume analysis - prefer shrink-volume pullbacks
    4. MACD/RSI indicator analysis
    """
    
    BIAS_THRESHOLD = 5.0  # bias threshold
    VOLUME_SHRINK_RATIO = 0.7
    VOLUME_HEAVY_RATIO = 1.5
    MA_SUPPORT_TOLERANCE = 0.02
    
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    
    RSI_SHORT = 6
    RSI_MID = 12
    RSI_LONG = 24
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    
    def analyze(self, df: pd.DataFrame, code: str) -> TrendAnalysisResult:
        """
        Analyze stock trend
        
        Args:
            df: DataFrame containing OHLCV data
            code: stock code
            
        Returns:
            TrendAnalysisResult analysis result
        """
        result = TrendAnalysisResult(code=code)
        
        if df is None or df.empty or len(df) < 20:
            logger.warning(f"{code} insufficient data for trend analysis")
            result.risk_factors.append("insufficient data to complete analysis")
            return result
        
        # Ensure data is sorted by date
        df = df.sort_values('date').reset_index(drop=True)
        
        # Calculate indicators
        df = self._calculate_mas(df)
        df = self._calculate_macd(df)
        df = self._calculate_rsi(df)
        
        # Get latest data
        latest = df.iloc[-1]
        result.current_price = float(latest['close'])
        result.ma5 = float(latest['MA5'])
        result.ma10 = float(latest['MA10'])
        result.ma20 = float(latest['MA20'])
        result.ma60 = float(latest.get('MA60', 0))
        
        # Analyze each component
        self._analyze_trend(df, result)
        self._calculate_bias(result)
        self._analyze_volume(df, result)
        self._analyze_support_resistance(df, result)
        self._analyze_macd(df, result)
        self._analyze_rsi(df, result)
        self._generate_signal(result)
        
        return result
    
    def _calculate_mas(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate moving averages"""
        df = df.copy()
        df['MA5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['MA10'] = df['close'].rolling(window=10, min_periods=1).mean()
        df['MA20'] = df['close'].rolling(window=20, min_periods=1).mean()
        if len(df) >= 60:
            df['MA60'] = df['close'].rolling(window=60, min_periods=1).mean()
        else:
            df['MA60'] = df['MA20']
        return df
    
    def _calculate_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate MACD"""
        df = df.copy()
        
        ema_fast = df['close'].ewm(span=self.MACD_FAST, adjust=False).mean()
        ema_slow = df['close'].ewm(span=self.MACD_SLOW, adjust=False).mean()
        
        df['MACD_DIF'] = ema_fast - ema_slow
        df['MACD_DEA'] = df['MACD_DIF'].ewm(span=self.MACD_SIGNAL, adjust=False).mean()
        df['MACD_BAR'] = (df['MACD_DIF'] - df['MACD_DEA']) * 2
        
        return df
    
    def _calculate_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate RSI"""
        df = df.copy()
        
        for period in [self.RSI_SHORT, self.RSI_MID, self.RSI_LONG]:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            
            avg_gain = gain.rolling(window=period, min_periods=1).mean()
            avg_loss = loss.rolling(window=period, min_periods=1).mean()
            
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            
            df[f'RSI_{period}'] = rsi.fillna(50)
        
        return df
    
    def _analyze_trend(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Analyze trend status"""
        ma5, ma10, ma20 = result.ma5, result.ma10, result.ma20
        
        if ma5 > ma10 > ma20:
            # Check trend strength
            if len(df) >= 5:
                prev = df.iloc[-5]
                prev_spread = (prev['MA5'] - prev['MA20']) / prev['MA20'] * 100 if prev['MA20'] > 0 else 0
                curr_spread = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0
                
                if curr_spread > prev_spread and curr_spread > 5:
                    result.trend_status = TrendStatus.STRONG_BULL
                    result.ma_alignment = "strong bullish alignment, moving averages diverging upward"
                    result.trend_strength = 90
                else:
                    result.trend_status = TrendStatus.BULL
                    result.ma_alignment = "bullish alignment MA5>MA10>MA20"
                    result.trend_strength = 75
            else:
                result.trend_status = TrendStatus.BULL
                result.ma_alignment = "bullish alignment MA5>MA10>MA20"
                result.trend_strength = 75
                
        elif ma5 > ma10 and ma10 <= ma20:
            result.trend_status = TrendStatus.WEAK_BULL
            result.ma_alignment = "weak bullish, MA5>MA10 but MA10<=MA20"
            result.trend_strength = 55
            
        elif ma5 < ma10 < ma20:
            if len(df) >= 5:
                prev = df.iloc[-5]
                prev_spread = (prev['MA20'] - prev['MA5']) / prev['MA5'] * 100 if prev['MA5'] > 0 else 0
                curr_spread = (ma20 - ma5) / ma5 * 100 if ma5 > 0 else 0
                
                if curr_spread > prev_spread and curr_spread > 5:
                    result.trend_status = TrendStatus.STRONG_BEAR
                    result.ma_alignment = "strong bearish alignment, moving averages diverging downward"
                    result.trend_strength = 10
                else:
                    result.trend_status = TrendStatus.BEAR
                    result.ma_alignment = "bearish alignment MA5<MA10<MA20"
                    result.trend_strength = 25
            else:
                result.trend_status = TrendStatus.BEAR
                result.ma_alignment = "bearish alignment MA5<MA10<MA20"
                result.trend_strength = 25
                
        elif ma5 < ma10 and ma10 >= ma20:
            result.trend_status = TrendStatus.WEAK_BEAR
            result.ma_alignment = "weak bearish, MA5<MA10 but MA10>=MA20"
            result.trend_strength = 40
            
        else:
            result.trend_status = TrendStatus.CONSOLIDATION
            result.ma_alignment = "moving averages tangled, trend unclear"
            result.trend_strength = 50
    
    def _calculate_bias(self, result: TrendAnalysisResult) -> None:
        """Calculate bias"""
        price = result.current_price
        
        if result.ma5 > 0:
            result.bias_ma5 = (price - result.ma5) / result.ma5 * 100
        if result.ma10 > 0:
            result.bias_ma10 = (price - result.ma10) / result.ma10 * 100
        if result.ma20 > 0:
            result.bias_ma20 = (price - result.ma20) / result.ma20 * 100
    
    def _analyze_volume(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Analyze volume"""
        if len(df) < 5:
            return
        
        latest = df.iloc[-1]
        vol_5d_avg = df['volume'].iloc[-6:-1].mean()
        
        if vol_5d_avg > 0:
            result.volume_ratio_5d = float(latest['volume']) / vol_5d_avg
        
        # Determine price change
        if len(df) >= 2:
            prev_close = df.iloc[-2]['close']
            price_change = (latest['close'] - prev_close) / prev_close * 100
            
            # Volume status judgment
            if result.volume_ratio_5d >= self.VOLUME_HEAVY_RATIO:
                if price_change > 0:
                    result.volume_status = VolumeStatus.HEAVY_VOLUME_UP
                    result.volume_trend = "heavy volume rally, strong bullish force"
                else:
                    result.volume_status = VolumeStatus.HEAVY_VOLUME_DOWN
                    result.volume_trend = "heavy volume drop, watch risk"
            elif result.volume_ratio_5d <= self.VOLUME_SHRINK_RATIO:
                if price_change > 0:
                    result.volume_status = VolumeStatus.SHRINK_VOLUME_UP
                    result.volume_trend = "shrink-volume rally, insufficient upside momentum"
                else:
                    result.volume_status = VolumeStatus.SHRINK_VOLUME_DOWN
                    result.volume_trend = "shrink-volume pullback, clear shakeout characteristics"
            else:
                result.volume_status = VolumeStatus.NORMAL
                result.volume_trend = "normal volume"
    
    def _analyze_support_resistance(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Analyze support and resistance levels"""
        price = result.current_price
        
        # Check MA5 support
        if result.ma5 > 0:
            ma5_distance = abs(price - result.ma5) / result.ma5
            if ma5_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma5:
                result.support_ma5 = True
                result.support_levels.append(result.ma5)
        
        # Check MA10 support
        if result.ma10 > 0:
            ma10_distance = abs(price - result.ma10) / result.ma10
            if ma10_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma10:
                result.support_ma10 = True
                if result.ma10 not in result.support_levels:
                    result.support_levels.append(result.ma10)
        
        # MA20 as important support
        if result.ma20 > 0 and price >= result.ma20:
            result.support_levels.append(result.ma20)
        
        # Recent highs as resistance
        if len(df) >= 20:
            recent_high = df['high'].iloc[-20:].max()
            if recent_high > price:
                result.resistance_levels.append(recent_high)
    
    def _analyze_macd(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Analyze MACD"""
        if len(df) < self.MACD_SLOW:
            result.macd_signal = "Insufficient data"
            return
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        result.macd_dif = float(latest['MACD_DIF'])
        result.macd_dea = float(latest['MACD_DEA'])
        result.macd_bar = float(latest['MACD_BAR'])
        
        # Detect golden/death crosses
        prev_dif_dea = prev['MACD_DIF'] - prev['MACD_DEA']
        curr_dif_dea = result.macd_dif - result.macd_dea
        
        is_golden_cross = prev_dif_dea <= 0 and curr_dif_dea > 0
        is_death_cross = prev_dif_dea >= 0 and curr_dif_dea < 0
        is_crossing_up = prev['MACD_DIF'] <= 0 and result.macd_dif > 0
        is_crossing_down = prev['MACD_DIF'] >= 0 and result.macd_dif < 0
        
        if is_golden_cross and result.macd_dif > 0:
            result.macd_status = MACDStatus.GOLDEN_CROSS_ZERO
            result.macd_signal = "⭐ golden cross above zero line, strong buy signal!"
        elif is_crossing_up:
            result.macd_status = MACDStatus.CROSSING_UP
            result.macd_signal = "⚡ DIF crosses above zero line, trend strengthens"
        elif is_golden_cross:
            result.macd_status = MACDStatus.GOLDEN_CROSS
            result.macd_signal = "✅ golden cross, trend upward"
        elif is_death_cross:
            result.macd_status = MACDStatus.DEATH_CROSS
            result.macd_signal = "❌ death cross, trend downward"
        elif is_crossing_down:
            result.macd_status = MACDStatus.CROSSING_DOWN
            result.macd_signal = "⚠️ DIF crosses below zero line, trend weakens"
        elif result.macd_dif > 0 and result.macd_dea > 0:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = "✓ bullish alignment, continuing rise"
        elif result.macd_dif < 0 and result.macd_dea < 0:
            result.macd_status = MACDStatus.BEARISH
            result.macd_signal = "⚠ bearish alignment, continuing decline"
        else:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = "MACD neutral zone"
    
    def _analyze_rsi(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Analyze RSI"""
        if len(df) < self.RSI_LONG:
            result.rsi_signal = "Insufficient data"
            return
        
        latest = df.iloc[-1]
        
        result.rsi_6 = float(latest[f'RSI_{self.RSI_SHORT}'])
        result.rsi_12 = float(latest[f'RSI_{self.RSI_MID}'])
        result.rsi_24 = float(latest[f'RSI_{self.RSI_LONG}'])
        
        rsi_mid = result.rsi_12
        
        if rsi_mid > self.RSI_OVERBOUGHT:
            result.rsi_status = RSIStatus.OVERBOUGHT
            result.rsi_signal = f"⚠️ RSI overbought({rsi_mid:.1f}>70)，high short-term pullback risk"
        elif rsi_mid > 60:
            result.rsi_status = RSIStatus.STRONG_BUY
            result.rsi_signal = f"✅ RSI strong({rsi_mid:.1f})，bullish momentum is sufficient"
        elif rsi_mid >= 40:
            result.rsi_status = RSIStatus.NEUTRAL
            result.rsi_signal = f"RSI neutral({rsi_mid:.1f})，consolidating in a range"
        elif rsi_mid >= self.RSI_OVERSOLD:
            result.rsi_status = RSIStatus.WEAK
            result.rsi_signal = f"⚡ RSI weak({rsi_mid:.1f})，watch for a rebound"
        else:
            result.rsi_status = RSIStatus.OVERSOLD
            result.rsi_signal = f"⭐ RSI oversold({rsi_mid:.1f}<30)，higher chance of rebound"
    
    def _generate_signal(self, result: TrendAnalysisResult) -> None:
        """Generate buy signal and composite score"""
        score = 0
        reasons = []
        risks = []
        
        # Trend score (30 points)
        trend_scores = {
            TrendStatus.STRONG_BULL: 30,
            TrendStatus.BULL: 26,
            TrendStatus.WEAK_BULL: 18,
            TrendStatus.CONSOLIDATION: 12,
            TrendStatus.WEAK_BEAR: 8,
            TrendStatus.BEAR: 4,
            TrendStatus.STRONG_BEAR: 0,
        }
        trend_score = trend_scores.get(result.trend_status, 12)
        score += trend_score
        
        if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            reasons.append(f"✅ {result.trend_status.value}，go long with the trend")
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            risks.append(f"⚠️ {result.trend_status.value}，not suitable for going long")
        
        # Bias score (20 points)
        bias = result.bias_ma5
        if bias < 0:
            if bias > -3:
                score += 20
                reasons.append(f"✅ price slightly below MA5({bias:.1f}%)，pullback entry point")
            elif bias > -5:
                score += 16
                reasons.append(f"✅ price pulls back to MA5({bias:.1f}%)，watch support")
            else:
                score += 8
                risks.append(f"⚠️ bias too large({bias:.1f}%)，possible breakdown")
        elif bias < 2:
            score += 18
            reasons.append(f"✅ price near MA5({bias:.1f}%)，good entry timing")
        elif bias < self.BIAS_THRESHOLD:
            score += 14
            reasons.append(f"⚡ price slightly above MA5({bias:.1f}%)，can enter with a small position")
        else:
            score += 4
            risks.append(f"❌ bias too high({bias:.1f}%>5%)，do not chase strength!")
        
        # Volume score (15 points)
        volume_scores = {
            VolumeStatus.SHRINK_VOLUME_DOWN: 15,
            VolumeStatus.HEAVY_VOLUME_UP: 12,
            VolumeStatus.NORMAL: 10,
            VolumeStatus.SHRINK_VOLUME_UP: 6,
            VolumeStatus.HEAVY_VOLUME_DOWN: 0,
        }
        vol_score = volume_scores.get(result.volume_status, 8)
        score += vol_score
        
        if result.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN:
            reasons.append("✅ shrink-volume pullback, likely a shakeout")
        elif result.volume_status == VolumeStatus.HEAVY_VOLUME_DOWN:
            risks.append("⚠️ heavy volume drop, watch risk")
        
        # Support score (10 points)
        if result.support_ma5:
            score += 5
            reasons.append("✅ MA5 support is valid")
        if result.support_ma10:
            score += 5
            reasons.append("✅ MA10 support is valid")
        
        # MACD score (15 points)
        macd_scores = {
            MACDStatus.GOLDEN_CROSS_ZERO: 15,
            MACDStatus.GOLDEN_CROSS: 12,
            MACDStatus.CROSSING_UP: 10,
            MACDStatus.BULLISH: 8,
            MACDStatus.BEARISH: 2,
            MACDStatus.CROSSING_DOWN: 0,
            MACDStatus.DEATH_CROSS: 0,
        }
        macd_score = macd_scores.get(result.macd_status, 5)
        score += macd_score
        
        if result.macd_status in [MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.GOLDEN_CROSS]:
            reasons.append(f"✅ {result.macd_signal}")
        elif result.macd_status in [MACDStatus.DEATH_CROSS, MACDStatus.CROSSING_DOWN]:
            risks.append(f"⚠️ {result.macd_signal}")
        else:
            reasons.append(result.macd_signal)
        
        # RSI score (10 points)
        rsi_scores = {
            RSIStatus.OVERSOLD: 10,
            RSIStatus.STRONG_BUY: 8,
            RSIStatus.NEUTRAL: 5,
            RSIStatus.WEAK: 3,
            RSIStatus.OVERBOUGHT: 0,
        }
        rsi_score = rsi_scores.get(result.rsi_status, 5)
        score += rsi_score
        
        if result.rsi_status in [RSIStatus.OVERSOLD, RSIStatus.STRONG_BUY]:
            reasons.append(f"✅ {result.rsi_signal}")
        elif result.rsi_status == RSIStatus.OVERBOUGHT:
            risks.append(f"⚠️ {result.rsi_signal}")
        else:
            reasons.append(result.rsi_signal)
        
        # Overall judgment
        result.signal_score = score
        result.signal_reasons = reasons
        result.risk_factors = risks
        
        if score >= 75 and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            result.buy_signal = BuySignal.STRONG_BUY
        elif score >= 60 and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL]:
            result.buy_signal = BuySignal.BUY
        elif score >= 45:
            result.buy_signal = BuySignal.HOLD
        elif score >= 30:
            result.buy_signal = BuySignal.WAIT
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            result.buy_signal = BuySignal.STRONG_SELL
        else:
            result.buy_signal = BuySignal.SELL


def analyze_stock(df: pd.DataFrame, code: str) -> TrendAnalysisResult:
    """Convenience function: analyze a single stock"""
    analyzer = StockTrendAnalyzer()
    return analyzer.analyze(df, code)
