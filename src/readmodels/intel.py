"""Intel 行动队列读模型（P10.2）。

替代 QuantX 当前对 `signals/recent + market/context` 的前端 guard-aware 启发式排序。
最小返回字段：id/signal_id/trace_id/symbol/timeframe/strategy/direction/actionability/
guard_reason_code/priority/rank_source/account_candidates[]/recommended_action/freshness。

account_candidates[] 来源：`signal_config.account_bindings`（{account_alias: [strategy]}）
反向索引 → strategy → list[account_alias]。前端一页即可回答"这个 signal 可在哪些账户跑"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.readmodels.freshness import (
    age_seconds as _age_seconds,
    build_freshness_block,
    freshness_state as _freshness_state_shared,
    iso_or_none as _iso,
)


INTEL_MAX_QUEUE_SIZE: int = 50
INTEL_STALE_AFTER_SECONDS: int = 60
INTEL_MAX_AGE_SECONDS: int = 300


def _freshness_state(age_value: Optional[float]) -> str:
    return _freshness_state_shared(
        age_value,
        stale_after_seconds=INTEL_STALE_AFTER_SECONDS,
        max_age_seconds=INTEL_MAX_AGE_SECONDS,
    )


class IntelReadModel:
    """Intel /action-queue canonical 读模型（P10.2）。"""

    def __init__(
        self,
        *,
        signal_module: Any,
    ) -> None:
        self._signal_module = signal_module

    # ────────────── /v1/intel/action-queue ──────────────
    def build_action_queue(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        observed_at = datetime.now(timezone.utc).isoformat()
        page_result = self._signal_module.recent_signal_page(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            actionability="actionable",
            scope="confirmed",
            page=page,
            page_size=min(page_size, INTEL_MAX_QUEUE_SIZE),
            sort="priority_desc",
        )
        rows = list(page_result.get("items") or [])
        total = int(page_result.get("total") or 0)

        strategy_to_accounts = self._strategy_to_accounts_index()
        entries = [
            self._build_entry(row, strategy_to_accounts) for row in rows
        ]
        last_generated = rows[0].get("generated_at") if rows else None
        freshness = build_freshness_block(
            observed_at=observed_at,
            data_updated_at=last_generated,
            stale_after_seconds=INTEL_STALE_AFTER_SECONDS,
            max_age_seconds=INTEL_MAX_AGE_SECONDS,
        )
        return {
            "observed_at": observed_at,
            "entries": entries,
            "pagination": {
                "page": int(page_result.get("page") or page),
                "page_size": int(page_result.get("page_size") or page_size),
                "total": total,
            },
            "freshness": freshness,
            "filters": {
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy": strategy,
            },
        }

    # ────────────── 内部构造 ──────────────
    def _strategy_to_accounts_index(self) -> Mapping[str, list[str]]:
        getter = getattr(self._signal_module, "strategy_account_bindings", None)
        if not callable(getter):
            return {}
        try:
            return getter() or {}
        except Exception:
            return {}

    @staticmethod
    def _build_entry(
        row: dict[str, Any],
        strategy_to_accounts: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        signal_id = row.get("signal_id")
        strategy = str(row.get("strategy") or "")
        candidates_aliases = strategy_to_accounts.get(strategy, [])
        account_candidates = [
            {
                "account_alias": alias,
                "binding_source": "signal_config.account_bindings",
            }
            for alias in candidates_aliases
        ]
        generated_at = row.get("generated_at")
        generated_at_iso = _iso(generated_at)
        age = _age_seconds(generated_at_iso)
        priority = row.get("priority")
        return {
            "id": signal_id,
            "signal_id": signal_id,
            "trace_id": row.get("trace_id"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy": strategy or None,
            "direction": row.get("direction"),
            "actionability": row.get("actionability"),
            "guard_reason_code": row.get("guard_reason_code"),
            "priority": priority,
            "rank_source": row.get("rank_source"),
            "confidence": row.get("confidence"),
            "account_candidates": account_candidates,
            "recommended_action": (
                "execute_from_signal" if account_candidates else "review_strategy_deployment"
            ),
            "generated_at": generated_at_iso,
            "freshness": {
                "age_seconds": age,
                "freshness_state": _freshness_state(age),
            },
        }
