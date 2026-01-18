"""
MT5 行情客户端：负责初始化、登录、行情/tick/K 线获取，并做时区转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

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
    indicators: Optional[Dict[str, float]] = None


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
        return Quote(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
                volume=tick.volume,
                time=self._to_tz(datetime.fromtimestamp(tick.time, tz=timezone.utc)),
            )

    @MT5BaseClient.measured("get_ticks")
    def get_ticks(self, symbol: str, limit: int, start: Optional[datetime] = None) -> List[Tick]:
        """支持从指定时间开始拉取，便于增量采集。"""
        self.connect()
        start = start or (datetime.utcnow() - timedelta(seconds=self.settings.tick_initial_lookback_seconds))
        ticks = mt5.copy_ticks_from(symbol, start, limit, mt5.COPY_TICKS_ALL)
        if ticks is None:
            raise MT5MarketError(f"Failed to get ticks for {symbol}: {mt5.last_error()}")
        return [
            Tick(
                symbol=symbol,
                price=self._extract_price(tick),
                volume=self._extract_volume(tick),
                time=self._to_tz(datetime.fromtimestamp(self._get_field(tick, "time"), tz=timezone.utc)),
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
                time=self._to_tz(datetime.fromtimestamp(self._get_field(rate, "time"), tz=timezone.utc)),
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
        rates = mt5.copy_rates_from(symbol, tf, start, limit)
        if rates is None:
            raise MT5MarketError(f"Failed to get OHLC from {start} for {symbol}: {mt5.last_error()}")
        return [
            OHLC(
                symbol=symbol,
                timeframe=timeframe,
                time=self._to_tz(datetime.fromtimestamp(self._get_field(rate, "time"), tz=timezone.utc)),
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
