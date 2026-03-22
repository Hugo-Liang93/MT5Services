"""
Unified indicator manager.

Event-driven indicator computation backed by the market data cache.
"""

from __future__ import annotations

import importlib
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from src.config.indicator_config import (
    IndicatorConfig,
    UnifiedIndicatorConfig,
    get_global_config_manager,
)
from src.market import MarketDataService
from src.utils.event_store import ClaimedEvent, get_event_store

from .bar_event_handler import (
    process_closed_bar_event,
    process_closed_bar_events_batch,
    process_intrabar_event,
    process_symbol_timeframe_batch,
)
from .cache.incremental import IncrementalIndicator
from .engine.dependency_manager import get_global_dependency_manager
from .engine.pipeline import OptimizedPipeline, get_global_pipeline
from .monitoring.metrics_collector import get_global_collector
from .pipeline_runner import (
    compute_priority_results,
    compute_results_with_priority_groups,
    compute_with_bars,
    run_pipeline,
)
from .result_store import (
    group_indicator_values,
    normalize_persisted_indicator_snapshot,
    store_results,
)
from .snapshot_publisher import (
    publish_intrabar_snapshot,
    publish_snapshot,
    store_preview_snapshot,
    write_back_results,
)

if TYPE_CHECKING:
    from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)
EVENT_BATCH_SIZE = 32


