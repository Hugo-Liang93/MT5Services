"""信号 metadata 键名常量。

跨模块共享的 metadata 字典键名中心化定义。
生产者（signals/orchestration）写入，消费者（trading/backtesting）读取。

新增键必须在此定义，禁止在业务代码中使用裸字符串字面量作为 metadata key。
"""

from __future__ import annotations


class MetadataKey:
    """Signal metadata 键名常量。

    命名约定：
      - 公有键：snake_case（跨模块共享）
      - 私有键：_前缀（模块内部 / 回测注入，不保证稳定）
    """

    # ── 信号管线（orchestration → trading） ──

    SIGNAL_STATE = "signal_state"
    SIGNAL_TRACE_ID = "signal_trace_id"
    SCOPE = "scope"
    BAR_TIME = "bar_time"
    BAR_PROGRESS = "bar_progress"
    SNAPSHOT_TIME = "snapshot_time"
    CLOSE_PRICE = "close_price"
    STATE_CHANGED = "state_changed"
    PREVIOUS_STATE = "previous_state"
    SESSION_BUCKETS = "session_buckets"
    SPREAD_POINTS = "spread_points"
    SYMBOL_POINT = "symbol_point"
    SPREAD_PRICE = "spread_price"

    # ── Regime 上下文（runtime_processing → evaluator / trading） ──

    REGIME = "regime"
    REGIME_HARD = "_regime"
    REGIME_SOFT = "_soft_regime"
    REGIME_PROBABILITIES = "regime_probabilities"
    MARKET_STRUCTURE = "market_structure"
    EVENT_IMPACT_FORECAST = "_event_impact_forecast"

    # ── 策略输出（strategy.evaluate() → trading） ──

    ENTRY_SPEC = "entry_spec"
    EXIT_SPEC = "exit_spec"
    SIGNAL_GRADE = "signal_grade"
    STRATEGY_CATEGORY = "strategy_category"
    COMPOSITE = "composite"

    # ── 策略评估明细（strategy → 诊断 / 日志） ──

    WHY = "why"
    WHEN = "when"
    WHY_SCORE = "why_score"
    WHEN_SCORE = "when_score"
    WHERE_SCORE = "where_score"
    VOL_SCORE = "vol_score"

    # ── 数据注入（runtime / service → strategy） ──

    RECENT_BARS = "recent_bars"

    # ── 交易状态（trading 内部） ──

    PARAMS = "params"
    ENTRY_ORIGIN = "entry_origin"
    ESTIMATED_MARGIN = "estimated_margin"
    SIGNAL = "signal"
    EXIT_REASON = "exit_reason"
    EXIT_REGIME = "exit_regime"
    R_MULTIPLE = "r_multiple"

    # ── Intrabar 交易链路（cross-TF trigger → coordinator → guard） ──

    INTRABAR_TRIGGER_TF = "intrabar_trigger_tf"
    INTRABAR_PARENT_BAR_TIME = "intrabar_parent_bar_time"
    INTRABAR_STABLE_BARS = "intrabar_stable_bars"
    INTRABAR_ATR_SOURCE = "intrabar_atr_source"

    # ── 私有 / 回测注入 ──

    ENQUEUED_AT = "_enqueued_at"
    MIN_RAW_CONFIDENCE = "_min_raw_confidence"
    PRE_COMPUTED_AFFINITY = "_pre_computed_affinity"
    SKIP_PERFORMANCE_TRACKER = "_skip_performance_tracker"
    SKIP_CALIBRATOR = "_skip_calibrator"
