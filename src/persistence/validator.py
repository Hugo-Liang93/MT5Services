"""
数据验证器：在写入数据库前验证数据的有效性。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Any

logger = logging.getLogger(__name__)


class DataValidator:
    """数据验证器，用于验证各种金融数据的有效性"""
    
    # 验证配置
    MAX_PRICE = 1000000.0  # 最大价格限制
    MIN_PRICE = 0.00001    # 最小价格限制
    MAX_VOLUME = 1e12      # 最大交易量
    
    @staticmethod
    def validate_tick(symbol: str, price: float, volume: float, time_str: str) -> Tuple[bool, str]:
        """
        验证 tick 数据有效性
        
        Args:
            symbol: 交易品种
            price: 价格
            volume: 交易量
            time_str: ISO 格式时间字符串
            
        Returns:
            (是否有效, 错误信息)
        """
        try:
            # 1. 验证时间格式
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                dt = dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                return False, f"Invalid time format: {time_str}"
            
            # 2. 验证时间范围（不能是未来时间，也不能太旧）
            now = datetime.now(timezone.utc)
            if dt > now + timedelta(seconds=10):
                return False, f"Future time detected: {dt} > {now}"
            
            # 3. 验证价格
            if price <= 0:
                return False, f"Invalid price: {price} <= 0"
            if price > DataValidator.MAX_PRICE:
                return False, f"Price too high: {price} > {DataValidator.MAX_PRICE}"
            if price < DataValidator.MIN_PRICE:
                return False, f"Price too low: {price} < {DataValidator.MIN_PRICE}"
            
            # 4. 验证交易量
            if volume < 0:
                return False, f"Invalid volume: {volume} < 0"
            if volume > DataValidator.MAX_VOLUME:
                return False, f"Volume too large: {volume} > {DataValidator.MAX_VOLUME}"
            
            # 5. 验证交易品种
            if not symbol or len(symbol.strip()) == 0:
                return False, "Empty symbol"
            if len(symbol) > 20:
                return False, f"Symbol too long: {symbol}"
            
            return True, ""
            
        except Exception as e:
            logger.error("Tick validation error: %s", e)
            return False, f"Validation error: {str(e)}"
    
    @staticmethod
    def validate_quote(symbol: str, bid: float, ask: float, last: float, 
                      volume: float, time_str: str) -> Tuple[bool, str]:
        """
        验证报价数据有效性
        
        Args:
            symbol: 交易品种
            bid: 买价
            ask: 卖价
            last: 最新价
            volume: 交易量
            time_str: ISO 格式时间字符串
            
        Returns:
            (是否有效, 错误信息)
        """
        try:
            # 1. 验证时间格式
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                dt = dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                return False, f"Invalid time format: {time_str}"
            
            # 2. 验证时间范围
            now = datetime.now(timezone.utc)
            if dt > now + timedelta(seconds=30):
                return False, f"Future time detected: {dt} > {now}"
            
            # 3. 验证价格关系
            if bid <= 0 or ask <= 0 or last <= 0:
                return False, f"Invalid prices: bid={bid}, ask={ask}, last={last}"
            
            if bid > ask:
                return False, f"Bid > Ask: {bid} > {ask}"
            
            # 检查价差合理性（避免异常数据）
            spread = ask - bid
            if spread < 0:
                return False, f"Negative spread: {spread}"
            
            # 4. 验证交易量
            if volume < 0:
                return False, f"Invalid volume: {volume} < 0"
            if volume > DataValidator.MAX_VOLUME:
                return False, f"Volume too large: {volume} > {DataValidator.MAX_VOLUME}"
            
            # 5. 验证交易品种
            if not symbol or len(symbol.strip()) == 0:
                return False, "Empty symbol"
            
            return True, ""
            
        except Exception as e:
            logger.error("Quote validation error: %s", e)
            return False, f"Validation error: {str(e)}"
    
    @staticmethod
    def validate_ohlc(symbol: str, timeframe: str, open_: float, high: float, 
                     low: float, close: float, volume: float, time_str: str,
                     indicators: Optional[dict] = None) -> Tuple[bool, str]:
        """
        验证 OHLC 数据有效性
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            open_: 开盘价
            high: 最高价
            low: 最低价
            close: 收盘价
            volume: 交易量
            time_str: ISO 格式时间字符串
            indicators: 指标数据
            
        Returns:
            (是否有效, 错误信息)
        """
        try:
            # 1. 验证时间格式
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                dt = dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                return False, f"Invalid time format: {time_str}"
            
            # 2. 验证时间范围
            now = datetime.now(timezone.utc)
            if dt > now + timedelta(minutes=10):
                return False, f"Future time detected: {dt} > {now}"
            
            # 3. 验证价格关系
            if not (low <= open_ <= high and low <= close <= high):
                return False, f"Price relationship invalid: L={low}, O={open_}, H={high}, C={close}"
            
            if high < low:
                return False, f"High < Low: {high} < {low}"
            
            # 4. 验证价格值
            for price in [open_, high, low, close]:
                if price <= 0:
                    return False, f"Invalid price: {price} <= 0"
                if price > DataValidator.MAX_PRICE:
                    return False, f"Price too high: {price} > {DataValidator.MAX_PRICE}"
            
            # 5. 验证交易量
            if volume < 0:
                return False, f"Invalid volume: {volume} < 0"
            if volume > DataValidator.MAX_VOLUME:
                return False, f"Volume too large: {volume} > {DataValidator.MAX_VOLUME}"
            
            # 6. 验证交易品种
            if not symbol or len(symbol.strip()) == 0:
                return False, "Empty symbol"
            
            # 7. 验证时间框架
            valid_timeframes = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN1']
            if timeframe not in valid_timeframes:
                return False, f"Invalid timeframe: {timeframe}"
            
            # 8. 验证指标数据（如果存在）
            if indicators is not None:
                if not isinstance(indicators, dict):
                    return False, f"Indicators must be dict, got {type(indicators)}"
                
                # 检查指标值是否合理
                for key, value in indicators.items():
                    if isinstance(value, (int, float)):
                        if not (-1e9 <= value <= 1e9):
                            return False, f"Indicator {key} value out of range: {value}"
            
            return True, ""
            
        except Exception as e:
            logger.error("OHLC validation error: %s", e)
            return False, f"Validation error: {str(e)}"
    
    @staticmethod
    def validate_intrabar(symbol: str, timeframe: str, open_: float, high: float,
                         low: float, close: float, volume: float, 
                         bar_time_str: str, recorded_at_str: str) -> Tuple[bool, str]:
        """
        验证盘中数据有效性
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            open_: 开盘价
            high: 最高价
            low: 最低价
            close: 收盘价
            volume: 交易量
            bar_time_str: K线时间 ISO 字符串
            recorded_at_str: 记录时间 ISO 字符串
            
        Returns:
            (是否有效, 错误信息)
        """
        # 首先验证 OHLC 数据
        valid, msg = DataValidator.validate_ohlc(
            symbol, timeframe, open_, high, low, close, volume, bar_time_str
        )
        if not valid:
            return False, msg
        
        # 验证记录时间
        try:
            recorded_at = datetime.fromisoformat(recorded_at_str.replace('Z', '+00:00'))
            recorded_at = recorded_at.astimezone(timezone.utc)
            
            bar_time = datetime.fromisoformat(bar_time_str.replace('Z', '+00:00'))
            bar_time = bar_time.astimezone(timezone.utc)
            
            # 记录时间应该在 K 线时间之后
            if recorded_at < bar_time:
                return False, f"Recorded time before bar time: {recorded_at} < {bar_time}"
            
            now = datetime.now(timezone.utc)
            if recorded_at > now + timedelta(seconds=10):
                return False, f"Future recorded time: {recorded_at} > {now}"
            
            return True, ""
            
        except (ValueError, TypeError) as e:
            return False, f"Invalid time format: {e}"
    
    @staticmethod
    def filter_valid_ticks(rows: List[Tuple[str, float, float, str]]) -> List[Tuple[str, float, float, str]]:
        """过滤有效的 tick 数据"""
        valid_rows = []
        for row in rows:
            symbol, price, volume, time_str = row
            valid, msg = DataValidator.validate_tick(symbol, price, volume, time_str)
            if valid:
                valid_rows.append(row)
            else:
                logger.warning("Invalid tick data dropped: %s - %s", row, msg)
        return valid_rows
    
    @staticmethod
    def filter_valid_quotes(rows: List[Tuple[str, float, float, float, float, str]]) -> List[Tuple[str, float, float, float, float, str]]:
        """过滤有效的报价数据"""
        valid_rows = []
        for row in rows:
            symbol, bid, ask, last, volume, time_str = row
            valid, msg = DataValidator.validate_quote(symbol, bid, ask, last, volume, time_str)
            if valid:
                valid_rows.append(row)
            else:
                logger.warning("Invalid quote data dropped: %s - %s", row, msg)
        return valid_rows
    
    @staticmethod
    def filter_valid_ohlc(rows: List[Tuple], upsert: bool = False) -> List[Tuple]:
        """过滤有效的 OHLC 数据"""
        valid_rows = []
        for row in rows:
            # 处理不同长度的行
            if len(row) == 8:
                symbol, timeframe, open_, high, low, close, volume, time_str = row
                indicators = {}
            elif len(row) == 9:
                symbol, timeframe, open_, high, low, close, volume, time_str, indicators = row
            else:
                logger.warning("Invalid OHLC row length: %s", len(row))
                continue
            
            valid, msg = DataValidator.validate_ohlc(
                symbol, timeframe, open_, high, low, close, volume, time_str, indicators
            )
            if valid:
                valid_rows.append(row)
            else:
                logger.warning("Invalid OHLC data dropped: %s - %s", row[:8], msg)
        return valid_rows
    
    @staticmethod
    def filter_valid_intrabar(rows: List[Tuple[str, str, float, float, float, float, float, str, str]]) -> List[Tuple[str, str, float, float, float, float, float, str, str]]:
        """过滤有效的盘中数据"""
        valid_rows = []
        for row in rows:
            symbol, timeframe, open_, high, low, close, volume, bar_time_str, recorded_at_str = row
            valid, msg = DataValidator.validate_intrabar(
                symbol, timeframe, open_, high, low, close, volume, bar_time_str, recorded_at_str
            )
            if valid:
                valid_rows.append(row)
            else:
                logger.warning("Invalid intrabar data dropped: %s - %s", row, msg)
        return valid_rows