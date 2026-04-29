"""ADR-013 审计表：每条信号的入场策略决策落地记录。

记录维度：account_key + signal_id + 策略/TF/PatternType + policy_name + branch +
group_id + members JSONB + decided_at + 终态（filled / expired / cancelled /
cancelled_oco_sibling）+ fill_member_id + fill_at。

用途：按 policy/branch/pattern_type 切片分析 W/L 比，为下一轮 mapping 调优提供
数据驱动证据。
"""

DDL = """
CREATE TABLE IF NOT EXISTS entry_policy_decisions (
    account_key TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('buy', 'sell')),
    pattern_type TEXT,
    policy_name TEXT NOT NULL,
    branch TEXT,
    group_id TEXT NOT NULL,
    cancellation_policy TEXT,
    members JSONB NOT NULL DEFAULT '[]'::jsonb,
    decided_at TIMESTAMPTZ NOT NULL,
    fill_member_id TEXT,
    fill_at TIMESTAMPTZ,
    fill_price DOUBLE PRECISION,
    fill_outcome TEXT
        CHECK (fill_outcome IS NULL OR fill_outcome IN (
            'filled', 'expired', 'cancelled', 'cancelled_oco_sibling',
            'rejected', 'orphan'
        )),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_key, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_entry_policy_decisions_strategy_tf
    ON entry_policy_decisions (strategy, timeframe, decided_at DESC);

CREATE INDEX IF NOT EXISTS idx_entry_policy_decisions_policy
    ON entry_policy_decisions (policy_name, branch, decided_at DESC);

CREATE INDEX IF NOT EXISTS idx_entry_policy_decisions_group
    ON entry_policy_decisions (account_key, group_id);

CREATE INDEX IF NOT EXISTS idx_entry_policy_decisions_pattern
    ON entry_policy_decisions (pattern_type, decided_at DESC);
"""


UPSERT_SQL = """
INSERT INTO entry_policy_decisions (
    account_key, signal_id, strategy, timeframe, direction, pattern_type,
    policy_name, branch, group_id, cancellation_policy, members,
    decided_at, fill_member_id, fill_at, fill_price, fill_outcome,
    metadata, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s
)
ON CONFLICT (account_key, signal_id) DO UPDATE SET
    strategy = EXCLUDED.strategy,
    timeframe = EXCLUDED.timeframe,
    direction = EXCLUDED.direction,
    pattern_type = EXCLUDED.pattern_type,
    policy_name = EXCLUDED.policy_name,
    branch = EXCLUDED.branch,
    group_id = EXCLUDED.group_id,
    cancellation_policy = EXCLUDED.cancellation_policy,
    members = EXCLUDED.members,
    decided_at = EXCLUDED.decided_at,
    fill_member_id = COALESCE(EXCLUDED.fill_member_id, entry_policy_decisions.fill_member_id),
    fill_at = COALESCE(EXCLUDED.fill_at, entry_policy_decisions.fill_at),
    fill_price = COALESCE(EXCLUDED.fill_price, entry_policy_decisions.fill_price),
    fill_outcome = COALESCE(EXCLUDED.fill_outcome, entry_policy_decisions.fill_outcome),
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
"""


# fill_outcome 单独更新：fill 路径已知 group_id + member_id，但不重写 strategy/policy 等字段。
UPDATE_FILL_OUTCOME_SQL = """
UPDATE entry_policy_decisions
SET fill_member_id = %s,
    fill_at = %s,
    fill_price = %s,
    fill_outcome = %s,
    updated_at = NOW()
WHERE account_key = %s AND group_id = %s
"""
