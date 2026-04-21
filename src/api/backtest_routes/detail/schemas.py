"""Backtest detail Pydantic 响应模型（Phase 1 P11）。

契约先行、字段固定、无 alias —— 前端可 codegen。与 `src/backtesting/models.py`
里 `BacktestMetrics` 已有字段对齐，派生字段（drawdown_pct / expectancy_r /
avg_r_multiple / monthly_returns）由 `BacktestDetailReadModel` 组装。

字段无法计算时显式返回 None，前端按 null 渲染"暂无"而非 fallback。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class MonthlyReturn(BaseModel):
    """月度收益（热力图单元）。"""

    label: str = Field(..., description="YYYY-MM")
    pnl: float = Field(..., description="该月绝对盈亏（USD）")
    return_pct: float = Field(..., description="相对月初权益的百分比，0.015 = +1.5%")
    trade_count: int = Field(..., description="该月平仓交易数（交易按 exit_time 归属）")


class FreshnessBlock(BaseModel):
    """和其他读模型一致的 freshness 契约（对齐 src/readmodels/freshness.py）。"""

    observed_at: str
    data_updated_at: Optional[str] = None
    age_seconds: Optional[float] = None
    stale_after_seconds: float
    max_age_seconds: float
    freshness_state: str
    source_kind: str = "native"
    fallback_applied: bool = False
    fallback_reason: Optional[str] = None


class BacktestMetricsSummary(BaseModel):
    """回测运行级总指标（evaluation page 顶部 summary 板）。

    与 `src.backtesting.models.BacktestMetrics` 字段一一对应，外加从 `config`
    派生的 `max_drawdown_pct`，以及从 trades 派生的 `expectancy_r / avg_r_multiple`。
    """

    run_id: str

    # 核心盈利指标
    total_pnl: float = Field(..., description="USD 绝对盈亏")
    total_pnl_pct: float = Field(..., description="相对 initial_balance 的百分比")
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float = Field(..., description="0~1")
    profit_factor: float
    expectancy: float = Field(..., description="USD 单位的单笔期望")
    expectancy_r: Optional[float] = Field(
        None,
        description=(
            "R 单位期望，mean(pnl / initial_risk) for trades with non-zero risk。"
            "trades 为空或全部 stop_loss 与 entry_price 相同时为 None。"
        ),
    )
    avg_r_multiple: Optional[float] = Field(
        None,
        description="平均 R 倍数（与 expectancy_r 同源；为保持契约显式而独立字段）",
    )

    # 风险指标
    max_drawdown: float = Field(..., description="USD 绝对回撤（正数）")
    max_drawdown_pct: float = Field(
        ...,
        description="相对 initial_balance 的百分比，0~1 范围",
    )
    max_drawdown_duration_bars: int = Field(..., description="最大回撤持续 bar 数")

    # 风险调整收益
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # 平均值
    avg_win: float
    avg_loss: float
    avg_bars_held: float

    # 连击
    max_consecutive_wins: int
    max_consecutive_losses: int

    # 配套时序字段
    monthly_returns: List[MonthlyReturn] = Field(default_factory=list)

    freshness: FreshnessBlock


class EquityCurvePoint(BaseModel):
    """净值曲线单点。"""

    label: str = Field(..., description="前端 x 轴 label（与 timestamp 相同，ISO8601）")
    timestamp: str = Field(..., description="ISO8601 UTC 时间戳")
    equity: float = Field(..., description="当前权益（USD）")
    drawdown: float = Field(..., description="当前回撤（running_max - equity，≥0）")
    drawdown_pct: float = Field(..., description="相对 running_max 的百分比，0~1")
    pnl_cumulative: float = Field(
        ..., description="累计盈亏 = equity - initial_balance"
    )


class BacktestEquityCurve(BaseModel):
    """净值曲线响应。"""

    run_id: str
    initial_balance: float
    points: List[EquityCurvePoint]
    freshness: FreshnessBlock


class BacktestMonthlyReturns(BaseModel):
    """月度收益响应。"""

    run_id: str
    initial_balance: float
    months: List[MonthlyReturn]
    freshness: FreshnessBlock


# ────────────────────── Phase 2: Walk-Forward ──────────────────────


class WalkForwardWindow(BaseModel):
    """单个 Walk-Forward 窗口 (IS / OOS) 分段结果。"""

    split_index: int
    window_label: str = Field(..., description="格式：'train_start_date→test_end_date'")
    train_start: Optional[str] = Field(None, description="ISO8601 UTC")
    train_end: Optional[str] = None
    test_start: Optional[str] = None
    test_end: Optional[str] = None
    best_params: dict = Field(
        default_factory=dict,
        description="训练窗口选出的最优策略参数",
    )
    is_pnl: Optional[float] = None
    oos_pnl: Optional[float] = None
    is_sharpe: Optional[float] = None
    oos_sharpe: Optional[float] = None
    is_win_rate: Optional[float] = None
    oos_win_rate: Optional[float] = None
    oos_max_drawdown: Optional[float] = None
    oos_trade_count: Optional[int] = None


class WalkForwardAggregate(BaseModel):
    """聚合 OOS 指标 + 过拟合 / 一致性判定。"""

    overfitting_ratio: Optional[float] = Field(
        None,
        description=(
            "avg(IS_metric)/avg(OOS_metric)。> 2.0 = 过拟合；"
            "None 表示 OOS 均为负（严重过拟合，原始值为 inf 无法落 NUMERIC）"
        ),
    )
    consistency_rate: Optional[float] = Field(None, description="OOS 盈利窗口占比，0~1")
    oos_sharpe: Optional[float] = None
    oos_win_rate: Optional[float] = None
    oos_total_trades: Optional[int] = None
    oos_total_pnl: Optional[float] = None


class WalkForwardDetail(BaseModel):
    """Walk-Forward 任务 detail 响应。"""

    wf_run_id: str
    backtest_run_id: Optional[str] = Field(
        None,
        description="关联的 backtest_runs.run_id；WF 独立任务时为空",
    )
    created_at: str
    completed_at: Optional[str] = None
    status: str = Field(..., description="'running' | 'completed' | 'failed'")
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    experiment_id: Optional[str] = None
    n_splits: int
    config: dict = Field(
        default_factory=dict,
        description="WF 配置快照（symbol/timeframe/n_splits/anchored 等）",
    )
    aggregate: WalkForwardAggregate
    windows: List[WalkForwardWindow] = Field(default_factory=list)
    freshness: FreshnessBlock


# ────────────────────── Phase 3: Correlation Analysis ──────────────────────


class CorrelationPairModel(BaseModel):
    """策略对的相关性。"""

    strategy_a: str
    strategy_b: str
    correlation: float = Field(..., description="Pearson 相关系数，[-1, 1]")
    overlap_count: int = Field(..., description="两策略同时有信号的 bar 数")
    agreement_rate: float = Field(..., description="方向一致占比，[0, 1]")


class CorrelationAnalysisDetail(BaseModel):
    """策略相关性分析 detail 响应。"""

    analysis_id: str
    backtest_run_id: str
    created_at: str
    correlation_threshold: float = Field(
        ..., description="调用参数快照：超过此值视为'高度相关'"
    )
    penalty_weight: float = Field(..., description="调用参数快照：降权倍数")
    total_bars_analyzed: int
    strategies_analyzed: int
    high_correlation_count: int
    pairs: List[CorrelationPairModel] = Field(default_factory=list)
    high_correlation_pairs: List[CorrelationPairModel] = Field(default_factory=list)
    strategy_weights: dict = Field(
        default_factory=dict,
        description="推荐策略权重（相关策略降权后）",
    )
    summary: dict = Field(
        default_factory=dict,
        description="辅助字段（all_pairs_count / strategies 列表等）",
    )
    freshness: FreshnessBlock


class CorrelationAnalysisResult(CorrelationAnalysisDetail):
    """POST 端点返回：同 detail 但附加 `replayed` 字段标识是否命中幂等回放。"""

    replayed: bool = Field(
        False,
        description="True 表示这次返回的是历史持久化结果（non-idempotent 语义未来保留）",
    )


# ────────────────── Phase 4a: Trade Structure ──────────────────


class HoldingBucket(BaseModel):
    """持仓时长分桶。"""

    label: str = Field(..., description="如 '<1h' / '1-4h' / '4-24h' / '>1d'")
    lower_minutes: Optional[int] = None
    upper_minutes: Optional[int] = None
    trade_count: int
    avg_pnl_pct: Optional[float] = None


class DistributionBucket(BaseModel):
    """MFE/MAE 分布直方图的一个桶。"""

    lower_pct: float = Field(..., description="下界，百分比")
    upper_pct: Optional[float] = Field(
        None, description="上界，百分比；最后一桶可为 None 表示 +inf"
    )
    count: int


class TradeStructure(BaseModel):
    """交易结构 detail 响应。

    所有字段从 `backtest_trades` 派生；历史无 mfe/mae/hold 数据的 run 会返回部分
    None（`partial_data=True`），前端按 null 渲染"暂无"。
    """

    run_id: str
    total_trades: int
    partial_data: bool = Field(
        False,
        description="True 表示部分 trades 缺 mfe/mae/hold 字段（旧回测无此数据）",
    )
    avg_hold_minutes: Optional[float] = None
    median_hold_minutes: Optional[float] = None
    max_loss_streak: int = Field(0, description="最长连续亏损笔数")
    max_win_streak: int = Field(0, description="最长连续盈利笔数")
    avg_mfe_pct: Optional[float] = None
    avg_mae_pct: Optional[float] = None
    mfe_distribution: List[DistributionBucket] = Field(default_factory=list)
    mae_distribution: List[DistributionBucket] = Field(default_factory=list)
    holding_buckets: List[HoldingBucket] = Field(default_factory=list)
    freshness: FreshnessBlock


# ────────────────── Phase 4a: Execution Realism ──────────────────


class ExecutionRealism(BaseModel):
    """执行现实性敏感性分析 detail 响应。

    Phase 4a 只提供契约和端点占位；实际字段计算在 Phase 4b（敏感性分析重放）。
    当前所有历史 run 的这些字段均为 None。
    """

    run_id: str
    available: bool = Field(
        False,
        description=(
            "True 表示该 run 已完成敏感性分析（Phase 4b 后）；"
            "False 表示未计算（占位 None 字段）"
        ),
    )
    spread_sensitivity: Optional[float] = Field(
        None, description="±50% spread 重放下 PnL 变化百分比"
    )
    slippage_sensitivity: Optional[float] = Field(
        None, description="±1 tick slippage 重放下 PnL 变化百分比"
    )
    broker_variance: Optional[float] = Field(
        None, description="spread + slippage 组合方差"
    )
    session_dependence: Optional[float] = Field(
        None, description="按交易时段分组 PnL 的基尼系数（0=均匀，1=集中）"
    )
    freshness: FreshnessBlock
