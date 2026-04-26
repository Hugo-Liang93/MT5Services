"""单账户执行工作台聚合读模型（P9 Phase 1）。

参考 docs/design/quantx-data-freshness-tiering.md：5 级 Tier 划分 + 9 块 contract。
跨账户聚合（contributors[] / total_equity）由 BFF 完成；本读模型仅暴露单账户视图。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from src.config.models.freshness import FreshnessConfig, default_freshness_config
from src.readmodels.runtime import RuntimeReadModel

# ── 默认 freshness 阈值 ────────────────────────────────────────────
# P9 Phase 3.2: 阈值从 src/config/models/freshness.py 派生（Pydantic 模型集中管理），
# 仍以 module-level 常量形式导出向后兼容（旧测试/导入不破坏）。
DEFAULT_FRESHNESS_HINTS: Mapping[str, Mapping[str, Any]] = (
    default_freshness_config().as_hints()
)

WORKBENCH_DEFAULT_BLOCKS: tuple[str, ...] = (
    "execution",
    "risk",
    "positions",
    "orders",
    "pending",
    "exposure",
    "events",
    "relatedObjects",
    "marketContext",
    "stream",
)

# 顶层 source 聚合时不参与降级判断的块（stream 是静态 SSE 元数据，无数据源依赖）
_SOURCE_AGGREGATION_SKIP: frozenset[str] = frozenset({"stream"})


def compute_source_kind(blocks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """聚合各 block 的 source_kind 到顶层 source 描述（P9 Phase 2.3 + §0r）。

    判定规则：
    - 全部块 source_kind=native → kind=live
    - 任意块 source_kind=fallback / 显式 available=false → kind=hybrid，
      fallback_reason 列出降级块名
    - source_kind="static" 不参与判定（如 stream 块）
    - source_kind="delegated" 不算 fallback（multi_account main role 不挂 executor
      是合法拓扑，参 §0r）；与 native 混合时仍认为 kind=live

    跨端点统一：未来其他读端点（/v1/account/* 等）补 source 字段时，可复用此函数。
    """
    fallback_reasons: list[str] = []
    for block_name, block_payload in blocks.items():
        if block_name in _SOURCE_AGGREGATION_SKIP:
            continue
        if not isinstance(block_payload, Mapping):
            continue
        kind = str(block_payload.get("source_kind") or "").lower()
        if kind == "fallback":
            fallback_reasons.append(block_name)
            continue
        if block_payload.get("available") is False:
            reason_label = str(block_payload.get("reason") or "unavailable")
            fallback_reasons.append(f"{block_name}:{reason_label}")
    if not fallback_reasons:
        return {"kind": "live", "fallback_applied": False, "fallback_reason": None}
    return {
        "kind": "hybrid",
        "fallback_applied": True,
        "fallback_reason": ",".join(sorted(set(fallback_reasons))),
    }


class WorkbenchReadModel:
    """单账户执行工作台聚合读模型。

    9 块 contract（execution / risk / positions / orders / pending / exposure /
    events / relatedObjects / marketContext / stream）+ 顶层 observed_at /
    freshness_hints / source。

    校验规则：
    - account_alias 必须匹配本实例（runtime_identity.account_alias）
    - symbol 可选，仅影响 marketContext 块
    """

    def __init__(
        self,
        *,
        runtime_read_model: RuntimeReadModel,
        market_service: Any = None,
        runtime_identity: Any = None,
        freshness_config: Optional[FreshnessConfig] = None,
    ) -> None:
        self._runtime = runtime_read_model
        self._market_service = market_service
        self._runtime_identity = runtime_identity
        # P9 Phase 3.2: freshness 阈值集中管理，可被注入覆盖（默认用 module 常量）。
        self._freshness_config = freshness_config or default_freshness_config()
        # P9 Phase 3.1: build() 期间共享底层快照，避免 6 个 block builder 重复扇出。
        # 每次 build() 入口 reset；WorkbenchReadModel 在 deps.py 中是 per-request
        # 构造的（每请求 new 实例），无并发风险。
        self._build_cache: dict[str, Any] = {}

    @property
    def account_alias(self) -> Optional[str]:
        if self._runtime_identity is None:
            return None
        return getattr(self._runtime_identity, "account_alias", None)

    def build(
        self,
        *,
        account_alias: str,
        symbol: Optional[str] = None,
        include: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        actual_alias = self.account_alias
        if actual_alias and account_alias != actual_alias:
            raise KeyError(
                f"account_alias not configured for this instance: {account_alias}"
            )

        # P9 Phase 3.1: build() 入口 reset 共享缓存，确保各次 build() 拿到 fresh 数据
        self._build_cache = {}
        observed_at = datetime.now(timezone.utc).isoformat()
        blocks = set(include) if include else set(WORKBENCH_DEFAULT_BLOCKS)
        payload: dict[str, Any] = {
            "account_alias": account_alias,
            "observed_at": observed_at,
            # P9 Phase 3.2: 实例化的 FreshnessConfig 派生（默认值与设计文档对齐）
            "freshness_hints": self._freshness_config.as_hints(),
            # state_version 在 Phase 2.1 SSE envelope 改造后与 sequence 对齐。
            "state_version": None,
        }

        if "execution" in blocks:
            payload["execution"] = self._build_execution_block()
        if "risk" in blocks:
            payload["risk"] = self._build_risk_block()
        if "positions" in blocks:
            payload["positions"] = self._build_positions_block()
        if "orders" in blocks:
            payload["orders"] = self._build_orders_block()
        if "pending" in blocks:
            payload["pending"] = self._build_pending_block()
        if "exposure" in blocks:
            payload["exposure"] = self._build_exposure_block(
                account_alias=account_alias
            )
        if "events" in blocks:
            payload["events"] = self._build_events_block()
        if "relatedObjects" in blocks:
            payload["relatedObjects"] = self._build_related_objects_block()
        if "marketContext" in blocks:
            payload["marketContext"] = self._build_market_context_block(symbol=symbol)
        if "stream" in blocks:
            payload["stream"] = self._build_stream_block(account_alias=account_alias)

        # P9 Phase 2.3: 顶层 source 从各 block source_kind 聚合，不再硬编码 live。
        # 仅对当前 build 包含的 block 取样（include 过滤后聚合）。
        source_blocks = {name: payload[name] for name in blocks if name in payload}
        payload["source"] = compute_source_kind(source_blocks)

        return payload

    # ── Shared snapshot cache (P9 Phase 3.1) ────────────────────

    def _shared_positions(self) -> dict[str, Any]:
        """共享 positions 快照：positions / exposure 块共用，limit=200 涵盖两者需求。"""
        if "positions" not in self._build_cache:
            self._build_cache["positions"] = (
                self._runtime.position_runtime_state_payload(limit=200) or {}
            )
        return self._build_cache["positions"]

    def _shared_pending_active(self) -> dict[str, Any]:
        """共享 active pending orders：orders / pending 块共用。"""
        if "pending_active" not in self._build_cache:
            self._build_cache["pending_active"] = (
                self._runtime.active_pending_order_payload(limit=100) or {}
            )
        return self._build_cache["pending_active"]

    def _shared_pipeline_events(self) -> dict[str, Any]:
        """共享 pipeline 事件：events / relatedObjects 块共用。"""
        if "pipeline_events" not in self._build_cache:
            self._build_cache["pipeline_events"] = (
                self._runtime.recent_trade_pipeline_events_payload(limit=20) or {}
            )
        return self._build_cache["pipeline_events"]

    # ── Block builders ──────────────────────────────────────────

    def _build_execution_block(self) -> dict[str, Any]:
        tradability = self._runtime.tradability_state_summary()
        executor = self._runtime.trade_executor_summary() or {}
        # P9 Phase 2.3 + §0r 修正：
        # - native：runtime_present=True
        # - delegated：multi_account main role 不挂 executor（合法拓扑）
        #   → readmodel 标 verdict='not_applicable' / reason='not_executor_role'
        #   且 executor.execution_scope='remote_executor' / state='delegated'
        #   不应被算作 fallback（旧 bug：会让顶层 source.kind 变 hybrid）
        # - fallback：runtime_present=False 且不是 delegated（真实运行时故障）
        runtime_present = bool(tradability.get("runtime_present", True))
        is_delegated_role = (
            str(tradability.get("verdict") or "").lower() == "not_applicable"
            and str(tradability.get("reason_code") or "").lower() == "not_executor_role"
        )
        if runtime_present:
            source_kind = "native"
        elif is_delegated_role:
            source_kind = "delegated"
        else:
            source_kind = "fallback"
        return {
            "tier": "T0",
            "source_kind": source_kind,
            "tradability": tradability,
            "executor": executor,
        }

    def _build_risk_block(self) -> dict[str, Any]:
        risk = self._runtime.account_risk_state_summary()
        # P9 Phase 2.3: account_risk_state_summary 返回 None 表示 trading_state_store
        # 与 db_writer 都不可用 → fallback。空 dict（store 仍在但暂无快照）算 native。
        snapshot = risk or {}
        return {
            "tier": "T0",
            "source_kind": "native" if risk is not None else "fallback",
            "state_updated_at": snapshot.get("updated_at"),
            "snapshot": snapshot,
        }

    def _build_positions_block(self) -> dict[str, Any]:
        payload = self._shared_positions()
        all_items = list(payload.get("items") or [])
        # 块限制 100 条；status_counts 保留共享快照原值（来自全 200 条更全面）
        items = all_items[:100]
        return {
            "tier": "T1",
            "source_kind": "native",
            "positions_updated_at": _latest_updated_at(items),
            "count": payload.get("count", 0),
            "status_counts": payload.get("status_counts") or {},
            "items": items,
        }

    def _build_orders_block(self) -> dict[str, Any]:
        payload = self._shared_pending_active()
        items = list(payload.get("items") or [])
        return {
            "tier": "T1",
            "source_kind": "native",
            "orders_updated_at": _latest_updated_at(items),
            "count": payload.get("count", 0),
            "status_counts": payload.get("status_counts") or {},
            "items": items,
        }

    def _build_pending_block(self) -> dict[str, Any]:
        return {
            "tier": "T1",
            "source_kind": "native",
            "active": self._shared_pending_active(),
            "lifecycle": self._runtime.pending_order_lifecycle_payload(limit=100) or {},
            "execution_contexts": self._runtime.pending_execution_context_payload()
            or {},
        }

    def _build_exposure_block(self, *, account_alias: str) -> dict[str, Any]:
        # P9 Phase 3.1: 复用 _shared_positions（无 statuses 过滤），在 Python 层
        # 过滤 status="open"。比之前 statuses=["open"] 多过滤一次（O(n) 200 条），
        # 但避免一次额外的 store 查询往返。
        positions_payload = self._shared_positions()
        items: Iterable[Mapping[str, Any]] = [
            item
            for item in (positions_payload.get("items") or [])
            if str(item.get("status") or "").lower() == "open"
        ]
        symbol_buckets: dict[tuple[str, str], dict[str, float]] = {}
        for item in items:
            symbol = str(item.get("symbol") or "").strip()
            direction_raw = str(item.get("direction") or "").lower()
            if not symbol or direction_raw not in ("buy", "sell", "long", "short"):
                continue
            direction = "long" if direction_raw in ("buy", "long") else "short"
            key = (symbol, direction)
            bucket = symbol_buckets.setdefault(
                key,
                {
                    "gross_volume": 0.0,
                    "floating_pnl": 0.0,
                    "margin_used": 0.0,
                    "count": 0.0,
                },
            )
            bucket["gross_volume"] += _coerce_float(item.get("volume"))
            bucket["floating_pnl"] += _coerce_float(item.get("floating_pnl"))
            bucket["margin_used"] += _coerce_float(item.get("margin_used"))
            bucket["count"] += 1
        symbols_payload = [
            {
                "symbol": symbol,
                "direction": direction,
                "gross_volume": round(bucket["gross_volume"], 4),
                "floating_pnl": round(bucket["floating_pnl"], 2),
                "margin_used": round(bucket["margin_used"], 2),
                "position_count": int(bucket["count"]),
            }
            for (symbol, direction), bucket in sorted(symbol_buckets.items())
        ]
        hotspots = sorted(
            symbols_payload,
            key=lambda x: x["gross_volume"],
            reverse=True,
        )[:5]
        return {
            "tier": "T2",
            "source_kind": "native",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "account_alias": account_alias,
            "symbols": symbols_payload,
            "hotspots": hotspots,
        }

    def _build_events_block(self) -> dict[str, Any]:
        payload = self._shared_pipeline_events()
        return {
            "tier": "T3",
            "source_kind": "native",
            "count": payload.get("count", 0),
            "items": payload.get("items") or [],
        }

    def _build_related_objects_block(self) -> dict[str, Any]:
        pending = self._runtime.pending_entries_summary() or {}
        entries = pending.get("entries") or []
        active_signals = [
            {
                "signal_id": entry.get("signal_id"),
                "symbol": entry.get("symbol"),
                "timeframe": entry.get("timeframe"),
                "strategy": entry.get("strategy"),
                "direction": entry.get("direction"),
            }
            for entry in entries[:20]
            if entry.get("signal_id")
        ]
        events = self._shared_pipeline_events()
        recent_commands = [
            {
                "audit_id": ev.get("id"),
                "command_id": ev.get("command_id"),
                "event_type": ev.get("event_type"),
                "recorded_at": ev.get("recorded_at"),
            }
            for ev in (events.get("items") or [])
            if str(ev.get("event_type") or "").startswith("command_")
        ][:10]
        return {
            "tier": "T1",
            "source_kind": "native",
            "active_signals": active_signals,
            "recent_commands": recent_commands,
        }

    def _build_market_context_block(self, *, symbol: Optional[str]) -> dict[str, Any]:
        target_symbol = symbol
        if self._market_service is None:
            return {
                "tier": "T2",
                "source_kind": "fallback",
                "symbol": target_symbol,
                "available": False,
                "reason": "market_service_unavailable",
            }
        try:
            quote = self._market_service.get_quote(target_symbol)
        except Exception:
            quote = None
        if quote is None:
            return {
                "tier": "T2",
                "source_kind": "fallback",
                "symbol": target_symbol,
                "available": False,
                "reason": "no_quote",
            }
        resolved_symbol = getattr(quote, "symbol", target_symbol)
        spread_points = self._safe_spread_points(resolved_symbol)
        quote_time = getattr(quote, "time", None)
        return {
            "tier": "T2",
            "source_kind": "native",
            "symbol": resolved_symbol,
            "available": True,
            "bid": getattr(quote, "bid", None),
            "ask": getattr(quote, "ask", None),
            "last": getattr(quote, "last", None),
            "spread_points": spread_points,
            "quote_updated_at": (
                quote_time.isoformat()
                if hasattr(quote_time, "isoformat")
                else quote_time
            ),
        }

    def _build_stream_block(self, *, account_alias: str) -> dict[str, Any]:
        return {
            "tier": "static",
            "source_kind": "static",
            "sse_url": f"/v1/trade/state/stream?account={account_alias}",
            "supports_stream": True,
            "recommended_poll_interval_seconds": 5,
        }

    def _safe_spread_points(self, symbol: Any) -> Optional[float]:
        if self._market_service is None:
            return None
        getter = getattr(self._market_service, "get_current_spread", None)
        if not callable(getter):
            return None
        try:
            value = getter(symbol)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None


def _latest_updated_at(items: Iterable[Mapping[str, Any]]) -> Optional[str]:
    candidates = [
        str(item.get("updated_at"))
        for item in items
        if item.get("updated_at") is not None
    ]
    return max(candidates) if candidates else None


def _coerce_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
