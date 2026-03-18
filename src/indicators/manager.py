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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from src.config.indicator_config import (
    IndicatorConfig,
    UnifiedIndicatorConfig,
    get_global_config_manager,
)
from src.core.market_service import MarketDataService
from src.utils.event_store import get_event_store

from .cache.incremental import IncrementalIndicator
from .engine.dependency_manager import get_global_dependency_manager
from .engine.pipeline import OptimizedPipeline, get_global_pipeline
from .monitoring.metrics_collector import get_global_collector

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
        self._last_preview_snapshot: Dict[str, Tuple[datetime, Dict[str, Dict[str, float]]]] = {}
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
        # Build intrabar eligibility cache after all indicators are registered.
        self._intrabar_eligible_cache = frozenset(
            cfg.name
            for cfg in self.config.indicators
            if cfg.enabled and cfg.intrabar_eligible
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
            durable_events = self.event_store.get_next_events(limit=EVENT_BATCH_SIZE)
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
        events: List[Tuple[str, str, datetime]],
        durable_event: bool,
    ) -> None:
        grouped: Dict[Tuple[str, str], List[datetime]] = {}
        for symbol, timeframe, bar_time in events:
            grouped.setdefault((symbol, timeframe), []).append(bar_time)

        for (symbol, timeframe), bar_times in grouped.items():
            ordered = sorted(bar_times)
            try:
                self._process_symbol_timeframe_batch(
                    symbol,
                    timeframe,
                    ordered,
                    durable_event=durable_event,
                )
            except Exception:
                logger.exception(
                    "Failed to process closed-bar batch for %s/%s (%s events)",
                    symbol,
                    timeframe,
                    len(ordered),
                )
                if durable_event:
                    for bar_time in ordered:
                        self.event_store.mark_event_failed(
                            symbol,
                            timeframe,
                            bar_time,
                            "batch_processing_failed",
                        )

    def _process_symbol_timeframe_batch(
        self,
        symbol: str,
        timeframe: str,
        bar_times: List[datetime],
        durable_event: bool,
    ) -> None:
        if not bar_times:
            return

        latest_bar_time = bar_times[-1]
        lookback = self._get_max_lookback()
        bars = self.market_service.get_ohlc_window(
            symbol,
            timeframe,
            end_time=latest_bar_time,
            limit=lookback + len(bar_times),
        )
        bar_index = {bar.time: idx for idx, bar in enumerate(bars)}
        computed = 0
        skipped_bar_missing = 0
        skipped_insufficient_history = 0
        failed = 0

        for bar_time in bar_times:
            try:
                end_idx = bar_index.get(bar_time)
                if end_idx is None:
                    prefix = self._load_bars(symbol, timeframe, bar_time=bar_time)
                    if not prefix or prefix[-1].time != bar_time:
                        logger.debug(
                            "Skipping closed-bar event for %s/%s at %s because the target OHLC bar is unavailable",
                            symbol,
                            timeframe,
                            bar_time,
                        )
                        skipped_bar_missing += 1
                        if durable_event:
                            self._mark_event_skipped(symbol, timeframe, bar_time, "bar_missing")
                        continue
                else:
                    prefix = bars[: end_idx + 1]
                    prefix = prefix[-lookback:]
                selected_names = self._select_indicator_names_for_history(len(prefix))
                if not selected_names:
                    logger.debug(
                        "Skipping indicator computation for %s/%s at %s due to insufficient history (%s bars)",
                        symbol,
                        timeframe,
                        bar_time,
                        len(prefix),
                    )
                    skipped_insufficient_history += 1
                    if durable_event:
                        self._mark_event_skipped(symbol, timeframe, bar_time, "insufficient_history")
                    continue

                results, compute_time_ms = self._compute_confirmed_results_for_bars(
                    symbol,
                    timeframe,
                    prefix,
                    bar_time=bar_time,
                )
                if not results:
                    skipped_insufficient_history += 1
                    if durable_event:
                        self._mark_event_skipped(symbol, timeframe, bar_time, "insufficient_history")
                    continue
                self._write_back_results(
                    symbol,
                    timeframe,
                    prefix,
                    results,
                    compute_time_ms,
                    bar_time=bar_time,
                )
                computed += 1
                if durable_event:
                    self.event_store.mark_event_completed(symbol, timeframe, bar_time)
            except Exception as exc:
                failed += 1
                logger.exception(
                    "Failed to process closed-bar event for %s/%s at %s in batch",
                    symbol,
                    timeframe,
                    bar_time,
                )
                if durable_event:
                    self.event_store.mark_event_failed(symbol, timeframe, bar_time, str(exc))

        logger.info(
            "Processed closed-bar batch for %s/%s: events=%s computed=%s skipped_history=%s skipped_bar_missing=%s failed=%s durable=%s",
            symbol,
            timeframe,
            len(bar_times),
            computed,
            skipped_insufficient_history,
            skipped_bar_missing,
            failed,
            durable_event,
        )

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
            for symbol, timeframe, bar_time in batch:
                try:
                    self.event_store.publish_event(symbol, timeframe, bar_time)
                except Exception:
                    logger.exception(
                        "Failed to persist bar close event for %s/%s at %s",
                        symbol, timeframe, bar_time,
                    )
        # Drain any remaining items before the thread exits.
        while True:
            try:
                symbol, timeframe, bar_time = self._event_write_queue.get_nowait()
                try:
                    self.event_store.publish_event(symbol, timeframe, bar_time)
                except Exception:
                    logger.exception(
                        "Failed to persist bar close event during shutdown for %s/%s at %s",
                        symbol, timeframe, bar_time,
                    )
            except queue.Empty:
                break

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
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        reason: str,
    ) -> None:
        mark_skipped = getattr(self.event_store, "mark_event_skipped", None)
        if callable(mark_skipped):
            mark_skipped(symbol, timeframe, bar_time, reason)
            return
        self.event_store.mark_event_completed(symbol, timeframe, bar_time)

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

        selected_names = self._select_indicator_names_for_history(len(bars), indicator_names)
        if not selected_names:
            return bars, {}, 0.0
        started_at = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, selected_names)
        compute_time_ms = (time.time() - started_at) * 1000
        return bars, results, compute_time_ms

    def _compute_with_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        indicator_names: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], float, List[str]]:
        if len(bars) < 2:
            return {}, 0.0, []

        selected_names = self._select_indicator_names_for_history(len(bars), indicator_names)
        if not selected_names:
            return {}, 0.0, []

        started_at = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, selected_names)
        compute_time_ms = (time.time() - started_at) * 1000
        return results, compute_time_ms, selected_names

    def _run_intrabar_pipeline(
        self,
        symbol: str,
        timeframe: str,
        bar: Any,
        indicator_names: Optional[List[str]] = None,
    ) -> Tuple[List[Any], Dict[str, Dict[str, Any]], float]:
        bars = self._load_intrabar_bars(symbol, timeframe, bar)
        if len(bars) < 2:
            return [], {}, 0.0

        selected_names = self._select_indicator_names_for_history(len(bars), indicator_names)
        if not selected_names:
            return bars, {}, 0.0

        started_at = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, selected_names)
        compute_time_ms = (time.time() - started_at) * 1000
        return bars, results, compute_time_ms

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
        grouped: Dict[str, Dict[str, float]] = {}
        for indicator_name, payload in results.items():
            if not isinstance(payload, dict) or not payload:
                continue
            normalized_payload: Dict[str, float] = {}
            for metric_name, raw_value in payload.items():
                if not isinstance(raw_value, (int, float)):
                    continue
                normalized_payload[metric_name] = float(raw_value)
            if normalized_payload:
                grouped[indicator_name] = normalized_payload
        return grouped

    def _normalize_persisted_indicator_snapshot(
        self,
        persisted: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        consumed_keys: set[str] = set()

        for indicator_name, payload in persisted.items():
            if not isinstance(payload, dict):
                continue
            numeric_payload = {
                metric_name: float(raw_value)
                for metric_name, raw_value in payload.items()
                if isinstance(raw_value, (int, float))
            }
            if not numeric_payload:
                continue
            normalized[indicator_name] = numeric_payload
            consumed_keys.add(indicator_name)

        legacy_scalars = {
            key: float(value)
            for key, value in persisted.items()
            if isinstance(value, (int, float))
        }
        configured_names = [config.name for config in self.config.indicators if config.enabled]

        for indicator_name in configured_names:
            direct_value = legacy_scalars.get(indicator_name)
            prefixed_values = {
                metric_name[len(indicator_name) + 1 :]: value
                for metric_name, value in legacy_scalars.items()
                if metric_name.startswith(f"{indicator_name}_")
            }
            if direct_value is None and not prefixed_values:
                continue

            payload = dict(normalized.get(indicator_name, {}))
            if direct_value is not None:
                payload[indicator_name] = direct_value
                consumed_keys.add(indicator_name)
            payload.update(prefixed_values)
            consumed_keys.update(
                metric_name
                for metric_name in legacy_scalars
                if metric_name == indicator_name or metric_name.startswith(f"{indicator_name}_")
            )
            normalized[indicator_name] = payload

        for key, value in legacy_scalars.items():
            if key in consumed_keys:
                continue
            normalized[key] = {key: value}

        return normalized

    def _store_results(
        self,
        symbol: str,
        timeframe: str,
        bar_time: Optional[datetime],
        results: Dict[str, Dict[str, Any]],
        compute_time_ms: float,
    ) -> None:
        timestamp = datetime.now(timezone.utc)
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
        current = self._last_preview_snapshot.get(cache_key)
        if current is not None and current[0] == bar_time and current[1] == normalized:
            return False
        self._last_preview_snapshot[cache_key] = (bar_time, normalized)
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
        with self._snapshot_listeners_lock:
            listeners = list(getattr(self, "_snapshot_listeners", []))
        for listener in listeners:
            try:
                listener(symbol, timeframe, bar_time, indicators, scope)
            except Exception:
                logger.exception(
                    "Failed to publish indicator snapshot for %s/%s at %s",
                    symbol,
                    timeframe,
                    bar_time,
                )

    def _publish_intrabar_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
    ) -> Dict[str, Dict[str, float]]:
        if not indicators:
            return {}
        if not self._store_preview_snapshot(symbol, timeframe, bar_time, indicators):
            return indicators
        self._publish_snapshot(
            symbol,
            timeframe,
            bar_time,
            indicators,
            scope="intrabar",
        )
        return indicators

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
        if len(bars) < 2:
            return {}, 0.0

        selected_names = self._select_indicator_names_for_history(len(bars), indicator_names)
        if not selected_names:
            return {}, 0.0

        selected_set = set(selected_names)
        priority_groups = [
            indicator_group
            for indicator_group in getattr(self, "_priority_indicator_groups", ())
            if indicator_group and all(indicator_name in selected_set for indicator_name in indicator_group)
        ]
        published_groups: set[tuple[str, ...]] = set()

        def on_level_complete(_level_results: Dict[str, Dict[str, Any]], accumulated_results: Dict[str, Dict[str, Any]]) -> None:
            for indicator_group in priority_groups:
                if indicator_group in published_groups:
                    continue
                if any(indicator_name not in accumulated_results for indicator_name in indicator_group):
                    continue
                group_results = {
                    indicator_name: accumulated_results[indicator_name]
                    for indicator_name in indicator_group
                }
                grouped = self._group_indicator_values(group_results)
                if not grouped or any(indicator_name not in grouped for indicator_name in indicator_group):
                    continue
                published_groups.add(indicator_group)
                if scope == "confirmed":
                    self._publish_snapshot(
                        symbol,
                        timeframe,
                        bar_time,
                        grouped,
                        scope="confirmed",
                    )
                else:
                    self._publish_intrabar_snapshot(symbol, timeframe, bar_time, grouped)

        started_at = time.time()
        results = self.pipeline.compute_staged(
            symbol,
            timeframe,
            bars,
            indicators=selected_names,
            on_level_complete=on_level_complete if priority_groups else None,
            scope=scope,
        )
        compute_time_ms = (time.time() - started_at) * 1000
        return results, compute_time_ms

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
        if len(bars) < 2:
            return {}, 0.0, set()

        selected = selected_names or self._select_indicator_names_for_history(len(bars))
        if not selected:
            return {}, 0.0, set()

        selected_set = set(selected)
        priority_groups = [
            indicator_group
            for indicator_group in getattr(self, "_priority_indicator_groups", ())
            if indicator_group and all(indicator_name in selected_set for indicator_name in indicator_group)
        ]
        if not priority_groups:
            return {}, 0.0, set()

        merged_results: Dict[str, Dict[str, Any]] = {}
        covered_names: set[str] = set()
        total_compute_time_ms = 0.0
        for indicator_group in priority_groups:
            group_results, compute_time_ms, _group_selected = self._compute_with_bars(
                symbol,
                timeframe,
                bars,
                indicator_names=list(indicator_group),
            )
            total_compute_time_ms += compute_time_ms
            if not group_results:
                continue
            grouped = self._group_indicator_values(group_results)
            if not grouped or any(indicator_name not in grouped for indicator_name in indicator_group):
                continue
            merged_results.update(group_results)
            covered_names.update(indicator_group)
            if scope == "confirmed":
                self._publish_snapshot(
                    symbol,
                    timeframe,
                    bar_time,
                    grouped,
                    scope="confirmed",
                )
            else:
                self._publish_intrabar_snapshot(symbol, timeframe, bar_time, grouped)

        return merged_results, total_compute_time_ms, covered_names

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
        try:
            bars = self._load_bars(symbol, timeframe, bar_time=bar_time)
            if not bars:
                if durable_event:
                    self._mark_event_skipped(symbol, timeframe, bar_time, "bar_missing")
                return
            results, compute_time_ms = self._compute_confirmed_results_for_bars(
                symbol,
                timeframe,
                bars,
                bar_time=bar_time,
            )
            if not results:
                if durable_event:
                    self._mark_event_skipped(symbol, timeframe, bar_time, "insufficient_history")
                return
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

    def _get_intrabar_eligible_names(self) -> frozenset:
        """Return the set of indicator names that should appear in intrabar snapshots.

        The result is cached at registration time and invalidated on config reload,
        so this is safe to call on every intrabar event without CPU overhead.
        """
        if self._intrabar_eligible_cache is None:
            # Fallback if called before _register_indicators (should not happen).
            self._intrabar_eligible_cache = frozenset(
                cfg.name
                for cfg in self.config.indicators
                if cfg.enabled and cfg.intrabar_eligible
            )
        return self._intrabar_eligible_cache

    def _process_intrabar_event(
        self,
        symbol: str,
        timeframe: str,
        bar: Any,
    ) -> Dict[str, Dict[str, float]]:
        # Filter indicator set upfront so the pipeline never runs volume-derived
        # or session-sensitive indicators on partial bars (avoids wasted CPU and
        # semantically misleading intrabar values for mfi14, obv30, vwap30, etc.).
        eligible = list(self._get_intrabar_eligible_names())
        bars = self._load_intrabar_bars(symbol, timeframe, bar)
        if not bars:
            return {}
        results, _compute_time_ms = self._compute_intrabar_results_for_bars(
            symbol,
            timeframe,
            bars,
            bar_time=bar.time,
            indicator_names=eligible,
        )
        if not bars or not results:
            return {}
        grouped = self._group_indicator_values(results)
        if not grouped:
            return {}
        return self._publish_intrabar_snapshot(symbol, timeframe, bar.time, grouped)

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

        The returned dict only contains indicators marked as ``intrabar_eligible``
        in the configuration — volume-derived and session-sensitive indicators are
        excluded because their mid-bar values are semantically unreliable.
        """
        cache_key = f"{symbol}_{timeframe}"
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
            "intrabar_eligible": config.intrabar_eligible,
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
