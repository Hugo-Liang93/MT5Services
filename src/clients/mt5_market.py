"""
MT5 行情客户端：负责初始化、登录、行情/tick/K 线获取，并做时区转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.clients.base import MT5BaseClient, MT5BaseError, mt5


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    time: datetime


@dataclass
class Tick:
    symbol: str
    price: float
    volume: float
    time: datetime
    time_msc: Optional[int] = None


@dataclass
class OHLC:
    symbol: str
    timeframe: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    indicators: Optional[Dict[str, Any]] = None


@dataclass
class SymbolInfo:
    symbol: str
    description: str
    digits: int
    point: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    margin_initial: float
    margin_maintenance: float
    tick_value: float
    tick_size: float


TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class MT5MarketError(MT5BaseError):
    pass


class MT5MarketClient(MT5BaseClient):
    """行情相关 API 封装。"""

    def __init__(self, settings: Optional[object] = None):
        super().__init__(settings=settings)

    @MT5BaseClient.measured("list_symbols")
    def list_symbols(self) -> List[str]:
        self.connect()
        symbols = mt5.symbols_get()
        return [s.name for s in symbols] if symbols else []

    @MT5BaseClient.measured("get_symbol_info")
    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """获取品种元数据，用于风控/下单参数校验。"""
        self.connect()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5MarketError(f"Failed to get symbol info for {symbol}: {mt5.last_error()}")
        return SymbolInfo(
            symbol=info.name,
            description=self._get_field(info, "description", "") or "",
            digits=int(self._get_field(info, "digits", 0) or 0),
            point=float(self._get_field(info, "point", 0.0) or 0.0),
            trade_contract_size=float(self._get_field(info, "trade_contract_size", 0.0) or 0.0),
            volume_min=float(self._get_field(info, "volume_min", 0.0) or 0.0),
            volume_max=float(self._get_field(info, "volume_max", 0.0) or 0.0),
            volume_step=float(self._get_field(info, "volume_step", 0.0) or 0.0),
            margin_initial=float(self._get_field(info, "margin_initial", 0.0) or 0.0),
            margin_maintenance=float(self._get_field(info, "margin_maintenance", 0.0) or 0.0),
            tick_value=float(self._get_field(info, "trade_tick_value", 0.0) or 0.0),
            tick_size=float(self._get_field(info, "trade_tick_size", 0.0) or 0.0),
        )

    @MT5BaseClient.measured("get_quote")
    def get_quote(self, symbol: str) -> Quote:
        self.connect()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5MarketError(f"Symbol {symbol} not found or no tick data")
        bid = float(self._get_field(tick, "bid", 0.0) or 0.0)
        ask = float(self._get_field(tick, "ask", 0.0) or 0.0)
        last = float(self._get_field(tick, "last", 0.0) or 0.0)
        return Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=self._normalize_last_price(bid, ask, last),
            volume=float(self._get_field(tick, "volume", 0.0) or 0.0),
            time=self._market_time_from_seconds(tick.time),
        )

    @MT5BaseClient.measured("get_ticks")
    def get_ticks(self, symbol: str, limit: int, start: Optional[datetime] = None) -> List[Tick]:
        """支持从指定时间开始拉取，便于增量采集。"""
        self.connect()
        try:
            configured_lookback = self.settings.tick_initial_lookback_seconds
        except AttributeError:
            configured_lookback = 20
        lookback_seconds = int(configured_lookback or 20)
        start = start or (datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds))
        ticks = mt5.copy_ticks_from(symbol, start, limit, mt5.COPY_TICKS_ALL)
        if ticks is None:
            raise MT5MarketError(f"Failed to get ticks for {symbol}: {mt5.last_error()}")
        return [
            Tick(
                symbol=symbol,
                price=self._extract_price(tick),
                volume=self._extract_volume(tick),
                time=self._tick_timestamp(tick),
                time_msc=self._tick_time_msc(tick),
            )
                for tick in ticks
            ]

    @MT5BaseClient.measured("get_ohlc")
    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[OHLC]:
        self.connect()
        tf = self._timeframe_to_mt5(timeframe)
        # 获取从当前位置开始的 K 线数据
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, limit)
        if rates is None:
            raise MT5MarketError(f"Failed to get OHLC for {symbol}: {mt5.last_error()}")
        return [
            OHLC(
                symbol=symbol,
                timeframe=timeframe,
                time=self._market_time_from_seconds(self._get_field(rate, "time")),
                open=self._get_field(rate, "open"),
                high=self._get_field(rate, "high"),
                low=self._get_field(rate, "low"),
                close=self._get_field(rate, "close"),
                volume=self._get_field(rate, "real_volume", 0.0),
                )
                for rate in rates
            ]

    @MT5BaseClient.measured("get_ohlc_from")
    def get_ohlc_from(self, symbol: str, timeframe: str, start: datetime, limit: int) -> List[OHLC]:
        """从指定时间开始获取最多 limit 条 K 线，便于补全历史。"""
        self.connect()
        tf = self._timeframe_to_mt5(timeframe)
        request_start = self._market_time_to_request(start)
        rates = mt5.copy_rates_from(symbol, tf, request_start, limit)
        if rates is None:
            raise MT5MarketError(f"Failed to get OHLC from {start} for {symbol}: {mt5.last_error()}")
        return [
            OHLC(
                symbol=symbol,
                timeframe=timeframe,
                time=self._market_time_from_seconds(self._get_field(rate, "time")),
                open=self._get_field(rate, "open"),
                high=self._get_field(rate, "high"),
                low=self._get_field(rate, "low"),
                close=self._get_field(rate, "close"),
                volume=self._get_field(rate, "real_volume", 0.0),
            )
            for rate in rates
        ]

    def _timeframe_to_mt5(self, timeframe: str) -> int:
        key = TIMEFRAME_MAP.get(timeframe.upper())
        if not key or not hasattr(mt5, key):
            raise MT5MarketError(f"Unsupported timeframe {timeframe}")
        return getattr(mt5, key)

    def _extract_price(self, tick) -> float:
        price = self._get_field(tick, "last")
        if price is None or price == 0:
            price = self._get_field(tick, "bid")
        if price is None or price == 0:
            price = self._get_field(tick, "ask", 0.0)
        return float(price)

    def _extract_volume(self, tick) -> float:
        vol = self._get_field(tick, "volume_real")
        if vol is None or vol == 0:
            vol = self._get_field(tick, "volume", 0.0)
        return float(vol)

    @staticmethod
    def _normalize_last_price(bid: float, ask: float, last: float) -> float:
        if last and last > 0:
            return float(last)
        if bid > 0 and ask > 0:
            return float((bid + ask) / 2.0)
        if bid > 0:
            return float(bid)
        if ask > 0:
            return float(ask)
        return float(last)

    def _tick_timestamp(self, tick) -> datetime:
        time_msc = self._get_field(tick, "time_msc")
        if time_msc is not None:
            return self._market_time_from_milliseconds(int(time_msc))
        return self._market_time_from_seconds(self._get_field(tick, "time"))

    def _tick_time_msc(self, tick) -> Optional[int]:
        time_msc = self._get_field(tick, "time_msc")
        if time_msc is not None:
            return int(time_msc)
        time_seconds = self._get_field(tick, "time")
        if time_seconds is None:
            return None
        return int(time_seconds) * 1000
