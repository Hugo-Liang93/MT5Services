"""
Unified indicator manager.

Event-driven indicator computation backed by the market data cache.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from src.config.indicator_config import (
    IndicatorConfig,
    UnifiedIndicatorConfig,
    get_global_config_manager,
)
from src.core.market_service import MarketDataService
from src.utils.event_store import get_event_store

from .engine.dependency_manager import get_global_dependency_manager
from .engine.pipeline_v2 import OptimizedPipeline, get_global_pipeline
from .monitoring.metrics_collector import get_global_collector

if TYPE_CHECKING:
    from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)


@dataclass
class IndicatorResult:
    name: str
    value: Dict[str, Any]
    symbol: str
    timeframe: str
    timestamp: datetime
    bar_time: Optional[datetime] = None
    cache_hit: bool = False
    incremental: bool = False
    compute_time_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None


@dataclass
class IndicatorSnapshot:
    symbol: str
    timeframe: str
    data: Dict[str, Any]
    timestamp: datetime
    bar_time: Optional[datetime] = None
    compute_time_ms: float = 0.0


class UnifiedIndicatorManager:
    """
    Single runtime entrypoint for indicator registration, scheduling and persistence.

    Runtime behavior:
    - consume closed-bar events from the durable event store
    - reconcile all configured symbol/timeframe pairs on an interval as a fallback
    - write indicator values back into the market cache and the OHLC persistence channel
    """

    def __init__(
        self,
        market_service: MarketDataService,
        config: Optional[UnifiedIndicatorConfig] = None,
        config_file: Optional[str] = None,
        storage_writer: Optional["StorageWriter"] = None,
    ):
        self.market_service = market_service
        self.storage_writer = storage_writer
        self.config_manager = get_global_config_manager(config_file)
        self.config = config or self.config_manager.get_config()
        self.event_store = get_event_store("events.db")

        self._indicator_funcs: Dict[str, Callable] = {}
        self._results: Dict[str, IndicatorResult] = {}
        self._results_lock = threading.RLock()
        self._stop = threading.Event()
        self._event_thread: Optional[threading.Thread] = None
        self._reload_thread: Optional[threading.Thread] = None
        self._last_reconcile_at: Optional[datetime] = None

        self._init_components()
        self._register_indicators()
        self.market_service.set_ohlc_event_sink(self._publish_closed_bar_event)

        logger.info(
            "UnifiedIndicatorManager initialized with %s indicators",
            len(self.config.indicators),
        )

    def _init_components(self) -> None:
        self.pipeline: OptimizedPipeline = get_global_pipeline(self.config.pipeline)
        self.pipeline.config = self.config.pipeline
        self.dependency_manager = get_global_dependency_manager()
        self.dependency_manager.clear()
        self.metrics_collector = get_global_collector()

    def _register_indicators(self) -> None:
        self._indicator_funcs.clear()
        for indicator_config in self.config.indicators:
            if not indicator_config.enabled:
                continue
            func = self._load_indicator_func(indicator_config)
            self.dependency_manager.add_indicator(
                name=indicator_config.name,
                func=func,
                params=indicator_config.params,
                dependencies=indicator_config.dependencies,
            )
            self._indicator_funcs[indicator_config.name] = func

    def _load_indicator_func(self, config: IndicatorConfig) -> Callable:
        cached = self._indicator_funcs.get(config.name)
        if cached is not None:
            return cached
        module_path, func_name = config.func_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, func_name)

    def start(self) -> None:
        if self._event_thread and self._event_thread.is_alive():
            return

        self._stop.clear()
        self.event_store.reset_processing_events()
        self.market_service.set_ohlc_event_sink(self._publish_closed_bar_event)
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="IndicatorEventLoop",
            daemon=True,
        )
        self._event_thread.start()

        if self.config.hot_reload and not (
            self._reload_thread and self._reload_thread.is_alive()
        ):
            self._reload_thread = threading.Thread(
                target=self._reload_loop,
                name="IndicatorConfigReloader",
                daemon=True,
            )
            self._reload_thread.start()

        logger.info("UnifiedIndicatorManager started")

    def stop(self) -> None:
        self._stop.set()
        if self._event_thread:
            self._event_thread.join(timeout=5.0)
        if self._reload_thread:
            self._reload_thread.join(timeout=2.0)
        self.market_service.set_ohlc_event_sink(None)
        logger.info("UnifiedIndicatorManager stopped")

    def _event_loop(self) -> None:
        reconcile_interval = max(float(self.config.pipeline.poll_interval), 0.5)
        next_reconcile_at = time.monotonic()

        while not self._stop.is_set():
            durable_event = self.event_store.get_next_event()
            if durable_event is not None:
                self._process_closed_bar_event(*durable_event, durable_event=True)
                continue

            now = time.monotonic()
            if now >= next_reconcile_at:
                self._reconcile_all()
                self._last_reconcile_at = datetime.utcnow()
                next_reconcile_at = time.monotonic() + reconcile_interval
                continue

            timeout = min(0.5, max(0.0, next_reconcile_at - now))
            legacy_event = self.market_service.get_ohlc_event(timeout=timeout)
            if legacy_event is not None:
                self._process_closed_bar_event(*legacy_event, durable_event=False)

    def _reload_loop(self) -> None:
        reload_interval = max(float(self.config.reload_interval), 1.0)
        while not self._stop.wait(reload_interval):
            try:
                if self.config_manager.reload():
                    self.config = self.config_manager.get_config()
                    self._reinitialize()
            except Exception:
                logger.exception("Indicator config reload failed")

    def _reinitialize(self) -> None:
        logger.info("Reinitializing indicator manager after config reload")
        self._init_components()
        self._register_indicators()

    def _publish_closed_bar_event(self, symbol: str, timeframe: str, bar_time: datetime) -> None:
        self.event_store.publish_event(symbol, timeframe, bar_time)

    def _load_bars(
        self,
        symbol: str,
        timeframe: str,
        bar_time: Optional[datetime] = None,
    ) -> List[Any]:
        bars = self.market_service.get_ohlc(
            symbol,
            timeframe,
            count=self._get_max_lookback(),
        )
        if bar_time is None:
            return bars
        selected = [bar for bar in bars if bar.time <= bar_time]
        if not selected or selected[-1].time != bar_time:
            return []
        return selected

    def _resolve_indicator_names(
        self,
        indicator_names: Optional[List[str]] = None,
    ) -> List[str]:
        if indicator_names is not None:
            return indicator_names
        return [config.name for config in self.config.indicators if config.enabled]

    def _run_pipeline(
        self,
        symbol: str,
        timeframe: str,
        indicator_names: Optional[List[str]] = None,
        bar_time: Optional[datetime] = None,
    ) -> Tuple[List[Any], Dict[str, Dict[str, Any]], float]:
        bars = self._load_bars(symbol, timeframe, bar_time=bar_time)
        if len(bars) < 2:
            return [], {}, 0.0

        selected_names = self._resolve_indicator_names(indicator_names)
        started_at = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, selected_names)
        compute_time_ms = (time.time() - started_at) * 1000
        return bars, results, compute_time_ms

    @staticmethod
    def _normalize_metric_name(
        indicator_name: str,
        metric_name: str,
        metric_count: int,
    ) -> str:
        if metric_count == 1:
            return indicator_name
        if metric_name == indicator_name:
            return indicator_name
        return f"{indicator_name}_{metric_name}"

    def _flatten_indicator_values(
        self,
        results: Dict[str, Dict[str, Any]],
    ) -> Dict[str, float]:
        flattened: Dict[str, float] = {}
        for indicator_name, payload in results.items():
            if not isinstance(payload, dict) or not payload:
                continue
            metric_count = len(payload)
            for metric_name, raw_value in payload.items():
                if not isinstance(raw_value, (int, float)):
                    continue
                flattened[
                    self._normalize_metric_name(
                        indicator_name,
                        metric_name,
                        metric_count,
                    )
                ] = float(raw_value)
        return flattened

    def _store_results(
        self,
        symbol: str,
        timeframe: str,
        bar_time: Optional[datetime],
        results: Dict[str, Dict[str, Any]],
        compute_time_ms: float,
    ) -> None:
        timestamp = datetime.utcnow()
        with self._results_lock:
            for name, value in results.items():
                if value is None:
                    continue
                result_key = f"{symbol}_{timeframe}_{name}"
                self._results[result_key] = IndicatorResult(
                    name=name,
                    value=value,
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=timestamp,
                    bar_time=bar_time,
                    cache_hit=False,
                    incremental=False,
                    compute_time_ms=compute_time_ms,
                    success=True,
                )

    def _write_back_results(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        results: Dict[str, Dict[str, Any]],
        compute_time_ms: float,
        bar_time: Optional[datetime] = None,
    ) -> Dict[str, float]:
        effective_bar_time = bar_time or (bars[-1].time if bars else None)
        self._store_results(symbol, timeframe, effective_bar_time, results, compute_time_ms)

        if not bars or effective_bar_time is None:
            return {}

        flattened = self._flatten_indicator_values(results)
        if not flattened:
            return {}

        latest_bar = bars[-1]
        self.market_service.update_ohlc_indicators(
            symbol,
            timeframe,
            effective_bar_time,
            flattened,
        )

        if self.storage_writer is not None:
            row = (
                latest_bar.symbol,
                latest_bar.timeframe,
                latest_bar.open,
                latest_bar.high,
                latest_bar.low,
                latest_bar.close,
                latest_bar.volume,
                latest_bar.time.isoformat(),
                dict(getattr(latest_bar, "indicators", {}) or {}),
            )
            self.storage_writer.enqueue("ohlc_indicators", row)

        return flattened

    def _process_closed_bar_event(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        durable_event: bool,
    ) -> None:
        try:
            bars, results, compute_time_ms = self._run_pipeline(
                symbol,
                timeframe,
                bar_time=bar_time,
            )
            if not bars:
                raise ValueError(
                    f"OHLC cache is missing the closed bar for {symbol}/{timeframe} at {bar_time}"
                )
            self._write_back_results(
                symbol,
                timeframe,
                bars,
                results,
                compute_time_ms,
                bar_time=bar_time,
            )
            if durable_event:
                self.event_store.mark_event_completed(symbol, timeframe, bar_time)
        except Exception as exc:
            logger.exception(
                "Failed to process closed-bar event for %s/%s at %s",
                symbol,
                timeframe,
                bar_time,
            )
            if durable_event:
                self.event_store.mark_event_failed(symbol, timeframe, bar_time, str(exc))

    def _reconcile_all(self) -> None:
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                try:
                    self._reconcile_symbol_timeframe(symbol, timeframe)
                except Exception:
                    logger.exception(
                        "Indicator reconcile failed for %s/%s",
                        symbol,
                        timeframe,
                    )

    def _reconcile_symbol_timeframe(self, symbol: str, timeframe: str) -> None:
        bars, results, compute_time_ms = self._run_pipeline(symbol, timeframe)
        if not bars:
            return
        self._write_back_results(symbol, timeframe, bars, results, compute_time_ms)

    def _get_max_lookback(self) -> int:
        max_lookback = 0
        for config in self.config.indicators:
            if not config.enabled:
                continue
            params = config.params
            max_lookback = max(
                max_lookback,
                int(params.get("min_bars", 0)),
                int(params.get("period", 0)),
                int(params.get("slow", 0)),
                int(params.get("signal", 0)),
                int(params.get("fast", 0)),
                int(params.get("atr_period", 0)),
            )
        return max(max_lookback, 100)

    def get_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator_name: str,
    ) -> Optional[Dict[str, Any]]:
        result_key = f"{symbol}_{timeframe}_{indicator_name}"
        with self._results_lock:
            result = self._results.get(result_key)
        if result:
            return result.value
        persisted = self.market_service.latest_indicators(symbol, timeframe)
        if indicator_name in persisted:
            return {indicator_name: persisted[indicator_name]}
        return None

    def get_all_indicators(
        self,
        symbol: str,
        timeframe: str,
    ) -> Dict[str, Dict[str, Any]]:
        prefix = f"{symbol}_{timeframe}_"
        results: Dict[str, Dict[str, Any]] = {}
        with self._results_lock:
            for key, result in self._results.items():
                if key.startswith(prefix):
                    results[key[len(prefix) :]] = result.value
        if results:
            return results
        persisted = self.market_service.latest_indicators(symbol, timeframe)
        return {name: {name: value} for name, value in persisted.items()}

    def compute(
        self,
        symbol: str,
        timeframe: str,
        indicator_names: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        bars, results, compute_time_ms = self._run_pipeline(
            symbol,
            timeframe,
            indicator_names=indicator_names,
        )
        if not bars:
            logger.warning("Insufficient data for computation: %s/%s", symbol, timeframe)
            return {}
        self._write_back_results(symbol, timeframe, bars, results, compute_time_ms)
        return results

    def get_indicator_info(self, name: str) -> Optional[Dict[str, Any]]:
        config = self.config_manager.get_indicator(name)
        if config is None:
            return None
        return {
            "name": config.name,
            "func_path": config.func_path,
            "params": config.params,
            "dependencies": list(self.dependency_manager.get_dependencies(name)),
            "dependents": list(self.dependency_manager.get_dependents(name)),
            "compute_mode": config.compute_mode.value,
            "enabled": config.enabled,
            "description": config.description,
            "tags": config.tags,
        }

    def list_indicators(self) -> List[Dict[str, Any]]:
        return [
            info
            for config in self.config.indicators
            if (info := self.get_indicator_info(config.name)) is not None
        ]

    def add_indicator(self, config: IndicatorConfig) -> bool:
        try:
            self.config_manager.add_indicator(config)
            self.config = self.config_manager.get_config()
            self._register_indicators()
            logger.info("Added indicator: %s", config.name)
            return True
        except Exception:
            logger.exception("Failed to add indicator %s", config.name)
            return False

    def update_indicator(self, name: str, **kwargs: Any) -> bool:
        config = self.config_manager.get_indicator(name)
        if config is None:
            logger.error("Indicator not found: %s", name)
            return False
        try:
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            self.config_manager.add_indicator(config)
            self.config = self.config_manager.get_config()
            self._register_indicators()
            logger.info("Updated indicator: %s", name)
            return True
        except Exception:
            logger.exception("Failed to update indicator %s", name)
            return False

    def remove_indicator(self, name: str) -> bool:
        try:
            if not self.config_manager.remove_indicator(name):
                return False
            self.config = self.config_manager.get_config()
            self.dependency_manager.remove_indicator(name)
            self._indicator_funcs.pop(name, None)
            with self._results_lock:
                stale_keys = [key for key in self._results if key.endswith(f"_{name}")]
                for key in stale_keys:
                    del self._results[key]
            logger.info("Removed indicator: %s", name)
            return True
        except Exception:
            logger.exception("Failed to remove indicator %s", name)
            return False

    def get_performance_stats(self) -> Dict[str, Any]:
        pipeline_stats = self.pipeline.get_stats()
        cache_stats = pipeline_stats.get("cache", {})
        with self._results_lock:
            result_stats = {
                "total_results": len(self._results),
                "symbols": len({result.symbol for result in self._results.values()}),
                "timeframes": len({result.timeframe for result in self._results.values()}),
                "indicators": len({result.name for result in self._results.values()}),
            }
        config_stats = {
            "total_indicators": len(self.config.indicators),
            "enabled_indicators": len([c for c in self.config.indicators if c.enabled]),
            "symbols": len(self.config.symbols),
            "timeframes": len(self.config.timeframes),
            "hot_reload": self.config.hot_reload,
            "auto_start": self.config.auto_start,
            "reconcile_interval_seconds": self.config.pipeline.poll_interval,
        }
        return {
            "mode": "event_driven",
            "event_loop_running": bool(self._event_thread and self._event_thread.is_alive()),
            "last_reconcile_at": (
                self._last_reconcile_at.isoformat() if self._last_reconcile_at else None
            ),
            "total_computations": pipeline_stats.get("total_computations", 0),
            "failed_computations": pipeline_stats.get("failed_computations", 0),
            "cached_computations": pipeline_stats.get("cached_computations", 0),
            "incremental_computations": pipeline_stats.get("incremental_computations", 0),
            "parallel_computations": pipeline_stats.get("parallel_computations", 0),
            "cache_hits": cache_stats.get("hits", 0),
            "cache_misses": cache_stats.get("misses", 0),
            "success_rate": pipeline_stats.get("success_rate", 0),
            "event_store": self.event_store.get_stats(),
            "pipeline": pipeline_stats,
            "results": result_stats,
            "config": config_stats,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def clear_cache(self) -> int:
        cleared = self.pipeline.clear_cache()
        with self._results_lock:
            self._results.clear()
        return cleared

    def get_dependency_graph(self, format: str = "mermaid") -> str:
        return self.dependency_manager.visualize(format)

    def get_snapshot(self, symbol: str, timeframe: str) -> Optional[IndicatorSnapshot]:
        prefix = f"{symbol}_{timeframe}_"
        with self._results_lock:
            matches = [result for key, result in self._results.items() if key.startswith(prefix)]
        if not matches:
            persisted = self.market_service.latest_indicators(symbol, timeframe)
            if not persisted:
                return None
            latest_bar = self.market_service.get_ohlc(symbol, timeframe, count=1)
            latest_time = latest_bar[-1].time if latest_bar else None
            return IndicatorSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                data={name: {name: value} for name, value in persisted.items()},
                timestamp=datetime.utcnow(),
                bar_time=latest_time,
                compute_time_ms=0.0,
            )
        latest = max(matches, key=lambda result: result.timestamp)
        return IndicatorSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            data={result.name: result.value for result in matches},
            timestamp=latest.timestamp,
            bar_time=latest.bar_time,
            compute_time_ms=latest.compute_time_ms,
        )

    def trigger_consistency_check(self) -> None:
        self._reconcile_all()

    def reset_failed_events(self) -> int:
        return self.event_store.reset_failed_events()

    def cleanup_old_events(self, days_to_keep: int = 7) -> None:
        self.event_store.cleanup_old_events(days_to_keep)

    def stats(self) -> Dict[str, Any]:
        return self.get_performance_stats()

    def shutdown(self) -> None:
        self.stop()
        self.pipeline.shutdown()


_global_unified_manager: Optional[UnifiedIndicatorManager] = None


def get_global_unified_manager(
    market_service: Optional[MarketDataService] = None,
    config_file: Optional[str] = None,
    storage_writer: Optional["StorageWriter"] = None,
    start_immediately: Optional[bool] = None,
) -> UnifiedIndicatorManager:
    global _global_unified_manager
    if _global_unified_manager is None:
        if market_service is None:
            raise ValueError("market_service is required on the first manager initialization")
        _global_unified_manager = UnifiedIndicatorManager(
            market_service=market_service,
            config_file=config_file,
            storage_writer=storage_writer,
        )
        should_start = (
            _global_unified_manager.config.auto_start
            if start_immediately is None
            else start_immediately
        )
        if should_start:
            _global_unified_manager.start()
    return _global_unified_manager


def shutdown_global_unified_manager() -> None:
    global _global_unified_manager
    if _global_unified_manager is not None:
        _global_unified_manager.shutdown()
        _global_unified_manager = None
