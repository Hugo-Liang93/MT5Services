"""Explicit mixins that expose runtime helper methods for UnifiedIndicatorManager."""

from __future__ import annotations

from .. import query_services as _query_services
from . import registry as _registry


def _bind(target, func_name):
    def _bound_method(self, *args, **kwargs):
        return getattr(target, func_name)(self, *args, **kwargs)

    _bound_method.__name__ = func_name
    _bound_method._is_query_service_binding = True
    return _bound_method


def _build_mixin(class_name, target, method_names):
    attrs = {name: _bind(target, name) for name in method_names}
    return type(class_name, (object,), attrs)


_REGISTRY_METHODS = (
    "_init_components",
    "_register_indicators",
    "_load_indicator_func",
    "_load_incremental_class",
    "_reinitialize",
    "add_indicator",
    "update_indicator",
    "remove_indicator",
)

_QUERY_SERVICE_METHODS = (
    "_resolve_indicator_names",
    "_indicator_history_requirement",
    "_indicator_requirements",
    "_select_indicator_names_for_history",
    "_mark_event_skipped",
    "_mark_event_completed",
    "_mark_event_failed",
    "_run_pipeline",
    "_compute_with_bars",
    "_load_confirmed_bars",
    "_normalize_indicator_group",
    "_load_intrabar_bars",
    "_group_indicator_values",
    "_indicator_delta_config",
    "_apply_delta_metrics",
    "_merge_snapshot_metrics_into_results",
    "_normalize_persisted_indicator_snapshot",
    "_store_results",
    "_store_preview_snapshot",
    "_write_back_results",
    "_publish_snapshot",
    "_publish_intrabar_snapshot",
    "_compute_results_with_priority_groups",
    "_compute_priority_results",
    "_compute_confirmed_results_for_bars",
    "_compute_intrabar_results_for_bars",
    "_process_closed_bar_event",
    "_process_closed_bar_events_batch",
    "set_confirmed_eligible_override",
    "_get_confirmed_eligible_names",
    "set_intrabar_eligible_override",
    "_get_intrabar_eligible_names",
    "get_intrabar_eligible_names",
    "_process_intrabar_event",
    "_reconcile_all",
    "_reconcile_symbol_timeframe",
    "_get_max_lookback",
    "_get_min_required_history",
    "_reconcile_min_bars",
    "_is_reconcile_target_ready",
    "_has_reconcile_ready_targets",
    "get_indicator",
    "get_all_indicators",
    "get_intrabar_snapshot",
    "compute",
    "get_indicator_info",
    "list_indicators",
    "add_snapshot_listener",
    "remove_snapshot_listener",
    "set_priority_indicator_names",
    "set_priority_indicator_groups",
    "get_performance_stats",
    "clear_cache",
    "get_snapshot",
    "trigger_consistency_check",
    "reset_failed_events",
    "cleanup_old_events",
    "stats",
    "get_dependency_graph",
)


RegistryBindingMixin = _build_mixin(
    "RegistryBindingMixin", _registry, _REGISTRY_METHODS
)
QueryBindingMixin = _build_mixin(
    "QueryBindingMixin", _query_services, _QUERY_SERVICE_METHODS
)
