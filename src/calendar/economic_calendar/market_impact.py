"""经济事件行情影响分析器。

统计重要经济数据发布前后的市场价格变化、波动率跳变，
提供历史聚合统计和策略消费接口。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.clients.economic_calendar import EconomicCalendarEvent

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _PendingAnalysis:
    """正在等待收集的事件分析任务。"""

    event: EconomicCalendarEvent
    symbols: List[str]
    timeframes: List[str]
    # 各阶段收集的目标时间
    collection_stages: Dict[int, datetime]  # post_minutes → target_time
    completed_stages: set = field(default_factory=set)
    created_at: datetime = field(default_factory=_utc_now)


class MarketImpactAnalyzer:
    """经济事件行情影响分析器。"""

    def __init__(
        self,
        db_writer: Any,
        market_repo: Any = None,
        settings: Any = None,
    ):
        self._db = db_writer
        self._market_repo = market_repo
        self._settings = settings
        self._pending: Dict[str, _PendingAnalysis] = {}
        self._lock = Lock()

    @property
    def _symbols(self) -> List[str]:
        if self._settings and hasattr(self._settings, "market_impact_symbols"):
            return self._settings.market_impact_symbols
        return ["XAUUSD"]

    @property
    def _timeframes(self) -> List[str]:
        if self._settings and hasattr(self._settings, "market_impact_timeframes"):
            return self._settings.market_impact_timeframes
        return ["M1", "M5"]

    @property
    def _importance_min(self) -> int:
        if self._settings and hasattr(self._settings, "market_impact_importance_min"):
            return self._settings.market_impact_importance_min
        return 2

    @property
    def _pre_windows(self) -> List[int]:
        if self._settings and hasattr(self._settings, "market_impact_pre_windows"):
            return self._settings.market_impact_pre_windows
        return [30, 60, 120]

    @property
    def _post_windows(self) -> List[int]:
        if self._settings and hasattr(self._settings, "market_impact_post_windows"):
            return self._settings.market_impact_post_windows
        return [5, 15, 30, 60, 120]

    @property
    def _final_delay_minutes(self) -> int:
        if self._settings and hasattr(self._settings, "market_impact_final_collection_delay_minutes"):
            return self._settings.market_impact_final_collection_delay_minutes
        return 130

    @property
    def _enabled(self) -> bool:
        if self._settings and hasattr(self._settings, "market_impact_enabled"):
            return self._settings.market_impact_enabled
        return False

    # ─────────────────── 数据收集 ───────────────────

    def on_event_released(self, event: EconomicCalendarEvent) -> None:
        """release_watch 检测到事件 released 时调用。"""
        if not self._enabled:
            return
        if (event.importance or 0) < self._importance_min:
            return
        if event.all_day:
            return

        with self._lock:
            if event.event_uid in self._pending:
                return

            released_at = event.released_at or event.scheduled_at
            stages: Dict[int, datetime] = {}
            for post_min in self._post_windows:
                stages[post_min] = released_at + timedelta(minutes=post_min + 1)
            # 最终收集
            stages[0] = released_at + timedelta(minutes=self._final_delay_minutes)

            self._pending[event.event_uid] = _PendingAnalysis(
                event=event,
                symbols=list(self._symbols),
                timeframes=list(self._timeframes),
                collection_stages=stages,
            )
            logger.info(
                "Registered market impact analysis for %s (%s), %d stages",
                event.event_name,
                event.event_uid,
                len(stages),
            )

    def tick(self) -> None:
        """在 release_watch 每轮轮询中调用，检查到期的收集任务。"""
        if not self._enabled:
            return

        now = _utc_now()
        completed_uids: List[str] = []

        with self._lock:
            pending_items = list(self._pending.items())

        for uid, analysis in pending_items:
            # 检查各阶段是否到期
            for stage_minutes, target_time in analysis.collection_stages.items():
                if stage_minutes in analysis.completed_stages:
                    continue
                if now < target_time:
                    continue

                # 到期，执行收集
                for symbol in analysis.symbols:
                    for timeframe in analysis.timeframes:
                        try:
                            self._collect_and_store(
                                analysis.event,
                                symbol,
                                timeframe,
                                is_final=(stage_minutes == 0),
                            )
                        except Exception:
                            logger.exception(
                                "Failed to collect market impact for %s %s/%s stage=%d",
                                uid,
                                symbol,
                                timeframe,
                                stage_minutes,
                            )
                analysis.completed_stages.add(stage_minutes)

            # 所有阶段完成
            if len(analysis.completed_stages) >= len(analysis.collection_stages):
                completed_uids.append(uid)

        if completed_uids:
            with self._lock:
                for uid in completed_uids:
                    self._pending.pop(uid, None)
            logger.info("Completed market impact analysis for %d events", len(completed_uids))

    def _collect_and_store(
        self,
        event: EconomicCalendarEvent,
        symbol: str,
        timeframe: str,
        *,
        is_final: bool,
    ) -> None:
        """收集一个事件对一个品种/时间框架的影响数据并写入 DB。"""
        impact = self.collect_impact(event, symbol, timeframe)
        if impact is None:
            return

        status = "complete" if is_final else "partial"
        row = self._impact_to_row(event, symbol, timeframe, impact, status)
        try:
            self._db.write_market_impact([row])
        except Exception:
            logger.exception(
                "Failed to write market impact for %s %s/%s",
                event.event_uid,
                symbol,
                timeframe,
            )

    def collect_impact(
        self,
        event: EconomicCalendarEvent,
        symbol: str,
        timeframe: str,
    ) -> Optional[Dict[str, Any]]:
        """从 market_repo 拉取事件前后 OHLC 数据，计算影响指标。"""
        if self._market_repo is None:
            return None

        event_time = event.released_at or event.scheduled_at
        max_pre = max(self._pre_windows) if self._pre_windows else 120
        max_post = max(self._post_windows) if self._post_windows else 120

        start = event_time - timedelta(minutes=max_pre + 5)
        end = event_time + timedelta(minutes=max_post + 5)

        try:
            bars = self._market_repo.fetch_ohlc_range(
                symbol, timeframe, start, end, limit=2000
            )
        except Exception:
            logger.exception("Failed to fetch OHLC for market impact: %s %s", symbol, timeframe)
            return None

        if not bars:
            return None

        result: Dict[str, Any] = {}

        # 找到事件时刻最近的 bar 作为基准价格
        pre_price = self._find_price_at(bars, event_time)
        result["pre_price"] = pre_price

        if pre_price is None:
            return result

        # OHLC 列索引：(symbol[0], tf[1], open[2], high[3], low[4], close[5], vol[6], time[7])
        _TIME_IDX = 7

        # Pre windows
        for minutes in self._pre_windows:
            window_start = event_time - timedelta(minutes=minutes)
            window_bars = [b for b in bars if window_start <= b[_TIME_IDX] < event_time]
            change, range_val = self._calc_window_stats(window_bars, pre_price)
            result[f"pre_{minutes}m_change"] = change
            result[f"pre_{minutes}m_range"] = range_val

        # Post windows
        for minutes in self._post_windows:
            window_end = event_time + timedelta(minutes=minutes)
            window_bars = [b for b in bars if event_time <= b[_TIME_IDX] <= window_end]
            change, range_val = self._calc_window_stats(window_bars, pre_price)
            result[f"post_{minutes}m_change"] = change
            result[f"post_{minutes}m_range"] = range_val

        # Surprise
        result["surprise_pct"] = self._calc_surprise(event)

        # Volatility spike (简化：用 pre/post 30m range 比率)
        pre_range = result.get("pre_30m_range")
        post_range = result.get("post_30m_range")
        if pre_range and post_range and pre_range > 0:
            result["volatility_spike"] = post_range / pre_range
            result["volatility_pre_atr"] = pre_range
            result["volatility_post_atr"] = post_range

        return result

    # OHLC 列索引常量（对应 market_repo.fetch_ohlc_range 返回格式）
    _COL_HIGH = 3
    _COL_LOW = 4
    _COL_CLOSE = 5
    _COL_TIME = 7

    @staticmethod
    def _find_price_at(bars: List[Tuple], target_time: datetime) -> Optional[float]:
        """找到最接近 target_time 且不晚于它的 bar 的 close 价格。

        bars 格式：(symbol, timeframe, open, high, low, close, volume, time, indicators)
        """
        best_bar = None
        best_diff = None
        for bar in bars:
            bar_time = bar[MarketImpactAnalyzer._COL_TIME]
            if isinstance(bar_time, datetime):
                if bar_time.tzinfo is None:
                    bar_time = bar_time.replace(tzinfo=timezone.utc)
            else:
                continue
            if bar_time > target_time:
                continue
            diff = (target_time - bar_time).total_seconds()
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_bar = bar
        if best_bar is not None:
            return float(best_bar[MarketImpactAnalyzer._COL_CLOSE])
        return None

    @staticmethod
    def _calc_window_stats(
        bars: List[Tuple], pre_price: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """计算窗口内的价格变化和波动幅度。"""
        if not bars:
            return None, None
        _C = MarketImpactAnalyzer._COL_CLOSE
        _H = MarketImpactAnalyzer._COL_HIGH
        _L = MarketImpactAnalyzer._COL_LOW
        closes = [float(b[_C]) for b in bars]
        highs = [float(b[_H]) for b in bars]
        lows = [float(b[_L]) for b in bars]
        last_close = closes[-1]
        change = last_close - pre_price
        range_val = max(highs) - min(lows)
        return change, range_val

    @staticmethod
    def _calc_surprise(event: EconomicCalendarEvent) -> Optional[float]:
        """计算 actual vs forecast 的偏差百分比。"""
        try:
            actual = float(event.actual) if event.actual else None
            forecast = float(event.forecast) if event.forecast else None
        except (ValueError, TypeError):
            return None
        if actual is None or forecast is None or forecast == 0:
            return None
        return (actual - forecast) / abs(forecast) * 100.0

    def _impact_to_row(
        self,
        event: EconomicCalendarEvent,
        symbol: str,
        timeframe: str,
        impact: Dict[str, Any],
        status: str,
    ) -> Tuple:
        """将影响数据转换为 DB 行。"""
        now = _utc_now()
        return (
            now,  # recorded_at
            event.event_uid,
            symbol,
            timeframe,
            event.event_name,
            event.country,
            event.currency,
            event.importance,
            event.scheduled_at,
            event.released_at,
            event.actual,
            event.forecast,
            event.previous,
            impact.get("surprise_pct"),
            impact.get("pre_price"),
            impact.get("pre_30m_change"),
            impact.get("pre_30m_range"),
            impact.get("pre_60m_change"),
            impact.get("pre_60m_range"),
            impact.get("pre_120m_change"),
            impact.get("pre_120m_range"),
            impact.get("post_5m_change"),
            impact.get("post_5m_range"),
            impact.get("post_15m_change"),
            impact.get("post_15m_range"),
            impact.get("post_30m_change"),
            impact.get("post_30m_range"),
            impact.get("post_60m_change"),
            impact.get("post_60m_range"),
            impact.get("post_120m_change"),
            impact.get("post_120m_range"),
            impact.get("volatility_pre_atr"),
            impact.get("volatility_post_atr"),
            impact.get("volatility_spike"),
            status,
            json.dumps({}),  # metadata
        )

    # ─────────────────── 查询接口 ───────────────────

    def get_event_impact(
        self, event_uid: str, symbol: str = "XAUUSD"
    ) -> Optional[Dict[str, Any]]:
        """查询单个事件的行情影响详情。"""
        row = self._db.fetch_market_impact_by_event(event_uid, symbol)
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_aggregated_stats(
        self,
        event_name: Optional[str] = None,
        country: Optional[str] = None,
        importance_min: Optional[int] = None,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """聚合统计：按事件类型分组的历史平均影响。"""
        rows = self._db.fetch_market_impact_stats(
            symbol=symbol,
            timeframe=timeframe,
            event_name=event_name,
            country=country,
            importance_min=importance_min,
            limit=limit,
        )
        results = []
        for row in rows:
            results.append({
                "event_name": row[0],
                "country": row[1],
                "importance": row[2],
                "symbol": row[3],
                "timeframe": row[4],
                "sample_count": row[5],
                "avg_post_5m_change": row[6],
                "avg_post_15m_change": row[7],
                "avg_post_30m_change": row[8],
                "avg_post_60m_change": row[9],
                "avg_post_5m_range": row[10],
                "avg_post_15m_range": row[11],
                "avg_post_30m_range": row[12],
                "avg_post_60m_range": row[13],
                "bullish_pct": row[14],
                "avg_volatility_spike": row[15],
                "max_volatility_spike": row[16],
                "surprise_positive_avg_change": row[17],
                "surprise_negative_avg_change": row[18],
            })
        return results

    def get_impact_forecast(
        self,
        event_name: str,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
    ) -> Optional[Dict[str, Any]]:
        """为策略/Trade Guard 提供的简化接口。"""
        stats = self.get_aggregated_stats(
            event_name=event_name,
            symbol=symbol,
            timeframe=timeframe,
            limit=1,
        )
        if not stats:
            return None
        s = stats[0]
        sample_count = s.get("sample_count", 0)
        if sample_count < 3:
            return None
        return {
            "expected_volatility_spike": s.get("avg_volatility_spike"),
            "directional_bias": s.get("bullish_pct"),
            "avg_post_30m_range": s.get("avg_post_30m_range"),
            "avg_post_30m_change": s.get("avg_post_30m_change"),
            "sample_count": sample_count,
        }

    def backfill(self) -> int:
        """回填过去 N 天内已发布但未统计的事件。"""
        if not self._enabled:
            return 0
        backfill_enabled = getattr(self._settings, "market_impact_backfill_enabled", True)
        if not backfill_enabled:
            return 0
        backfill_days = getattr(self._settings, "market_impact_backfill_days", 30)
        since = _utc_now() - timedelta(days=backfill_days)

        try:
            rows = self._db.fetch_released_events_without_impact(
                self._symbols, since, importance_min=self._importance_min
            )
        except Exception:
            logger.exception("Failed to fetch events for backfill")
            return 0

        count = 0
        for row in rows:
            try:
                event = EconomicCalendarEvent.from_db_row(row)
                if event.all_day:
                    continue
                for symbol in self._symbols:
                    for timeframe in self._timeframes:
                        impact = self.collect_impact(event, symbol, timeframe)
                        if impact:
                            db_row = self._impact_to_row(event, symbol, timeframe, impact, "complete")
                            self._db.write_market_impact([db_row])
                            count += 1
            except Exception:
                logger.exception("Failed to backfill market impact for row: %s", row[:5])
        if count:
            logger.info("Backfilled market impact for %d event/symbol/tf combinations", count)
        return count

    @staticmethod
    def _row_to_dict(row: Tuple) -> Dict[str, Any]:
        """将 DB 行转为字典。"""
        columns = [
            "recorded_at", "event_uid", "symbol", "timeframe",
            "event_name", "country", "currency", "importance",
            "scheduled_at", "released_at",
            "actual", "forecast", "previous", "surprise_pct",
            "pre_price",
            "pre_30m_change", "pre_30m_range",
            "pre_60m_change", "pre_60m_range",
            "pre_120m_change", "pre_120m_range",
            "post_5m_change", "post_5m_range",
            "post_15m_change", "post_15m_range",
            "post_30m_change", "post_30m_range",
            "post_60m_change", "post_60m_range",
            "post_120m_change", "post_120m_range",
            "volatility_pre_atr", "volatility_post_atr", "volatility_spike",
            "analysis_status", "metadata",
        ]
        result = {}
        for i, col in enumerate(columns):
            if i < len(row):
                val = row[i]
                if isinstance(val, datetime):
                    val = val.isoformat()
                result[col] = val
        return result

    def stats(self) -> Dict[str, Any]:
        """返回分析器运行时状态。"""
        with self._lock:
            pending_count = len(self._pending)
            pending_events = [
                {
                    "event_uid": uid,
                    "event_name": a.event.event_name,
                    "completed_stages": len(a.completed_stages),
                    "total_stages": len(a.collection_stages),
                }
                for uid, a in self._pending.items()
            ]
        return {
            "enabled": self._enabled,
            "pending_analyses": pending_count,
            "pending_details": pending_events[:10],
            "symbols": self._symbols,
            "timeframes": self._timeframes,
            "importance_min": self._importance_min,
        }