from src.utils.common import ohlc_key, same_listener_reference, timeframe_seconds


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
        self._snapshot_listeners: list[
            Callable[[str, str, datetime, Dict[str, Dict[str, float]], str], None]
        ] = []
        self._snapshot_listeners_lock = threading.Lock()
        # 使用 OrderedDict 实现 LRU 淘汰，防止长期运行后内存无限增长。
        # 上限按 symbols × timeframes 的 10 倍估算，远超正常部署规模。
        self._last_preview_snapshot: OrderedDict[
            str, Tuple[datetime, Dict[str, Dict[str, float]]]
        ] = OrderedDict()
        self._preview_snapshot_max_entries = 500
        self._priority_indicator_groups: tuple[tuple[str, ...], ...] = ()
        # Throttle guard: minimum wall-clock gap between intrabar computations
        # per (symbol, timeframe).  The ingestor already controls frequency via
        # its own next_intrabar_at schedule; this is a defense-in-depth guard
        # that prevents duplicate runs caused by race conditions or config drift.
        self._last_intrabar_compute: Dict[str, float] = {}
        # Intrabar events are dispatched here from the ingestor thread so that
        # indicator computation never blocks data acquisition.  The dedicated
        # _intrabar_thread drains this queue independently.
        self._intrabar_queue: queue.Queue = queue.Queue(maxsize=200)
        self._intrabar_thread: Optional[threading.Thread] = None
        # Cached set of indicator names eligible for intrabar snapshots.
        # Built once in _register_indicators() and invalidated on _reinitialize().
        self._intrabar_eligible_cache: Optional[frozenset] = None
        # Bar-close events are published into this queue from the ingestor thread
        # (via _publish_closed_bar_event) without any I/O.  The dedicated
        # _writer_thread flushes them to SQLite in small batches so that data
        # acquisition is never stalled by a synchronous database write.
        self._event_write_queue: queue.Queue = queue.Queue(maxsize=2048)
        self._writer_thread: Optional[threading.Thread] = None

        self._init_components()
        self._register_indicators()
        self.market_service.set_ohlc_event_sink(self._publish_closed_bar_event)

        logger.info(
            "UnifiedIndicatorManager initialized with %s indicators across %s symbols x %s timeframes",
            len(self.config.indicators),
            len(self.config.symbols),
            len(self.config.timeframes),
        )

    def _init_components(self) -> None:
        self.pipeline: OptimizedPipeline = get_global_pipeline(self.config.pipeline)
        self.pipeline.update_config(self.config.pipeline)
        self.dependency_manager = get_global_dependency_manager()
        self.dependency_manager.clear()
        self.metrics_collector = get_global_collector()

    def _register_indicators(self) -> None:
        self._indicator_funcs.clear()
        self._intrabar_eligible_cache = None  # invalidate on re-registration
        for indicator_config in self.config.indicators:
            if not indicator_config.enabled:
                continue
            func = self._load_indicator_func(indicator_config)
            incremental_class = self._load_incremental_class(indicator_config)
            self.pipeline.register_indicator(
                name=indicator_config.name,
                func=func,
                params=indicator_config.params,
                dependencies=indicator_config.dependencies or None,
                incremental_class=incremental_class,
            )
            self._indicator_funcs[indicator_config.name] = func
        # Intrabar eligible 集合在 set_intrabar_eligible_override() 注入前为空；
        # 正常启动路径下由策略的 preferred_scopes + required_indicators 自动推导。
        self._intrabar_eligible_cache = frozenset()

        # Validate that every dependency of every enabled indicator is itself
        # enabled.  A disabled dependency causes a silent ValueError at runtime
        # (caught inside _compute_indicator) which surfaces as a missing result.
        # Surface the problem early as a warning so operators can fix the config.
        enabled_names = set(self._indicator_funcs)
        for cfg in self.config.indicators:
            if not cfg.enabled:
                continue
            for dep in cfg.dependencies or []:
                if dep not in enabled_names:
                    logger.warning(
                        "Indicator '%s' declares dependency '%s' which is not enabled. "
                        "Computation of '%s' will fail at runtime until '%s' is enabled.",
                        cfg.name,
                        dep,
                        cfg.name,
                        dep,
                    )

    def _load_indicator_func(self, config: IndicatorConfig) -> Callable:
        cached = self._indicator_funcs.get(config.name)
        if cached is not None:
            return cached
        module_path, func_name = config.func_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, func_name)

    def _load_incremental_class(self, config: IndicatorConfig) -> Optional[type]:
        """Return an IncrementalIndicator subclass for *config* when available.

        Convention: if ``compute_mode == "incremental"`` and the indicator's
        module exports a class named ``<FuncName>Incremental`` (e.g. ``ema``
        → ``EmaIncremental``), that class is returned.  If not found, or if
        ``compute_mode`` is not ``"incremental"``, returns ``None`` and the
        pipeline falls back to the standard full-recompute path.
        """
        from src.config.indicator_config import ComputeMode

        if config.compute_mode != ComputeMode.INCREMENTAL:
            return None
        module_path, func_name = config.func_path.rsplit(".", 1)
        class_name = func_name.capitalize() + "Incremental"
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name, None)
            if cls is None or not issubclass(cls, IncrementalIndicator):
                logger.warning(
                    "No IncrementalIndicator subclass '%s' in %s for '%s'; "
                    "falling back to standard full-recompute",
                    class_name,
                    module_path,
                    config.name,
                )
                return None
            return cls
        except Exception as exc:
            logger.warning(
                "Failed to load incremental class for '%s': %s", config.name, exc
            )
            return None

    def start(self) -> None:
        if self._event_thread and self._event_thread.is_alive():
            return

        self._stop.clear()
        self.event_store.reset_processing_events()
        self.market_service.set_ohlc_event_sink(self._publish_closed_bar_event)
        self.market_service.add_intrabar_listener(self._on_intrabar)
        self._writer_thread = threading.Thread(
            target=self._event_writer_loop,
            name="IndicatorEventWriter",
            daemon=True,
        )
        self._writer_thread.start()
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="IndicatorEventLoop",
            daemon=True,
        )
        self._event_thread.start()

        self._intrabar_thread = threading.Thread(
            target=self._intrabar_loop,
            name="IndicatorIntrabar",
            daemon=True,
        )
        self._intrabar_thread.start()

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
        if self._writer_thread:
            self._writer_thread.join(timeout=3.0)
        if self._event_thread:
            self._event_thread.join(timeout=5.0)
        if self._intrabar_thread:
            self._intrabar_thread.join(timeout=2.0)
        if self._reload_thread:
            self._reload_thread.join(timeout=2.0)
        self.market_service.set_ohlc_event_sink(None)
        self.market_service.remove_intrabar_listener(self._on_intrabar)
        logger.info("UnifiedIndicatorManager stopped")

    def _event_loop(self) -> None:
        reconcile_interval = max(float(self.config.pipeline.poll_interval), 0.5)
        next_reconcile_at = time.monotonic()

        while not self._stop.is_set():
            durable_events = self.event_store.claim_next_events(limit=EVENT_BATCH_SIZE)
            if durable_events:
                self._process_closed_bar_events_batch(durable_events, durable_event=True)
                continue

            now = time.monotonic()
            if now >= next_reconcile_at:
                if self._has_reconcile_ready_targets():
                    self._reconcile_all()
                    self._last_reconcile_at = datetime.now(timezone.utc)
                next_reconcile_at = time.monotonic() + reconcile_interval
                continue

            sleep_for = min(0.1, max(0.0, next_reconcile_at - time.monotonic()))
            self._stop.wait(sleep_for)

    def _process_closed_bar_events_batch(
        self,
        events: List[ClaimedEvent],
        durable_event: bool,
    ) -> None:
        process_closed_bar_events_batch(self, events, durable_event)

    def _process_symbol_timeframe_batch(
        self,
        symbol: str,
        timeframe: str,
        events: List[ClaimedEvent],
        durable_event: bool,
    ) -> None:
        process_symbol_timeframe_batch(self, symbol, timeframe, events, durable_event)

    def _on_intrabar(self, symbol: str, timeframe: str, bar: Any) -> None:
        """Called from the ingestor thread — must return instantly.

        Just enqueue the event; the dedicated _intrabar_thread does the heavy
        lifting so that data acquisition is never stalled by indicator computation.
        Overflow is silently dropped: intrabar snapshots are best-effort.
        """
        try:
            self._intrabar_queue.put_nowait((symbol, timeframe, bar))
        except queue.Full:
            pass  # best-effort; the intrabar loop will catch up next cycle

    def _intrabar_loop(self) -> None:
        """Dedicated thread: drains _intrabar_queue and computes live indicators.

        The throttle guard here (not in _on_intrabar) keeps computation at the
        right cadence even when the queue has burst items for the same key.
        """
        while not self._stop.is_set():
            try:
                item = self._intrabar_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # Skip computation entirely when there are no snapshot listeners —
            # nobody would consume the intrabar results, so the CPU cost is pure waste.
            if not self._snapshot_listeners:
                continue
            symbol, timeframe, bar = item
            key = ohlc_key(symbol, timeframe)
            now = time.monotonic()
            # Throttle guard: skip if this (symbol, tf) was computed too recently.
            # Uses 2% of bar duration (min 1 s) as the minimum gap.
            min_gap = max(1.0, timeframe_seconds(timeframe) * 0.02)
            if now - self._last_intrabar_compute.get(key, 0.0) < min_gap:
                continue
            self._last_intrabar_compute[key] = now
            try:
                self._process_intrabar_event(symbol, timeframe, bar)
            except Exception:
                logger.exception(
                    "Failed to process intrabar preview for %s/%s at %s",
                    symbol,
                    timeframe,
                    getattr(bar, "time", None),
                )

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
        # Clear stale cached values so that disabled or re-parameterised
        # indicators do not continue to appear in snapshots / API results.
        self._last_preview_snapshot.clear()
        self.clear_cache()
        self._init_components()
        self._register_indicators()

    def _event_writer_loop(self) -> None:
        """Drain _event_write_queue → SQLite in small batches.

        Runs in a dedicated thread so that bar-close notifications from the
        ingestor thread never block on disk I/O.  Items are flushed in batches
        of up to 64 to amortise SQLite round-trip cost.  Remaining items are
        flushed synchronously on shutdown before the thread exits.
        """
        while not self._stop.is_set():
            batch: list = []
            try:
                item = self._event_write_queue.get(timeout=0.1)
                batch.append(item)
                for _ in range(63):
                    try:
                        batch.append(self._event_write_queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                continue
            try:
                self.event_store.publish_events_batch(batch)
            except Exception:
                logger.exception(
                    "Failed to persist %s queued bar close events in batch",
                    len(batch),
                )
        # Drain any remaining items before the thread exits.
        while True:
            try:
                batch = [self._event_write_queue.get_nowait()]
                for _ in range(63):
                    try:
                        batch.append(self._event_write_queue.get_nowait())
                    except queue.Empty:
                        break
                self.event_store.publish_events_batch(batch)
            except queue.Empty:
                break
            except Exception:
                logger.exception(
                    "Failed to persist %s queued bar close events during shutdown",
                    len(batch),
                )

    def _publish_closed_bar_event(self, symbol: str, timeframe: str, bar_time: datetime) -> None:
        """Non-blocking: enqueue for async SQLite write by _event_writer_loop."""
        try:
            self._event_write_queue.put_nowait((symbol, timeframe, bar_time))
        except queue.Full:
            logger.warning(
                "Event write queue full — bar close event dropped for %s/%s at %s; "
                "reconcile will recover it.",
                symbol, timeframe, bar_time,
            )

    def _load_bars(
        self,
        symbol: str,
        timeframe: str,
        bar_time: Optional[datetime] = None,
    ) -> List[Any]:
        lookback = self._get_max_lookback()
        if bar_time is None:
            return self.market_service.get_ohlc(
                symbol,
                timeframe,
                count=lookback,
            )

        bars = self.market_service.get_ohlc_window(
            symbol,
            timeframe,
            end_time=bar_time,
            limit=lookback,
        )
        if not bars or bars[-1].time != bar_time:
            return []
        return bars

    def _resolve_indicator_names(
        self,
        indicator_names: Optional[List[str]] = None,
    ) -> List[str]:
        if indicator_names is not None:
            return indicator_names
        return [config.name for config in self.config.indicators if config.enabled]

    @staticmethod
    def _indicator_history_requirement(config: IndicatorConfig) -> int:
        params = getattr(config, "params", {}) or {}
        return max(
            *(int(params.get(key, 0) or 0) for key in (
                "min_bars",
                "period",
                "slow",
                "signal",
                "fast",
                "atr_period",
                "k_period",
                "d_period",
                "lookback",
            )),
            2,
        )

    def _indicator_requirements(
        self,
        indicator_names: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        selected_names = set(self._resolve_indicator_names(indicator_names))
        config_items = getattr(getattr(self, "config", None), "indicators", None)
        if config_items is None:
            return {name: 2 for name in selected_names}
        requirements: Dict[str, int] = {}
        for config in config_items:
            if not config.enabled or config.name not in selected_names:
                continue
            requirements[config.name] = self._indicator_history_requirement(config)
        return requirements

    def _select_indicator_names_for_history(
        self,
        available_bars: int,
        indicator_names: Optional[List[str]] = None,
    ) -> List[str]:
        requirements = self._indicator_requirements(indicator_names)
        return [
            name
            for name in self._resolve_indicator_names(indicator_names)
            if requirements.get(name, 2) <= available_bars
        ]

    def _mark_event_skipped(
        self,
        event_id: int,
        reason: str,
    ) -> None:
        self.event_store.mark_event_skipped_by_id(event_id, reason)

    def _mark_event_completed(
        self,
        event_id: int,
    ) -> None:
        self.event_store.mark_event_completed_by_id(event_id)

    def _mark_event_failed(
        self,
        event_id: int,
        error: str,
    ) -> None:
        self.event_store.mark_event_failed_by_id(event_id, error)

    def _run_pipeline(
        self,
        symbol: str,
        timeframe: str,
        indicator_names: Optional[List[str]] = None,
        bar_time: Optional[datetime] = None,
    ) -> Tuple[List[Any], Dict[str, Dict[str, Any]], float]:
        return run_pipeline(
            self,
            symbol,
            timeframe,
            indicator_names=indicator_names,
            bar_time=bar_time,
        )

    def _compute_with_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        indicator_names: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], float, List[str]]:
        return compute_with_bars(
            self,
            symbol,
            timeframe,
            bars,
            indicator_names=indicator_names,
        )

    @staticmethod
    def _normalize_indicator_group(indicator_names: List[str]) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for indicator_name in indicator_names:
            if indicator_name in seen:
                continue
            seen.add(indicator_name)
            ordered.append(indicator_name)
        return tuple(ordered)

    def _load_intrabar_bars(
        self,
        symbol: str,
        timeframe: str,
        bar: Any,
    ) -> List[Any]:
        lookback = self._get_max_lookback()
        closed_limit = max(lookback - 1, 1)
        closed_bars = self.market_service.get_ohlc(symbol, timeframe, count=closed_limit)
        preview_bars = [item for item in closed_bars if item.time != bar.time]
        preview_bars.append(bar)
        return preview_bars[-lookback:]

    def _group_indicator_values(
        self,
        results: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, float]]:
        return group_indicator_values(self, results)

    def _indicator_delta_config(self) -> Dict[str, tuple[int, ...]]:
        config_items = getattr(getattr(self, "config", None), "indicators", None) or []
        mapping: Dict[str, tuple[int, ...]] = {}
        for config in config_items:
            if not config.enabled or not getattr(config, "delta_bars", None):
                continue
            mapping[config.name] = tuple(
                sorted({int(item) for item in config.delta_bars if int(item) > 0})
            )
        return mapping

    def _apply_delta_metrics(
        self,
        symbol: str,
        timeframe: str,
        indicators: Dict[str, Dict[str, float]],
        *,
        bar_time: Optional[datetime] = None,
    ) -> Dict[str, Dict[str, float]]:
        delta_config = self._indicator_delta_config()
        if not delta_config or not indicators:
            return indicators

        max_delta = max((max(values) for values in delta_config.values()), default=0)
        if max_delta <= 0:
            return indicators

        try:
            if bar_time is not None and hasattr(self.market_service, "get_ohlc_window"):
                history = list(
                    self.market_service.get_ohlc_window(
                        symbol,
                        timeframe,
                        end_time=bar_time,
                        limit=max_delta + 1,
                    )
                )
            else:
                history = list(
                    self.market_service.get_ohlc(
                        symbol,
                        timeframe,
                        count=max_delta + 1,
                    )
                )
        except Exception:
            logger.debug(
                "Failed to load historical bars for delta metrics: %s/%s",
                symbol,
                timeframe,
                exc_info=True,
            )
            return indicators

        if not history:
            return indicators

        current_in_history = bool(bar_time is not None and history[-1].time == bar_time)
        offset = 1 if current_in_history else 0

        for indicator_name, payload in indicators.items():
            delta_bars = delta_config.get(indicator_name)
            if not delta_bars or not isinstance(payload, dict):
                continue
            for delta in delta_bars:
                ref_index = -(delta + offset)
                if len(history) < delta + offset:
                    continue
                try:
                    reference_bar = history[ref_index]
                except IndexError:
                    continue
                reference_payload = getattr(reference_bar, "indicators", None) or {}
                reference_indicator = reference_payload.get(indicator_name)
                if not isinstance(reference_indicator, dict):
                    continue
                for metric_name, current_value in list(payload.items()):
                    if not isinstance(current_value, (int, float)):
                        continue
                    previous_value = reference_indicator.get(metric_name)
                    if not isinstance(previous_value, (int, float)):
                        continue
                    payload[f"{metric_name}_d{delta}"] = round(
                        float(current_value) - float(previous_value),
                        6,
                    )
        return indicators

    def _merge_snapshot_metrics_into_results(
        self,
        symbol: str,
        timeframe: str,
        indicators: Dict[str, Dict[str, float]],
    ) -> None:
        prefix = f"{symbol}_{timeframe}_"
        results = getattr(self, "_results", None)
        if not isinstance(results, dict) or not results:
            return

        def merge_into_results() -> None:
            for indicator_name, payload in indicators.items():
                result = results.get(f"{prefix}{indicator_name}")
                if result is None or not isinstance(result.value, dict):
                    continue
                result.value.update(payload)

        results_lock = getattr(self, "_results_lock", None)
        if results_lock is None:
            merge_into_results()
            return
        with results_lock:
            merge_into_results()

    def _normalize_persisted_indicator_snapshot(
        self,
        persisted: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        return normalize_persisted_indicator_snapshot(self, persisted)

    def _store_results(
        self,
        symbol: str,
        timeframe: str,
        bar_time: Optional[datetime],
        results: Dict[str, Dict[str, Any]],
        compute_time_ms: float,
    ) -> None:
        store_results(
            self,
            symbol,
            timeframe,
            bar_time,
            results,
            compute_time_ms,
        )

    def _store_preview_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
    ) -> bool:
        cache_key = f"{symbol}_{timeframe}"
        normalized = {
            name: dict(payload)
            for name, payload in indicators.items()
        }
        with self._results_lock:
            current = self._last_preview_snapshot.get(cache_key)
            if current is not None and current[0] == bar_time and current[1] == normalized:
                return False
            self._last_preview_snapshot.pop(cache_key, None)
            self._last_preview_snapshot[cache_key] = (bar_time, normalized)
            max_entries = getattr(self, "_preview_snapshot_max_entries", 500)
            while len(self._last_preview_snapshot) > max_entries:
                self._last_preview_snapshot.popitem(last=False)
        return True

    def _write_back_results(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        results: Dict[str, Dict[str, Any]],
        compute_time_ms: float,
        bar_time: Optional[datetime] = None,
    ) -> Dict[str, Dict[str, float]]:
        effective_bar_time = bar_time or (bars[-1].time if bars else None)
        self._store_results(symbol, timeframe, effective_bar_time, results, compute_time_ms)

        if not bars or effective_bar_time is None:
            return {}

        grouped = self._group_indicator_values(results)
        if not grouped:
            return {}
        grouped = self._apply_delta_metrics(
            symbol,
            timeframe,
            grouped,
            bar_time=effective_bar_time,
        )
        self._merge_snapshot_metrics_into_results(symbol, timeframe, grouped)

        latest_bar = bars[-1]
        self.market_service.update_ohlc_indicators(
            symbol,
            timeframe,
            effective_bar_time,
            grouped,
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

        self._publish_snapshot(
            symbol,
            timeframe,
            effective_bar_time,
            grouped,
            scope="confirmed",
        )

        return grouped

    def _publish_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
        *,
        scope: str,
    ) -> None:
        publish_snapshot(self, symbol, timeframe, bar_time, indicators, scope=scope)

    def _publish_intrabar_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
    ) -> Dict[str, Dict[str, float]]:
        enriched = self._apply_delta_metrics(
            symbol,
            timeframe,
            {
                name: dict(payload)
                for name, payload in indicators.items()
            },
            bar_time=bar_time,
        )
        return publish_intrabar_snapshot(self, symbol, timeframe, bar_time, enriched)

    def _compute_results_with_priority_groups(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        *,
        bar_time: datetime,
        scope: str,
        indicator_names: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], float]:
        return compute_results_with_priority_groups(
            self,
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
            scope=scope,
            indicator_names=indicator_names,
        )

    def _compute_priority_results(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        *,
        bar_time: datetime,
        scope: str,
        selected_names: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], float, set[str]]:
        return compute_priority_results(
            self,
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
            scope=scope,
            selected_names=selected_names,
        )

    def _compute_confirmed_results_for_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        *,
        bar_time: datetime,
    ) -> Tuple[Dict[str, Dict[str, Any]], float]:
        return self._compute_results_with_priority_groups(
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
            scope="confirmed",
        )

    def _compute_intrabar_results_for_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        *,
        bar_time: datetime,
        indicator_names: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], float]:
        return self._compute_results_with_priority_groups(
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
            scope="intrabar",
            indicator_names=indicator_names,
        )

    def _process_closed_bar_event(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        durable_event: bool,
    ) -> None:
        process_closed_bar_event(self, symbol, timeframe, bar_time, durable_event)

    def set_intrabar_eligible_override(self, names: frozenset) -> None:
        """设置 intrabar 计算的指标集合。

        由 SignalModule.intrabar_required_indicators() 在启动时自动推导
        （策略的 preferred_scopes + required_indicators 的并集），注入到这里。
        """
        enabled = frozenset(
            cfg.name for cfg in self.config.indicators if cfg.enabled
        )
        self._intrabar_eligible_cache = names & enabled
        logger.info(
            "Intrabar eligible indicators (auto-derived from strategy scopes): %s",
            sorted(self._intrabar_eligible_cache),
        )

    def _get_intrabar_eligible_names(self) -> frozenset:
        """Return the set of indicator names eligible for intrabar computation.

        Populated by set_intrabar_eligible_override() at startup.
        Returns empty frozenset if no override has been injected (standalone mode).
        """
        return getattr(self, "_intrabar_eligible_cache", None) or frozenset()

    def _process_intrabar_event(
        self,
        symbol: str,
        timeframe: str,
        bar: Any,
    ) -> Dict[str, Dict[str, float]]:
        return process_intrabar_event(self, symbol, timeframe, bar)

    def _reconcile_all(self) -> None:
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                if not self._is_reconcile_target_ready(symbol, timeframe):
                    continue
                try:
                    self._reconcile_symbol_timeframe(symbol, timeframe)
                except Exception:
                    logger.exception(
                        "Indicator reconcile failed for %s/%s",
                        symbol,
                        timeframe,
                    )

    def _reconcile_symbol_timeframe(self, symbol: str, timeframe: str) -> None:
        bars = self._load_bars(symbol, timeframe)
        if not bars:
            return
        results, compute_time_ms = self._compute_confirmed_results_for_bars(
            symbol,
            timeframe,
            bars,
            bar_time=bars[-1].time,
        )
        self._write_back_results(symbol, timeframe, bars, results, compute_time_ms)

    def _get_max_lookback(self) -> int:
        return max(self._indicator_requirements().values(), default=2)

    def _get_min_required_history(self) -> int:
        return max(min(self._indicator_requirements().values(), default=2), 2)

    def _reconcile_min_bars(self) -> int:
        return min(max(self._get_max_lookback(), 2), 100)

    def _is_reconcile_target_ready(self, symbol: str, timeframe: str) -> bool:
        return self.market_service.has_cached_ohlc(
            symbol,
            timeframe,
            minimum_bars=self._reconcile_min_bars(),
        )

    def _has_reconcile_ready_targets(self) -> bool:
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                if self._is_reconcile_target_ready(symbol, timeframe):
                    return True
        return False

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
        normalized = self._normalize_persisted_indicator_snapshot(
            self.market_service.latest_indicators(symbol, timeframe)
        )
        return normalized.get(indicator_name)

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
        return self._normalize_persisted_indicator_snapshot(
            self.market_service.latest_indicators(symbol, timeframe)
        )

    def get_intrabar_snapshot(
        self,
        symbol: str,
        timeframe: str,
    ) -> Optional[Tuple[datetime, Dict[str, Dict[str, float]]]]:
        """Return the most recent intrabar (live/partial-bar) indicator snapshot.

        Returns ``(bar_time, indicators)`` if a preview snapshot is available for
        the given symbol/timeframe, or ``None`` if no intrabar computation has
        run yet (e.g. intrabar ingestion disabled or no bar received).

        The returned dict only contains indicators that are eligible for intrabar
        computation (auto-derived from strategy scopes at startup).
        """
        cache_key = f"{symbol}_{timeframe}"
        with self._results_lock:
            return self._last_preview_snapshot.get(cache_key)

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

    def add_snapshot_listener(
        self,
        listener: Callable[[str, str, datetime, Dict[str, Dict[str, float]], str], None],
    ) -> None:
        with self._snapshot_listeners_lock:
            if not hasattr(self, "_snapshot_listeners"):
                self._snapshot_listeners = []
            self._snapshot_listeners.append(listener)

    def remove_snapshot_listener(
        self,
        listener: Callable[[str, str, datetime, Dict[str, Dict[str, float]], str], None],
    ) -> None:
        with self._snapshot_listeners_lock:
            if not hasattr(self, "_snapshot_listeners"):
                return
            self._snapshot_listeners = [
                item for item in self._snapshot_listeners if not same_listener_reference(item, listener)
            ]

    def set_priority_indicator_names(self, indicator_names: List[str]) -> None:
        group = self._normalize_indicator_group(indicator_names)
        self._priority_indicator_groups = (group,) if group else ()

    def set_priority_indicator_groups(self, indicator_groups: List[List[str] | tuple[str, ...]]) -> None:
        ordered: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for indicator_group in indicator_groups:
            group = self._normalize_indicator_group(list(indicator_group))
            if not group or group in seen:
                continue
            seen.add(group)
            ordered.append(group)
        self._priority_indicator_groups = tuple(ordered)

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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            persisted = self._normalize_persisted_indicator_snapshot(
                self.market_service.latest_indicators(symbol, timeframe)
            )
            if not persisted:
                return None
            latest_bar = self.market_service.get_ohlc(symbol, timeframe, count=1)
            latest_time = latest_bar[-1].time if latest_bar else None
            return IndicatorSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                data=persisted,
                timestamp=datetime.now(timezone.utc),
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
