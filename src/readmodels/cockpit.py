"""Cockpit 总控台跨账户读模型（P10.1）。

替代 QuantX 前端当前对 `/api/overview/* + /api/signals/summary + /api/market/context`
的本地聚合，首页一请求内即可回答：
- 能否继续交易（decision）
- 先处理谁（triage_queue）
- 风险集中处（exposure_map）
- 当前机会（opportunity_queue）
- 数据是否新鲜（data_health / freshness_hints）

架构约束：所有块基于 TimescaleDB 单库多账户数据（同一 environment 下 live-main/
live-exec-a/... 共享 db.live），不向其他实例做 RPC。跨账户聚合由本读模型 SQL 完成。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from src.readmodels.freshness import age_seconds as _age_seconds_shared
from src.readmodels.freshness import build_freshness_block
from src.readmodels.freshness import freshness_state as _freshness_state_shared
from src.readmodels.freshness import iso_or_none
from src.readmodels.workbench import compute_source_kind

COCKPIT_BLOCKS: tuple[str, ...] = (
    "decision",
    "triage_queue",
    "market_guard",
    "data_health",
    "exposure_map",
    "opportunity_queue",
    "safe_actions",
)

# Triage 优先级层级（仅后端派生，前端不再推导）
TRIAGE_LAYER_CRITICAL: str = "critical"
TRIAGE_LAYER_WARN: str = "warn"
TRIAGE_LAYER_HEALTHY: str = "healthy"


# P1.1: 下述 _iso / _age_seconds / _freshness_state 全部改为 src/readmodels/freshness.py
# 内的 iso_or_none / age_seconds / freshness_state。保留别名以向后兼容模块内调用。
_iso = iso_or_none
_age_seconds = _age_seconds_shared
_freshness_state = _freshness_state_shared


def _max_iso_timestamp(*streams: Iterable[Any]) -> Optional[str]:
    """从多个 row 字段流中取最大 (latest) ISO 时间戳，None / 空/非法值跳过。

    支持 datetime 对象与 ISO 字符串混合；datetime → isoformat。
    用于 cockpit exposure_map.data_updated_at 反映底层数据真实陈旧度
    （§0s 回归：旧实现写 observed_at 永远 fresh）。
    """
    latest_iso: Optional[str] = None
    latest_dt: Optional[datetime] = None
    for stream in streams:
        for raw in stream:
            if raw is None:
                continue
            if isinstance(raw, datetime):
                dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
                iso = dt.isoformat()
            else:
                try:
                    iso = str(raw)
                    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_iso = iso
    return latest_iso


class CockpitReadModel:
    """Cockpit overview 跨账户 canonical 读模型（P10.1）。"""

    DEFAULT_MAX_OPPORTUNITY: int = 10
    DEFAULT_MAX_TRIAGE_ROWS: int = 20

    # 各块 freshness 推荐阈值
    FRESHNESS_HINTS: Mapping[str, Mapping[str, int]] = {
        "decision": {"stale_after_seconds": 30, "max_age_seconds": 120},
        "triage_queue": {"stale_after_seconds": 30, "max_age_seconds": 120},
        "market_guard": {"stale_after_seconds": 10, "max_age_seconds": 60},
        "data_health": {"stale_after_seconds": 30, "max_age_seconds": 120},
        "exposure_map": {"stale_after_seconds": 15, "max_age_seconds": 90},
        "opportunity_queue": {"stale_after_seconds": 30, "max_age_seconds": 180},
        "safe_actions": {"stale_after_seconds": 30, "max_age_seconds": 120},
    }

    def __init__(
        self,
        *,
        trading_state_repo: Any,
        signal_module: Any,
        runtime_read_model: Any,
        intel_read_model: Any = None,
    ) -> None:
        self._trading_state_repo = trading_state_repo
        self._signal_module = signal_module
        self._runtime_read_model = runtime_read_model
        # P1.3: opportunity_queue 委托给 IntelReadModel，避免两个读模型重复查 actionable
        # signals；intel_read_model 可为 None（冒烟/单测场景），此时就地派生（最小字段）
        self._intel_read_model = intel_read_model

    def build_overview(
        self,
        *,
        include: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        requested = tuple(include) if include else COCKPIT_BLOCKS
        observed_at = datetime.now(timezone.utc).isoformat()

        # 共享查询（所有块可复用，减少 DB 扇出）
        risk_states = self._trading_state_repo.fetch_latest_risk_state_per_account()
        exposure_rows = (
            self._trading_state_repo.aggregate_open_positions_by_account_symbol()
        )

        # pending 挂单聚合：仅 exposure_map 使用，其它块不查
        pending_rows: list[dict[str, Any]] = []
        if "exposure_map" in requested:
            pending_rows = list(
                self._trading_state_repo.aggregate_pending_orders_by_account_symbol()
            )

        blocks: dict[str, Any] = {}
        if "decision" in requested:
            blocks["decision"] = self._build_decision(risk_states, observed_at)
        if "triage_queue" in requested:
            blocks["triage_queue"] = self._build_triage_queue(risk_states, observed_at)
        if "market_guard" in requested:
            blocks["market_guard"] = self._build_market_guard(risk_states, observed_at)
        if "data_health" in requested:
            blocks["data_health"] = self._build_data_health(risk_states, observed_at)
        if "exposure_map" in requested:
            blocks["exposure_map"] = self._build_exposure_map(
                exposure_rows, pending_rows, observed_at
            )
        if "opportunity_queue" in requested:
            blocks["opportunity_queue"] = self._build_opportunity_queue(observed_at)
        if "safe_actions" in requested:
            blocks["safe_actions"] = self._build_safe_actions(
                risk_states,
                blocks.get("decision"),
                observed_at,
            )

        source = compute_source_kind(blocks)
        return {
            "observed_at": observed_at,
            "source": source,
            "freshness_hints": dict(self.FRESHNESS_HINTS),
            "accounts": [
                {
                    "account_alias": row.get("account_alias"),
                    "account_key": row.get("account_key"),
                    "updated_at": _iso(row.get("updated_at")),
                }
                for row in risk_states
            ],
            **blocks,
        }

    # ────────────── 各块 ──────────────
    def _build_decision(
        self,
        risk_states: Iterable[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(risk_states)
        blocked = [r for r in rows if r.get("should_block_new_trades")]
        circuit_open = [r for r in rows if r.get("circuit_open")]
        close_only = [r for r in rows if r.get("close_only_mode")]
        emergency_close = [r for r in rows if r.get("should_emergency_close")]
        last_updated = max(
            (r.get("updated_at") for r in rows if r.get("updated_at") is not None),
            default=None,
        )

        can_trade = not (blocked or circuit_open or emergency_close)
        verdict = (
            "healthy"
            if can_trade and not close_only
            else ("degraded" if close_only and not blocked else "blocked")
        )
        reason_codes: list[str] = []
        if emergency_close:
            reason_codes.append("emergency_close_required")
        if circuit_open:
            reason_codes.append("circuit_open")
        if blocked:
            reason_codes.append("block_new_trades")
        if close_only:
            reason_codes.append("close_only_mode")
        return {
            **self._freshness(
                block="decision",
                observed_at=observed_at,
                data_updated_at=_iso(last_updated),
            ),
            "verdict": verdict,
            "can_trade": can_trade,
            "account_count": len(rows),
            "blocked_accounts": [r.get("account_alias") for r in blocked],
            "circuit_open_accounts": [r.get("account_alias") for r in circuit_open],
            "close_only_accounts": [r.get("account_alias") for r in close_only],
            "emergency_close_accounts": [
                r.get("account_alias") for r in emergency_close
            ],
            "reason_codes": reason_codes,
            "source_kind": "native",
        }

    def _build_triage_queue(
        self,
        risk_states: Iterable[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(risk_states)
        entries: list[dict[str, Any]] = []
        for row in rows:
            layer, reason_code, recommended_action, risk_score = self._classify_triage(
                row
            )
            entries.append(
                {
                    "account_alias": row.get("account_alias"),
                    "account_key": row.get("account_key"),
                    "account_label": row.get("account_alias"),
                    "priority_layer": layer,
                    "risk_score": risk_score,
                    "reason_code": reason_code,
                    "reason": self._triage_reason_text(row, reason_code),
                    "recommended_action": recommended_action,
                    "metrics": {
                        "margin_level": row.get("margin_level"),
                        "margin_guard_state": row.get("margin_guard_state"),
                        "consecutive_failures": row.get("consecutive_failures"),
                        "open_positions_count": row.get("open_positions_count"),
                        "pending_orders_count": row.get("pending_orders_count"),
                        "active_risk_flags": row.get("active_risk_flags") or [],
                        "last_risk_block": row.get("last_risk_block"),
                    },
                    "updated_at": _iso(row.get("updated_at")),
                }
            )
        # 按 priority_layer + risk_score 排序：critical > warn > healthy；同级 score 高先
        order_weight = {
            TRIAGE_LAYER_CRITICAL: 0,
            TRIAGE_LAYER_WARN: 1,
            TRIAGE_LAYER_HEALTHY: 2,
        }
        entries.sort(
            key=lambda e: (
                order_weight.get(e["priority_layer"], 99),
                -float(e.get("risk_score") or 0.0),
            )
        )
        last_updated = max(
            (r.get("updated_at") for r in rows if r.get("updated_at") is not None),
            default=None,
        )
        return {
            **self._freshness(
                block="triage_queue",
                observed_at=observed_at,
                data_updated_at=_iso(last_updated),
            ),
            "entries": entries[: self.DEFAULT_MAX_TRIAGE_ROWS],
            "total": len(entries),
            "source_kind": "native",
        }

    def _build_market_guard(
        self,
        risk_states: Iterable[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(risk_states)
        quote_stale = [r for r in rows if r.get("quote_stale")]
        indicator_degraded = [r for r in rows if r.get("indicator_degraded")]
        db_degraded = [r for r in rows if r.get("db_degraded")]
        last_updated = max(
            (r.get("updated_at") for r in rows if r.get("updated_at") is not None),
            default=None,
        )
        verdict = (
            "ok"
            if not (quote_stale or indicator_degraded or db_degraded)
            else "degraded"
        )
        return {
            **self._freshness(
                block="market_guard",
                observed_at=observed_at,
                data_updated_at=_iso(last_updated),
            ),
            "verdict": verdict,
            "quote_stale_accounts": [r.get("account_alias") for r in quote_stale],
            "indicator_degraded_accounts": [
                r.get("account_alias") for r in indicator_degraded
            ],
            "db_degraded_accounts": [r.get("account_alias") for r in db_degraded],
            "source_kind": "native",
        }

    def _build_data_health(
        self,
        risk_states: Iterable[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(risk_states)
        runtime_modes: dict[str, int] = defaultdict(int)
        last_updated = None
        for row in rows:
            mode = str(row.get("runtime_mode") or "unknown")
            runtime_modes[mode] += 1
            ts = row.get("updated_at")
            if ts is not None and (last_updated is None or ts > last_updated):
                last_updated = ts
        hints = self.FRESHNESS_HINTS["data_health"]
        return {
            **build_freshness_block(
                observed_at=observed_at,
                data_updated_at=last_updated,
                stale_after_seconds=hints["stale_after_seconds"],
                max_age_seconds=hints["max_age_seconds"],
            ),
            "account_count": len(rows),
            "runtime_modes": dict(runtime_modes),
        }

    def _build_exposure_map(
        self,
        exposure_rows: Iterable[dict[str, Any]],
        pending_rows: Iterable[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(exposure_rows)
        pending = list(pending_rows)

        # (A) symbol-major entries[]（symbol × direction bucket，向后兼容）
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row.get("symbol") or ""),
                str(row.get("direction") or ""),
            )
            gross_volume = float(row.get("gross_volume") or 0.0)
            position_count = int(row.get("position_count") or 0)
            bucket = grouped.setdefault(
                key,
                {
                    "symbol": key[0],
                    "direction": key[1],
                    "gross_exposure": 0.0,
                    "position_count": 0,
                    "contributors": [],
                },
            )
            bucket["gross_exposure"] += gross_volume
            bucket["position_count"] += position_count
            bucket["contributors"].append(
                {
                    "account_alias": row.get("account_alias"),
                    "account_key": row.get("account_key"),
                    "volume": gross_volume,
                    "position_count": position_count,
                    # weight 临时占位，之后按 bucket 总量回填
                    "weight": 0.0,
                }
            )
        # 回填 contributors[].weight = 本账户 volume / bucket.gross_exposure
        entries: list[dict[str, Any]] = []
        for bucket in grouped.values():
            total = float(bucket["gross_exposure"] or 0.0)
            contributors = bucket["contributors"]
            for contrib in contributors:
                vol = float(contrib["volume"] or 0.0)
                contrib["weight"] = (vol / total) if total > 0 else 0.0
            contributors.sort(key=lambda c: -float(c.get("volume") or 0.0))
            entries.append(bucket)
        entries.sort(key=lambda b: -float(b.get("gross_exposure") or 0.0))

        # (B) account-major risk_matrix[]（P12-1 新增，账户 × 品种维度）
        risk_matrix = self._build_risk_matrix(rows, pending)

        # §0s 回归：data_updated_at 必须取自 row 的真实 updated_at，
        # 而非 observed_at（聚合时刻）。否则陈旧底层数据仍报 freshness=fresh。
        # SQL 现已暴露 latest_updated_at（aggregate_open_positions_by_account_symbol +
        # aggregate_pending_orders_by_account_symbol 都加了 MAX(updated_at)）。
        # 兼容 mock/单测可能给的 data_updated_at / updated_at 字段名。
        latest_data_at = _max_iso_timestamp(
            (row.get("latest_updated_at") for row in rows),
            (row.get("data_updated_at") for row in rows),
            (row.get("updated_at") for row in rows),
            (row.get("latest_updated_at") for row in pending),
            (row.get("data_updated_at") for row in pending),
            (row.get("updated_at") for row in pending),
        )
        # 没有任何 row 提供 updated_at → fallback 到 observed_at（旧行为，
        # 新数据接入前不破坏既有 mock；但 SQL 已强制返回该字段，prod 路径必有值）
        data_updated_at = latest_data_at or observed_at
        return {
            **self._freshness(
                block="exposure_map",
                observed_at=observed_at,
                data_updated_at=data_updated_at,
            ),
            "mode": "account_symbol",
            "entries": entries,
            "risk_matrix": risk_matrix,
            "total": len(entries),
            "source_kind": "native",
        }

    def _build_risk_matrix(
        self,
        open_rows: Sequence[dict[str, Any]],
        pending_rows: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构造账户 × 品种的风险矩阵。

        输入是 `(account, symbol, direction)` 三元组（open 持仓）和
        `(account, symbol, direction)`（pending 挂单）。按账户分组，品种
        下再按方向聚合得 net/gross/pending；同账户全品种汇总得 total_risk。

        risk_score = cell.gross_exposure / account.total_risk（账户内占比，0~1）。
        一期不叠加 margin_guard_state / margin_level 等绝对风险信号——
        前端按 cell.risk_score 排序即可识别账户内风险集中度。
        """
        # cell_agg[(account_key, symbol)] = {direction → volume} open / pending
        open_agg: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"buy": 0.0, "sell": 0.0, "position_count": 0.0}
        )
        pending_agg: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"buy": 0.0, "sell": 0.0, "pending_count": 0.0}
        )
        account_meta: dict[str, dict[str, Any]] = {}

        for row in open_rows:
            account_key = str(row.get("account_key") or "")
            symbol = str(row.get("symbol") or "")
            direction = str(row.get("direction") or "")
            volume = float(row.get("gross_volume") or 0.0)
            position_count = float(row.get("position_count") or 0)
            open_agg[(account_key, symbol)][direction] += volume
            open_agg[(account_key, symbol)]["position_count"] += position_count
            account_meta.setdefault(
                account_key,
                {
                    "account_alias": row.get("account_alias"),
                    "account_key": account_key,
                },
            )

        for row in pending_rows:
            account_key = str(row.get("account_key") or "")
            symbol = str(row.get("symbol") or "")
            direction = str(row.get("direction") or "")
            volume = float(row.get("pending_volume") or 0.0)
            pending_count = float(row.get("pending_count") or 0)
            pending_agg[(account_key, symbol)][direction] += volume
            pending_agg[(account_key, symbol)]["pending_count"] += pending_count
            account_meta.setdefault(
                account_key,
                {
                    "account_alias": row.get("account_alias"),
                    "account_key": account_key,
                },
            )

        cells_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        all_symbols: dict[str, set[str]] = defaultdict(set)
        for (account_key, symbol), dir_map in open_agg.items():
            all_symbols[account_key].add(symbol)
        for (account_key, symbol), _dir_map in pending_agg.items():
            all_symbols[account_key].add(symbol)

        for account_key, symbols in all_symbols.items():
            for symbol in symbols:
                open_dirs = open_agg.get((account_key, symbol), {})
                pending_dirs = pending_agg.get((account_key, symbol), {})
                buy = float(open_dirs.get("buy", 0.0))
                sell = float(open_dirs.get("sell", 0.0))
                gross = buy + sell
                net = buy - sell
                pending_buy = float(pending_dirs.get("buy", 0.0))
                pending_sell = float(pending_dirs.get("sell", 0.0))
                pending_total = pending_buy + pending_sell
                position_count = int(open_dirs.get("position_count", 0))
                pending_count = int(pending_dirs.get("pending_count", 0))
                cells_by_account[account_key].append(
                    {
                        "symbol": symbol,
                        "gross_exposure": gross,
                        "net_exposure": net,
                        "pending_exposure": pending_total,
                        "position_count": position_count,
                        "pending_count": pending_count,
                    }
                )

        matrix: list[dict[str, Any]] = []
        for account_key, cells in cells_by_account.items():
            total_risk = sum(float(c["gross_exposure"]) for c in cells)
            for cell in cells:
                gross = float(cell["gross_exposure"])
                cell["risk_score"] = (gross / total_risk) if total_risk > 0 else 0.0
            cells.sort(key=lambda c: -float(c.get("gross_exposure") or 0.0))
            meta = account_meta.get(account_key, {})
            account_alias = meta.get("account_alias")
            matrix.append(
                {
                    "account_alias": account_alias,
                    "account_key": account_key,
                    "account_label": account_alias,
                    "total_risk": total_risk,
                    "cells": cells,
                }
            )
        matrix.sort(key=lambda e: -float(e.get("total_risk") or 0.0))
        return matrix

    def _build_opportunity_queue(
        self,
        observed_at: str,
    ) -> dict[str, Any]:
        """最近 actionable signals。

        P1.3: 优先委托给 IntelReadModel（带 account_candidates / recommended_action 富化）；
        未注入 intel_read_model 时退化为就地派生（仅最小字段），确保组件可独立使用。
        """
        if self._intel_read_model is not None:
            intel_payload = self._intel_read_model.build_action_queue(
                page=1,
                page_size=self.DEFAULT_MAX_OPPORTUNITY,
            )
            return {
                **self._freshness(
                    block="opportunity_queue",
                    observed_at=observed_at,
                    data_updated_at=intel_payload.get("freshness", {}).get(
                        "data_updated_at"
                    ),
                ),
                "entries": intel_payload.get("entries") or [],
                "total": int((intel_payload.get("pagination") or {}).get("total") or 0),
                "source_kind": "native",
            }
        # Fallback: 就地派生（保留旧语义，单测/脱 intel 场景可用）
        page = self._signal_module.recent_signal_page(
            actionability="actionable",
            scope="confirmed",
            page=1,
            page_size=self.DEFAULT_MAX_OPPORTUNITY,
            sort="priority_desc",
        )
        rows = list(page.get("items") or [])
        last_generated = rows[0].get("generated_at") if rows else None
        return {
            **self._freshness(
                block="opportunity_queue",
                observed_at=observed_at,
                data_updated_at=iso_or_none(last_generated),
            ),
            "entries": [
                {
                    "signal_id": row.get("signal_id"),
                    "trace_id": row.get("trace_id"),
                    "symbol": row.get("symbol"),
                    "timeframe": row.get("timeframe"),
                    "strategy": row.get("strategy"),
                    "direction": row.get("direction"),
                    "confidence": row.get("confidence"),
                    "priority": row.get("priority"),
                    "rank_source": row.get("rank_source"),
                    "generated_at": iso_or_none(row.get("generated_at")),
                }
                for row in rows
            ],
            "total": int(page.get("total") or 0),
            "source_kind": "native",
        }

    def _build_safe_actions(
        self,
        risk_states: Iterable[dict[str, Any]],
        decision_block: Optional[dict[str, Any]],
        observed_at: str,
    ) -> dict[str, Any]:
        rows = list(risk_states)
        actions: list[dict[str, Any]] = []
        for row in rows:
            alias = row.get("account_alias")
            if row.get("should_emergency_close"):
                actions.append(
                    {
                        "account_alias": alias,
                        "action": "emergency_close_all",
                        "severity": "critical",
                        "reason_code": "emergency_close_required",
                    }
                )
                continue
            if row.get("circuit_open"):
                actions.append(
                    {
                        "account_alias": alias,
                        "action": "review_circuit_breaker",
                        "severity": "critical",
                        "reason_code": "circuit_open",
                    }
                )
            elif row.get("close_only_mode"):
                actions.append(
                    {
                        "account_alias": alias,
                        "action": "toggle_auto_entry",
                        "severity": "warn",
                        "reason_code": "close_only_mode",
                    }
                )
            elif row.get("should_block_new_trades"):
                actions.append(
                    {
                        "account_alias": alias,
                        "action": "review_risk_block",
                        "severity": "warn",
                        "reason_code": "block_new_trades",
                    }
                )
        last_updated = max(
            (r.get("updated_at") for r in rows if r.get("updated_at") is not None),
            default=None,
        )
        return {
            **self._freshness(
                block="safe_actions",
                observed_at=observed_at,
                data_updated_at=_iso(last_updated),
            ),
            "entries": actions,
            "total": len(actions),
            "source_kind": "native",
        }

    # ────────────── 辅助 ──────────────
    def _classify_triage(
        self,
        row: dict[str, Any],
    ) -> tuple[str, Optional[str], Optional[str], float]:
        """返回 (priority_layer, reason_code, recommended_action, risk_score)。"""
        active_flags = row.get("active_risk_flags") or []
        if row.get("should_emergency_close"):
            return (
                TRIAGE_LAYER_CRITICAL,
                "emergency_close_required",
                "emergency_close_all",
                100.0,
            )
        if row.get("circuit_open"):
            return (
                TRIAGE_LAYER_CRITICAL,
                "circuit_open",
                "review_circuit_breaker",
                90.0,
            )
        if row.get("should_block_new_trades"):
            return (
                TRIAGE_LAYER_CRITICAL,
                "block_new_trades",
                "review_risk_block",
                80.0,
            )
        if row.get("close_only_mode"):
            return (
                TRIAGE_LAYER_WARN,
                "close_only_mode",
                "toggle_auto_entry",
                60.0,
            )
        if row.get("should_tighten_stops"):
            return (
                TRIAGE_LAYER_WARN,
                "tighten_stops_required",
                "review_position_stops",
                50.0,
            )
        if str(row.get("margin_guard_state") or "").lower() == "warn":
            return (TRIAGE_LAYER_WARN, "margin_warn", "reduce_exposure", 45.0)
        if active_flags:
            return (
                TRIAGE_LAYER_WARN,
                "active_risk_flags",
                "review_risk_flags",
                30.0 + min(20.0, float(len(active_flags)) * 5.0),
            )
        return (TRIAGE_LAYER_HEALTHY, None, None, 0.0)

    @staticmethod
    def _triage_reason_text(
        row: dict[str, Any],
        reason_code: Optional[str],
    ) -> Optional[str]:
        if reason_code is None:
            return None
        text_map = {
            "emergency_close_required": "emergency close required by risk projection",
            "circuit_open": "circuit breaker is open for this account",
            "block_new_trades": "new trades blocked (risk projection)",
            "close_only_mode": "account in close-only mode",
            "tighten_stops_required": "stops should be tightened",
            "margin_warn": "margin guard raising warn",
            "active_risk_flags": "active risk flags present",
        }
        return text_map.get(reason_code)

    def _freshness(
        self,
        *,
        block: str,
        observed_at: str,
        data_updated_at: Optional[str],
    ) -> dict[str, Any]:
        hints = self.FRESHNESS_HINTS[block]
        return build_freshness_block(
            observed_at=observed_at,
            data_updated_at=data_updated_at,
            stale_after_seconds=hints["stale_after_seconds"],
            max_age_seconds=hints["max_age_seconds"],
        )
