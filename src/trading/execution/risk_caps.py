"""单笔交易风险上限契约。

## 职责

独立的风险上限判定模块。不依赖 TradeExecutor / PreTradePipeline / MT5 —— 纯函数，
只接受契约 DTO，只返回契约 DTO。消费方负责将判定结果翻译为拒单或放行。

## 契约

- `MaxSingleTradeLossPolicy` (frozen dataclass)：单笔亏损 hard cap 配置
- `SingleTradeLossCheckResult` (frozen dataclass)：判定结果
- `check_single_trade_loss_cap(policy, *, position_size, sl_distance, contract_size)`
    → `SingleTradeLossCheckResult`：纯函数

## 设计纪律

- 无 getattr / or default 兜底：调用方必须传完整参数
- 无静默通过：policy.enabled() 为 False 时**明确返回 allowed=True 并标注 reason=None**
- 无双入口：仅通过 `check_single_trade_loss_cap` 唯一接口访问判定逻辑
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MaxSingleTradeLossPolicy:
    """单笔交易绝对亏损 hard cap 配置。

    Attributes:
        max_loss_usd: 单笔交易最大允许亏损（USD 绝对值）。
          - `None` 显式禁用 cap（调用方清楚知道无上限）
          - `0.0` 禁止任何新仓（紧急停手 / 测试）
          - `> 0` 正常阈值
    """

    max_loss_usd: Optional[float]

    def __post_init__(self) -> None:
        if self.max_loss_usd is not None and self.max_loss_usd < 0:
            raise ValueError(
                f"max_loss_usd must be None or >= 0, got {self.max_loss_usd}"
            )

    def enabled(self) -> bool:
        """策略是否有效（None = 禁用）。"""
        return self.max_loss_usd is not None


@dataclass(frozen=True)
class SingleTradeLossCheckResult:
    """单笔亏损 cap 判定结果。"""

    allowed: bool
    estimated_loss_usd: float
    max_allowed_usd: Optional[float]
    reason: Optional[str]


def check_single_trade_loss_cap(
    policy: MaxSingleTradeLossPolicy,
    *,
    position_size: float,
    sl_distance: float,
    contract_size: float,
) -> SingleTradeLossCheckResult:
    """判断单笔交易若触发 SL 是否超过亏损上限。

    估算公式（USD 计价品种）：
        loss = sl_distance × position_size × contract_size

    例：XAUUSD, sl_distance=15.0（点）, size=0.1 手, contract=100
         → loss = 15.0 × 0.1 × 100 = $150

    Args:
        policy: 策略契约
        position_size: 仓位大小（手）
        sl_distance: |entry - stop_loss|（价格单位）
        contract_size: 每手合约规模（如 XAUUSD = 100 盎司）

    Returns:
        SingleTradeLossCheckResult：
          - allowed=True + max_allowed_usd=None 表示 policy 禁用
          - allowed=True + max_allowed_usd=X 表示已启用但通过
          - allowed=False 表示拒绝（reason 必非 None）

    Raises:
        ValueError: 任一输入参数为负值（契约违反）
    """
    if position_size < 0:
        raise ValueError(f"position_size must be >= 0, got {position_size}")
    if sl_distance < 0:
        raise ValueError(f"sl_distance must be >= 0, got {sl_distance}")
    if contract_size < 0:
        raise ValueError(f"contract_size must be >= 0, got {contract_size}")

    estimated = sl_distance * position_size * contract_size

    if not policy.enabled():
        return SingleTradeLossCheckResult(
            allowed=True,
            estimated_loss_usd=estimated,
            max_allowed_usd=None,
            reason=None,
        )

    cap = policy.max_loss_usd
    assert cap is not None  # enabled() 保证非 None
    if estimated > cap:
        return SingleTradeLossCheckResult(
            allowed=False,
            estimated_loss_usd=estimated,
            max_allowed_usd=cap,
            reason=(
                f"estimated single-trade loss ${estimated:.2f} exceeds cap "
                f"${cap:.2f} (size={position_size:.4f}, sl={sl_distance:.4f}, "
                f"contract={contract_size})"
            ),
        )
    return SingleTradeLossCheckResult(
        allowed=True,
        estimated_loss_usd=estimated,
        max_allowed_usd=cap,
        reason=None,
    )
