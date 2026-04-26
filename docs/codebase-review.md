# 代码库审查报告

> 首次审查日期：2026-04-10
> 最近更新：2026-04-23
> 范围：当前工作区全量源码、配置与主要文档。
> 结论定位：风险台账与后续整改入口，不代表已修复代码问题。

---

## ⚠️ 2026-04-23 全面重置声明（重要）

**本日之前所有挖掘结果 / 回测数据 / paper 记录一律作废**——经过多轮架构重构
（P4 解耦、Gap 1/2/3 挖掘方法学修复、综合审查资源优化、结构化策略框架等），
**旧基线的数据依据已不成立**，不得作为新一轮挖掘/策略调优的参考。

### 作废范围

- `data/research/*.json` 历史挖掘/回测输出 → 本地已清空
- `docs/research/*.md` 时点快照 → 已整体删除（2026-04-19 / -20 / -22 / -23 多份）
- TimescaleDB: `research_mining_runs` / `backtest_runs` / `backtest_trades` /
  `backtest_signal_evaluations` / `paper_trading_sessions` / `paper_trade_outcomes` /
  `experiments` → 已 TRUNCATE
- `structured_mdi_sell` 策略（基于作废挖掘编码）→ 已删除代码 + 测试 + 注册
- signal.local.ini 中所有基于作废挖掘的数值（如 `_htf_adx_upper = 45/40`）→ 已清除

### 保留范围（不作废）

**架构性改动保留**（未来挖掘/回测的基础设施）：
- P4 研究↔回测解耦（ResearchDataDeps 端口）
- Gap 1/2a/2b/3 挖掘方法学修复（bug fix 级）
- 综合审查 H1/H2/H3（资源优化）
- `_htf_adx_upper` 参数定义（架构对称扩展，仅清数值不删参数）
- BarrierPredictivePower analyzer
- TODO.md P0 单闭环纪律（流程本身）

**下方 §F 中 2026-04-22/23 的所有实验记录段落已于 2026-04-23 二次彻底清理时
物理删除**（B-1 / P0 Round 1 mdi_sell / Gap 1/2/3 / P4 解耦 / Fresh mining baseline），
仅以 commit hash 索引形式保留（见 §F 顶部）。追溯细节强制走 git log，不经过 MD——
避免未来决策时引用作废 baseline 数字。所有 go-live 决策必须基于 2026-04-23
**之后**的新一轮 fresh mining+backtest+paper 链路。

### 重启流程

1. 跑全新 weekly mining（架构已修复后的 fresh 基线）
2. 根据新 `barrier_predictive_power` top sig 选候选
3. 单策略回测 → PF 门槛判决
4. 通过则 paper_only 启动 7 天 OBSERVE
5. 评估 → 升级 active_guarded 或归档结束

---

## 0v. 2026-04-26 trading 域 critical 降级 + 缓存 snapshot 漂白 + trade_trace 跨账户泄漏 + 跨账户去重折叠 + control_state PK 漂移（P2 ×3 + P3 ×2）

### 触发

User 报告 5 处独立 bug，本轮聚焦"监控 critical 评级链路 + trade_trace 多账户安全 + control_state PK 一致性":

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | P2 | `monitoring/health/common.py:14-23` | `metric_overall_impact()` 漏 4 个 trading 域阻断指标 → critical 时只算 advisory_critical → overall_status 被降成 warning |
| 2 | P2 | `monitoring/health/reporting.py:147-160` | `get_system_status` 读缓存 snapshot 后只拼 active_alerts，不重新评级 → healthy + critical alert 同时呈现的矛盾态 |
| 3 | P2 | `readmodels/trade_trace.py:47-57` | `auto_executions` / `trade_outcomes` 不按 account_alias 过滤 → 多账户共享 signal_id 时跨账户泄漏到当前账户 facts/timeline |
| 4 | P3 | `readmodels/trade_trace.py:_fetch_by_signal_ids` | 去重键仅 `(signal_id, ts)` 无 account 维度 → 两账户同时刻同 signal 事件被静默丢一条 |
| 5 | P3 | `persistence/schema/trade_control_state.py:1-15` | PK=account_alias + UNIQUE INDEX=account_key 落点不一致 → alias 改名时 unique 冲突 ON CONFLICT 接不住 → UPSERT 抛 UNIQUE violation |

### 根因

#### #1 metric_overall_impact 漏 trading 域阻断指标

```python
# 旧实现
def metric_overall_impact(metric_name: str) -> str:
    if metric_name in {
        "data_latency", "indicator_freshness", "queue_depth",
        "economic_calendar_staleness", "economic_provider_failures",
    }:
        return "blocking"
    return "advisory"     # ← reconciliation_lag / circuit_breaker_open /
                           #    execution_failure_rate / execution_queue_overflows
                           #    全落到 advisory
```

evaluate path（reporting.py:70-75）：
```python
elif alert_level == "critical":
    if impact == "blocking":
        report["summary"]["critical_count"] += 1
    else:
        report["summary"]["advisory_critical_count"] += 1
# overall_status 评级
if critical_count > 0: status = "critical"
elif warning_count > 0: status = "warning"
elif (advisory_critical or advisory_warning): status = "warning"   # ← 只升到 warning
```

User 复现：execution_failure_rate=0.5 → 组件 status='critical' + advisory_critical_count=1
+ overall_status='warning'。**正是该报警的指标在 overall_status 上看不到**。

#### #2 get_system_status 缓存与 active_alerts 时序漂移

```python
# 旧实现
def get_system_status(monitor):
    status = monitor._store.get_system_status()  # ← 上一次 generate_report 写入
    if status:
        result = dict(status)
        result["active_alerts"] = list(monitor.active_alerts.values())  # ← 实时
        return result
```

`active_alerts` 是 record_metric 实时写入的，`system_status` snapshot 是
generate_report 周期写入的（默认 60s 间隔）。两者之间 critical alert 触发 →
`active_alerts` 已含 critical，但 snapshot 仍是上一次的 healthy → 接口同时返
overall_status='healthy' + active_alerts=[critical] 矛盾态。消费侧（外部
healthcheck / cockpit / 推送）读这个接口判断系统状态时被瞒过去。

#### #3 trade_trace auto/outcome 跨账户泄漏

```python
# 旧实现
def trace_by_signal_id(signal_id):
    pending_orders = trading_state_repo.fetch_pending_order_states(
        account_alias=self._account_alias_getter(), signal_id=...)  # ← 过滤
    positions = trading_state_repo.fetch_position_runtime_states(
        account_alias=self._account_alias_getter(), signal_id=...)  # ← 过滤
    operations = command_audit_repo.fetch_trace_operations(
        account_alias=self._account_alias_getter(), signal_id=...)  # ← 过滤
    auto_executions = signal_repo.fetch_auto_executions(signal_id=...)  # ← 全局
    trade_outcomes = signal_repo.fetch_trade_outcomes(signal_id=...)    # ← 全局
```

`auto_executions` / `trade_outcomes` 表本身已带 `account_key` / `account_alias`
列（fetcher SELECT 出来），但 fetcher 不接受过滤参数 → readmodel 拿到全局数据。
multi-account 下同 signal_id 被 acct_a / acct_b 都执行 → trace_by_signal_id
返回的 facts['auto_executions'] / facts['trade_outcomes'] / timeline 全混入
其他账户记录。

`signal_outcomes` 是 signal-level（无 account 字段），不在此范围。

#### #4 _fetch_by_signal_ids dedup key 缺 account 维度

```python
# 旧实现
seen: set[tuple[str, str]] = set()
for signal_id in signal_ids:
    for row in fetcher(signal_id=signal_id, limit=limit):
        key = (str(row.get("signal_id")), str(row.get("recorded_at") or row.get("executed_at")))
        if key in seen: continue   # ← 跨账户同时刻同 signal 被错误折叠
        seen.add(key)
        rows.append(row)
```

即使 #3 修复，readmodel 也只问"当前账户"——但 `_fetch_by_signal_ids` 是
helper 不只服务于 by_signal_id 路径；trace_by_trace_id 内部聚合多 signal_id
跨账户 fetch 时，相同 (signal_id, ts) 会被去重；正确去重键必须含 account。

#### #5 trade_control_state PK 与 unique 索引落点不一致

```sql
-- 旧 schema
CREATE TABLE trade_control_state (
    account_alias TEXT PRIMARY KEY,           -- ← PK 在 alias
    account_key TEXT,
    ...
);
CREATE UNIQUE INDEX idx_trade_control_state_account_key
    ON trade_control_state (account_key);     -- ← 唯一约束在 key

-- 旧 upsert
ON CONFLICT (account_alias) DO UPDATE SET ... -- ← 接 alias
```

User 复现：先插 `(alias_a, key_1)`，再插 `(alias_b, key_1)` → 第二条
`account_key` 已存在 → unique 索引先于 PK 冲突 → ON CONFLICT (account_alias)
不匹配 → 直接抛 `UNIQUE constraint failed: trade_control_state.account_key`。
后果：alias 重命名 / 规范化后控制状态持久化直接失败。

### 修复（commit 待 push）

#### #1 metric_overall_impact 加 4 trading metrics

```python
def metric_overall_impact(metric_name: str) -> str:
    if metric_name in {
        # 行情 / 指标 / 经济日历类
        "data_latency", "indicator_freshness", "queue_depth",
        "economic_calendar_staleness", "economic_provider_failures",
        # §0v P2：交易域阻断指标
        "reconciliation_lag", "circuit_breaker_open",
        "execution_failure_rate", "execution_queue_overflows",
    }:
        return "blocking"
    return "advisory"
```

#### #2 get_system_status 共用 escalation helper 重新评级

抽取 `_escalate_status_for_active_alerts()` helper（reporting.py module-level），
被 generate_report 和 get_system_status 共用，确保两条路径对 active_alerts
评级口径一致：

```python
def _escalate_status_for_active_alerts(base_status, active_alerts):
    has_critical = any(... severity/alert_level == "critical" ...)
    has_warning = any(...)
    if has_critical and base_status != "critical": return "critical"
    if has_warning and base_status == "healthy": return "warning"
    return base_status

def get_system_status(monitor):
    status = monitor._store.get_system_status()
    if status:
        result = dict(status)
        active = list(monitor.active_alerts.values())
        result["active_alerts"] = active
        # §0v P2：缓存 snapshot 与 active_alerts 之间可能 stale，
        # 用同款 helper 重新评级避免矛盾态
        result["overall_status"] = _escalate_status_for_active_alerts(
            str(result.get("overall_status") or "unknown"), active)
        return result
    return {...}
```

#### #3 signal_repo + trade_trace 透传 account_alias

```python
# signal_repo.fetch_auto_executions / fetch_trade_outcomes
def fetch_auto_executions(*, signal_id, limit=50,
                          account_alias=None, account_key=None):
    sql = "SELECT ... FROM auto_executions WHERE signal_id = %s"
    params = [signal_id]
    if account_key is not None:
        sql += " AND account_key = %s"; params.append(account_key)
    elif account_alias is not None:
        sql += " AND account_alias = %s"; params.append(account_alias)
    sql += " ORDER BY executed_at ASC LIMIT %s"; params.append(limit)
    return self._fetch_dict_rows(sql, params)

# trade_trace.trace_by_signal_id
current_account_alias = self._account_alias_getter()
auto_executions = self._signal_repo.fetch_auto_executions(
    signal_id=normalized_signal_id, limit=50,
    account_alias=current_account_alias)
trade_outcomes = self._signal_repo.fetch_trade_outcomes(
    signal_id=normalized_signal_id, limit=20,
    account_alias=current_account_alias)
# signal_outcomes 是 signal-level 无 account 字段，不过滤
```

调用方未传 account_alias 时保留旧的全局返回行为（research / analytics 路径合法）。

#### #4 _fetch_by_signal_ids dedup key 含 account 维度 + 透传 account_alias

```python
@staticmethod
def _fetch_by_signal_ids(*, signal_ids, fetcher, limit, account_alias=None):
    seen: set[tuple[str, str, str, str]] = set()
    for signal_id in signal_ids:
        kwargs = {"signal_id": signal_id, "limit": limit}
        if account_alias is not None:
            kwargs["account_alias"] = account_alias
        for row in fetcher(**kwargs):
            key = (
                str(row.get("signal_id") or ""),
                str(row.get("recorded_at") or row.get("executed_at") or ""),
                str(row.get("account_key") or ""),       # ← 新增维度
                str(row.get("account_alias") or ""),     # ← 新增维度
            )
            if key in seen: continue
            seen.add(key); rows.append(row)
```

#### #5 trade_control_state PK 升级 + 删除冗余 unique 索引

```sql
-- DDL（fresh install）
CREATE TABLE trade_control_state (
    account_key TEXT NOT NULL PRIMARY KEY,    -- ← PK 改 account_key
    account_alias TEXT NOT NULL,              -- ← 仍保留作 display + 索引
    ...
);
CREATE INDEX idx_trade_control_state_alias
    ON trade_control_state (account_alias);   -- ← 普通 index 而非 unique

-- POST_INIT migration
UPDATE trade_control_state
SET account_key = account_alias
WHERE account_key IS NULL OR account_key = '';

ALTER TABLE trade_control_state
    ALTER COLUMN account_key SET NOT NULL;

DROP INDEX IF EXISTS idx_trade_control_state_account_key;  -- ← 删冗余 unique

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'trade_control_state'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ILIKE '%(account_key)'
    ) THEN
        ALTER TABLE trade_control_state
            DROP CONSTRAINT IF EXISTS trade_control_state_pkey;
        ALTER TABLE trade_control_state
            ADD CONSTRAINT trade_control_state_pkey
            PRIMARY KEY (account_key);
    END IF;
END $$;

-- UPSERT
ON CONFLICT (account_key) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,   -- ← alias 改名时同步覆盖
    ...
```

### 测试（6 项新契约 + 2 项现有 stub 升级）

| 测试 | 锁定 |
|---|---|
| `test_generate_report_critical_trading_metric_drives_overall_status_to_critical` | execution_failure_rate critical → impact='blocking' + overall_status='critical' |
| `test_generate_report_circuit_breaker_open_critical_drives_overall_status` | circuit_breaker_open critical 同样 |
| `test_get_system_status_does_not_report_healthy_with_active_critical_alert` | 缓存 snapshot=healthy + active critical 注入 → 接口返 critical（非 healthy）|
| `test_trace_by_signal_id_does_not_mix_other_accounts_executions_and_outcomes` | acct_a 调 trace 不能拿到 acct_b 的 auto/outcome |
| `test_fetch_by_signal_ids_dedup_preserves_cross_account_same_timestamp_events` | 两账户同时刻 row 必须保留两条，不被去重折叠 |
| `test_trade_control_state_pk_matches_unique_index_to_avoid_alias_rename_failure` | DDL PK = account_key + UPSERT ON CONFLICT (account_key)（非 alias）|

### 元层教训：**评级链路与去重键必须包含影响维度全集**

§0u 引入 PK 复合化原则：业务实体 ID + 租户/账户维度。§0v 把同一原则
推广到**评级链路 / 去重键 / 缓存评级**：

1. **告警评级元数据完整性**：metric → impact 映射必须涵盖所有阻断类指标，
   不能只覆盖创建时的"行情/指标"集合。新增交易/订单/风控类指标时必须同步
   updated 这张表，否则 critical 评级被自动降级
2. **缓存与实时事件的混合视图必须 re-evaluate**：缓存 snapshot + 实时
   active_alerts 拼接时，必须用同款 escalation helper 重新评级，否则两个
   时点的口径错配产生矛盾态
3. **去重键必须含影响维度全集**：(signal_id, ts) 作 key 假设了"signal 在
   时点上唯一"——多账户拓扑下这个假设不成立，必须把 account_key/alias
   纳入 key
4. **PK + UNIQUE INDEX + ON CONFLICT 三者必须落在同一列上**：迁移过程中
   "PK 在 alias，唯一索引在 key" 这种交错状态会让任意一边的冲突落点都
   不能被另一边接住

强建议（扩充 §0g→§0u 元层教训）：
- 全 schema 审计：`grep "PRIMARY KEY\|UNIQUE INDEX\|ON CONFLICT"`，
  确认每张表 PK / unique 约束 / upsert 落点完全一致
- 监控/告警的 metric → impact 映射应作 single source of truth，
  注册新 metric 时必须同时声明 impact
- readmodel 任何 fetcher 返回 multi-tenant 数据时必须接受 account 过滤参数，
  否则跨账户泄漏只是时间问题
- 任何"缓存 + 实时 patch"模式必须用共享 helper 重新评级，避免两侧时点漂移

### §0g→§0v 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **43 个 bug + 43 次反向锁** + 1 次契约阻断：

| § | bug 数 |
|---|---|
| 0g–0r | 27 |
| 0s | 3 |
| 0t | 3 + 1 契约阻断 |
| 0u | 5 |
| 0v | 5 |

### 测试基线

- 修复前：5 处 user-facing bug；critical 评级 / 缓存 stale / trace 跨账户 / 去重 / control PK 全部零覆盖
- 修复后：6 项新契约测试；`pytest -q` → **2848 passed / 6 skipped / 0 failed**

### 减少边界泄漏的方式

- impact map 在 common.py single source of truth，新增 metric 时一处变更生效
- escalation helper module-level 共享，generate_report 和 get_system_status
  口径自动一致
- signal_repo fetcher account_alias / account_key 参数化，readmodel
  只需透传不需要改 fetcher 内部
- trade_control_state PK / UPSERT / ON CONFLICT 全部锁在 account_key 上，
  alias 仅作 display + 普通索引

---

## 0u. 2026-04-26 多账户共享 ticket PK 互覆 + 监控 count 漂白 + 全失败静默（P1 ×2 + P2 ×3）

### 触发

User 报告 5 处独立 bug，本轮聚焦"多账户拓扑下的事实污染 + 监控/告警漂白":

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | **P1** | `persistence/schema/pending_order_states.py:5` | `order_ticket BIGINT PRIMARY KEY` + `ON CONFLICT (order_ticket)` → 跨账户同 MT5 ticket 互相覆盖 |
| 2 | **P1** | `persistence/schema/position_runtime_states.py:5` | 同前——`position_ticket` 单列 PK 导致跨账户持仓状态被踩掉 |
| 3 | P2 | `monitoring/manager.py:81-86` | `_summary_status_value` 用 `row['count']`，TradingStateAlerts.summary() 输出无 count → failed 行被算成 0 → 监控记录成 healthy（1.0）|
| 4 | P2 | `readmodels/runtime.py:378-397` | `build_trading_summary` 同样 `row.get('count', 0)` → status='healthy' + coordination_issues=[] 把已 failed 监控漂白 |
| 5 | P2 | `monitoring/manager.py:_check_execution_quality` + `trading/execution/eventing.py:763` | execution_count 是"成功数"，全失败场景 (execution_count=0 + execution_failed>0) 被 `if total > 0` 短路掉 → execution_failure_rate 静默空白 |

### 根因

#### #1+#2 单列 ticket 主键违反多账户拓扑契约

仓库明确支持 `multi_account` 拓扑（`config/topology.py` + `instance_context.py`），
不同账户的 MT5 ticket **不保证全局唯一**：

```sql
-- 旧 schema
CREATE TABLE pending_order_states (
    account_alias TEXT NOT NULL,
    account_key TEXT,                         -- ← 后置 ALTER 加入，nullable
    order_ticket BIGINT PRIMARY KEY,          -- ← 全局唯一 PK
    ...
);

-- 旧 upsert
ON CONFLICT (order_ticket) DO UPDATE SET    -- ← 不含账户维度
    account_alias = EXCLUDED.account_alias, ...
```

User 复现：先写 `acct_a/12345`，再写 `acct_b/12345` → 表里只剩 `acct_b`。
account_alias / account_key 字段还在，但 PK 把 acct_a 的整行用 acct_b 覆盖。

后果：
- `cockpit.exposure_map` 跨账户聚合（`DISTINCT ON (account_key, position_ticket)`）
  以为每行账户独立——**但底层数据已经丢失**，呈现的是错误聚合
- `trade_trace` / `risk_projection` 都基于该表 → 下游全部污染
- 风控决策可能基于错误的"另一账户的"持仓状态

`position_runtime_states.position_ticket` 同样问题，cockpit / trace / risk 投影
全链污染。

#### #3+#4 count 字段缺省漂白告警

`TradingStateAlerts.summary()`（§0t 新接入的告警源之一）输出形状：
```python
[{"code": "state_store_unavailable", "status": "failed",
  "severity": "critical", "message": "状态存储不可用"}]
```
**无 count 字段**——因为每行就是一条告警事件，"行存在"本身即为告警事实。

但消费侧（manager + runtime）都用 `int(row.get("count", 0) or 0)`：
```python
# manager._summary_status_value
failed = sum(int(row.get("count", 0) or 0) for row in rows if status == "failed")
return 0.5 if failed > 0 else 1.0  # ← 默认 0 → 1.0 healthy

# RuntimeReadModel.build_trading_summary
failed = sum(int(row.get("count", 0) or 0) for row in summary_rows if ...)
status = "warning" if failed > 0 else "healthy"  # ← 同问题
```

后果：§0t 修了 alerts.summary() 不再标 healthy，但下游消费侧把它再次漂白成
healthy → 整体 dashboard 仍显示无异常，告警链路在最后一环失效。

#### #5 execution_failure_rate 在全失败场景静默

`execute_market_order` 失败路径（eventing.py:763）：
```python
except Exception as exc:
    executor.last_error = str(exc)
    executor.consecutive_failures += 1
    # ← 不增 execution_count，不写 skip_reasons["execution_failed"]
```

vs. 成功路径（line 555）：
```python
result = executor.trading.dispatch_operation("trade", payload)
executor.execution_count += 1  # ← 仅成功增
```

`_check_execution_quality` 用 `total = execution_count`（成功数）做分母：
```python
total = exec_status["execution_count"]     # ← 0（全失败）
failed = skip_reasons.get("execution_failed", 0)  # ← 0（dispatch 路径不写）
if total > 0:                              # ← false → 全失败场景跳过
    failure_rate = failed / total
    record_metric("execution_failure_rate", ...)
```

后果：**恰好在最严重场景里（全部 dispatch 失败）**，execution_failure_rate 一条
都不记录。监控 dashboard 显示无失败率指标，运维以为"没在交易"，实际上
"在交易但全失败"。这是监控反向漂白的典型反模式。

### 修复（commit 待 push）

#### #1+#2 复合主键 (account_key, ticket) + 幂等 migration

```sql
-- DDL（fresh install）
CREATE TABLE IF NOT EXISTS pending_order_states (
    account_alias TEXT NOT NULL,
    account_key TEXT NOT NULL,        -- ← 升为 NOT NULL
    order_ticket BIGINT NOT NULL,     -- ← 单列 PK 标记移除
    ...,
    PRIMARY KEY (account_key, order_ticket)  -- ← 显式复合 PK
);

-- POST_INIT migration（existing install）
UPDATE pending_order_states
SET account_key = account_alias
WHERE account_key IS NULL OR account_key = '';   -- ← backfill

ALTER TABLE pending_order_states
    ALTER COLUMN account_key SET NOT NULL;       -- ← PK 列必须 NN

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'pending_order_states'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ILIKE '%(account_key, order_ticket)'
    ) THEN
        ALTER TABLE pending_order_states
            DROP CONSTRAINT IF EXISTS pending_order_states_pkey;
        ALTER TABLE pending_order_states
            ADD CONSTRAINT pending_order_states_pkey
            PRIMARY KEY (account_key, order_ticket);
    END IF;
END $$;

-- UPSERT
ON CONFLICT (account_key, order_ticket) DO UPDATE SET
    account_alias = EXCLUDED.account_alias, ...
```

`position_runtime_states` 同模式修复，MIGRATION_SQL 注册到
`POST_INIT_DDL_STATEMENTS` 列表末尾。account_key 选择依据：
- 已为全局唯一身份（`build_account_key(env, server, login)` = `"env:server:login"`）
- 现有跨账户聚合 SQL（`aggregate_open_positions_by_account_symbol`）
  已用 `DISTINCT ON (account_key, position_ticket)` —— 复合 PK 与之天然一致
- 单账户场景下 account_key 总等于 alias 或固定值，PK 行为退化为单 ticket，
  无回归

#### #3+#4 count 缺省按 1 计算

```python
# manager._summary_status_value
failed += int(row.get("count", 1) or 1)  # ← 缺省 1（行存在=1 条告警）

# RuntimeReadModel.build_trading_summary
failed = sum(int(row.get("count", 1) or 1) for row in summary_rows
             if row.get("status") == "failed")
```

#### #5 eventing.py 失败路径写 execution_failed + 分母用尝试数

```python
# eventing.py except 分支
except Exception as exc:
    executor.last_error = str(exc)
    executor.consecutive_failures += 1
    # §0u P2：失败 dispatch 必须走 notify_skip 写入 skip_reasons["execution_failed"]
    notify_skip(executor, event.signal_id, "execution_failed",
                event.timeframe or "")
    ...

# manager._check_execution_quality
total_success = int(exec_status.get("execution_count", 0) or 0)
failed = int(skip_reasons.get("execution_failed", 0) or 0)
total_attempts = total_success + failed       # ← 尝试数 = 成功 + 失败
if total_attempts > 0:                        # ← 全失败也满足
    failure_rate = failed / total_attempts    # ← 全失败 → 1.0
    record_metric(... "execution_failure_rate" ...)
```

### 测试（10 项新契约，对应分支零覆盖）

| 测试 | 锁定 |
|---|---|
| `test_pending_order_states_pk_includes_account_scope` | DDL 不能仅是 order_ticket PK；必须 (account_*, order_ticket) |
| `test_pending_order_states_upsert_conflict_target_includes_account_scope` | UPSERT ON CONFLICT 同步 |
| `test_position_runtime_states_pk_includes_account_scope` | 同 PK 锁 |
| `test_position_runtime_states_upsert_conflict_target_includes_account_scope` | 同 ON CONFLICT 锁 |
| `test_summary_status_value_treats_failed_row_without_count_as_failure` | 无 count 字段的 failed 行必须 < 1.0 |
| `test_summary_status_value_returns_healthy_when_no_failed_rows` | 无 failed 时仍 1.0（契约对称）|
| `test_summary_status_value_handles_empty_summary` | 空 payload 兜底 |
| `test_check_execution_quality_records_metric_when_all_dispatches_failed` | 全失败场景 failure_rate=1.0 必写入 |
| `test_check_execution_quality_records_correct_rate_with_partial_failures` | 3 成功 + 1 失败 → rate=0.25（非 1/3=0.333）|
| `test_build_trading_summary_treats_failed_row_without_count_as_failure` | failed 行存在 → status≠healthy + coordination_issues 非空 |

### 元层教训：**契约假设的"幽灵字段"必须被消除**

§0u 与 §0r/§0n/§0p/§0q/§0r/§0t 累计 8 处的统一根因是消费侧对生产侧的隐式
schema 假设——这次升级到**结构层面**（PK / DDL）：

1. **PK 必须反映真实的 entity identity**——多账户拓扑下"账户+ticket"才是
   business-level entity，单 ticket 是 broker-level；schema 必须对齐 business
2. **"幽灵 count 字段"是漂白告警的典型机制**——告警 schema 应明确：
   行存在即告警事实，**禁止**通过缺省 count=0 把行算成"无事件"
3. **监控/告警分母**必须用"尝试数"而非"成功数"——成功数当分母在全失败场景
   会自动退化为 0/0 短路，正是最该报警的时刻反而失声

强建议（扩充 §0g→§0t 元层教训）：
- 任何包含 ticket / 业务实体 ID 的 PK，如果系统支持多租户/多实例拓扑，
  PK 必须包含租户/实例维度——`grep "PRIMARY KEY"` 全 schema 审计一次
- 任何 `int(row.get("count", 0))` 类聚合在新增告警源时必须 PR 检查行 schema 是否带 count
- 任何 `if total > 0` 类分母守卫必须确认 total 在 fail-only 场景里仍非 0
- 失败路径与成功路径在每个统计维度上必须对称（成功 → execution_count++ +
  consecutive_failures=0；失败 → notify_skip + consecutive_failures++）

### §0g→§0u 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **38 个 bug + 38 次反向锁** + 1 次契约阻断：

| § | bug 数 |
|---|---|
| 0g–0r | 27 |
| 0s | 3 |
| 0t | 3 + 1 契约阻断 |
| 0u | 5 |

### 测试基线

- 修复前：5 处 user-facing bug；composite PK / count 缺省 / 全失败分母全部零覆盖
- 修复后：10 项新契约测试；`pytest -q` → **2842 passed / 6 skipped / 0 failed**

### 减少边界泄漏的方式

- 多账户 PK 升级反映在 schema 层面，不依赖"调用方记得加 account_key 过滤"
- migration 用 DO 块幂等，二次启动不重复 drop+add，不污染 startup 时序
- count 缺省语义统一（行存在=1 条告警），打破"消费侧假设生产侧带 count"的幽灵契约
- failure_rate 分母用尝试数（success+failed），失败侧 + 成功侧 statisical 对称

---

## 0t. 2026-04-26 TradingStateAlerts 双源静默 + reporting active_alerts 失效 + monitoring_summary 死参（P1 + P2 + P3）

### 触发

User 报告 4 处独立 bug，全是**告警/健康路径"异常被吞 → 静默标 healthy"**模式：

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | **P1** | `trading/state/alerts.py:_safe_*` | DB / MT5 同时失败 → 所有 `_safe_*` 返 [] → status=healthy（系统瞎眼）|
| 2 | P2 | `monitoring/health/reporting.py:91-99` | `active_alerts` 透传 payload 但不参与 `overall_status` 评级 → 未消除的 critical 静默被报 healthy |
| 3 | P3 | `trading/state/alerts.py:monitoring_summary` | `del hours` 后无条件返当前快照 → hours 参数完全死 |
| 4 | — | `monitoring/health/monitor.py:cache_hit_rate` | （**经核验为设计契约**：`test_cache_hit_rate_no_longer_triggers_alert` 显式锁住"informational only"，已被 `indicator_compute_p99_ms` 替代；不修复，案例归档为 §0 协议成功阻断之例）|

### 根因

#### #1 _safe_* 把异常吞成空列表 → 告警判据全 false

```python
# 旧实现
def _safe_pending_states(self, *, statuses, limit) -> list[dict]:
    try:
        return list(self._state_store.list_pending_order_states(...))
    except Exception:
        return []        # ← DB 故障与"无数据"混同

def summary(self):
    active_pending = self._safe_pending_states(...)        # → []
    lifecycle_missing = self._safe_pending_states(...)     # → []
    open_positions = self._safe_position_states(...)       # → []
    live_orders = self._safe_live_orders()                 # → []
    live_positions = self._safe_live_positions()           # → []
    # 所有告警判据：
    # missing_count > 0 → false
    # orphan_count > 0 → false
    # persisted_active_count != live_order_count → 0 != 0 false
    # unmanaged_positions → []
    # → alerts=[] → status='healthy'
```

后果：DB + MT5 双故障被报"系统无异常"，掩盖严重故障。这是告警系统**最致命**的失败模式。

#### #2 reporting overall_status 只看本次窗口 metric，忽略 active_alerts

```python
# 旧实现 line 91-99
if report["summary"]["critical_count"] > 0:
    report["overall_status"] = "critical"
elif report["summary"]["warning_count"] > 0:
    report["overall_status"] = "warning"
elif (...advisory...):
    report["overall_status"] = "warning"
# active_alerts 已写进 report["active_alerts"] = list(monitor.active_alerts.values())
# 但 overall_status 完全不读它
```

后果：过去触发但仍未消除的 critical alert 在窗口内无新指标进 critical 时被忽略，整体被报 healthy。告警面板上看到 active critical 但 overall_status=healthy，消费侧（Cockpit / 推送）丢链。

#### #3 monitoring_summary(hours) 完全死

```python
def monitoring_summary(self, *, hours: int = 24) -> dict[str, Any]:
    del hours              # ← 参数被显式扔掉
    return self.summary()  # ← 返当前快照，无窗口语义
```

后果：调用方传 `hours=1` 与 `hours=24` 拿到一模一样的数据，但调用方以为"已按窗口过滤"，决策口径失真。

#### #4 cache_hit_rate（**核验为契约**，不是 bug）

User 报告 "cache_hit_rate 永远不可能触发告警 + `pass` 死分支"。**§0 协议三步追查**：

1. 模型默认值：`monitor.alerts["cache_hit_rate"] = {"warning": 0.0, "critical": 0.0}` 是有意为之（line 93）
2. test 锁定：`tests/monitoring/test_health_check_report.py::test_cache_hit_rate_no_longer_triggers_alert`（line 23-31）显式注释 "cache_hit_rate 阈值已设为 0（仅作信息指标），即使值为 0 也不触发告警"
3. 替代指标：同文件 `test_indicator_compute_p99_triggers_warning` 显式声明 "indicator_compute_p99_ms 是替代 cache_hit_rate 的新告警指标"

按 §0 "任何'反常/存在 bug'的代码片段，先读相邻 ADR/契约——若对应已定决策，不是 bug 而是契约"——**不修复**。`pass` 与 `_generate_alert_message` 的 cache_hit_rate 分支是为"未来重新启用阈值"保留的占位符（cosmetic dead code），翻案需先删 test。

### 修复（commit 待 push）

#### #1 source_errors 透传 + 数据源故障 → 显式 critical 告警

```python
# 新实现
def summary(self, *, pending_limit=50, position_limit=50) -> dict[str, Any]:
    source_errors: dict[str, str] = {}
    active_pending = self._safe_pending_states(..., source_errors=source_errors)
    # ... 所有 _safe_* 都接收同一 source_errors dict 引用
    ...
    # 末尾把失败源转告警
    if "state_store" in source_errors:
        alerts.append(self._alert(
            code="state_store_unavailable",
            severity="critical",
            message="状态存储不可用，告警判据失效",
            details={"error": source_errors["state_store"]},
        ))
    if "trading_module" in source_errors:
        alerts.append(self._alert(
            code="trading_module_unavailable",
            severity="critical",
            message="交易模块查询不可用，告警判据失效",
            details={"error": source_errors["trading_module"]},
        ))
    # payload 暴露 source_errors 给消费侧审计
    return {..., "source_errors": dict(source_errors)}

def _safe_pending_states(self, *, statuses, limit, source_errors) -> list[dict]:
    try:
        return list(self._state_store.list_pending_order_states(...))
    except Exception as exc:
        logger.warning("TradingStateAlerts: list_pending_order_states failed: %s: %s",
                       type(exc).__name__, exc)
        source_errors.setdefault("state_store", f"{type(exc).__name__}: {exc}")
        return []
```

#### #2 reporting overall_status 把 active_alerts 拉进评级

```python
# 在原 critical_count / warning_count 评级后追加
active_alerts_list = report.get("active_alerts") or []
has_active_critical = any(
    str((a.get("severity") or a.get("alert_level") or "")).lower() == "critical"
    for a in active_alerts_list
)
has_active_warning = any(
    str((a.get("severity") or a.get("alert_level") or "")).lower() == "warning"
    for a in active_alerts_list
)
if has_active_critical and report["overall_status"] != "critical":
    report["overall_status"] = "critical"
elif has_active_warning and report["overall_status"] == "healthy":
    report["overall_status"] = "warning"
```

#### #3 monitoring_summary 至少把 hours 写进 payload

```python
def monitoring_summary(self, *, hours: int = 24) -> dict[str, Any]:
    # 现阶段 alerts 只看最新状态而非历史窗口，无法真正按 hours 聚合，
    # 但至少必须把 hours 写进 payload 让消费方知道窗口意图
    payload = self.summary()
    payload["time_range_hours"] = int(hours)
    return payload
```

### 测试（4 项新契约 + 1 项契约保留）

| 测试 | 锁定 |
|---|---|
| `test_trading_state_alerts_does_not_report_healthy_when_data_sources_fail` | 双数据源故障必须不被标 healthy + 告警 code 含 data_source_unavailable / state_store_unavailable / trading_module_unavailable |
| `test_trading_state_alerts_state_store_only_failure_still_alerts` | 单源故障同样触发告警，不能因另一源正常被掩盖 |
| `test_monitoring_summary_records_hours_in_payload` | hours=1 vs hours=24 必须在 `time_range_hours` 体现 |
| `test_generate_report_overall_status_reflects_active_critical_alert` | 注入 active critical → overall_status=critical（即便本次窗口无新 critical metric）|
| `test_generate_report_overall_status_reflects_active_warning_alert` | 注入 active warning → overall_status≥warning |
| `test_cache_hit_rate_no_longer_triggers_alert`（保留）| 设计契约保留：阈值=0 仅作信息指标，永不告警 |

### 元层教训：**告警/健康路径必须区分"无数据"与"数据源失败"**

§0g→§0s 累计反复见到一个反模式：**消费侧默认 fail-soft 吞异常 → 关键判据被静默 short-circuit**。
§0t 把这条原则升级到告警/健康路径本身：**告警系统自身故障必须 fail-loud**——
如果告警源（DB / MT5 / metric pipeline）失效，必须 (a) 在 payload 显式暴露 (b) 把 overall_status
推到不低于 warning 而不是默认 healthy。

具体到本案：
- `_safe_*` 仍然 fail-soft（保证调用方拿到 list 而不挂），但**额外**把异常透传给上层
- `summary()` 在末尾把数据源故障转成显式 critical 告警
- `payload["source_errors"]` 字段让消费侧/审计能定位源
- `reporting.generate_report` 把 `active_alerts` 拉进 overall_status 评级，未消除 alert 不再被新窗口洗白

强建议（扩充 §0g→§0s 元层教训）：
- 告警系统/健康路径所有 `except: return []` 必须额外接受 `errors` 透传 dict 上送
- `overall_status` 类聚合必须把"运行中未消除的 active_alerts"拉进决策，不能只看本窗口聚合
- `del param` 模式（参数显式扔）必须**立即**触发 PR review 红旗——要么真正实现要么把签名改成不带该参数

强建议（**核验阻断之例**：P3#4 案）：
- §0 协议"任何'反常/存在 bug'的代码片段，先读相邻 ADR/契约"在本轮成功阻断 1 例 user 报告
- 死分支若是有意 informational-only 占位，必须在源码注释中显式声明 "intentional dead — informational only" 减少未来误判
- 翻案"informational metric → 告警 metric" 的设计决定必须先**删除** lock test 再实施

### §0g→§0t 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **33 个 bug + 33 次反向锁** + **1 次契约阻断**：

| § | bug 数 |
|---|---|
| 0g–0r | 27 |
| 0s | 3 |
| 0t | 3 + 1 契约阻断 |

### 测试基线

- 修复前：3 处 user-facing bug；告警/健康双源故障路径零覆盖
- 修复后：5 项新契约测试；`pytest -q` → **2832 passed / 6 skipped / 0 failed**

### 减少边界泄漏的方式

- `_safe_*` 不再单独"吞 + 返空"，而是把异常透传给 `source_errors` 引用 dict，调用方可决策
- `reporting.generate_report` 把 `active_alerts`（注册侧权威）拉进 overall_status（消费侧聚合），消除生产/消费视图漂移
- `monitoring_summary(hours)` 即使无法真正窗口聚合，也至少在 payload 显式记录窗口意图
- 死分支契约（cache_hit_rate）有 lock test 守门，**不让"看似 bug 的契约"被反复重报反复消解**

---

## 0s. 2026-04-26 cancel-result schema 解析 + cockpit exposure freshness（P2 ×2 + P3）

### 触发

User 报告 3 处独立 bug，全是消费侧未识别仓库已声明的标准 schema/字段：

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | P2 | `trading/state/recovery.py:88-100` | 过期挂单的撤单失败因 helper TypeError 被外层 except 吞，路径稳定性依赖兜底 |
| 2 | P2 | `trading/state/recovery_policy.py:55-60` | orphan 路径同源 helper TypeError 无 try/except，**直接打挂 startup recovery** |
| 3 | P3 | `readmodels/cockpit.py:_build_exposure_map` | data_updated_at 写成 observed_at → freshness 永远 fresh |

### 根因

#### #1+#2 `_ticket_was_cancelled` helper schema 假设错

仓库标准 cancel API schema（`mt5_trading.py` 多处 + `tests/trading/test_*.py`
确认）：
```python
{"canceled": [int_ticket, ...], "failed": [{"ticket": int, "error": str}, ...]}
```

旧 helper 假设 `failed` 是 `list[int]`：
```python
failed = {int(item) for item in (result.get("failed") or []) if item is not None}
# ↑ item 是 dict 时 int(dict) → TypeError
```

后果：
- recovery.py：外层 `except Exception` 吞 → cancelled=False → 路径稳定但
  依赖兜底（不可观测）
- recovery_policy.py：orphan 路径**无 try/except** → TypeError 冒出 →
  整个 startup recovery 挂掉

#### #3 cockpit exposure_map data_updated_at 写错时间源

```python
# 旧实现
return {
    **self._freshness(
        block="exposure_map",
        observed_at=observed_at,
        data_updated_at=observed_at,   # ← 用聚合时刻，永远新鲜
    ),
    ...
}
```

后果：6 小时前的 exposure row 仍报 `freshness_state='fresh'` → 掩盖明显
陈旧的风险暴露数据。

### 修复（commit `8b1a81c`）

#### #1+#2 引入 `_normalize_ticket_set` 公共 helper + orphan 兜底

```python
# recovery_policy.py 新增 module-level helper
def _normalize_ticket_set(items: Iterable[Any]) -> set[int]:
    """归一化为 ticket 整数集合。
    支持：list[int] / list[str] / list[{'ticket': int, 'error': str}]
    脏元素静默跳过，不抛 TypeError。
    """
    tickets: set[int] = set()
    for item in items or []:
        if item is None:
            continue
        if isinstance(item, dict):
            raw = item.get("ticket")
            if raw is None:
                continue
            try:
                tickets.add(int(raw))
            except (TypeError, ValueError):
                continue
            continue
        try:
            tickets.add(int(item))
        except (TypeError, ValueError):
            continue
    return tickets

# 两处 _ticket_was_cancelled 都改用该 helper
# orphan 路径加 try/except 兜底（fail-soft 防 broker 未来返其他形状）
try:
    cancelled = self._ticket_was_cancelled(result, ticket)
except Exception as exc:
    logger.warning(...)
    cancelled = False
```

#### #3 SQL + cockpit 双层修复

**SQL** `aggregate_open_positions_by_account_symbol` /
`aggregate_pending_orders_by_account_symbol` 都加 `MAX(updated_at) AS
latest_updated_at` 列。

**cockpit** `_build_exposure_map` 取 max(exposure_rows + pending_rows 的
`latest_updated_at` / `data_updated_at` / `updated_at`) 作为真实
`data_updated_at`；helper `_max_iso_timestamp` 兼容 datetime 与 ISO 字符串
混合。无 row 时 fallback 到 observed_at（旧行为，不破坏 mock）。

### 测试（4 项新契约，对应分支零覆盖）

| 测试 | 锁定 |
|---|---|
| `test_expired_pending_with_standard_failed_payload_marks_restored_correctly` | 标准 failed=[{ticket}] 不再 TypeError；restored 计数稳定 |
| `test_expired_pending_correctly_marked_when_cancel_succeeds_standard_schema` | cancel 全成功 → expired += 1 |
| `test_orphan_with_standard_failed_payload_does_not_crash_startup` | orphan 路径不再 TypeError 打挂 startup |
| `test_exposure_map_freshness_reflects_underlying_row_updated_at` | 6h 陈旧 row → freshness != fresh + data_updated_at != observed_at |

### 元层教训：**纯解析路径必须 fail-soft**

§0r 提出"readmodel 已声明 vs 消费侧未识别"模式，§0s 是其分支：**当解析路径
有跨服务/跨模块 schema 假设时，必须 (a) 显式 normalize helper 接受多种 schema
变种 (b) 关键路径加 try/except fail-soft 兜底**。

仅靠"假设单一 schema + 外层 except 吞"会让 startup recovery / health probe /
cockpit 等关键路径在 broker 升级或 schema 演进时静默挂掉/误报。

强建议（扩充 §0n / §0p / §0q / §0r 元层教训）：
- 所有 cancel/fill/transaction 类响应解析必走 `_normalize_*` helper（接受多
  schema variant 并归一化）
- startup recovery 的每一步都必须 fail-soft（per-row try/except 已在 §0r 落地）
- API readmodel 输出"data_updated_at"类时间字段时，必须从底层 row 取真实值
  而非聚合时刻（参 §0s 修复 + 同模式建议给所有 readmodel 块审核）

### §0g→§0s 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **30 个 bug + 30 次反向锁**：

| § | bug 数 |
|---|---|
| 0g–0r | 27 |
| 0s | 3 |

### 测试基线

- 修复前：3 处 user-facing bug；helper / cockpit freshness / orphan startup 路径零覆盖
- 修复后：4 项新契约测试；`pytest -m "not slow"` → **2827 passed / 0 failed**

### 减少边界泄漏的方式

`_ticket_was_cancelled` 跨域 helper 复用一个 `_normalize_ticket_set`
（recovery + recovery_policy 共享）；cockpit 的 freshness 真实反映底层数据
而非聚合时刻；orphan startup 路径 fail-soft 不再被未知 schema 打挂。

---

## 0r. 2026-04-26 delegated 拓扑误判 + 启动恢复脆弱 + ADR-005 calibrator 违反（P1 + P2 ×3）

### 触发

User 报告 4 处独立 bug，全是 **readmodel/runtime 已声明的语义/契约被消费侧
未识别**：

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | **P1** | `trading/admission/service.py:226-243` | delegated main role 被 admission 误判为运行时故障 → block |
| 2 | P2 | `signals/orchestration/runtime_recovery.py:25-41` | 单条坏 generated_at 让整个 startup restore 被 ValueError 打挂 |
| 3 | P2 | `readmodels/workbench.py:192-200` | delegated executor 拓扑被误报成 hybrid/fallback |
| 4 | P2 | `signals/evaluation/calibrator.py:stop_background_refresh` | join(timeout) 后无条件 `_bg_thread = None`，违反 ADR-005 |

### 根因

#### #1 admission 不识别 delegated verdict

`tradability_state_summary()` 已为 multi_account main role 输出
`verdict='not_applicable'` / `reason_code='not_executor_role'`，明示"本实例
不直接执行交易"——这是受支持的 delegated 拓扑。但 admission 仅看
`runtime_present=False` 和 `admission_enabled=False` 追加 `runtime_absent` +
`admission_disabled` → `decision=block`。

后果：受支持的 delegated 拓扑在 admission 层被当成运行时故障。

#### #2 runtime_recovery 缺 per-row 隔离

```python
# 旧实现：循环无 try/except 隔离
for row in rows:
    ...
    generated_at = runtime._parse_event_time(generated_at_raw)  # ← ValueError 直接冒出
    bar_time = runtime._parse_event_time(bar_time_raw)
```

库里混入一条 `generated_at='bad-date'` → `ValueError` → 整个 for 循环中断 →
`prepare_startup()` 无保护调 `restore_state()` → 启动恢复阶段被打断。

#### #3 workbench `runtime_present=False` 一律标 fallback

```python
# 旧实现仅二分 native vs fallback
runtime_present = bool(tradability.get("runtime_present", True))
return {
    "source_kind": "native" if runtime_present else "fallback",
    ...
}
```

但 multi_account main role 不挂 executor 是合法拓扑（`execution_scope='remote_executor'`）。
旧二分法 → 顶层 `source.kind='hybrid'` → 正常拓扑被误报降级。

#### #4 ConfidenceCalibrator 违反 ADR-005

```python
# 旧实现
def stop_background_refresh(self) -> None:
    self._bg_stop.set()
    if self._bg_thread is not None:
        self._bg_thread.join(timeout=10.0)
        self._bg_thread = None       # ← 违反 ADR-005：join 超时仍清引用
```

`start_background_refresh` 防重逻辑只看 `_bg_thread is not None and is_alive()`：
若 stop 后线程仍 alive 但引用已清 → 后续 start 错误地启动第二条刷新线程 →
**双线程消费 / 重复缓存写入** （ADR-005 明确禁止的反模式）。

### 修复（commit `d76a00e`）

#### #1 admission 识别 delegated role

```python
is_delegated_role = (
    str(tradability.get("verdict") or "").lower() == "not_applicable"
    and str(tradability.get("reason_code") or "").lower() == "not_executor_role"
)
if not is_delegated_role and not bool(tradability.get("runtime_present", False)):
    reasons.append(_build_reason(code="runtime_absent", ...))
if not is_delegated_role and not bool(tradability.get("admission_enabled", True)):
    reasons.append(_build_reason(code="admission_disabled", ...))
```

#### #2 runtime_recovery per-row try/except

```python
for row in rows:
    try:
        ...
        generated_at = runtime._parse_event_time(generated_at_raw)
        bar_time = runtime._parse_event_time(bar_time_raw)
        ...
    except (ValueError, TypeError, KeyError) as exc:
        skipped_bad_rows += 1
        logger.warning("restore_state: skipping bad row (%s): %s; row=%r",
                       type(exc).__name__, exc, row)
        continue
if skipped_bad_rows:
    logger.warning("restore_state: skipped %d bad rows during signal runtime restore",
                   skipped_bad_rows)
```

#### #3 workbench source_kind 三态

```python
runtime_present = bool(tradability.get("runtime_present", True))
is_delegated_role = (...)  # 同 admission

if runtime_present:
    source_kind = "native"
elif is_delegated_role:
    source_kind = "delegated"   # ← 新增第三种状态
else:
    source_kind = "fallback"

# compute_source_kind 不把 "delegated" 算 fallback（与 native 混合 → kind=live）
```

#### #4 ConfidenceCalibrator 遵守 ADR-005

```python
def stop_background_refresh(self) -> None:
    self._bg_stop.set()
    if self._bg_thread is not None:
        self._bg_thread.join(timeout=10.0)
        if self._bg_thread.is_alive():
            logger.warning(
                "ConfidenceCalibrator: bg refresh thread still alive after"
                " 10s join timeout; preserving reference per ADR-005"
            )
        else:
            self._bg_thread = None
```

### 测试（6 项新契约，对应分支之前零覆盖）

| 测试 | 锁定 |
|---|---|
| `test_delegated_main_role_not_blocked_by_admission` | not_applicable verdict 不触发 runtime_absent / admission_disabled |
| `test_restore_state_isolates_per_row_bad_time_string` | bad-date 不再抛 |
| `test_restore_state_continues_after_single_bad_row` | 后续好行仍能 restore |
| `test_workbench_delegated_main_role_not_classified_as_fallback` | source_kind ≠ fallback / hybrid |
| `test_stop_background_refresh_preserves_alive_thread_per_adr_005` | 仍 alive → 引用保留（ADR-005 守护） |
| `test_stop_background_refresh_clears_reference_when_thread_exits` | 已退出 → 引用清空（让 start 能重启） |

### 元层教训：**readmodel 已声明 vs 消费侧未识别 = 同模式累积**

§0g→§0r 累计修过的 24 个 bug 中，**至少 8 处**是 readmodel 已经把语义声明清楚
但消费侧仍按旧规则解析的反模式：

- §0g: signal evaluator 不读 calendar 域 EconomicDecayService 端口（已建）
- §0l: health_check 用旧 trade/overview 路径 + 旧 schema
- §0n: daily_report executor flat schema vs 实际 nested signals.* / 顶层 circuit_open
- §0n: diagnose_no_trades 不用 /v1/signals/recent 的 timeframe / from query
- §0o: confidence_check 不用 deployment.allows_live_execution
- §0p: confidence_check 不用 deployment.effective_min_confidence
- §0q: admin schemas 裁掉 readmodel 已声明的字段
- **§0r**: admission / workbench 不识别 readmodel 已标的 not_applicable verdict

强建议（扩充 §0n / §0p / §0q 元层教训为 4 条统一原则）：

1. **新增/扩展 readmodel 字段语义时，必加"消费侧 grep 检查"**：
   `grep -rn "<new_field>\|<verdict_value>" src/ --include='*.py'` 出消费方清单，
   逐个评估是否需同步识别（同 §0o 强建议 a 类似的 PR 流程）。
2. **API/CLI/admin 等消费侧改 logic 时，必同步加"语义识别"测试**：
   不仅锁 URL 路径或字段名，还要锁"业务结论与 readmodel 一致性"（§0p 已开此模式）。
3. **生命周期类约束（线程 stop/start、cache TTL）必须 ADR**：
   §0r 是 ADR-005 落实的第二例（前为 §0g 的 service join 顺序），
   说明现有 ADR 守护是有效的——但需要在 review 时主动检查。
4. **CLI/API/readmodel 之间画"语义流"图**：明确每个层"声明什么 / 识别什么 /
   决策什么"，让 reviewer 能一眼看出哪处漏识别（建议进 docs/architecture.md）。

### §0g→§0r 累计基线

24+ 小时累计修 P0/P1/P2/P3 共 **27 个 bug + 27 次反向锁**。

### 测试基线

- 修复前：4 处 user-facing bug；对应分支零测试覆盖
- 修复后：6 项新契约测试；`pytest -m "not slow"` → **2823 passed / 0 failed**

### 减少边界泄漏的方式

admission/workbench 都识别 readmodel 已标的 `verdict='not_applicable'` 语义；
runtime_recovery 单条坏数据不再让整个启动恢复挂掉；ConfidenceCalibrator 严格
遵守 ADR-005 后台线程契约。**消费侧首次开始与 readmodel 已声明的语义对齐**。

---

## 0q. 2026-04-26 admin schemas 裁掉 readmodel 核心运行态字段（P2 ×2 + P3 ×2）

### 触发

User 用 TestClient mock readmodel 后请求 /v1/admin/dashboard / /v1/admin/strategies
直接复现 4 个 schema 字段被静默裁的 bug，全是 Pydantic 默认 `extra='ignore'`
+ schema 字段集 < readmodel 真实字段集 的**同模式**：

| # | 等级 | 文件 | 被裁字段 | 影响 |
|---|---|---|---|---|
| 1 | **P2** | `admin_schemas.py:33-42` `DashboardOverview` | trading_state / account_risk / validation / external_dependencies | admin dashboard 永远暴露不出交易态/风险态/MT5 依赖 |
| 2 | **P2** | `admin_schemas.py:22-30` `ExecutorSnapshot` | status / running / last_error / signals / execution_gate / pending_entries / recent_executions / execution_scope 等 ~13 字段 | 执行器从"诊断状态机"降成"简化计数器"，前端无法判断 disabled/delegated/critical/blocked-by-gate |
| 3 | P3 | `admin_schemas.py:68-75` `StrategyDetail` | htf_policy | /v1/admin/strategies 列表无完整策略契约 |
| 4 | P3 | `admin_routes/view_models.py:42` `StrategySessionDetailView` | htf_policy（与 #3 双重裁剪） | /v1/admin/strategies/{name} 详情比 confidence_check 更不完整 |

### 根因（共同模式）

```python
# Pydantic 默认 extra='ignore'：未声明字段被静默丢弃
DashboardOverview(**dashboard_overview_dict)  # 11 keys → 7 (default 4 fields lost)
ExecutorSnapshot(**trade_executor_summary())  # 16 keys → 6
StrategyDetail(**describe_strategy())          # ... → 5 (htf_policy lost)
```

readmodel 演进时（如 §0g ADR-011 加 `account_risk`，§0n trade_executor_summary
schema 调整加 `signals.*`），**schema 没同步更新** → 静默漂移。

无 OpenAPI 验证、无 round-trip test、无 readmodel→schema 字段对齐 CI 检查 →
bug 长期不可见。

### 修复（commit `e9cb6fa`）

**核心策略**：补缺失字段（让 OpenAPI 仍有类型提示）+ `ConfigDict(extra='allow')`
（让 readmodel 后续添加新字段时自动透传，避免再次漂移）。

```python
# admin_schemas.py
class DashboardOverview(BaseModel):
    model_config = ConfigDict(extra="allow")
    system: SystemStatusSnapshot
    account: Dict[str, Any] = Field(default_factory=dict)
    positions: Dict[str, Any] = Field(default_factory=dict)
    trading_state: Dict[str, Any] = Field(default_factory=dict)        # 补
    account_risk: Dict[str, Any] = Field(default_factory=dict)         # 补
    signals: Dict[str, Any] = Field(default_factory=dict)
    executor: ExecutorSnapshot
    validation: Dict[str, Any] = Field(default_factory=dict)            # 补
    external_dependencies: Dict[str, Any] = Field(default_factory=dict) # 补
    storage: Dict[str, Any] = Field(default_factory=dict)
    indicators: Dict[str, Any] = Field(default_factory=dict)

class ExecutorSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    # 状态机：status/configured/armed/running/enabled
    # 熔断+计数：circuit_open/consecutive_failures/execution_count
    # 时间线+错误：last_execution_at/last_error/last_risk_block
    # 诊断：signals/execution_gate/execution_quality/config
    # 待挂单+最近执行：pending_entries_count/pending_entries/recent_executions/execution_scope
    # （共 17 字段，对齐 readmodel.trade_executor_summary 当前真实结构）

class StrategyDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    # ... + htf_policy: Optional[str] = None

# admin_routes/view_models.py
class StrategySessionDetailView(BaseModel):
    model_config = {"extra": "allow"}
    # ... + htf_policy: Optional[str] = None
```

### 测试（5 项新契约，schema preservation 之前 100% 零覆盖）

| 测试 | 锁定 |
|---|---|
| `test_preserves_real_runtime_payload_blocks` | 4 个 dashboard 顶层区块不再被裁 |
| `test_preserves_critical_runtime_diagnostic_fields` | ExecutorSnapshot 17 字段全部保留 |
| `test_preserves_htf_policy_field` | StrategyDetail.htf_policy 必须保留 |
| `test_preserves_htf_policy` (Session view) | StrategySessionDetailView.htf_policy 双重裁剪修复 |
| `test_roundtrip` 扩展 | StrategyDetail 含 htf_policy 的 round-trip 不丢字段 |

### 元层教训：**Pydantic 默认 ignore + schema 字段漂移 = 静默裁剪批量制造机**

这是与 §0n CLI Stale-schema 同根的一类 bug，但发生在 API 层：
- §0n: CLI 读 API schema 与 readmodel 漂移（消费侧问题）
- §0q: API schema 与 readmodel 漂移（生产侧问题）

两者都因为 **字符串/字段名是事实源，但没有自动校验机制**。强建议（扩充
§0n 元层教训）：

a. **所有 API response_model 默认 `extra='allow'`**（除非有明确"不允许额外字段"
   的安全理由）。Pydantic v2 文档级建议：API 输出 schema 应保持向前兼容，
   `extra='ignore'` 在数据生产侧是反模式。
b. **加 readmodel ↔ schema 字段对齐 test**：每个 readmodel 函数的返回 dict
   keys 必须是其对应 schema 的子集（防止生产侧字段被 schema 裁）。本次 4 个
   测试是该模式的开端。
c. **OpenAPI 文档 review 加进 PR 流程**：schema 字段变化必须在 PR description
   附 `/openapi.json` diff 截图，让 reviewer 看见字段集变化。

### §0g→§0q 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **23 个 bug + 23 次反向锁**：

| § | bug 数 | 性质 |
|---|---|---|
| 0g–0p | 19 | 见前各节 |
| 0q | 4 | API schema 反复裁剪 readmodel |

### 测试基线

- 修复前：4 处 user-facing 字段消失；schema preservation 零测试覆盖
- 修复后：5 项新契约测试；`pytest -m "not slow"` → **2817 passed / 0 failed**

### 减少边界泄漏的方式

API schema 与 readmodel 之间从"字段集严格相等"漂移为"schema ⊇ readmodel"
（通过 `extra='allow'` 透传 + 显式声明已知字段保留 OpenAPI 类型提示）。
未来 readmodel 添加新字段（如 P9/P10 readmodel 演进）不再要求同步改 schema
就能透传给前端，但同时仍有 5 项 round-trip 测试守住关键字段不被回退。

---

## 0p. 2026-04-26 confidence_check 三处深层语义错 + live_preflight 硬编码 symbol（P2 ×3 + P3）

### 触发

User §0o 修复后再深入 audit 发现 4 处独立问题，全是已修过的两个 CLI 内部更深层的
语义错误（"修对了表面，但内部逻辑仍假阳/假阴"）：

| # | 等级 | 文件 | 问题 |
|---|---|---|---|
| 1 | **P2** | `confidence_check.py:142-150` | `all([])=True` → 无 live 策略时误报 "ALL TFs BLOCKED" + 调阈值建议 |
| 2 | **P2** | `confidence_check.py:98-123` | 不叠加 `deployment.effective_min_confidence()` → ACTIVE_GUARDED + min_final 假 PASS |
| 3 | **P2** | `confidence_check.py:60-64` | `regime_map` 压平 symbol→TF 后写覆盖；多品种部署诊断结果不确定 |
| 4 | P3 | `live_preflight.py:168-182` | `mt5.symbol_info("XAUUSD")` 硬编码；改 symbol/多品种会误报 |

### 根因 #1 — `all(空生成器) → True` 经典陷阱

```python
# 旧实现
all_blocked = all(
    all(... for name in _live_candidates(tf))
    for tf in check_tfs
    if _live_candidates(tf)        # ← 过滤掉空候选 TF
)
# 当全部 TF 候选都空，外层 generator 完全无产出 → all([]) → True
```

逻辑误读："没有 TF 满足"被解释成"所有 TF 都满足条件 (BLOCKED)"。
真因是无 live-eligible 策略，建议方向应为推 ACTIVE_GUARDED 而非调 min_confidence。

### 根因 #2 — 工具门槛与执行链路漂移

`pre_trade_checks.py` 真实门槛：
```python
effective_min_conf = max(
    timeframe_baseline_min,
    deployment.effective_min_confidence(...)  # min_final_confidence / ACTIVE_GUARDED 推高
)
```

工具旧实现仅用 `timeframe_baseline_min`。ACTIVE_GUARDED + `min_final_confidence=0.8`
的策略 → 工具显示 PASS（baseline=0.5）但执行器实际 reject（effective=0.8）。
**与 §0n 同模式**：CLI 读旧 schema/逻辑，与执行链路真实门槛漂移。

### 根因 #3 — dict key 维度坍缩

```python
# /v1/admin/dashboard 返 regime_map = {"XAUUSD/M30": ..., "EURUSD/M30": ...}
for key, info in regime_map.items():
    tf_part = key.split("/")[-1]                # 丢掉 symbol
    regimes[tf_part] = info["current_regime"]   # 后写覆盖前者
```

多品种部署里同一 TF 不同 symbol 的 regime 互相覆盖；诊断结果取决于 dict 遍历顺序。

### 根因 #4 — SSOT 偏离

仓库 SSOT 是 `app.ini trading.symbols / default_symbol`（`get_trading_config()` /
`get_shared_default_symbol()`），但 live_preflight 硬编码 `mt5.symbol_info("XAUUSD")`。
当前默认 XAUUSD 没炸；改名/多品种实例会把"目标 symbol 不可见"误报为
"XAUUSD 不可见"。

### 修复（commit `6984d76`）

**P2#1** — 区分两种空集状态：
```python
has_any_live_candidate = any(live_candidates_by_tf.get(tf) for tf in check_tfs)

if not has_any_live_candidate:
    print("*** NO LIVE-ELIGIBLE STRATEGIES — all current strategies are "
          "CANDIDATE / DEMO_VALIDATION / PAPER_ONLY ***")
    print("  1. Promote a strategy to ACTIVE / ACTIVE_GUARDED ...")
    print("  3. 调 min_confidence 不会有用 — 没策略能 live 执行")
    return
# 然后再做 all_blocked 判断（此时至少有一个 TF 有 live 候选）
```

**P2#2** — 内联 helper 与 pre_trade_checks 同语义：
```python
def _effective_min_for(name: str, tf: str) -> float:
    baseline = float(tf_min_conf.get(tf, min_conf_global))
    deployment = deployments.get(name) if deployments else None
    if deployment is None:
        return baseline
    eff = deployment.effective_min_confidence(
        timeframe_baseline=baseline,
        global_min_confidence=float(min_conf_global),
    )
    return baseline if eff is None else max(baseline, float(eff))
```
渲染时 `min={effective:.2f}*` 标记 deployment 推高，便于诊断。

**P2#3** — 加 `--symbol` 参数 + `regime_map` 按 symbol 过滤：
```python
target_symbol = (args.symbol or get_shared_default_symbol()).strip().upper()
for key, info in regime_map.items():
    if "/" in key:
        sym_part, tf_part = key.split("/", 1)
        if sym_part.strip().upper() != target_symbol:
            continue   # ← 过滤掉非目标 symbol
    ...
    regimes[tf_part.strip().upper()] = info.get("current_regime", "uncertain")
```

**P3** — live_preflight 读 trading config：
```python
try:
    from src.config.centralized import get_trading_config
    trading_cfg = get_trading_config()
    target_symbols = list(trading_cfg.symbols) or [trading_cfg.default_symbol]
except Exception:
    target_symbols = ["XAUUSD"]   # 兜底向后兼容

for symbol_name in target_symbols:
    symbol_info = mt5.symbol_info(symbol_name)
    ...
```

### 测试（4 项新契约，之前对应分支 100% 零覆盖）

| 测试 | 锁定 |
|---|---|
| `test_no_live_eligible_strategies_does_not_misreport_all_blocked` | NO LIVE-ELIGIBLE 优先于 ALL BLOCKED |
| `test_effective_min_confidence_threaded_for_guarded_strategy` | ACTIVE_GUARDED + min_final=0.8 必须 FAIL |
| `test_regime_map_isolates_by_symbol_via_explicit_arg` | --symbol XAUUSD 只取 XAUUSD/M30，不被 EURUSD/M30 覆盖 |
| `test_live_preflight_symbol_probe_not_hardcoded_xauusd` | sentinel 锁 mt5.symbol_info 不再硬编码 + 必须读 trading config |

### User 实际复现验证

```bash
$ python -m src.ops.cli.confidence_check --tf M30
Settings: raw_confidence=0.65, perf=1.0, calibrator=1.0
Per-TF min_confidence: {}

*** NO LIVE-ELIGIBLE STRATEGIES — all current strategies are
    CANDIDATE / DEMO_VALIDATION / PAPER_ONLY ***
Suggestions:
  1. Promote a strategy to ACTIVE / ACTIVE_GUARDED ...
  2. Verify strategy_deployments map covers strategies in scope
  3. 调 min_confidence 不会有用 — 没策略能 live 执行
```
（之前同命令误报 "ALL TFs BLOCKED" + "Lower per-TF min_confidence"）

### 元层教训：**修了表面 ≠ 修对内部**

§0o 修了 confidence_check 的 sys.path / default TF / deployment filter，但工具
内部的 `all_blocked` 误报、`effective_min` 漂移、`regime_map` symbol 坍缩 三处
深层 bug 仍存在。User 在 §0o 修复后立刻深入 audit 才发现这些。

启示：
1. **修一个 CLI bug 后应做"内部端到端 audit"**：模拟典型用户输入跑一遍，
   断言每段输出对当前真实配置都准确（而不仅仅是 grep 字符串通过）
2. **CLI 工具的"诊断准确性"应被独立 test**：旧 sentinel 测试只锁 URL 路径
   或字段名，不锁"语义真值"。新增 4 项测试是首次锁住 confidence_check 的
   诊断结论与执行链路一致性
3. **dict key 维度多 + dict.get 默认值** 是 Python 易踩雷区——需 review 时
   主动问"如果 key 集合是空的，会发生什么"

### §0g→§0p 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **19 个 bug + 19 次反向锁**：

| § | bug 数 | 性质 |
|---|---|---|
| 0g–0o | 15 | 见前各节 |
| 0p | 4 | confidence_check 3 深层 + live_preflight symbol |

### 测试基线

- 修复前：4 处 user-facing 假阳/假阴；零测试覆盖
- 修复后：4 项新契约测试；`pytest -m "not slow"` → **2813 passed / 0 failed**

### 减少边界泄漏的方式

confidence_check 与 pre_trade_checks 真实执行门槛对齐（`effective_min_confidence`）；
诊断输出区分"无 live 策略"vs"门槛阻拦"两种本质不同问题；多品种部署有显式
`--symbol` 入口避免 regime 解析歧义；live_preflight symbol probe 接通 SSOT
（trading.symbols）支持任意配置目标。

---

## 0o. 2026-04-26 sys.path 阴影 stdlib + 候选 deployment 不收口 + 已删字段误读（P1 ×2 + P2 ×3）

### 触发

User 报告 5 个独立 bug：

| # | 等级 | 文件 | 触发命令 |
|---|---|---|---|
| 1 | **P1** | `confidence_check.py:14` | `python -m src.ops.cli.confidence_check --tf M30` 直接抛 ImportError |
| 2 | **P1** | `diagnose_no_trades.py:18` | `python -m src.ops.cli.diagnose_no_trades --tf M15` 同源 |
| 3 | P2 | `confidence_check.py:50-72` | 默认路径下脚本只打 settings 不输出任何 TF 段 |
| 4 | P2 | `confidence_check.py:71-72` | "可通过"集合含 CANDIDATE/DEMO_VALIDATION 策略 → live 假阳性 |
| 5 | P2 | `live_preflight.py:223` | `Config: min_confidence live=0.55` 是伪事实 |

### 根因 #1+#2（最严重）— sys.path 阴影 stdlib calendar

`src/calendar/` 包名与 stdlib `calendar` 冲突是 latent bug 长期存在。
`sys.path.insert(0, repo_root)` 把 src 提到 stdlib **之前**激活该冲突：
- `import requests` → `requests.compat` → `http.cookiejar`
- → `from calendar import timegm` 命中 `src/calendar/__init__.py`（lazy lookup 无 `timegm`）
- → `ImportError: cannot import name 'timegm' from 'calendar'`

只有在 module-top `import requests` 的两个 CLI（`confidence_check` /
`diagnose_no_trades`）触发；其余 15 个 CLI 都有同样 latent 模式但 lazy import 避开了。

### 根因 #3 — strategy_timeframes 维度用错

`strategy_timeframes: dict[str, list[str]]` 的 **keys** 是策略名，**values** 是 TF 列表。
旧代码 `tfs = sorted(stf.keys(), ...)` 把策略名当 TF 用 → 后续 `if tf in tfs_list`
匹配永远空 → 默认路径下脚本只打 settings 然后退出。

### 根因 #4 — 候选不收口 deployment.allows_live_execution()

候选集合只从 `strategy_timeframes` 推导，未过滤 deployment。但
`StrategyDeployment.allows_live_execution()` 只允许 `ACTIVE` / `ACTIVE_GUARDED`，
`CANDIDATE` / `DEMO_VALIDATION` / `PAPER_ONLY` 在 live 链路（pre-trade
filter）会被直接拒绝。当前配置下 user 实测 M30 候选 7 个全是 candidate /
demo_validation，allows_live_execution() 全为 False — 但工具显示 "有策略可通过"。

### 根因 #5 — 已删字段 getattr 默认占位

`SignalConfig.min_preview_confidence` 字段早已删除（hasattr=False）。
旧 `getattr(signal_cfg, 'min_preview_confidence', 0.55)` 永远走 default 0.55。
真实 live 阈值是 `auto_trade_min_confidence` (实测 0.35)。
preflight "实盘 vs 回测差异表"成为伪事实。

### 修复（commit `8cbfc6c`）

**P1 (×2 + 同源 hardening 15 个 CLI)**:
```python
# 17 个 src/ops/cli/*.py 同步：
- sys.path.insert(0, repo_root)
+ if repo_root not in sys.path:
+     sys.path.append(repo_root)
```
让 stdlib 在 sys.path 优先位置（`-m` 模式下 cwd 由 Python 自动加），
src/* 仍可 import 但不再阴影 stdlib。

**P2#3** confidence_check 默认 TF：
```python
- tfs = sorted(stf.keys(), ...)                        # 策略名（错）
+ derived = {t.strip().upper() for tfs_list in stf.values() for t in tfs_list}
+ tfs = sorted(derived, key=lambda x: _TF_ORDER.index(x) if x in _TF_ORDER else 99)
```

**P2#4** confidence_check deployment 收口：
```python
deployments = getattr(cfg, "strategy_deployments", {}) or {}

def _strategy_allowed_live(name: str) -> bool:
    if not deployments:
        return True   # 配置未启用 deployment 合同 → fallback 不过滤
    deployment = deployments.get(name)
    if deployment is None:
        return False  # 启用但策略未声明 → 保守拒绝（避免假阳性）
    return bool(deployment.allows_live_execution())

allowed = [name for name, tfs_list in stf.items()
           if tf in [t.strip().upper() for t in tfs_list]
           and _strategy_allowed_live(name)]
```

**P2#5** live_preflight：
```python
- ("min_confidence", getattr(signal_cfg, "min_preview_confidence", 0.55), ...)
+ ("min_confidence", getattr(signal_cfg, "auto_trade_min_confidence", 0.55), ...)
```

### 测试（之前 100% 零覆盖；与 §0g→§0n 同模式）

| 文件 | 项数 | 锁定的契约 |
|---|---|---|
| `tests/ops/test_confidence_check.py`（新建） | 5 | subprocess `--help` 不抛 ImportError（P1 真复现）+ 2 sentinel 锁 sys.path.insert(0 + default TF 推导从 values + deployment 过滤 CANDIDATE/DEMO_VALIDATION |
| `tests/ops/test_live_preflight.py` | +1 | sentinel 锁 min_preview_confidence 不再出现 + auto_trade_min_confidence 必须出现 |

### 元层教训：包名冲突 + sys.path 顺序 = 隐藏巨坑

`src/calendar/` 与 stdlib `calendar` 撞名是**根因之根因**。`sys.path.insert(0,...)`
只是激活机关，真正的设计错误是 calendar 包没起一个独特名字。**短期**已通过
sys.path.append 缓解；**长期**应考虑：

a. **重命名 `src/calendar` → `src/economic_calendar` 或 `src/calendar_service`** ——
   彻底消除 stdlib 撞名；影响 100+ import 但一次性永久修。优先级：中。
b. **加 CI 烟测**：`python -c "import src.ops.cli.confidence_check"` 等 17 个 CLI
   全部能加载 → 任何 sys.path 回退或 import 链漂移立即抓到。
c. **CLAUDE.md "代码规范"段加规则**：禁止 `sys.path.insert(0, ...)`；项目根目录
   名（`src/`）不得与 stdlib 模块同名。

### §0g→§0o 累计基线

短短 24+ 小时累计修 P0/P1/P2/P3 共 **15 个 bug + 15 次反向锁**：

| § | bug 性质 |
|---|---|
| 0g | P0 timezone NameError + 复制粘贴漂移 |
| 0h | P2 frozen-mutate + 静默吞 |
| 0i | P2 pyproject 与代码现实不符 |
| 0j | P3 时区一致性 |
| 0k | P3 CLI dead route + 解析无信息量 |
| 0l | P2+P3 三处 CLI 死路径/死 helper |
| 0m | P1 探针漂移 |
| 0n | P2 ×3 CLI 参数/Schema 漂移 |
| 0o | **P1 ×2 + P2 ×3** sys.path 阴影 + deployment 不收口 + 已删字段 |

### 测试基线

- 修复前：5 处 user-facing bug（2 个 P1 直接崩 + 3 个 P2 静默错）；零测试覆盖
- 修复后：7 项新契约测试 + 17 个 CLI sys.path 模式统一；
  `pytest -m "not slow"` → **2809 passed / 0 failed**

### 减少边界泄漏的方式

CLI sys.path 模式从"insert(0)"统一到"append"，不再阴影 stdlib；
confidence_check 候选集合首次与 deployment 合同收口，"可通过"结论与 live
执行链路一致；live_preflight 配置差异表读真实 live 阈值，与 auto_trade
执行链路对齐。

---

## 0n. 2026-04-26 三处 CLI 参数/Schema 漂移修复（daily_report ×2 + diagnose ×1，P2）

### 触发

User 连续报告 3 个独立 P2 + 1 个同源残余。**全部是同一类问题**：CLI 工具与
后端 API/schema 漂移，但因 CLI 长期无 test 覆盖 + 输出看起来"格式正确"，
未引发崩溃 → 用户得到貌似正常但语义失真的报告/诊断。

| # | 文件 | 表面症状 | 真实根因 |
|---|---|---|---|
| 1 | `daily_report.py:137-145` | --date 只改标题不改数据 | 路由 `/v1/trade/daily_summary` 不接 date 参数；CLI 也未拼 query |
| 2 | `daily_report.py:72-91` | 接收/通过/拒绝永远 0；熔断告警永远不显示 | 旧 schema：flat `signals_received` + nested `circuit_breaker.{open}`；新 schema：nested `signals.{received,passed,blocked,skip_reasons}` + 顶层 `circuit_open`/`consecutive_failures` |
| 3 | `diagnose_no_trades.py:136-138` | 标题"最近 N 小时 / TF"但内容混入其他 TF 与更早记录 | `--tf`/`--hours` 完全没拼进 `/v1/signals/recent` query |
| + | `health_check.py:198`（同源 carry-on） | `circuit_open=True` 时仍报 OK enabled | 同 #2 schema 误读 + 漏判 circuit_open |

### 影响

| # | 长期实际危害 |
|---|---|
| 1 | 用户生成"标题写历史日期、内容仍是今天"的假日报；事后追溯/合规审计严重偏差 |
| 2 | 关键交易统计（信号接收/通过/拒绝、熔断状态）完全无显示；运维盯日报以为系统空闲实际是探针错误 |
| 3 | 诊断"为什么没下单"时 DB SIGNAL EVENTS 段汇总跨 TF 与陈旧记录，可能把"M15 最近 24h 异常"判读为"系统正常"；反向也可能把更早的失败误判为当下问题 |
| + | health_check 显示 "Trade executor: OK enabled" 但熔断器实际打开 → 同 §0m P1 假阳性 |

### 修复（commit `cf5221b`）

**P2 #1（双层修）**：
```python
# 路由层 src/api/trade_routes/state_routes/overview.py
@router.get("/trade/daily_summary", ...)
def trade_daily_summary(
    summary_date: Optional[date] = Query(default=None, alias="date"),
    service: TradingQueryService = Depends(...),
):
    return ApiResponse.success_response(
        data=service.daily_trade_summary(summary_date=summary_date),
        metadata={..., "summary_date": summary_date.isoformat() if summary_date else None},
    )

# CLI src/ops/cli/daily_report.py
summary_url = f"{base}/v1/trade/daily_summary"
if args.date:
    summary_url = f"{summary_url}?date={args.date}"
```
（`TradingQueryService.daily_trade_summary(summary_date=None)` 早就支持，
路由层之前没暴露这个 capability。）

**P2 #2**：`_render_daily_report` 全面重写按新 schema：
```python
- received = executor.get("signals_received", 0)            # 旧 flat
+ signals = executor.get("signals", {}) or {}
+ received = signals.get("received", 0)
- cb = executor.get("circuit_breaker", {})
- if cb.get("open"): ...
+ if executor.get("circuit_open"):
+     consecutive = executor.get("consecutive_failures", "?")
+     max_failures = executor.get("config", {}).get("max_consecutive_failures", "?")
```

**P2 #3**：用 `requests.get(url, params=dict)` 拼 timeframe + from（alias）：
```python
params: dict[str, str] = {"symbol": "XAUUSD", "limit": "50"}
if args.tf:
    params["timeframe"] = args.tf
if args.hours:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    params["from"] = cutoff
r = requests.get(f"{BASE}/v1/signals/recent", params=params, timeout=5)
```

**附带 carry-on（health_check.py:192-218）**：
```python
- if enabled:
-     results.append(("Trade executor", "OK", f"enabled, executions={exec_count}"))
+ if circuit_open:
+     results.append(("Trade executor", "FAIL",
+                    f"circuit OPEN ({consecutive} consecutive failures)"))
+ elif enabled:
+     results.append(("Trade executor", "OK", f"enabled, executions={exec_count}"))
```
+ except 收窄到 `(HTTPError, URLError, ValueError)` 同 §0m 异常分层契约。

### 测试（之前 daily_report 100% 零覆盖；diagnose 仅 source-level sentinel）

| 文件 | 项数 | 锁定的契约 |
|---|---|---|
| `tests/ops/test_daily_report.py`（新建） | 6 | 渲染 signals.* / skip_reasons / circuit_open / circuit_closed-no-section + main --date 透传 + 缺省不附 query |
| `tests/ops/test_diagnose_no_trades.py` | +1 | source sentinel 锁 timeframe/from/to/args.tf/args.hours 透传 |
| `tests/ops/test_health_check.py` | +1 | circuit_open=True 即使 enabled=True 也必须 FAIL |
| `tests/api/test_trade_api.py` | +1 | 路由 summary_date 透传 service + metadata 反映 |

### 元层教训（与 §0g→§0m 累计扩充）

短短 24 小时累计修 P0/P1/P2/P3 共 **10 个 CLI/API 漂移 bug + 10 次反向锁沉淀**。
"CLI 死路径反模式"已扩充为更广义的 **"CLI 工具与后端漂移"模式**，包括：

1. **Dead-route**（§0k live_preflight `/v1/health` / §0l 三处）
2. **Dead-judgment-criterion**（§0m health_check `event_loop_running` 探针漂移）
3. **Stale-schema**（§0n daily_report `signals_received` / `circuit_breaker` 已迁字段）
4. **Dead-CLI-arg**（§0n daily_report `--date` / diagnose `--tf` `--hours`）

强建议（扩充 §0l 已记的 3 条）：
- a/b/c：参 §0l
- d. **Schema 变更（readmodel rename / nest）必须在 PR description 附 grep 截图**
  证明已扫所有 `executor.get("...")` / `signals_*` 之类的字符串引用，含 `src/ops/`
  与 `tests/`
- e. **CLI argparse 加 args 时同时加 sentinel test**：`grep "args.<name>"` 必须
  出现在请求构造代码中（不能只出现在 print 标题里）

### 测试基线

- 修复前：3 个 user-facing 漂移 bug + 1 个同源遗漏；测试覆盖：daily_report 0%
- 修复后：9 项新契约测试；`pytest -m "not slow"` → **2802 passed / 0 failed**

### 减少边界泄漏的方式

CLI 层不再"独自决定"参数语义——`--date` 真正透传给 API；`--tf`/`--hours`
真正影响请求 URL；executor schema 解析对齐 readmodel 真实结构而非历史快照。
**CLI 与 API/readmodel 之间的契约一致性**第一次有 9 项测试守护。

---

## 0m. 2026-04-26 health_check indicator engine 假阳性修复（event_loop_running，P1）

### 触发

User 直接 monkeypatch 复现：让 `/v1/monitoring/performance` 返
`{'data': {'event_loop_running': False}}`，旧 `health_check._run_checks()`
仍输出 `"Indicator engine: OK"`——**indicator 引擎已停摆，健康检查仍报 OK**。

### 根因（双层）

`src/ops/cli/health_check.py:163-166` 旧实现：
```python
perf_data = perf_resp.get("data", {})
if isinstance(perf_data, dict) and perf_data.get("status") != "disabled":
    results.append(("Indicator engine", "OK", "running"))
```

1. **判断条件错** —— `/v1/monitoring/performance` 真实 schema：
   - executor role 返 `{"status": "disabled", "role": "executor"}`（status 字段存在）
   - main role 返 `indicator_manager.get_performance_stats()` 字典（**无 status 字段**，
     但含 `event_loop_running`）
   - 旧 if 永远命中（None ≠ "disabled"），完全跳过 main role 的健康判断

2. **except Exception: pass** —— 任何 HTTP/parse/coding 异常被静默吞，
   indicator engine row 直接消失（用户不会注意到）→ 同 ADR-011 / §0l 反模式

### 影响

P1：**生产假阳性 — 指标引擎挂掉但健康检查全绿**。
- main role 实例的 event loop 因任何原因停止（线程死掉 / OOM / pool 耗尽）
- runtime 仍写入半新半旧指标，下游策略评估读到 stale data
- 健康检查脚本对运维显示一切正常，**长期掩盖真实故障**

同源同模块对照：`monitoring_routes/health.py:159` 的 K8s readiness probe 用的就是
`perf.get("event_loop_running")`——一个文件两段同源代码，CLI 那段写错了。

### 修复（commit `0b9dfd8`）

```python
# 4. 指标引擎
try:
    perf_resp = _fetch_json(f"{base}/v1/monitoring/performance")
    perf_data = perf_resp.get("data", {})
    if not isinstance(perf_data, dict):
        results.append(("Indicator engine", "FAIL",
                       f"unexpected payload type: {type(perf_data).__name__}"))
    elif perf_data.get("status") == "disabled":
        pass  # executor role 主动禁用，不输出 row
    elif perf_data.get("event_loop_running"):
        results.append(("Indicator engine", "OK", "event_loop_running"))
    else:
        results.append(("Indicator engine", "FAIL",
                       f"event_loop_running={perf_data.get('event_loop_running')!r}"))
except (HTTPError, URLError, ValueError) as exc:
    # 异常分层（同 ADR-011）：网络/JSON → FAIL；coding error 透传
    results.append(("Indicator engine", "FAIL", str(exc)))
```

### 测试（5 项契约锁，之前 indicator engine 段 100% 零覆盖）

| 测试 | 锁定 |
|---|---|
| `marked_ok_when_event_loop_running` | True → OK（健康路径） |
| `false_positive_when_event_loop_stopped` | False → FAIL（**user 复现的核心 P1 回归锁**） |
| `fail_on_empty_fallback` | `{}` → FAIL（防 API 内部异常被前端逻辑掩盖） |
| `skipped_for_executor_role` | status="disabled" → 不输出 row（保留合理跳过语义） |
| `fail_on_http_error` | 503 → FAIL（防 `except Exception: pass` 回归静默） |

附带：更新 `tests/ops/test_health_check.py` 的 `_BASE_ROUTES` 默认 perf payload
为 main role 健康场景，避免之前 `{"status": "ok"}` 占位让无关 trade tests 静默
通过 indicator 段。

### 元层教训：同模块两份"健康判断"代码不对齐

`monitoring_routes/health.py:159`（K8s readiness）与 `health_check.py:163-166`（CLI 巡检）
都查同一个 `/v1/monitoring/performance`，**判断字段却不一致**：
- readiness（API 内部）：`perf.get("event_loop_running")` ✓ 正确
- CLI 巡检（外部消费者）：`perf_data.get("status") != "disabled"` ✗ 错误

**这是同一仓库内的"探针漂移"问题**——不应再加新检查点扩大漂移面，应做：

a. 把"判断 indicator engine 是否健康"沉淀为 **calendar 域 / monitoring 域的
   公开函数**（`is_indicator_engine_running(perf_data) -> bool`），CLI + readiness
   + 任何未来消费者都调同一函数；
b. 或至少把 perf endpoint 包成 typed View（`PerformanceStatsView` with explicit
   `event_loop_running: bool` field），消费者拿 typed object 后 `view.event_loop_running`
   一目了然，无法再 typo `status` vs `event_loop_running`。

记入 §0l 元层 follow-up "CLI 死路径反模式"扩充：除了 dead route，还有
**dead-judgment-criterion**（路径对但判断字段错）模式，更隐蔽。

### 累计基线（§0g→§0m）

短短 24 小时累计修 P0/P1/P2/P3 共 **7 个真实 bug + 7 次反向锁沉淀**：

| § | 主题 | bug 性质 |
|---|---|---|
| 0g | EconomicDecayService 边界整改 | P0 timezone NameError + 复制粘贴漂移 |
| 0h | warm_up_from_db FrozenInstanceError | P2 frozen-mutate + 静默吞 |
| 0i | Python 版本契约失真 | P2 pyproject 与代码现实不符 |
| 0j | research utcnow → utc_now | P3 时区一致性 |
| 0k | live_preflight 探死 endpoint | P3 CLI dead route + 解析无信息量 |
| 0l | 三处 CLI 死路径/死 helper | P2+P3（trade/overview / signals/latest / hold_decision） |
| 0m | health_check indicator 假阳性 | **P1**（探针漂移，最高严重度） |

### 减少边界泄漏的方式

短期：CLI 不再无脑接受"无 status 字段就算 OK"，必须读真实 health 字段；
中期建议（见元层）：抽 `is_indicator_engine_running` helper 收敛同源判断。

---

## 0l. 2026-04-26 三处 CLI 工具死路径/死 helper 修复（healthchk × 2 + diagnose × 1 + hold_decision × 1）

### 触发

User 连续报告 3 个独立 bug，均为同模式（**死路径/死 helper + 异常被吞 + 零测试覆盖**），
是 §0k live_preflight 同种类的"运维 CLI 工具长期失真"问题：

| # | 文件 | 表面症状 | 真实根因 |
|---|---|---|---|
| 1 | `src/ops/cli/health_check.py:172` | "Trade executor: WARN unavailable: HTTP 404" | GET `/v1/trade/overview` 路由已删，应 `/v1/trade/control` |
| 2 | `src/ops/cli/diagnose_no_trades.py:138` | print "Failed to query: HTTP 404" | GET `/v1/signals/latest` 已删，应 `/v1/signals/recent` |
| 3 | `src/signals/evaluation/indicators_helpers.py:119` `hold_decision()` | 直接调用即抛 TypeError | `SignalDecision(action="hold", ...)` 字段名错误，真实字段是 `direction` |

附带在修 #1 时发现 `daily_report.py:152` 同样硬编码 `/v1/trade/overview`——同源残余，一并清。

### 影响

| # | 长期实际危害 |
|---|---|
| 1 | 健康检查"交易模块"段长期标 WARN，无法反映 executor enabled / circuit breaker / execution_count 真实状态。运维盯仪表盘以为交易模块异常实际是探针错路径 |
| 2 | 诊断"为什么没下单"流程缺失 DB signal-events 视角，容易把问题误判到信号生成下游而漏看真正原因 |
| 3 | `hold_decision` 通过 `evaluation/__init__.py:7` export 为公共 helper，但任何策略真用即崩 → 长期处于"声明 API 但坏"状态。所幸全仓 0 真实调用（dead helper），未触发线上事故 |

### 修复（commit `738f23b`）

```python
# health_check.py + daily_report.py
- _fetch_json(f"{base}/v1/trade/overview")
+ _fetch_json(f"{base}/v1/trade/control")  # TradeControlStatusView 含 executor dict

# diagnose_no_trades.py
- requests.get(f"{BASE}/v1/signals/latest?...")
+ requests.get(f"{BASE}/v1/signals/recent?...")

# indicators_helpers.py:119
- SignalDecision(strategy=..., action="hold", confidence=0.0, ...)
+ SignalDecision(strategy=..., direction="hold", confidence=0.0, ...)
```

### 测试（之前 100% 零覆盖；与 P2 warm_up_from_db / P3 _check_api 同模式）

| 文件 | 项数 | 锁定的契约 |
|---|---|---|
| `tests/ops/test_health_check.py` | 3 | URL 路径锁（防回退 /v1/trade/overview）+ executor 解析（OK/WARN）+ 真 404 降级 |
| `tests/ops/test_diagnose_no_trades.py` | 1 | source-level sentinel（imperative print 脚本难做 monkeypatch，函数化重构超出 scope） |
| `tests/signals/evaluation/test_indicators_helpers.py` | 4 | TypeError 不再抛 + direction='hold' + reason 默认/自定义 |

### Sentinel

```bash
$ grep -rn "/v1/trade/overview\|/v1/signals/latest" src/ --include='*.py'
(empty - clean)
```

### 元层教训：CLI 死路径反模式 = 已发现 4 处（§0k + §0l × 3 + daily_report 同源）

短短 24 小时连续暴露 4 个 CLI 工具探测已删除路由的 bug，证明这是**结构性问题**而非偶发：

1. **API 路由 rename / 删除时缺乏"消费者扫描"流程** —— 删 `/v1/trade/overview` 时
   没人 grep `src/ops/` 找消费者，CLI 默默挂了月级时间
2. **CLI 工具既是 API 消费者，又长期无测试** —— 即使 API 变化了，CLI 测试 0 覆盖也无人发现
3. **except Exception + print/debug log 默认成为"死路径生存土壤"** —— 与 ADR-011 反模式同源

**强建议下一维护窗口**：

a. 在 CLAUDE.md "Git 工作流" 段加入 sentinel grep（已在 §0k follow-up 加了 1 条
   `/v1/health` 类的；现在还缺"删 API 路由前必须 grep `src/ops/cli/` 与 `tests/`
   找消费者"的工作流硬性要求）

b. 给 `src/ops/cli/` 中**人会盯着输出做决策**的 6 个工具补 ≥ smoke 级测试：
   - ✅ `live_preflight` (§0k 已补)
   - ✅ `health_check` (§0l 本次)
   - ✅ `diagnose_no_trades` (§0l source-level sentinel)
   - 🔲 `daily_report` (本次顺手修但未补 test，待加)
   - 🔲 `confidence_check`
   - 🔲 `mt5_session_gate`（已有 4 项测试覆盖核心入口）

c. API 路由变更纳入 PR checklist：删除 / 改名 endpoint 必须在 PR description
   附带 `grep -rn "<old-path>" src/ops/ tests/` 截图证明无消费者残留

### 测试基线

- 修复前：3 处真实 bug + 0 测试覆盖
- 修复后：8 项新契约测试转绿；`pytest -m "not slow"` **2788 passed / 0 failed / 6 skipped**
- 累计 §0g→§0l 一周内 P0/P1/P2/P3 共 **6 次 bug 修复 + 6 次反向锁** 沉淀

### 减少边界泄漏的方式

CLI 工具改用稳定 API 端点（`/v1/trade/control` 比 `/v1/trade/overview` 更明确语义；
`/v1/signals/recent` 比 `/v1/signals/latest` 与现有路由命名统一）。`hold_decision`
helper 修对了字段名（与 `SignalDecision` 真实契约对齐），未来可放心使用。

---

## 0k. 2026-04-26 live_preflight._check_api 探测已删除路由修复（误导启动前检查）

### 触发

User 报告：`src/ops/cli/live_preflight.py:_check_api()` 仍请求 `/v1/health`，
但当前路由树只定义根 `/health` 和 `/v1/monitoring/health{,/live,/ready}`。
preflight 在现服务上稳定 404 → "API health: FAIL"，直接误导启动前检查与
live canary（运维以为服务异常实际是探针错路径）。

### 路由真实定义（核验）

| 路径 | 定义点 | 用途 |
|---|---|---|
| `/health` (root) | `src/api/__init__.py:171` `@app.get("/health")` | 业务级 ApiResponse[dict]（mode/market/trading/runtime） |
| `/v1/monitoring/health/live` | `monitoring_routes/health.py:87` | K8s liveness probe |
| `/v1/monitoring/health/ready` | `monitoring_routes/health.py:92` | K8s readiness probe（含 startup phase 检查） |
| `/v1/monitoring/health` | `monitoring_routes/health.py:180` | 详细 health 报告（含 deps 状态） |

`/v1/health` 不存在。preflight 应取 root `/health`（业务级 ApiResponse 结构匹配既有解析）。

### 隐藏次因（即使 URL 正确旧实现也无信息量）

`ApiResponse[dict]` 的 `data` 结构是 `{mode, market, trading, ingestor, runtime}`，
**无顶层 `status` 字段**。旧实现 `data.get("data", {}).get("status", "unknown")`
即使 200 也永远输出 `"status=unknown"`——preflight 报告**完全无业务诊断价值**。

→ 这是 user 报告的"假警报"之外的"真静默"：即使有人手动改 URL 修了 404，
也会发现 preflight 报告内容空洞。

### 修复（commit `ef61a96`）

```python
# 旧
r = requests.get(f"{api_base}/v1/health", timeout=5)
status = data.get("data", {}).get("status", "unknown")
# → "status=unknown" 即使 200

# 新
r = requests.get(f"{api_base}/health", timeout=5)
if not payload.get("success"): → FAIL with error code
mode = data.get("mode", "unknown")
market_connected = data.get("market", {}).get("connected", False)
trading_running = data.get("trading", {}).get("running", False)
# → "mode=FULL market=connected trading=running"
```

异常分层（同 ADR-011 契约）：
- `requests.ConnectionError` → SKIP（service not running 是合法 preflight 场景）
- `Timeout/RequestException/ValueError(JSON)` → FAIL with detail
- Coding error（AttributeError/TypeError 等）→ 透传 fail-fast

### 测试（4 项新增回归，先零覆盖）

| 测试 | 锁定的契约 |
|---|---|
| `test_check_api_hits_root_health_not_v1_health` | URL 路径锁；防回退到 /v1/health |
| `test_check_api_extracts_mode_from_response` | 防回归 "status=unknown" 死写 |
| `test_check_api_handles_connection_error` | ConnectionError → SKIP |
| `test_check_api_marks_failure_when_success_false` | 防把 ApiResponse.success=False 错当 OK |

之前 **`_check_api` 零测试覆盖** —— 这是与 P2 (`warm_up_from_db`) 完全相同的模式，
也解释了为什么 bug 数月未发现。

### 元层教训：CLI 工具同样需要测试覆盖

`src/ops/cli/` 的 23 个 CLI 长期被视为"运维脚本，能跑就行"，但 `live_preflight`
是**真实生产前的最后一道门**——它的 false positive (404 误报) 比业务代码的 bug
更危险（可能让运维取消上线决策，或反向"既然 preflight 一直 fail 就忽略它"）。

建议下次维护窗口：
- 给 `src/ops/cli/` 中**人会盯着输出做决策**的工具（preflight / health_check /
  diagnose_no_trades）补 ≥ smoke 级测试
- 其他纯产生数据 / 单次诊断的（mining_runner / backtest_runner）测试优先级低

### 未决整改（不阻塞合并）

`live_preflight.py` 残余 **48 处 pre-existing flake8** 违例（E501 长行 ×33 +
F401 unused imports ×3 + E402 module-level imports ×4 + F541 f-string without
placeholder ×6 + sys.path manipulation in line 23 ×1）。本次修复 net 49 → 48
（反而减 1）。

整体清扫需要重构文件结构（sys.path hack 移除 + 长行折行 + 死 import 清理），
风险较高且 surface 大，不在本次 P3 主题。

### 测试基线

- pytest tests/ops/test_live_preflight.py：4 → **8 passed / 0 failed**（含 4 项新回归锁）
- pytest -m "not slow"：**2780 passed / 0 failed / 6 skipped**（4 个新 _check_api 测试 + 之前 2776）

### 减少边界泄漏的方式

preflight 不再硬编码已删除路径——通过 ApiResponse 标准 envelope 的 `success`
+ `data.mode` 解析，与业务 health endpoint 的契约保持一致；未来 health 改动
（如 add fields）只要保留 ApiResponse[dict] 包装就不破 preflight。

---

## 0j. 2026-04-26 Research 链路时间戳 UTC-aware 一致化（datetime.utcnow → utc_now helper）

### 触发

User 报告：research 链路 3 处 `datetime.utcnow()` 输出 naive datetime
（无 `tzinfo`），与仓库其余模块通过 `src.utils.timezone.utc_now()` 统一产生
UTC-aware datetime 的惯例不一致。

> "它不一定立刻崩，但会造成前端/下游按本地时区解释、排序歧义和审计时间漂移。"

### 全仓影响范围

`grep -rn "datetime\.utcnow()" src/`：3 处全部在 research 链路：
- `src/api/research_routes/routes.py:54` — `mining_jobs["started_at"]`
- `src/research/orchestration/runner.py:166` — `MiningResult.started_at`
- `src/research/orchestration/runner.py:271` — `MiningResult.completed_at`

`tests/` 中另有 4 处同源问题（fixture 用 wall clock 造时间戳）。

### 实际危害（即使不立刻崩）

1. **前端时区解释歧义**：API 返回 `"started_at": "2026-04-26T12:34:56"`（无 `+00:00` 后缀），
   前端（如 QuantX）若用 `new Date(s)` 会按本地时区解析 → 显示偏移 N 小时
2. **排序与比较歧义**：naive datetime 与 aware datetime 混排时
   `a < b` 抛 `TypeError: can't compare offset-naive and offset-aware datetimes`；
   研究端到端流程若任一步加入 aware 时间戳，整条链路崩
3. **Python 3.12 持续告警**：`datetime.utcnow()` 已 deprecated，pytest 持续输出
   `DeprecationWarning`（19+ warnings/run），干扰真实警告可见性
4. **审计漂移**：mining run `started_at` / `completed_at` 入库时若被 timescaledb
   补 timezone（`TIMESTAMPTZ` 列），假设的 UTC 与实际写入时间可能错开（依赖驱动行为）

### 工具类已存在

`src/utils/timezone.py:utc_now()` 是项目 single source of truth，docstring
line 3-4 明示：

> All modules should use these functions instead of inline
> `datetime.now(timezone.utc)` or ad-hoc timezone conversions.

### 修复

**src（commit `72efa55`）**：3 处 `datetime.utcnow()` → `utc_now()`
- 添加 `from src.utils.timezone import utc_now` import（routes.py 与 runner.py）
- `datetime` import 保留（仍用于 `datetime.fromisoformat` 与 type annotations）

**tests（同 commit）**：4 处同源 fixture 也切换：
- `tests/research/orchestration/test_rank_findings.py`：3 处 `datetime.utcnow()` → `utc_now()`
- `tests/research/features/test_runner_hub_integration.py`：1 处 `_dt.datetime.utcnow()` → `utc_now()`
- 同步删除不再使用的 `from datetime import datetime` import

**附带 hardening（同 commit）**：
- 7 处 pre-existing F401 unused imports 顺手清扫（`ast` / `textwrap` /
  `typing.List/Optional/Tuple` / `unittest.mock.patch` / `pytest` /
  `MinedRule`）
- 1 处 `runner.py:510` E501 长行折行
- 1 处测试 docstring 长行拆 2 段

### Sentinel 验证

```bash
$ grep -rn "datetime\.utcnow()" src/ tests/ --include='*.py'
(empty - clean)
```

### 测试基线

- `pytest -m "not slow"`：**2776 passed / 0 failed**（与修复前一致；
  纯类型一致化无 runtime 行为变更）
- 19 个 `DeprecationWarning: datetime.datetime.utcnow() is deprecated` 警告
  应在下次跑测试时全部消失

### 减少边界泄漏的方式

研究域不再自带"本地 wall clock"产生时间戳——与仓库其他模块（signal /
trading / monitoring）共用同一 `utc_now()` 端口。下次新增模块若想自己
`datetime.utcnow()` 偷懒，sentinel grep 可在 CI 加一道：

```bash
grep -rn "datetime\.utcnow\|datetime\.now()  *#  *no.*tz" src/ --include='*.py' && exit 1
```

### 元层教训

工具类存在 ≠ 一定被使用。`src.utils.timezone.utc_now()` 已存在但 research 域
始终用 `datetime.utcnow()`——可能因为：
- 项目早期就有的代码，没人 refactor
- 写时图省事，没查 utils
- code review 没拦下来

建议：把"时间戳必须 UTC-aware"写进 CLAUDE.md "代码规范"段（与 Pydantic v2 /
mypy strict / 线程安全 同级），加 grep sentinel 命令到 pre-commit hook
或 CI lint 阶段。

---

## 0i. 2026-04-26 Python 版本契约失真修复（声明 >=3.10 与代码实际对齐）

### 触发

User 报告：`pyproject.toml:10` 声明 `requires-python = ">=3.9"`，但 `src/config/utils.py:20`
`def _canonical_config_name(...) -> str | None:` 在无 `from __future__ import annotations`
情况下使用 PEP 604 union——Python 3.9 import 时即抛 `TypeError`。当前环境 Python 3.12
所以测试无暴露，部署到 3.9 起不来。

### 全仓影响范围

`grep` 全 src 后发现：
- 132 个文件用 PEP 604 union（`X | Y` / `X | None`）
- 其中 129 个文件有 `from __future__ import annotations` lazy 化（PEP 563），3.10 之前可工作
- **3 个文件无 future-import**：`config/utils.py` / `indicators/core/momentum.py` /
  `monitoring/manager.py` — 在 3.9 下 import 即崩

mypy 实际**已经在 strict 模式下报这个错误**（`X | Y syntax requires 3.10+ [syntax]`），
但 mypy `python_version = "3.9"` + CI 跑 3.12 → 报错被当成"忽略噪声"放过数月。

### 修复方案：契约对齐而非代码降级

代码实际是 3.10+ 写法（PEP 604 / PEP 585 native generics），强行兼容 3.9 需要扫
全部 132 个文件 + 改写 union；不如把契约改为代码现实：

| 位置 | 旧 | 新 |
|---|---|---|
| `pyproject.toml:10` `requires-python` | `">=3.9"` | `">=3.10"` |
| `pyproject.toml:21` classifiers | 含 `Python :: 3.9` | 删除 |
| `pyproject.toml [tool.black]:target-version` | `['py39', ...]` | `['py310', 'py311', 'py312']` |
| `pyproject.toml [tool.mypy]:python_version` | `"3.9"` | `"3.10"` |
| `CLAUDE.md` 项目概览行 | `Python 3.9–3.12` | `Python 3.10–3.12` |

### 附带 hardening

- 3 个无 future-import 文件补上 `from __future__ import annotations`，与项目 96%
  convention 对齐；防止未来若降级到 3.9 再踩同坑（即使 3.10 原生支持，习惯保留）
- `utils.py:66-70` mypy strict 真实 bug 顺手修：`candidate` 变量被 reassign
  Path → str 类型不一致，改用两个分别命名的局部变量 `instance_candidate` / `abs_candidate`

### 元层教训

**mypy 报错≠噪声**。本次 P2 是 mypy 已经持续报警数月（line 20 `X | Y syntax`），
但因为 CI 跑 3.12 + 报错混在 540+ 行历史 mypy noise 里，没人区分"真错误 vs 历史包袱"。

下次维护窗口建议：
1. 把 `pyproject.toml [tool.mypy]` 的 python_version 与 `requires-python` 双向校验
   写入 CI（避免再次失同步）
2. 540+ 行 mypy 历史噪声分类——区分 "type narrowing TODO" / "无害 lib 缺 stub" /
   "真实 bug"；定期消除真实 bug 类目
3. 加 pre-commit hook：禁止新文件无 `from __future__ import annotations`（除非显式标注豁免理由）

### 测试基线

- pytest -m "not slow"：**2776 passed / 0 failed**（与本次修改前一致；纯文档/契约级修改无 runtime 影响）
- mypy `src/config/utils.py`：从 2 errors → **0 errors**

### 减少边界泄漏的方式

声明 vs 实现"双向校验" — pyproject 的 `requires-python` / mypy `python_version` /
black `target-version` 三个版本契约从此都指向同一 minimum (3.10)，未来 bump 时一处改、
全部跟随；防止单点漂移让代码与契约失真。

---

## 0h. 2026-04-26 PerformanceTracker.warm_up_from_db FrozenInstanceError 修复（ADR-011 异常分层契约二次落实）

### 触发

User 直接复现 `src/signals/evaluation/performance.py:525-526` 的 `FrozenInstanceError`：
`warm_up_from_db()` 直接给 `PerformanceTrackerConfig`（`@dataclass(frozen=True)`）的
`pnl_circuit_enabled` 字段赋值，每次调用必抛 `FrozenInstanceError`。

启动链路实际**已 active**——`_start_performance_warmup` 注册为
`FunctionalRuntimeComponent("performance_warmup", supported_modes=all_modes)`，
每次启动都执行；但 `runtime_controls.py:198` 的 `except Exception` + `debug` 日志
（默认级别看不到）静默吞噬数月，用户感知不到 warm-up 完全失效。**与 ADR-011 完全相同的反模式**。

### 影响

- StrategyPerformanceTracker 重启后**永远以空白状态启动**：
  - 无历史 wins/losses/streak 数据 → `get_multiplier()` 退化为基线乘数
  - PnL 熔断器历史 loss streak 丢失 → 风控统计断裂
- `_start_performance_warmup` 的 `is_running_fn` 用 `warmup_state["done"]` 判定，
  exception 路径下 `done` 永不为 True → 该 RuntimeComponent 永远显示未运行
  （但启动流程未阻塞，因为 except 吞噬）

### 修复

**performance.py（commit `0bbb478`）**：
- `__init__` 新增 `self._warmup_active: bool = False`
- `record_outcome` 熔断分支检查 `not self._warmup_active` 后绕过
- `warm_up_from_db` 不再 mutate frozen `self._config`，改为 set/reset instance flag
- 设计权衡：保持 frozen 语义；单一行为变更点（line 297）；thread-safety 由
  `record_outcome` 的 `self._lock` + RuntimeComponent 启动顺序保障

**runtime_controls.py（commit `4fcca68`）**：异常分层契约同 ADR-011：
- catch 收窄为 `(ConnectionError, TimeoutError, OSError, RuntimeError)` —— 仅运行时降级
- 编码错误（`FrozenInstanceError` / `AttributeError` / `TypeError` 等）直接 fail-fast 透传
- 日志级别从 `debug` 升 `WARNING`（默认级别可见，运维看得到）
- 错误消息含异常类型名（与 `EconomicDecayService` warning 同格式）

**测试（commit `a3a2a19`）**：6 项契约测试（之前零覆盖正是 P2 数月未发现的根因）：
| 测试 | 锁住的契约 |
|---|---|
| `does_not_mutate_frozen_config` | 不再触碰 frozen 字段 |
| `replays_outcomes_returns_count` | happy path |
| `skips_invalid_rows` | 健壮性 |
| `does_not_trigger_pnl_circuit` | warm-up 期间历史亏损不进熔断 |
| `restores_pnl_circuit_for_subsequent_real_trades` | warm-up 后真实交易仍能触发 |
| `propagates_coding_errors_fail_fast` | 用 NameError 反向锁死异常分层 |

### 顺手清扫（同 commit `4fcca68`）

`performance.py` 3 处 pre-existing 违例：
- F401 `dataclasses.field` 未使用 import 删除
- 2 处 E501 长行折行（log message 拆两段；dict literal value 加括号）

### 元层教训

**ADR-011 不是孤例**：本仓库还存在多少处 `except Exception` + 静默 debug 日志
吞噬编码错误的反模式？建议在下一个维护窗口扫一遍：
```bash
grep -rn "except Exception" src/ --include='*.py' | grep -v "# noqa\|raise"
grep -rn "logger.debug.*exc_info=True\|logging.*\.debug.*exc_info" src/ --include='*.py'
```
逐项判断是合理降级还是吞噬编码错误。

### 减少边界泄漏的方式

ADR-011 的异常分层契约从纯文档升级为**机制级强约束**：
- service 端口（calendar 域）已落实
- 装配层 try/except（runtime_controls）也落实
- 任何新增的 `except Exception` 在 PR review 时应明确触发"是降级还是吞噬"判断

### 测试基线

- 修复前：`tests/signals/evaluation/test_performance.py` 20 项，**零项覆盖 warm_up 路径**
- 修复后：26 项，含 6 项 warm_up 契约测试 + 反向 fail-fast 锁；全仓 `pytest -m "not slow"` 通过

---

## 0g. 2026-04-26 经济事件衰减边界整改（ADR-011）

### 触发

`signals/orchestration/runtime_evaluator.py:_query_symbol_event_decay` 缺 `timezone` import → NameError 被 `except Exception` 吞 + 缓存 1.0 → 所有 symbol 渐进降权静默失效（疑似数月）。同步审视发现复制粘贴漂移、时间源错误、测试桩本体三连根因。

### 边界变化

- **新增**：`src/calendar/economic_decay.py:EconomicDecayService` 作为 calendar 域唯一对外 decay/blocking 查询端口
- **删除**：`signals/orchestration/runtime_evaluator.py` 中 `_compute_economic_event_decay` / `_query_symbol_event_decay` / `_resolve_eco_context` / `_SIGNAL_EVENT_STATUSES` / `_EVENT_DECAY_TTL_SECONDS` / `_EVENT_DECAY_CACHE_MAX`
- **删除**：`signals/execution/filters.py` 中 `_SIGNAL_EVENT_STATUSES` / `_symbol_context` 与重复 `EconomicEventsProvider` Protocol（迁移至 `calendar/economic_decay.py`）
- **删除**：`SignalRuntime._event_decay_cache` 字段（cache 归属 service 内部）
- **变更**：`apply_confidence_adjustments` 签名增加必填 `event_time` 参数；`EconomicEventFilter` 字段从 `(provider, policy)` 改为 `(service: Optional["EconomicDecayService"])`
- **新增端口**：`SignalRuntime.set_economic_decay_service` / `economic_decay_service` property
- **新增 container 字段**：`AppContainer.economic_decay_service`
- **新增工厂**：`build_economic_decay_service`（factories/signals.py）
- **公共 API 上提**：`infer_symbol_context` 与 `EconomicDecayService` 通过 `src/calendar/__init__.py` 暴露
- **simulator 同步**：`backtesting/filtering/simulator.py` 移除内嵌 `EconomicEventFilter(service=EconomicDecayService(...))` 双层包裹

### 异常分层契约（CLAUDE.md §12 落实）

| 场景 | 行为 |
|---|---|
| policy 关闭 / provider 缺失 | 返回 1.0，无日志（设计降级）|
| `ConnectionError/TimeoutError/OSError/RuntimeError` | 返回 1.0 + `WARNING` 日志，**不写缓存** |
| `NameError/AttributeError/ImportError/TypeError` 等 | 直接传播，fail-fast |

### 测试架构修正

- 禁用 `monkeypatch.setattr(...被测函数, ...)` anti-pattern（旧模式让 P1 timezone bug 数月不被发现）
- `tests/calendar/test_economic_decay.py` 新增 13 项契约测试，含 fail-fast 反向锁死 + timezone 回归锁
- `tests/signals/test_runtime_evaluator.py` 重写：注入 stub service，新增 event_time 透传 + service 缺失降级测试
- `tests/signals/test_filters.py` + `tests/app_runtime/test_signal_factory.py` fixture 同步迁移

### 未决兼容项

无生产兼容路径残留。

原"未决文档化整改"已在合并 main 后立即清扫完毕（commits `4fef5a1` / `13eed62` / `930e662`）：
- ✅ `src/backtesting/filtering/builder.py` + `src/research/orchestration/runner.py` 改为 `from src.calendar import infer_symbol_context`，跨域子包私有路径 import 全仓 0 命中
- ✅ `runtime.py` 删除 `UnifiedIndicatorManager` / `SoftRegimeResult` / `MetadataKey as MK` 三个 unused imports（含 `TYPE_CHECKING` 块清空）
- ✅ `runtime_evaluator.py` 删除 `evaluate_strategies` 中 `soft_parsed` 死代码（11 行）+ 对应 `SoftRegimeResult` import；commit `175295d` 删 `min_affinity_skip` 时遗留的死代码已清理
- ✅ `runtime{,_evaluator}.py` 跑 `black` + `isort` sweep；F401/F841 在两文件 0 命中
- ✅ pre-existing flake8 残余 3 处（E301 Protocol 间空行 + 2 处 91/90 字符 E501）也清扫完毕（commit `3a9d200`）：补空行、docstring 拆两段、sorted key 抽 local 变量。两文件 flake8 + black 双 0 violation

### 减少边界泄漏的方式

signal 域不再组装 calendar 查询参数 / 不再跨域 lazy import 子包私有路径 / 不再各自维护 cache。signal/risk/backtest 三域只与 `EconomicDecayService` 单一端口对话。filter 与 evaluator 共用同一 service 实例；hot reload 同步重建 service + filter chain，原子注入到 SignalRuntime。

### 测试基线对比

- 提交前：未跑该路径的真实测试，桩本体 monkeypatch 让 wall-clock + timezone bug 完全不可见
- 提交后：full test suite **2770 passed / 0 failed / 6 skipped**；新增结构性回归锁（timezone / event_time / fail-fast / service-None）4 项

---

## 0f. 2026-04-22 P8 回测 deployment gate 收口（ADR-009）

**背景**：2026-04-20 的 M30 baseline 审查（原快照已于 2026-04-23 重置时删除）记录的 M30 污染事件——`structured_price_action`（`deployment=paper_only` 且未绑定账户）在回测中被全量评估（1,258 笔），生产中完全不跑，导致 M30 baseline 虚高 7×（1,463 → 真实 206 笔）。根因是 `BacktestEngine` 的 deployment gate 只过滤 CANDIDATE，PAPER_ONLY 不过滤。

**本次改动**：
- `BacktestConfig.include_paper_only: bool = False`（新字段，默认严格 baseline 语义）
- `BacktestEngine` gate 拆成两段：CANDIDATE 永远排除；PAPER_ONLY 按 flag 选择
- 日志分离两类 filter 输出，便于排查
- `backtest_runner --include-paper-only` flag 用于 paper-shadow 回测
- 覆盖测试：`test_engine.py` +2（engine-level gate）/ `test_backtest_config.py` +2（config 默认值）

**职责变化**：
- 回测域职责不变：仍只按 `StrategyDeployment.status` 契约过滤，不读账户拓扑
- `allows_live_execution()` 从"只给 trading 域用"扩展到"也给 backtesting 域用"——合同层无变化，使用者增加

**清理清单**：
- 其他 backtest CLI（`aggression_search` / `exit_experiment` / `walkforward_runner` / `nightly_wf` / `correlation_runner` / API routes）**本次不改**——它们继承 strict baseline 语义符合"默认安全"原则。需要 paper-shadow 的 CLI 后续按需追加透传
- 旧 baseline 快照（含 price_action 污染的 M30 数据）已在 tf-baseline-review 头注链到 ADR-009，不回改正文

**减少边界泄漏**：
- 回测 gate 的判定语义从"允许 runtime 评估"向"允许 live 执行"收口，默认对齐实盘行为，消除"回测指标含实盘永远不跑的信号"这类隐性失真
- 不新增字段、不跨域读 local.ini、不需要 CLI 逐个打补丁——全部通过 `StrategyDeployment` 已有合同解决

**未决兼容项**：无。`include_paper_only=False` 是语义更严的新默认，旧调用方自动获得正确行为。

---

## §F. 历史已完成归档（2026-04-21 从 TODO.md 迁入）

> 原本在 `TODO.md` 里的已完成段落，按规范应归档到审计台账。迁入时保留原文结构。
> 时点数据（baseline 表）已移到 `docs/research/<日期>-*.md`，本段只保留"做了什么 + 证据"。

### 2026-04-22~23 架构与方法学改动索引（已脱敏归档）

> **2026-04-23 二次彻底清理**（`A` 选项）：本索引替代原 5 段具体实验记录
> （B-1 / P0 Round 1 mdi_sell / Gap 1/2/3 / P4 解耦 / Fresh mining baseline）。
> 物理删除所有作废数据的具体数字（PF / WR / drift / 估算值等），避免未来
> 决策时被作废 baseline 污染。细节追溯 **强制走 git log**，不经过 MD。

**架构与方法学 commit（保留未作废）**：
- `3379ec8` P4 research↔backtesting 解耦（ResearchDataDeps 端口）
- `f16f053` Gap 1+2a：cost model + forward_horizons
- `e85cb7e` Gap 2b：BarrierPredictivePower analyzer
- `1053631` Gap 3：weekend/holiday gap mask
- `2eda599` CLI 输出模板补 barrier
- `231804a` `_rank_findings` 纳入 barrier
- `097d838` 综合审查 H1+H2+H3（资源优化）
- `d5bc224` trend_continuation `_htf_adx_upper` 参数化（代码保留数值清空）
- `c40698a` 挖掘方法学审计（已物理删除的 research md）
- `d0c48c8` TODO P0 单闭环纪律
- `9288f86` P0 Round 1 mdi_sell（**策略代码已删**，commit 留作方法学反例）
- `b3f155a` `4aad26f` 全面重置
- `0d5680e` 2026-04-23 fresh mining baseline（commit message 记录 timing）
- `826e082` 二次彻底清理（§F 脱敏归档）
- `6df6f5b` breakout_follow paper_only → candidate（baseline audit 降级）
- `1e9e0ec` 评估层 `max_positions` 3→999（分层：评估无限制 / live 风控保留）

**P0 Round 2 注记**（2026-04-23，无独立 commit — 代码已完整 revert）：
基于 fresh mining H1 pp#4 候选编码 `structured_strong_adx_trend`，3 月 H1 回测
未过门槛（trades < 50 且 PF < 1.2），按 P0 单闭环 + 反循环纪律物理删除策略
代码/测试/注册——工作树回到 Round 2 开始前状态，git 未见过这些文件。
具体回测数字不入 MD（二次清理纪律）。

**P0 Step 2 Round 1 注记**（2026-04-24，paper 观察中断 — 周末休市关停）：
首轮 paper_only 策略实时观察在 session `ps_cb8200f3e9d4` 跑 ~2h
（`max_positions=999` 解除后确认开仓可超过旧上限 3），样本不足 5 不足以判决。
周五收盘前关停 live-main 实例 + 清理后台监控 cron。具体数字不入 MD，走
git log / `data/research/paper_monitor_2026-04-24.log` 追溯。
下周一重启连续观察，累计 ≥20 trades 后才给判决。

### 2026-04-20 P9 前端读侧 API 全套交付 ✅

QuantX 前端读侧支撑 P9 全部 5 个 Phase + API 治理 4 项已交付。**2374/2374 测试全过**。

**新增文件**：
- `src/readmodels/workbench.py` — WorkbenchReadModel（10 块组装）+ `compute_source_kind`
- `src/api/execution.py` + `src/api/execution_routes/workbench.py` — `GET /v1/execution/workbench`
- `src/app_runtime/factories/admission_writeback.py` — admission writeback listener（multicast / skip / intent_published）
- `src/config/models/freshness.py` — `FreshnessConfig` Pydantic
- `docs/design/quantx-data-freshness-tiering.md` — 5 级 Tier 契约文档

**关键扩展**：
- `src/persistence/schema/signals.py` — `MIGRATION_SQL`（5 ALTER COLUMN + 2 INDEX）+ `UPDATE_ADMISSION_SQL`
- `src/readmodels/runtime.py` — `compute_tradability_verdict()` + tradability 加 verdict/reason_code/recommended_action/source_kind/tier
- `src/api/trade_routes/state_routes/stream.py` — SSE envelope schema 1.1（tier/entity_scope/changed_blocks/snapshot_version）
- `src/api/schemas.py` — `MutationActionResultBase`（13 字段共享） + `WorkbenchPayload`
- `src/signals/analytics/diagnostics.py` — `build_strategy_audit_report()`

**前端可对接端点**（按 `docs/quantx-backend-backlog.md` §0 表）：
1. 工作台读口：`GET /v1/execution/workbench?account_alias=X`
2. 实时事件：`GET /v1/trade/state/stream`（按 envelope.changed_blocks 局部刷新）
3. 信号查询：`GET /v1/signals/recent?sort=priority_desc&actionability=blocked`
4. 策略诊断：`GET /v1/signals/diagnostics/strategy-audit`
5. Trace 详情：`GET /v1/trade/trace/{signal_id}`
6. Mutation 回显：13 字段统一基类（accepted/action_id/audit_id/...）

**部署提示**：实例启动自动跑 `ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS`（PG 11+ instant op，不锁表）。

### 2026-04-20 P10 QuantX 交易员主控台闭环 ✅

新增 canonical read model 端点（详见 commit 7aedd70）：

- `GET /v1/cockpit/overview`（P10.1：7 块 + 跨账户 contributors + triage_queue 原生派生）
- `GET /v1/intel/action-queue`（P10.2：guard-aware 队列 + account_candidates 反向索引）
- `GET /v1/trades/workbench` + `GET /v1/trades/{trade_id}`（P10.3：canonical 列表 + 6 维详情）
- `GET /v1/trade/command-audits` 扩 `audit_id/action_id/idempotency_key` 过滤（P10.4）
- `GET /v1/trade/command-audits/{audit_id}`（P10.4：含 linked_operator_command）
- `GET /v1/lab/impact`（P10.5：WF 分阶段 + recommendation lifecycle + paper session 贯通）

**Schema 迁移**：
- `paper_trading_sessions` 加 `source_backtest_run_id / recommendation_id / experiment_id` 列 + 3 索引（PAPER_TRADING_MIGRATION_SQL）

**P10.6 字段契约**：所有新端点 snake_case + ISO8601，无 camelCase 同义词（grep 0 hit）。

### 2026-04-17 Telegram 通知模块 Phase 1 ✅

插拔式、模板化、分级过滤的运行时事件推送。详见 `docs/design/notifications.md`。

- Pydantic 配置模型 + 分层 ini 加载器
- 核心类型：`NotificationEvent` / `Severity` / `build_dedup_key`
- 轻量模板引擎（`{{ var }}` + `{% if %}`）+ 启动期严格校验
- 5 CRITICAL + 3 WARNING + 1 INFO 起步模板
- `Deduper`（LRU + TTL）+ `RateLimiter`（令牌桶 global + per-chat）
- `OutboxStore`（L2 SQLite + 指数退避 + DLQ + 崩溃恢复）
- `TelegramTransport`（requests + 429 retry_after + 5xx 可重试 / 4xx 终态 + proxy）
- `PipelineEventClassifier`（6 个事件 mapper + 异常隔离）
- `NotificationDispatcher` + worker 线程（CRITICAL 限流不丢 + ADR-005 防双线程）
- DI 接入 Phase 5.5，`HealthMonitor.set_alert_listener()` 钩子
- `/v1/admin/notifications/{status,toggle}` API
- **206 条测试**

### 2026-04-17 Telegram 通知模块 Phase 2 ✅

- `NotificationScheduler`：DailyAt + Interval job，1 秒 tick，ADR-005 合规
- `TradingStateAlerts` 轮询适配（60s 轮询 `summary()`，3 种 code 映射）
- Outbox 清理定时器（`purge_sent_older_than(7 days)` 每 6h）
- daily_report 调度占位
- `scheduler_running` + `scheduler_jobs` 进 status()

### 2026-04-17 FP.2 strong_trend_follow ✅

基于 FP.1 长窗口挖掘（12 个月）H1 rule_mining #5：
`adx>40.12 AND macd_fast.hist<=1.61 AND roc12.roc>-1.17 → buy`（test 60.1%/n=143）

- 设计 + 实现 `structured_strong_trend_follow`（与 regime_exhaustion 在 adx_d3 互斥）
- 12 个月回测 Round 0（`_adx_extreme=40`）：Sharpe 1.446 未达 1.5 最低线
- C2 Round 1（`_adx_extreme=38` 放宽，signal.local.ini 覆盖）：**Sharpe 1.546 达标**
- 全量：Trades 64 / WR 46.9% / PnL +873 / PF 2.15 / MaxDD 6.97% / MC p=0.019
- `deployment = paper_only` 部署（locked_timeframes=H1，locked_sessions=london,new_york）

### 2026-04-14 Intrabar 交易链路已实现 ✅

> IntrabarTradeCoordinator 独立负责 bar 计数稳定性判定。
> 连续 N 根同方向子 TF bar 达标时直接发布 `intrabar_armed` 信号触发盘中入场。

- bar 内去重机制：IntrabarTradeGuard（同 bar/策略/方向只允许入场一次）
- confirmed 协调：盘中已开仓 → confirmed 同向 skip / 反向放行
- 盘中入场条件：coordinator min_stable_bars + min_confidence 阈值
- 风控覆盖：复用完整 pre-trade filter chain

### 2026-04-06 架构清理归档 ✅

**P0 策略验证闭环**
- 各 TF 基线回测 → 全部亏损，确认问题在出场结构
- 策略相关性分析 + 裁剪 + 投票组重组
- 新增 5 个 legacy 策略（range_box_breakout/bar_momentum_surge/atr_regime_shift/swing_structure_break/range_mean_reversion）
- 出场参数配置化（Chandelier Exit aggression α 驱动）

**路径 B 策略体系架构重构**
- 结构化策略框架：StructuredStrategyBase + Where(软)/When(硬)/Why(硬) 三层评估
- 6 个结构化策略：TrendContinuation/SweepReversal/BreakoutFollow/RangeReversion/SessionBreakout/TrendlineTouch
- 策略目录子包化：structured/（活跃）+ legacy/（冻结/待定）
- Volume 指标启用：vwap30/obv30/mfi14 + 新增 volume_ratio20
- 回测基础设施修复：MarketStructure 注入 + recent_bars 注入 + HTF fallback
- 信号质量优化：冻结 4 个亏损策略 + regime 门控收紧 + reversion trending=0.0
- 计算优化：regime fast-reject + signal_grade A/B/C 自动计算

**长期运行稳定性修复**
- EOD 跨日自动恢复 + apply_mode 异常保护 + TradeExecutor 双线程防护
- WAL Queue 重入 + IndicatorManager 线程守卫 + PositionManager 启动同步
- DLQ 文件清理 + listener_fail_counts 清理

**Legacy 策略全面移除**
- 35 个 legacy 策略 + 4 个复合策略代码删除
- 切换到纯结构化策略架构：7 个策略（8 个注册实例含 trend_h4 变体）
- 清理 signal.ini：移除 legacy strategy_timeframes/regime_affinity/strategy_params/voting_groups
- 清理 composites.json / registry.py / composite.py

**A7 入场职责回归策略层**
- 策略 `_entry_spec()` 输出入场意图（entry_type/entry_price/entry_zone_atr）
- PendingEntryManager 退化为纯执行层：读取策略入场规格，不再按 category 推算
- 移除 `_CATEGORY_ZONE_MODE` / `_compute_reference_price()` / `category_timeout_multiplier`
- TradeExecutor 分支简化：entry_type=market → 直接市价，limit/stop → 挂单

**Regime 预检清理 + A9 + 死代码清理**
- 移除三层冗余 regime affinity 预检
- `htf_alignment` 完全移除（不再计算/记录/乘 confidence）
- Executor HTF 冲突检查移除（执行层不做信号质量决策）
- 统一评分框架：每层 score 0~1 × budget（why=0.15, when=0.15, where=0.10, vol=0.05）
- 策略必须实现 6 个接口：_why / _when / _entry_spec / _exit_spec（强制）+ _where / _volume_bonus（可选）

### 2026-04-05 配置 & 基础设施清理 ✅

- BacktestConfig 拆分为 8 个嵌套子配置 + `from_flat()` 向后兼容
- 回测子包化（5 个子包：engine/filtering/analysis/optimization/data）+ API 迁移到 `src/api/backtest*`
- 硬编码魔数配置化（htf_alignment_boost/conflict_penalty/bars_to_evaluate）
- 补全缺失测试（+24 用例）
- API 响应格式统一（15 个端点 → ApiResponse[T] 包装）
- Paper Trading 模块：完整子包（6 文件 + 独立 DB 表 + API + Studio agent）
- A4: 信号队列持久化（WalSignalQueue，SQLite WAL 模式）
- A3: 增量指标扩展（9→15 incremental）+ 孤儿指标禁用（21→15 启用）
- 数据库 schema 全面重建：26 表，14 hypertable，统一索引命名，CHECK 约束
- 持久化架构审查：health_monitor.db → 内存环形缓冲（3.3GB→0）
- TimescaleDB retention + compression policy：16 表三级保留策略
- 日志文件持久化：RotatingFileHandler 100MB×10 + WARNING 独立 errors.log
- signal_preview_events 异步化：同步直写 PG → StorageWriter 异步队列
- 配置隐私分层：signal.ini/risk.ini 私域值置空，新建 risk.local.ini
- 统一 SQLite 连接工厂
- 文档架构重组：CLAUDE.md 1710→248 行，docs/ 11→5 个文件

---

## 0e. 2026-04-21 md 二轮深度审查 + 职责固化

**背景**：§0d 建立 md 管理规范后，用户要求对 32 份 md **再做一次完整审查**，找冗余 / 重复 / 界限不清，并把 `TODO.md` 一并纳入审查，确定和 `CLAUDE.md` + `docs/` 的职责分工。

**审查结果**（子代理并行盘点 + 交叉扫描）：

所有 32 份 md **开头职责声明覆盖率 100%**（§0d 建立的规范有效）。发现 3 处高严重度问题 + 3 处中等：

- 🔴 **PnL 熔断双处描述**：`signal-system.md §5.5` + `design/risk-enhancement.md §2.1` 讲同一特性
- 🔴 **持仓管理 3 份文档状态不清**：`pending-entry.md`（历史方案）/ `position-state-consistency.md`（已实现）/ `r-based-position-management.md`（规划未落地）—— 头注未明确区分
- 🔴 **Exit rules 3 处相关文档缺互链**：`research-system.md F-12b` / `position-state-consistency.md` / `codebase-review.md §P0-83`
- ⚠️ `high-risk-remediation-milestones.md` 已闭环未标"归档"
- ⚠️ `docs/README.md §1.2` 导航表缺持仓 / 风控 / 回测差异三行
- ⚠️ `TODO.md` vs `design/next-plan.md` 职责边界未声明

**TODO.md 审查**：发现它承载了 4 类内容（活跃待办 35% / 已完成归档 30% / 时点快照 15% / 长期路线 20%），违反"每 md 一职责"原则。

**本次修复**（温和方案，不搬动大段内容，只加头注 + 引用）：

**A. doc 偏差 (5 处)**：
- `design/pending-entry.md` — 头部加 ⚠️ 历史方案状态栏 + 当前实现位置（base.py `_entry_spec()`）
- `design/risk-enhancement.md` — 头部改为"设计演进背景"+ 指向 signal-system §5.5
- `design/position-state-consistency.md` — 头部加 ✅ IMPLEMENTED + 出场主题地图（研究/执行/bug/规划 4 条链接）
- `design/r-based-position-management.md` → **改名 `r-based-exit-plan.md`** + 头部加 📐 规划方案（未落地）
- `research-system.md` F-12b — 补"相关"链接到 position-state-consistency / codebase-review §P0-83/84 / r-based-exit-plan

**B. 导航与状态核对 (3 处)**：
- `docs/README.md §1.2` 表格补 3 行：持仓管理 / 风控 / 回测 vs 实盘差异
- `design/high-risk-remediation-milestones.md` 头部加 📦 归档状态（6 项闭环 + F-6~F-11 接管）
- `design/next-plan.md` 头部加**职责分工表**（本文 vs TODO / codebase-review / architecture）

**C. TODO.md 职责固化（温和方案，不搬内容，仅加顶部声明）**：
- `TODO.md` 顶部加**职责分工表** + 内容迁移规则（baseline 该去 research/、长期规划该去 next-plan、完成归档该去 codebase-review）
- `CLAUDE.md §项目概览` 尾部加**文档路线表**，清晰标注"读哪个文档回答什么问题"

**结果**：
- 所有 32 份 md 现在都有清晰的"我是什么、不是什么、相关文档在哪"头注
- 持仓管理 / 风控两个主题的文档地图首次完整（从 position-state-consistency.md 可一键跳转到所有相关文档）
- TODO.md / next-plan.md / codebase-review.md 三份核心文档职责**互不重叠**，未来新内容有明确归属
- 重命名的 `r-based-exit-plan.md` 名字更精确（不和已实现的 position-state-consistency 混淆）

**未执行的激进选项**（以后可按需做）：
- 搬 ~500 行从 TODO.md 到 codebase-review + research/ —— 会破坏 session-start 读 TODO 的现有习惯，先不动
- 新建 `docs/design/exit-rules-overview.md` 作为出场规则统一入口 —— 现在通过 `position-state-consistency.md` 头部地图已能导航，不急

---

## 0d. 2026-04-21 文档审查 + md 规范 + 历史挖掘导入

**背景**：用户提出两个问题，连带第三件事：
1. 当前挖掘是不是还用 N-bar 端点判断？→ 触发 research 代码审查
2. 希望清理与代码不符的 md + 建立 md 管理规范防止乱加
3. 把磁盘上的历史 mining JSON 落 DB（延续 §0c 打通的入库管线）

**Research 代码现状核实**（审查结论，重要事实）：
- Triple-Barrier target labeling 已完整落地（cf838d5 / e64a8e7 / 5b651a2）
  - `src/research/core/barrier.py` 向量化 SL/TP/Time 三分支先触，SL 与 TP 同 bar 保守取 SL
  - `data_matrix.py:335-359` 自动填充 `barrier_returns_long/short`，9 组默认 RR 网格
  - `rule_mining.py:870-927` `_compute_barrier_stats_for_rule()` 每条规则产 BarrierStats
- Feature Providers 6 个全部就位（temporal / microstructure / regime_transition / session_event / intrabar / cross_tf）
- `FeatureCandidateSpec.feature_kind` 已区分 `derived`（组合型）vs `computed`（计算型，4ead09a）

**文档清理**（4 处偏差修正）：
- `docs/design/adr.md` ADR-007：状态"拟定中"→"已确定（2026-04-17）; 特征晋升自动化仍未实现"，"当前实现缺口"章节重写为"已落地 4 项 + 仍未实现 4 项"
- `docs/research-system.md` F-12b：补充"研究层在 `barrier.py`+`data_matrix.py`，执行层在 `exit_rules.py`"的双层实现位置对照
- `docs/research/2026-04-18-mining-vs-backtest-gap.md`（已于 2026-04-23 重置时删除）：原加"时点快照不回改"头注 + "后续演进"注释链到 F-12a/b/d 的 commit（"端点判断"现等价 Triple-Barrier 的 Time 分支）
- `docs/README.md`：补充 `docs/research/` 和 `docs/superpowers/` 导航；重写 §2 分层（5 类：事实源 / 审计 / Runbook / 领域设计 / 研究快照）

**md 管理规范**（新增，`docs/README.md §3` + `CLAUDE.md §13`）：
- 新建 md 前必须回答 4 问：WHERE 归哪层 / WHY 不能并入现有 / WHEN 生命周期 / WHO 维护
- 禁止：根目录业务 md（仅 CLAUDE/AGENTS/README/TODO）、`-old`/`-v2` 版本后缀、事故写进事实源文档、同主题平行 md
- 时点快照"正确保鲜方式"：不回改正文，加"后续演进"头注链到最新 commit/ADR
- commit 触发 SOP：架构改 → architecture+adr，策略数变 → CLAUDE §概览+signal-system，Research 变 → research-system+本文件，事故 → 本文件新段落（不删旧），API 契约 → quantx-canonical-ia

**历史挖掘导入**（`src/ops/cli/import_historical_mining.py`）：
- 扫 `data/artifacts/mining_*.json` + `data/research/mining_*.json`
- 解析 `{"results": [{"tf","run_id","data_summary",...}, ...]}` 格式
- 用文件 mtime 推 `experiment_id`（`historical_<YYYYMMDD>`）便于按批次追溯
- dry-run 默认，`--execute` 才真写；`INSERT ... ON CONFLICT DO UPDATE` 自身幂等，重导不会重复
- **已执行**：3 份 JSON → 8 行落 `research_mining_runs` 表（historical_20260415: 5 runs，historical_20260417: 3 runs，共 41,239 bars 样本量）

**测试**（8 新用例）：`tests/ops/test_import_historical_mining.py` 覆盖 JSON→row 转换 / dry-run 不写 / execute 写 batch / 部分 malformed 跳过 / 空 results 安全。

**附带检查**：`tests/{ops,monitoring,config,persistence,clients,indicators,readmodels}` **共 429 测试全绿**。mypy 干净。

**下一步推荐**：
1. 通过 `/v1/research/mining` API 前端能看到 8 条历史挖掘（验证过），前端 Research 页可以直接对接
2. 未来真正跑一次带 `--experiment` 的端到端闭环（mining → backtest → paper → 前端审计），让 `experiments` 表首次写入

---

## 0c. 2026-04-21 CLI 实验管线打通（`--persist` / `--experiment`）

**背景**：调查当前 mining / backtest 结果从未入 DB 的原因时发现：`src/ops/cli/mining_runner.py` 和 `backtest_runner.py` 的设计定位是"快速诊断，供 AI 助手拿摘要"，只输出 stdout / JSON 文件，**完全不调 `repo.save_*`**。而 `src/api/*_routes/routes.py` 配合 BackgroundTask 的正式端点才入库——但从未被使用，导致 `research_mining_runs / backtest_runs / experiments` 表长期 0 行、ADR-007 声称的"Research→Backtest→Paper→Live experiment 追踪"管线**从未真正启用**。

**本次打通**（方案 A：最小侵入，opt-in flag）：

- **新增 `src/ops/cli/_persistence.py`** helper 模块（统一 3 个 CLI 使用）：
  - `_writer_scope(environment)` context manager：CLI 短生命周期场景下打开 `TimescaleWriter` 并在退出时 `pool.closeall()`（符合 ADR-008 精神——禁令针对长期运行 API 路由，不针对一次性 CLI）
  - `persist_mining_results({tf: MiningResult}, env, experiment_id=None)` 批量入库
  - `persist_backtest_result(BacktestResult, env, exp_id)` / `persist_backtest_results_many(...)`
  - `add_persist_arguments(parser)` 统一 `--persist` / `--experiment <id>` 两个 flag 的注册
  - 所有失败 warning 日志，不 raise——CLI 结果已在 stdout/JSON 输出，不该阻塞退出

- **`src/ops/cli/mining_runner.py`**：`main()` 尾端收到 `raw_results: {tf: MiningResult}` 后，`--persist` 时调 `persist_mining_results`
- **`src/ops/cli/backtest_runner.py`**：`_run_single` 返回的 `_raw_result` 保留为 `raw_objects: List[BacktestResult]`，`--persist` 时调 `persist_backtest_results_many`

**使用方式（不改已有工作流）**：

```bash
# 原行为不变（默认不入库，AI 助手快速诊断）
python -m src.ops.cli.backtest_runner --environment live --tf H1

# 正式 baseline（入库 + 关联实验）
python -m src.ops.cli.backtest_runner --environment live --tf H1 \
    --persist --experiment exp_20260421_h1_baseline

# 跨 TF baseline 一次性入库
python -m src.ops.cli.mining_runner --environment live --tf H1,M30,M15 \
    --compare --emit-candidates --persist --experiment exp_20260421_cross_tf
```

**测试覆盖**（1 新文件 7 用例全绿）：
- `tests/ops/test_cli_persistence.py` —— helper 契约：writer 生命周期 / experiment_id 透传 / 部分失败继续 / writer open 失败 warning 不 raise / `add_persist_arguments` flag 注册

**附带检查**：`tests/{ops,monitoring,config,persistence,clients,indicators,readmodels}` **406 测试全绿**。mypy 干净。

**未完成（已知）**：
- `src/ops/cli/walkforward_runner.py` **本次未加 `--persist`**：`WalkForwardResult` 目前只在 `BacktestRuntimeStore` 内存缓存（上限 50，重启丢；CLAUDE.md Known Issues 已记录），尚无对应 `walk_forward_repo` DDL / save 方法。需要先新增 `src/persistence/repositories/walk_forward_repo.py` + schema 才能接入——留待下一轮
- `src/backtesting/cli.py`（legacy）的 `_persist_result` 仍在（双入库路径共存）——后续可标 `@deprecated` 或迁移到 `_persistence.py`

**下一步推荐**：
1. 真正跑一次带 `--experiment` 的 H1 baseline + mining + paper_trading `--from-backtest-run-id`，让 `experiment_id` 贯穿 4 张表（目前 `experiments` 表仍是 0 行——管线打通但没走过闭环）
2. 修 CLAUDE.md Known Issues 的 "WF 结果内存缓存" + "回测 BackgroundTask 结果丢"

---

## 0b. 2026-04-21 事故后跟进优化（pool 容量 + monitor race defense）

**背景**：ADR-008 修复部署后观察 6h，指标仍有两类"瘙痒"：
1. warm-up 期 5-15 分钟 + 偶发突发时 `connection pool exhausted` 15 次（不再连锁故障，但说明 10 连接在峰值不够）
2. `Failed to check cache stats: dictionary changed size during iteration` 极低频发生（6h 共 2 次）——indicator manager 内部 50+ dict hot path 与 monitor 快照 iteration 的瞬时 race

**本次优化**：

- **pool 容量配置化** (`src/config/database.py`、`src/persistence/db.py`、`config/db.ini`)
  - `DBSettings` 新增 `pool_min_conn / pool_max_conn` 字段（默认 **1-20**，从事故前的隐式 1-10 提升）
  - `TimescaleWriter.__init__(settings, min_conn=None, max_conn=None)`：显式传参仍优先（保留 ops CLI / 单元测试使用小 pool），未传则从 settings 读取
  - `db.ini [db.live]` / `[db.demo]` 新增 `pool_min_conn` / `pool_max_conn` 注释条目，本地可覆盖

- **monitor race defense** (`src/monitoring/health/checks.py`、`src/monitoring/manager.py`)
  - `check_cache_stats` 提取 `_collect_worker_stats` helper：对 `RuntimeError: dictionary changed size during iteration` 做 1 次重试（race 瞬时，通常成功）
  - `_safe_get_performance_stats` 在 `monitoring/manager.py` 同样对 `performance_stats` 路径做 1 次重试
  - 重试仍失败 → 降级为 DEBUG 日志 + 返回 `{}` / `None`，不再污染 errors.log、不中断当轮监控
  - **非 race 的 RuntimeError 仍会 ERROR 上报**（通过 `"dictionary changed size" in str(exc)` 精准匹配），不吞有意义错误

**测试覆盖**（2 个新测试文件，10 个测试用例全绿）：
- `tests/config/test_db_pool_config.py` —— 默认 1-20 / 显式覆盖 / ini 读取 / settings 字段传递
- `tests/monitoring/test_checks_race_defense.py` —— race 重试成功 / 持续 race 降级 / 非 race 错误仍 ERROR

**附带检查**：`tests/{monitoring,config,persistence,clients,indicators,readmodels}` **共 399 个测试全绿**。

**未做**（分析后确认非代码修复必需）：
- 深挖每个可能的 dict iteration race 源头 —— monitor 层 defense-in-depth 已经把影响降到 DEBUG 层级（不影响业务、不污染日志），而每个 hot-path dict 加锁会牺牲吞吐，收益小
- 业务层「Risk BLOCK 日志刷」—— live 账户保证金 + 策略 entry_spec 缺 SL 的业务决策，不属代码修复范围

---

## 0. 2026-04-21 生产事故根因修复（ADR-008）

**事件**：2026-04-20 20:19 启动的 live supervisor 进程组在次日 08 点被观察到 HTTP 000、indicator pipeline 死 8.5 小时、live-exec-a reconciliation 滞后 3 小时。根因链：

1. `src/api/{research,experiment,backtest}_routes/*.py` 在请求处理路径 `new TimescaleWriter(min_conn=1, max_conn=2)` + `repo.ensure_schema()`。22:44 pg `connection pool exhausted` 后 11 秒内连接池被 8 次重建（日志 `Database connection pool initialized: 1-2 connections × 8`）。
2. 23:07 `indicator writer thread` 在 pool 异常后静默死亡（daemon thread 无顶层 try/except），主进程继续跑但完全不处理任何 bar close，supervisor 无感知。
3. `client.health()` 默认 `attempt_initialize=True, attempt_login=True` —— MT5 假死时 HTTP 线程池（40）全部堵在 `mt5.initialize()`，观测性完全失效。

**本次修复（无任何兼容分支）**：

- **持久化 repo 单例化**（`src/persistence/db.py`、`storage_writer.py`、`repositories/__init__.py`）：
  - `TimescaleWriter` 新增 `research_repo / experiment_repo / backtest_repo` lazy `@property`（research/experiment 为消除循环依赖采用延迟 import）
  - `StorageWriter.ensure_schema_ready()` 启动时一次性调 3 个 repo 的 `ensure_schema()`
  - `src/api/deps.py` 新增 `get_research_repo / get_experiment_repo / get_backtest_repo` 3 个 getter，走 `container.storage_writer.db.<repo>`
  - 3 个 API routes 的 `_get_xxx_repo()` / `get_backtest_repo()` 函数改为转发到 `deps`，**完全删除请求路径里的 `new TimescaleWriter` 与 `ensure_schema` 调用**，同时去掉 `backtest_routes/execution.py` 多余的 `_cached_backtest_repo` 全局缓存
  - `research_routes/routes.py` 中 `ExperimentRepository(repo.writer)` 的手工构造也改为 `deps.get_experiment_repo()` —— 完全收敛到单一 writer

- **indicator 运行时线程 fail-fast**（`src/indicators/runtime/event_loops.py`）：
  - 新增模块级 `_on_thread_crash(thread_name, exc)`：生产调 `logger.critical + os._exit(1)`，测试 monkeypatch 替换为收集 stub
  - 四个主循环（`run_event_loop / run_intrabar_loop / run_event_writer_loop / run_reload_loop`）拆成 `run_*_loop` 外壳 + `_run_*_loop_body` 实现体，外壳顶层 `try/except BaseException` → `_on_thread_crash`
  - 禁止"except 后重入循环"——静默吞异常是事故核心成因
  - ADR-004 的 `_any_thread_alive()` 保留不变（stop 流程用），不再做冗余 runtime liveness check（fail-fast + supervisor 足矣）

- **`/health` 切换为只读**（`src/clients/base.py`、`src/readmodels/runtime.py`）：
  - `MT5BaseClient.health()` 签名 `(self, attempt_mt5_reconnect: bool = False)`，默认透传 `attempt_initialize=False, attempt_login=False`
  - `RuntimeReadModel` 的 `inspect_session_state(...)` 也改 `False`
  - 重连职责明确归还给 `BackgroundIngestor` 错误恢复（它本来就有）与 `src/ops/mt5_session_gate.py` / `live_preflight.py` 启动 preflight（这两处保留 `attempt_initialize=True`）

**测试覆盖**（3 个新测试文件，10 个测试用例全绿）：
- `tests/clients/test_health_readonly.py` —— 验证 `health()` 默认不触发 MT5 重连，显式 `attempt_mt5_reconnect=True` 才 opt-in
- `tests/persistence/test_writer_repo_singletons.py` —— 验证 3 个 repo @property lazy 单例 + `ensure_schema_ready` 调所有 repo
- `tests/indicators/test_event_loops_failfast.py` —— 验证 4 个主循环异常被正确转发到 `_on_thread_crash`

**附带检查**：tests/clients + tests/persistence + tests/indicators + tests/readmodels + tests/api + tests/trading + tests/app_runtime **共 837 个测试全绿**，无回归。

**未动的相关问题**（分析后确认非本次必修）：
- 风控拒绝死循环 —— 追踪发现 `PreTradeRiskBlockedError` 已在 `src/trading/execution/eventing.py:706` 被 `trade_executor.process_event` 捕获转成 `status='skipped'`，intent 已正确进入终态不会 re-claim。日志"循环"实为上游策略每 bar 都在产生新的 entry intent（所有新 intent 被同理由拒）。真正的修复在信号产出侧（策略冷却），超出本次故障根因修复范围。
- pool size 配置化（`max_conn=10` 默认值配置化）—— 本次根因是"每请求各自起小 pool"而非"单一主 pool 不够大"，配置化无必要；主 pool 默认 10 足够。

详见 `docs/design/adr.md` ADR-008。

---

## 1. 2026-04-20 修复更新（P9 全套 + API 治理 4 项）

本轮新增前端读侧支撑能力，**未引入任何边界泄漏**，所有跨域调用走公开端口（ADR-006 合规）。详见 [TODO.md](../TODO.md) "P9 完成快照"。

**新增组件 / 公开端口**：

- **WorkbenchReadModel**（`src/readmodels/workbench.py`）— 单账户执行工作台聚合读模型（10 块：execution / risk / positions / orders / pending / exposure / events / relatedObjects / marketContext / stream）。仅依赖 RuntimeReadModel 公开方法 + MarketDataService.get_quote()，不读任何 `_` 私有属性。`compute_source_kind()` 是纯函数（可被其他端点复用做 source 聚合）。每次 build() 入口 reset `_build_cache`，6 个 builder 共享 3 个底层调用（扇出减半），WorkbenchReadModel 在 `deps.py` 是 per-request 构造（无并发风险）。

- **MutationActionResultBase**（`src/api/schemas.py`）— 13 字段共享基类（accepted/status/action_id/command_id/audit_id/actor/reason/idempotency_key/request_context/message/error_code/recorded_at/effective_state）。`src/api/trade_routes/view_models.py` 与 `src/api/monitoring_routes/view_models.py` 都从 schemas 导入，旧 `RuntimeActionResultView` 保留为别名向后兼容。

- **AdmissionWriteback Listener**（`src/app_runtime/factories/admission_writeback.py`）— `make_skip_listener` / `make_intent_published_listener` / `multicast` 三个工厂函数。listener 在 `factories/signals.py` 通过 multicast 链入 `on_execution_skip`（不取代 SignalQualityTracker），通过 `pipeline_event_bus.add_listener()` 监听全部事件后内部按 `event.type == "intent_published"` 过滤。**listener 内部 try/except 异常隔离**——DB 写失败不影响执行链。executor 接口（`on_execution_skip(signal_id, reason)`）签名不变，**未读取 SignalRepository 私有属性**。

- **FreshnessConfig**（`src/config/models/freshness.py`）— Pydantic 模型集中管理 8 块阈值。WorkbenchReadModel 接受可选 `freshness_config` 注入覆盖，未传则用 `default_freshness_config()`。`DEFAULT_FRESHNESS_HINTS` module-level 常量保留作为派生值（向后兼容旧导入）。

- **SSE envelope schema 1.1**（`src/api/trade_routes/state_routes/stream.py`）— `_EVENT_METADATA` 表为 13 个事件类型注册 `(tier, entity_scope, changed_blocks)` 元数据；未知事件 fallback 到 `_PIPELINE_DEFAULT_METADATA`。`next_stream_envelope()` 加 3 个可选覆盖参数（tier / entity_scope / changed_blocks），调用方可显式覆盖默认值（用于 pipeline 透传等特殊场景）。**`state_version` 字段保留向后兼容**，新增 `snapshot_version` 与之同值。

**关键边界变化**：

- **signal_events 表新增 5 列**（actionability / guard_reason_code / guard_category / priority / rank_source）+ 2 索引（priority DESC NULLS LAST + actionability partial）。通过 `POST_INIT_DDL_STATEMENTS` 启动期 `ALTER TABLE IF NOT EXISTS` 自动迁移，PG 11+ instant operation 不锁表。INSERT_SQL 不变（新字段 NULL 默认），UPDATE 由独立 `UPDATE_SIGNAL_ADMISSION_SQL` 处理。**旧记录 NULL 字段 API 返回 null**，向后兼容前端。

- **`/v1/positions` `/v1/orders` 标 FastAPI `deprecated=True`**（OpenAPI schema 自动告警）+ metadata 含 `deprecation` 块（successor / sunset 2026-06-01 / reason）。**端点未删除**（兼容期 1 个月），消费者可继续调用，但前端 codegen 会显示 deprecation。

- **新增 `/v1/execution/workbench` 端点**——单账户聚合，9 块 contract。`account_alias` 不匹配返回 404 `ACCOUNT_NOT_FOUND`（保持单实例=单账户语义，跨账户聚合是 BFF 职责）。

- **新增 `/v1/signals/diagnostics/strategy-audit` 端点**——backlog P0.3 单端点替代 4 路拼接（strategy-conflicts + outcomes/winrate + aggregate-summary + admin/performance/strategies）。

**测试覆盖**：2374/2374 全套测试全过（+~100 个本轮新增）。无回归。

**未决跟进**（不阻塞前端）：
- 多账户 `contributors[]` 聚合：BFF 职责，后端不实现
- SSE 事件缓冲 + Last-Event-ID 续传：前端有 `state_snapshot` 兜底，性价比低，留给 Phase 4+
- `/v1/positions` `/v1/orders` 实际下线：2026-06-01 后

---

## 1. 2026-04-11 ~ 2026-04-12 修复更新

本轮已直接处理并验证以下启动阻塞项：

补充更新（2026-04-17）：Telegram 通知模块 Phase 1

- **HealthMonitor 新增 `set_alert_listener` 外部订阅端口，仅在 warning/critical 转换为 active_alerts 时同步回调，listener 异常被 try/except 隔离（不污染 monitor 自身状态）；NullHealthMonitor 同步实现为 no-op 保持接口兼容**  
  `record_metric()` 路径原本只把告警写入 `self.active_alerts` 与 SQLite 历史表，没有对外通知机制。本轮最小侵入加入 `_alert_listener: Optional[Callable]` + `set_alert_listener()` 方法（约 15 行），在告警升级点同步调用 listener，由通知模块负责将 alert dict 映射成 NotificationEvent 并分发。listener 异常被捕获后仅记日志，不回退 active_alerts 的写入。该改动只扩展公开端口、不更改 monitor 内部告警判定逻辑，符合 ADR-006（不读私有属性、只通过公开 setter 接入）。

- **AppContainer 新增 `notification_module` 字段（Optional[NotificationModule]）；AppRuntime 在启动尾段 `_start_notifications()`，关闭顺序中先于 monitoring/ingestor 拆除以确保 bus/health listener 及时解绑，防止僵尸回调**  
  `builder_phases/notifications.py` 作为独立 Phase 5.5 在 read_models 之后、studio 之前执行。工厂 `create_notification_module()` 会在缺少 `bot_token` 或 `default_chat_id` 时返回 `None`，`NotificationModule` 不被构建，container 字段保留为 `None`，运行时路径全量跳过通知链路——不会影响未配置推送的实例。`NotificationModule.stop()` 只停 worker + 解 listener，`close()` 额外关闭 outbox SQLite 句柄；运行时 toggle 调 `stop/start` 即可在线切换而不丢失 outbox 内容。

- **`src/notifications/` 引入 6 层开关控制但不改任何业务模块：物理（无 token → 不构建）/ 全局（[runtime] enabled）/ 运行时（`/v1/admin/notifications/toggle` API）/ 事件级（[events] `<name>` = off）/ 实例级（[event_filters] suppress_info_on_instances）/ 最终防刷（dedup TTL + rate limit）**  
  模块自身遵守 L2 持久化（SQLite outbox + DLQ 崩溃恢复）与 ADR-005（worker 线程 join 超时后保留引用），与 StorageWriter 的线程模式对齐。详见 `docs/design/notifications.md`。

补充更新（2026-04-13）：

- **本地单账户执行适配路径已并入同一 terminal 结果发射职责，不再由 queue worker 悄悄吞掉正式结果**  
  上一轮虽然把 execution intent 主链的 terminal 事件所有权收口到了 `ExecutionIntentConsumer`，但 `TradeExecutor._exec_worker()` 仍只是调用 `process_event()` 后直接丢弃返回值。这意味着单账户/本地测试场景下通过 `on_signal_event()`、`on_intrabar_trade_signal()` 进入的事件，虽然会被真正执行或跳过，却不会留下统一的 `execution_succeeded / execution_skipped / execution_failed` 终态，形成“主链干净、本地路径失真”的残留双轨。现已把“执行结果解释 + terminal pipeline 事件发射”抽到 `src.trading.execution.eventing` 的共享端口中，由 `ExecutionIntentConsumer` 与本地 queue worker 共用；执行器继续只负责返回正式结果，queue/consumer 这类交付层再根据结果发 terminal 事件，职责边界恢复一致。  

- **`duplicate_signal_id` 已回到统一 reject 合同，不再保留一条只记 skip 计数、不写完整阻断事实的历史旁路**  
  `pre_trade_checks.run_pre_trade_filters()` 之前对 `signal_id` 幂等冲突使用的是 `_notify_skip_helper()` 直接返回，这让它不同于其他 pre-trade reject：不会稳定生成 `execution_blocked + admission_report_appended` 这组阶段性事实。现已改为与其他 reject 一样统一走 `reject_signal()`，因此 duplicate guard 与 quote stale、position limit、governance reject 等同属一套正式拒绝合同，trace 与 admission 视图不再为这条历史残留分支做例外解释。  

- **execution intent 主链的 terminal 事件所有权已收口到 `ExecutionIntentConsumer`，不再靠执行器本地和 consumer 双方各发一遍**  
  上一轮虽然把 `auto_trade_disabled` 等跳过路径补成了结构化结果，但更深一层的问题仍在：`pre_trade_checks.reject_signal()`、`execute_market_order()`、`submit_pending_entry()` 这些执行器内部 helper 还会直接发 `execution_skipped / execution_failed`，随后 `ExecutionIntentConsumer` 又会根据 `process_event()` 的返回值再补一条 terminal 事件，导致同一条 intent 在 trace 里可能同时出现“本地 failed/skipped”和“consumer 再次 failed/skipped”的重复终态。现已按职责边界重构为单一所有权：执行器内部只保留阶段性事实（如 `execution_blocked`、`execution_submitted`、admission report），并统一返回正式 `completed / skipped / failed` 结果；真正的 terminal pipeline 事件只由 `ExecutionIntentConsumer` 发一次。这样 execution intent 的生命周期与 trace 终态重新对齐，不再依赖额外 suppress 标记或兼容字段。  

- **confirmed/disabled 跳过分支已统一返回结构化 skipped result，live trace 不再只剩 `reason=null`**  
  之前虽然 `intrabar` 的大部分门禁已经开始返回 `status/reason/category/details`，但 `confirmed` 路径里仍有几类关键分支直接 `return None`：例如 `auto_trade_enabled=false`、confirmed 预交易过滤链拒绝、trade params 不可计算、spread-to-stop 超限，以及 intrabar 仓位被 confirmed 验证/保持不动。这会让 `ExecutionIntentConsumer` 只能按“结果为空”粗略落成 `execution_skipped`，summary reason 继续显示为 `unknown/null`。现已把这些分支统一收口为正式 skipped result；对本地未经过 `reject_signal()` 的 `auto_trade_disabled / intrabar_position_*` 场景，还补了统一的 execution log + skip 计数更新，但不重复发第二条 pipeline skip 事件。这样当前 live 配置仍处于 `auto_trade_enabled=false` 时，confirmed 与 intrabar canary 也能在 `/v1/trade/traces` 里稳定看到正式 skip reason。  

- **`execution_skipped` 现在会保留正式 skip reason，不再被 intent consumer 覆盖成 `unknown`**  
  之前 `ExecutionIntentConsumer` 只按“`result is None` / 非空”粗略判定 `completed|skipped`，并在 trace 中追加一条不带原因的 `execution_skipped`。这会把执行侧已经明确知道的 `trade_params_unavailable`、`intrabar_gate_blocked` 等事实抹平成 `reason=unknown`，降低 `/v1/trade/traces` 与 gate audit 的可审计性。现已把 skipped 结果收口为正式结构：执行器会在 intrabar 关键跳过分支返回 `status/reason/category/details`，consumer 会把这些字段透传到 pipeline event 顶层与 `result` 内，读模型也同步补了嵌套 reason 提取，确保 trace summary、gate audit 和 intent completion 看到的是同一套 skip reason。  

- **snapshot trace 的 fallback 已前移到指标发布层，`SignalRuntime` 不再在真实主链上自起 detached filter trace**  
  之前当 `SignalRuntime._on_snapshot()` 拿不到 `snapshot_source.get_current_trace_id()` 时，会直接生成新的 `uuid4` 写入 `signal_trace_id`。由于这一步发生在 `snapshot_published` 之后，最终会在目录里留下只有 `signal_filter_decided(filtered_pass)` 的 detached trace，看起来像“只有过滤事件、没有上游 bar/indicator/snapshot”。现已把 fallback trace ownership 收口到 `UnifiedIndicatorManager.publish_snapshot()`：如果指标发布时没有上游 trace，就在那里一次性分配 trace 并同时用于 `snapshot_published` 与 listeners；真实运行时因此至少会形成 `snapshot_published -> signal_filter_decided -> ...` 的同 trace 链，而不是继续由 signals 层临时补一个只有下游事件的新 trace。  

- **文档漂移已继续清理到“单策略 + execution intents + worker 执行”口径**  
  `signal-system.md`、`signals-dataflow-overview.md`、`intrabar-data-flow.md`、`README.md`、`architecture.md`、`adr.md`、`pending-entry.md` 已同步去掉或降级旧的 `TradeExecutor.on_intrabar_trade_signal()` / `voting_group` / direct listener 叙述，统一改回当前事实：`confirmed` 与 `intrabar_armed` 都经 `ExecutionIntentPublisher` 写入 `execution_intents`，再由 `ExecutionIntentConsumer -> TradeExecutor` 执行；vote/consensus 已退役，仅在少量历史说明处保留为“已移除语义”。  

- **`intrabar_armed_*` 已正式并入 `execution_intents` 主链，不再依赖本地 `TradeExecutor` 直连 listener**  
  此前 `confirmed` 已经统一走 `main -> ExecutionIntentPublisher -> execution_intents -> ExecutionIntentConsumer -> TradeExecutor`，但 `intrabar_armed_*` 仍只在装配了本地 `trade_executor` 的 runtime 上通过 `signal_runtime.add_signal_listener(trade_executor.on_intrabar_trade_signal)` 直接触发。这导致单账户场景可以盘中执行，而 `live-main + live-exec-*` 双实例场景下 `intrabar` 无法进入 worker 链。现已把 `ExecutionIntentPublisher` 扩展为同时接受 `confirmed_*` 与 `intrabar_armed_*` 两类可执行信号，并移除本地 intrabar 直连 listener，让 `confirmed / intrabar` 两条交易入口统一收口到 execution intent 交付面，避免继续保留一条 live 双实例下不可见的旁路。  

- **`intrabar_synthesis` 健康态已从“未初始化即 stale”拆分为 `warming_up / healthy / stale`**  
  之前 ingestor 的 intrabar synthesis 汇总在 `count=0`、尚未等到首个子 TF close 时，会直接把 parent TF 标成 `stale`，导致 H1/H4 这类触发周期较长的时间框架在启动初期看起来像“已经断更”。现已把 runtime health 聚合改为：从未合成过的目标明确标记为 `warming_up`，已有合成记录且超阈值才算 `stale`，其余为 `healthy`。`RuntimeReadModel.build_storage_summary()` 也同步补齐 `warming_up` 计数与整体状态投影，避免 `/health` 再把正常 warmup 窗口误报成真实故障。  

- **`intrabar` 的 intent 消费结果合同已修复，不再把“被门禁挡住”误记成 `execution_succeeded`**  
  在 `intrabar_armed_*` 接入 `execution_intents` 后，live canary 暴露出一个新的执行侧语义错误：`TradeExecutor.process_event()` 对 `scope=\"intrabar\"` 之前无论 `_handle_intrabar_entry()` 是否真正放行或下单，都会固定返回 `{\"status\": \"intrabar_processed\"}`。这会让 `ExecutionIntentConsumer` 把被 deployment / intrabar_synthesis / execution_gate 等门禁挡住的 intrabar intent 也统一记成 `status=completed`，并向 trace 发出 `execution_succeeded`，从而把“已消费但跳过”和“真实执行成功”混在一起。现已把 intrabar 路径改为返回真实执行结果：被任何门禁挡住或未产生下单动作时返回 `None`，只有真正进入执行结果时才向上返回 result；consumer 因而能把这类 canary 正确落成 `execution_skipped`，恢复 `intent -> execution` 生命周期的状态语义。  

- **启动期 `Spread/cost` 告警已从拍脑袋启发式收口为“仅在配置自相矛盾时告警”**  
  `src.app_runtime.builder` 之前仅凭 `base_spread_points` 与 `max_spread_to_stop_ratio` 的固定比例关系，就会在 `base=30 / ratio=0.33` 这类正常配置下持续刷出 “may be too tight” warning，既不基于真实止损距离，也不反映运行时实际拦单事实。现已改为：启动时只输出一条事实型的 execution cost gate 摘要；只有当 `max_spread_points` 或 `session_spread_limits` 明确低于 `base_spread_points`、形成内部自相矛盾时才告警，避免 observability 面继续被伪风险噪音污染。  

- **`signal_filter_decided` trace 已补齐 `evaluation_time`，时段冷却审计不再依赖 `recorded_at` 猜测**  
  本轮审计 `session_transition_cooldown:london_to_new_york` 时发现，部分 detached trace 只剩 `signal_filter_decided`，没有同 trace 的 `bar_closed`，导致只能看到持久化 `recorded_at`，而无法直接还原 filter 实际使用的 `event_time/bar_time`。现已在 pipeline event payload 中正式写入 `evaluation_time`（即 filter 真实判定时间）；后续查库、trace 目录投影或审计脚本都可直接按该字段判断是否落在 13:00 UTC handoff 窗口内，不再把“延迟落库”和“策略误判”混淆。  

- **已新增延后门禁审计 CLI，避免运行几天后再临时翻库拼 SQL**  
  由于系统刚上线时样本不足，`session_transition_cooldown_minutes`、`quote_stale`、`intrabar_synthesis_*` 等门禁是否“过严”不应立即下结论。现已新增 `python -m src.ops.cli.pipeline_gate_audit --environment live --days N`：它基于 pipeline trace 的只读投影汇总 `signal_filter` / `execution` 两侧的真实拦截分布，并按 `gate family / gate reason / day / timeframe / source` 输出。这样后续运行 3 到 5 天后，可以直接拿真实样本评估哪些门禁需要收紧，而不必再次临时拼接数据库查询。  

- **MT5 根级共享默认配置在实例上下文下已恢复生效**  
  `src.config.mt5` 之前在 `MT5_INSTANCE` 已设置时，连根级 `config/mt5.ini` / `config/mt5.local.ini` 也会误走到 `config/instances/<instance>/...`，导致 `server_time_offset_hours` 这类共享默认项在 `live-exec-a` 这类未重复声明的实例上失效，并持续退回运行时自动探测 offset。现已把根级层读取改为显式绕过实例上下文，只让实例层参与覆盖；新增回归测试覆盖“实例上下文下仍继承根级共享默认”这一合同，避免再出现“`live-main` 因实例重复配置看似正常、`live-exec-a` 却丢失共享默认”的隐蔽漂移。

- **`live-main` 的重复 MT5 offset override 已移除，根级 `config/mt5.local.ini` 恢复为唯一事实源**  
  在修复 MT5 loader 的根级继承 bug 后，`config/instances/live-main/mt5.local.ini` 中那条冗余的 `server_time_offset_hours = 3` 已删除，避免继续用实例层重复配置掩盖共享默认层回归。当前 `server_time_offset_hours` 的正式来源已回到根级 `config/mt5.local.ini`；若未来 live/demo 的 broker server offset 确实分叉，应通过根配置分层或新的正式环境级合同解决，而不是继续在单个实例文件里偷偷覆盖。

- **Vote/consensus 已从 runtime、backtest、配置与公开接口中正式移除**  
  当前信号主链路已收口为“单策略评估 → `no_signal` 或直接进入状态机/执行链路”，不再保留 `consensus fallback`、`voting_groups`、`standalone_override`、`strategy="consensus"` 或 `/signals/consensus/recent` 之类的公开语义。`config/signal.ini` 已删除对应 section，`signal.local.ini` 若仍保留旧 vote 配置会在启动时直接按配置漂移失败；trace 读侧则仅保留对历史 `voting_completed` 的最小归一化兼容。

- **交易命令消费者已修复 `close_position.volume` 与 `cancel_orders.magic` 转发缺失**  
  `OperatorCommandConsumer` 执行 `close_position` 时现已把队列 payload 的 `volume` 原样透传到 `command_service.close_position(...)`，避免“部分平仓请求退化为全平仓”；执行 `cancel_orders` 时也已补传 `magic` 过滤条件，避免“限定策略撤单退化为同品种全撤”。本轮同时新增消费者级回归测试，直接断言上述两个字段会被正确转发，防止后续重构再次回归。

- **`economic_calendar_staleness` 监控阈值已按最快启用的经济日历刷新路径收口**  
  之前 health monitor 固定把 warning 阈值设成 `stale_after_seconds / 2`，在 `release_watch_idle_interval_seconds = 1800`、`stale_after_seconds = 1800` 的配置下，会在服务仍然 `health_state=ok / stale=false` 时提前持续刷 warning。现已改为按“最快启用刷新路径的最大预期间隔”计算 warning 阈值：当前 `calendar_sync=21600 / near_term=0 / release_watch_idle=1800` 时，warning 与 critical 都对齐到 `1800s`，避免 idle release_watch 期间的误报型日志噪音；若未来重新启用 `near_term_sync=900`，warning 也会自动回落到 `900s`。

1. **demo/live 环境已收口为 topology 的唯一事实源，MT5 不再反推系统环境**  
   `load_db_settings()` 现已只按当前 topology environment 路由到 `db.live` / `db.demo`，不再从 MT5 账户字段二次推导；`RuntimeIdentity` 统一收口为 `instance_id / instance_role / live_topology_mode / environment / account_alias / account_key`。启动时会强制校验：`demo` 只允许 `single_account + main`，`live` 的 `multi_account` 下所有启用账户必须使用不同 terminal path。回测、research、experiment 入口也已改为默认读取当前环境数据库，不再写死 `live` 或隐式跟随旧单库。

2. **自动交易已统一收口到 `execution_intents` 队列，而不再保留“信号直接下单”双轨语义**  
   `main` 现在只负责写入 `execution_intents`，`executor`/单账户 main 再按 `target_account_key` claim 并执行；`signal_runtime -> trade_executor` 的直接监听已移除，改为 `ExecutionIntentPublisher + ExecutionIntentConsumer` 的正式交付面。当前一期策略分发来源固定为 `signal.ini` 中的 `account_bindings.<alias>.strategies` 静态绑定，`single_account` 下默认投递到当前账户，`multi_account` 下只投递到显式绑定账户。

3. **signal performance 与 execution performance 已拆成两条正式链路**  
   `SignalModule` 现在只消费基于 `signal_outcomes` 的 signal performance；账户真实成交侧则通过独立的 execution performance tracker 仅消费当前 `account_key` 的 `trade_outcomes`。warm-up 也已同步拆分为 `fetch_recent_signal_outcomes()` 与 `fetch_recent_trade_outcomes(account_key=...)`，避免不同 live 账户的真实盈亏再反向污染当前账户的信号 multiplier。

4. **账户事实表已补齐 `account_key`，运行态追踪补齐 `instance_id / instance_role`**  
   `auto_executions`、`trade_outcomes` 之外，本轮继续把 `trade_command_audits`、`pending_order_states`、`position_runtime_states`、`trade_control_state`、`position_sl_tp_history` 的写库契约补齐到 `account_key`；`runtime_task_status`、`pipeline_trace_events` 已补上实例维度，执行侧事件同时带 `intent_id` / `account_key`。现阶段仍保留 `account_alias` 作为展示字段与兼容过滤口径，但新的事实写入已经按稳定 `account_key` 输出，读模型/API 可逐步迁移到 key 口径。

5. **SL/TP 历史写入已从占位 alias 回到运行时真实账户语义**  
   `PositionManager` 过去写 `position_sl_tp_history` 时仍依赖空 `account_alias` 占位；当前已改为在运行时装配层基于 `RuntimeIdentity` 注入真实 `account_alias + account_key`，避免多账户下 SL/TP 审计继续丢账户归属。

6. **本轮改动已补齐定向回归测试**  
   已新增/扩展配置与交易侧回归，覆盖 `db` 分库路由、`validate_mt5_topology()` 的多终端冲突校验、`RuntimeIdentity` 的 `account_key` 生成、`ExecutionIntentPublisher/Consumer` 的单账户/多账户发布消费语义，以及 `TradingStateStore` / `TradingModule` 的 `account_key` 落库。定向执行通过：`tests/config/test_mt5_multi_account_config.py`、`tests/trading/test_execution_intents.py`、`tests/trading/test_state_store.py`、`tests/trading/test_trading_module.py`、`tests/readmodels/test_trade_trace.py`、`tests/api/test_trade_api.py`、`tests/api/test_monitoring_runtime_tasks.py`、`tests/readmodels/test_runtime.py`、`tests/config/test_signal_config.py`、`tests/app_runtime/test_signal_factory.py`。

7. **指标 durable event 消费断链已修复**  
  `IndicatorEventLoop` 已改为调用 `event_store.claim_next_events(...)`，真实启动时事件循环线程可正常存活，不再因接口名不匹配崩溃。

8. **`runtime_task_status` 持久化契约已统一并补库表迁移**  
  新增统一状态集合，覆盖 `ready / failed / idle / disabled / ok / partial / error / stopped` 等当前真实生产者语义；`TimescaleWriter.init_schema()` 现在会在启动时重建相关 check constraint，现有数据库不需要手工删表。

9. **经济日历 `session_bucket/status` 与 schema 漂移已修复并补库表迁移**  
  现已允许 `all_day / asia_europe_overlap / europe_us_overlap`，同时允许 `imminent / pending_release` 状态；真实启动后不再出现该表 check constraint 失败和因此进入 DLQ 的问题。

10. **`StorageWriter.stop()` 生命周期语义已修正**  
  线程 `join(timeout)` 后若仍存活，将保留线程引用并保持 `is_running()` 为真，避免误判组件已停止。

11. **`/health` 已扩展为链路级组件视图**  
   除市场与交易摘要外，现在还会返回 `storage_writer / indicator_engine / signal_runtime / trade_executor / pending_entry_manager / position_manager / economic_calendar` 运行状态，便于直接判断“采集 → 指标 → 信号 → 执行”链路是否在线。

12. **日志目录已统一回到项目根目录 `data/`**  
   `src.entrypoint.web` 的相对 `log_dir` 解析现已锚定仓库根目录，不再向 `src/entrypoint/data/logs` 写入；本轮也已把历史遗留日志迁移到根目录 `data/logs/` 下的 `legacy-src-entrypoint-*.log`。

13. **系统 readiness 盲区已补齐：采集线程现在纳入就绪探针**  
   `/monitoring/health/ready` 过去只看 `storage_writer` 与 `indicator_engine`，即使 `BackgroundIngestor` 已退出也可能误报 `ready`。本轮已把 `ingestion` 纳入同一探针，`RuntimeReadModel.build_storage_summary()` 也同步把 `ingest_alive=false` 视为 `critical`。

14. **`PipelineTraceRecorder` 生命周期语义已修正**  
   该组件此前在 `stop()` 的 `join(timeout)` 后会无条件清空线程引用，且监听器注册失败时仍会假装启动成功。本轮已改为：监听器挂接失败立即抛错；线程未退出时保留引用并维持真实运行态，避免 observability 组件假死但状态看起来正常。

15. **`calendar` 包顶层导入导致的循环依赖已修复**  
   `src.persistence.schema.economic_calendar` 引入 contract 时会先执行 `src.calendar.__init__`，此前这里立即导入 `service`，从而形成 `db -> schema -> calendar -> service -> db` 的循环。现已改为懒加载导出，不再阻塞采集/持久化相关测试与运行时装配。

16. **测试资产已做一轮去噪和契约收口**  
   已删除 7 个只有 `pytest.mark.skip`、实际不产生任何覆盖的历史空文件；同时移除了 2 个“打印 + 吞异常”的脚本式伪测试。`tests/data/test_data_layer.py` 也已改造成无外部 DB 依赖的真实单元测试，`tests/api/test_monitoring_config_reload.py` 则同步对齐当前 404 契约，避免旧口径误报失败。

17. **`SignalRuntime` 测试已从私有子方法绑定收口到行为契约**  
   原 `tests/signals/test_runtime_submethods.py` 直接断言 `_dequeue_event / _is_stale_intrabar / _apply_filter_chain / _detect_regime` 等私有实现细节，重构成本高而行为保护有限。本轮已删除该文件，并把仍有价值的覆盖迁移为 `tests/signals/test_signal_runtime.py` 中的行为测试，直接校验队列反饥饿、stale intrabar 丢弃、filter 统计和 stop 后队列清理。

18. **`docs/` 已完成一次“运行时真相 / 审计治理 / 方案规划”分层审计，并新增单一导航入口**  
   已新增 `docs/README.md` 作为文档入口，`architecture / full-runtime-dataflow / signals-dataflow-overview / intrabar-data-flow / entrypoint-map` 已明确标注为当前实现真相文档；`signal-system / research-system / next-plan / r-based-position-management` 等文档已补充“设计参考/规划”定位说明，避免再把方案稿直接当作当前运行结论。`architecture.md` 也已把本应属于专题设计的参数表和方案细节收口到对应专题文档。与此同时，`docs/` 下运行时流图已统一为 Claude 风格 ASCII，不再保留 Mermaid 图。

19. **系统启动巡检与 live canary 已收口成独立 runbook**  
   已新增 `docs/runbooks/system-startup-and-live-canary.md`，把 `live_preflight`、启动后 5 分钟巡检、休盘/开盘日志判读、以及开盘窗口的系统级 live canary 步骤统一成一套固定流程。`entrypoint-map.md` 与 `docs/README.md` 已同步改为链接到该 runbook，避免启动入口文档和运行时文档再各自维护一套巡检说明。

20. **回测已正式区分 `research` 与 `execution_feasibility` 两种执行语义**  
   本轮没有拆分指标、策略、过滤或 regime/voting 内核，仍然复用同一套生产数据指标流；变化只发生在“信号如何转成可成交动作”这一层。`BacktestConfig` 新增 `simulation_mode`，CLI / API / `config/backtest.ini` 已能显式指定；`BacktestResult` 新增 `execution_summary`，会输出当前模式、accepted entries、rejected entries 与 rejection reasons。`research` 模式允许理论子最小手数仓位继续用于策略研究，`execution_feasibility` 模式则会在最小手数不可成交时明确拒单，避免再把研究回测误当成上线可执行性结论。

21. **手工运行产物目录已收口到 `data/artifacts/`**  
   根目录平级 `runtime/` 过去混放了回测 JSON、压测日志、启动排查 stdout/stderr 等人工执行产物，语义上与正式运行期目录 `data/` 并行，容易形成第二套“事实源”。本轮已把这类文件迁到 `data/artifacts/`，并在运行时流图/runbook 中明确：`data/` 是唯一运行期根目录，`data/artifacts/` 只承载手工回测、压测、排障输出，仓库根目录不再保留 `runtime/`。

22. **Research feature → shared indicator 的半自动晋升链路已落地第一版**  
   本轮新增了 `FeatureCandidateSpec / IndicatorPromotionDecision / FeaturePromotionReport`，并把 research feature registry 的元数据扩展为 `formula_summary / source_inputs / runtime_state_inputs / live_computable / compute_scope / bounded_lookback / strategy_roles / promotion_target_default`。`MiningRunner` 现在可直接输出 feature candidate 工件与 promotable 过滤结果；首个真实晋升指标 `momentum_consensus14` 已注册进 `config/indicators.json`，并接入 `structured_breakout_follow` 作为共享动量一致性确认因子。与此同时，回测验证报告已能携带 `feature_candidate_id / promoted_indicator_name / strategy_candidate_id / research_provenance`，用于把 research → indicator → strategy 的证据链真正串起来。

23. **`src/research` 已按职责边界重组为 `core / analyzers / features / strategies / orchestration`**  
   过去 `src/research` 顶层同时平铺 `runner.py`、`data_matrix.py`、`feature_candidates.py`、`candidates.py`、`models.py` 等文件，公共基础、feature 路径和 strategy 路径混在同一层，包边界不清晰。本轮已把公共基础能力收口到 `src/research/core/`，将 research feature / indicator promotion 路径收口到 `src/research/features/`，将 strategy candidate 发现收口到 `src/research/strategies/`，并把编排入口迁到 `src/research/orchestration/runner.py`。共享统计分析器仍保留在 `src/research/analyzers/`，避免按“指标版/策略版”复制两套证据引擎。

24. **`docs/research-system.md` 已补齐 research 模块职责、流程和关键文件作用说明**  
   在目录重组之后，`research-system.md` 已同步新增“模块职责总览”和“关键文件职责表”，明确写清 `orchestration / core / analyzers / features / strategies` 五层的输入、输出和边界，并把 research 的两条正式分支整理为“feature/indicator 晋升路径”和“strategy candidate 晋升路径”。这样后续再看 `src/research/*` 时，不需要再从代码倒推“谁负责编排、谁负责证据、谁负责候选工件”，减少继续演化时的边界漂移风险。

25. **首个真实 promoted indicator 已接入受限 consumer strategy，并补齐 research / execution / WF 证据链**  
    本轮没有继续把 `momentum_consensus14` 硬塞到休眠策略里，而是新增了 `structured_trend_h4_momentum` 作为 `StructuredTrendContinuation` 的受限变体：复用同一套结构化策略骨架，只在 `why` 层接入 `momentum_consensus14`，并通过 `strategy_deployment` 明确收口为 `paper_only + tf_specific + locked_timeframes=H1 + locked_sessions=london,new_york`。同时，`ValidationDecision` 已修正为支持 `research backtest result + execution_feasibility result` 双结果输入，不再混用一份回测结果承担两种语义；`walkforward_runner` 也已修复对旧字段 `is_result/oos_result` 的错误引用，并把 split 详情落盘。当前真实产物表明：该指标的 promotion 成立，但首个 downstream strategy consumer 结论仍为 `refit`，主因是研究回测样本不足、执行可行性下最小手数全部拒单，以及 WF 一致性仅 40%。

26. **QuantX 前端消费契约已从“多接口手工拼装”收口到正式分页/目录/流式接口**  
    本轮围绕 `docs/quantx-backend-backlog.md` 与 `docs/design/quantx-trade-state-stream.md`，把原先只适合后台排查的只读接口升级为前端可直接消费的正式契约：`/signals/recent` 现已支持 `direction/status/from/to/page/page_size/sort`；`/trade/command-audits` 新增 `symbol/signal_id/trace_id/actor` 与时间窗口分页；`/trade/traces` 提供 trace 目录视图；`/trade/state/stream` 提供统一 SSE 状态流（`state_snapshot`、`position_changed`、`order_changed`、`pending_entry_changed`、`alert_raised/resolved`、`command_audit_appended` 等事件）。对应的 `TradingFlowTraceReadModel` 也已补齐 trace 摘要状态、目录列表和详情关联事实，减少了前端直接拼多条后端读模型、探测隐式状态的边界泄漏；本次没有新增兼容别名路径，而是把缺失的查询/目录能力补成正式端口。

27. **QuantX 控制闭环已补上“统一动作结果 + 审计 ID + SSE 关联”第一版正式契约**  
    本轮进一步把 `POST /trade/control`、`POST /trade/runtime-mode`、`POST /trade/closeout-exposure` 从“只返回状态快照”升级为统一动作结果模型，正式返回 `accepted / status / action_id / audit_id / actor / reason / idempotency_key / recorded_at / effective_state`，并支持 `request_context`。后端会把同一 `action_id` 写入命令审计，再由 `/trade/state/stream` 的 `trade_control_changed / runtime_mode_changed / closeout_started / closeout_finished / command_audit_appended` 一并带出，前端不再需要靠时间接近性去猜“哪条状态变化对应刚才哪个按钮”。与此同时，`trade_control` 状态快照已优先走 live state 而非旧持久化快照，减少了控制后立刻读取 `/trade/state` 与 SSE 时状态源不一致的边界泄漏。

28. **QuantX 控制类 mutation 已补成真正的服务端幂等，而不再只是透传 `idempotency_key`**  
    本轮把 `POST /trade/control`、`POST /trade/runtime-mode`、`POST /trade/closeout-exposure` 的幂等能力从“请求/响应里有 `idempotency_key` 字段”推进到正式服务端语义：应用层新增独立的 operator action replay 服务，按 `command_type + idempotency_key` 收口短期内存去重，并在进程重启后回退到命令审计中查找最近一次已记录结果；同键同请求会直接回放原始动作结果，同键不同请求会返回显式冲突错误，而不是再次执行控制动作。这样前端的重试、双击和网络重放不再依赖时间接近性猜测，也避免把幂等逻辑散落在 3 条路由各自维护。当前实现没有引入兼容别名字段或第二套老路径，而是把 replay/冲突语义补成 `TradingCommandService` 背后的正式能力。

29. **QuantX 手动平仓/撤单 mutation 已接入同一套 operator action replay 边界**  
    本轮继续把 `POST /close`、`POST /close_all`、`POST /close/batch`、`POST /cancel_orders`、`POST /cancel_orders/batch` 从“原始 MT5 结果直出”升级为统一动作结果模型，正式返回 `accepted / status / action_id / audit_id / actor / reason / idempotency_key / recorded_at / effective_state`，并复用同一套 `command_type + idempotency_key` 回放/冲突拒绝语义。实现上没有再为这几条路由单独复制一套幂等逻辑，而是让 `TradingModule` 在 `_execute_command` 里直接产出 operator action response，并把结果同步写入 replay cache 与命令审计；同时，operator replay 指纹已去掉 `account_alias` 这类作用域内生字段，避免 API 请求体与审计载荷仅因账户上下文字段不同而被误判成冲突。这样前端控制台的手动平仓、批量平仓和撤单操作终于与控制类 mutation 落在同一条正式动作合同上，而不是继续保留“有些按钮可重试，有些按钮只能靠前端自己防重”的双轨语义。

30. **`pending-entry cancel` 与 `backtest/run` 已补进统一 action contract，而不再直出裸布尔/裸 job**  
    本轮继续把 `POST /monitoring/pending-entries/{signal_id}/cancel`、`POST /monitoring/pending-entries/cancel-by-symbol` 与 `POST /backtest/run` 纳入同一组执行类 mutation 语义。`pending-entry cancel` 现在改为正式请求体，支持 `reason / actor / idempotency_key / request_context`，并复用交易命令审计背后的 operator action replay 边界；同键同请求会直接回放原始取消结果，同键不同请求会显式冲突拒绝。`backtest/run` 则把动作状态拥有方收口到 `BacktestRuntimeStore`：同样支持 `actor / reason / idempotency_key / request_context`，返回统一的 `accepted / status / action_id / audit_id / message / effective_state`，并在 runtime store 内按 `command_type + idempotency_key` 做提交回放/冲突拒绝，避免网络重试或双击重复生成多条回测任务。与此同时，共享的 action contract helper 已从 `trade_routes/common.py` 抽到 `src/api/action_contracts.py`，后续再扩展其它危险操作时不需要继续复制一套字段归一化与 replay 响应拼装逻辑。

31. **单代码目录 + `config/instances/<instance>` 多实例配置与 `instance/supervisor` 入口已落地第一版**  
    配置底座已支持在共享 `config/*.ini` 之上叠加 `config/instances/<instance>/*.ini` 与实例级 `.local.ini`；`load_config_with_base()` / `get_merged_option_source()` 已能按实例名解析最终配置来源。新增 `src.entrypoint.instance` 与 `src.entrypoint.supervisor` 后，同一代码目录下可通过 `python -m src.entrypoint.instance --instance live-main` 启动单实例，也可通过 `python -m src.entrypoint.supervisor --environment live`（或 `--group live`）按 `config/topology.ini` 拉起 `main + workers` 进程组，不再依赖复制多份代码目录来承载多账户部署。

32. **账户自治风控已补齐正式当前态投影，`main` 只读聚合而不再以本地对象假装管理其他账户**  
    新增 `account_risk_state` 表与 `AccountRiskStateProjector`，由每个账户执行拥有者基于本地 `trade_control / circuit breaker / margin_guard / pending / positions / runtime_mode / quote freshness` 独立计算并写出当前风险态；`RuntimeReadModel` 与 `/trade/accounts` 现已优先读取正式投影，而不是探测当前进程里是否恰好持有某个本地风控对象。与此同时，executor 角色的 runtime registry 已停止启动共享采集、共享指标计算、信号运行时和 paper trading，只保留账户本地执行、持仓保护、风控投影与 trace，边界上正式收敛为“`main` 做共享计算，账户实例做本地执行与风控”。

33. **实例配置模型已进一步收口：只有 `mt5/market/risk` 允许实例级覆盖，其余配置保持共享事实源**  
    `config/instances/<instance>/` 现在不再被视为“什么都能放的第二套配置树”。配置加载层已显式限制只有 `mt5.ini`、`market.ini`、`risk.ini` 参与实例级合并；`app.ini`、`db.ini`、`signal.ini`、`topology.ini`、`economic.ini`、`ingest.ini`、`storage.ini`、`cache.ini` 等都保持根配置唯一事实源，即使实例目录下存在同名文件也不会被加载。这样可以避免实例配置再次演化成第二套系统，把角色/拓扑/策略/数据库等共享语义重新分叉到实例目录。

34. **多实例本地运行态与日志已按实例自动隔离，消除了共享 `data/` 污染风险**  
    之前多实例虽然已经共享同一代码目录，但 `health_monitor.db`、`signal_queue.db`、`events.db`、`mt5services.log` 等仍默认写到同一个 `data/` 与 `data/logs/`，存在运行态互相覆盖和日志混写风险。当前已改为命名实例自动隔离到 `data/runtime/<instance>/` 与 `data/logs/<instance>/`；`instance` / `supervisor` 启动下不再需要手工为每个实例额外改 `runtime_data_dir/log_dir`，也避免了多实例下本地 SQLite/WAL 与日志文件继续互相污染。

35. **入口日志现已补齐 `environment / instance / role` 上下文，多实例控制台输出不再完全不可判读**  
    `src.entrypoint.web` 与 `src.entrypoint.supervisor` 现已统一通过入口级 LogRecord context 注入，把 `environment / instance / role` 收口到每条日志记录，而不是只在启动首行打印实例名。这样多实例并行时，控制台输出与 `data/logs/<instance>/` 文件日志的内容语义保持一致；`main` 在 single-account 与 multi-account 下仍写入同一个 `data/logs/live-main/`，差异只体现在日志上下文和 topology，而不是路径漂移。

36. **账户风险投影中的 `quote_stale` 已从“API 级 1 秒 stale”收口为“执行侧行情失联”语义**  
    开盘实测中，XAUUSD 的真实 quote age 常落在 `1.1s ~ 1.9s`，而 `app.ini[limits].quote_stale_seconds = 1.0` 原本同时被 API 与 `AccountRiskStateProjector` 复用，导致 `account_risk_state.quote_stale` 在行情健康时也长期误报。当前已将 projector 改为显式使用执行侧阈值：至少 `3s`，并同时参考 `quote_stale_seconds` 与 `stream_interval_seconds` 的 3 倍；`trade/state` 中也新增 `metadata.quote_health.age_seconds / stale_threshold_seconds` 供巡检判读。这样 API 的紧阈值 stale 提示仍保留，但账户风控投影不再把正常开盘报价误标成风险盲区。

37. **`intrabar` 合成 freshness 已收口为正式元数据合同与执行门禁**  
    过去 `intrabar` 支链虽然已经能由子 TF close 合成父 TF 当前 bar，但执行侧并不知道这根 intrabar snapshot 是“刚由哪根子 bar 合成的、已经多久没更新、是否已断更”，因此 `main -> intent -> executor` 多实例路径里无法基于同一事实源判断盘中 trigger 是否已经过时。当前已在 `MarketDataService.set_intrabar(...)` 正式缓存 `trigger_tf / synthesized_at / stale_threshold_seconds / last_child_bar_time / child_bar_count / count` 等 synthesis 元数据，并由 `SignalRuntime.build_snapshot_metadata()` 注入到 signal metadata；执行侧通过共享 helper 统一计算 `intrabar_synthesis` 健康度，若 `scope=intrabar` 且 metadata 缺失或超时，会直接以 `intrabar_synthesis_unavailable / intrabar_synthesis_stale` 阻断交易。与此同时，`/health.runtime.components.ingestor.intrabar_synthesis` 与 runtime storage summary 已补齐 `configured / status / stale / worst_age_seconds` 聚合，用于把“盘中 trigger 时效不足”从日志告警提升为正式可观测状态。

38. **Timescale schema 初始化已补串行门禁，避免双实例并发启动互相踩库对象**  
    本轮实机拉起 `live-main + live-exec-a` 时，发现两个实例同时进入 `StorageWriter.ensure_schema_ready()` 会偶发触发 `could not open relation with OID ...`，而顺序启动可恢复，说明问题不在固定坏库而在 schema 初始化竞争。当前 `TimescaleWriter.init_schema()` 已补为会话级 PostgreSQL advisory lock 串行执行：同一数据库上的第二个实例会等待前一个实例完成 `DDL + POST_INIT_DDL_STATEMENTS`，而不是并发修改 hypertable / 索引 / migration 对象。这样双实例 supervisor/instance 并发启动时，schema 初始化不再是随机炸掉的启动盲点。

本轮验证结果：

- `pytest` 相关回归测试已通过，新增了事件循环接口、schema 初始化迁移、StorageWriter 生命周期、日志路径、经济日历时间上下文等测试。
- 测试资产清理后，全量测试树已移除 skip-only 僵尸文件、脚本式伪测试，以及一组过度绑定 `SignalRuntime` 私有子方法的低价值测试，`passed` 数字更接近真实覆盖。
- 真实运行时启动已验证 `startup_ready=True`，指标事件循环、信号运行时、经济日历后台线程和存储写线程均能启动。
- 系统链路专项回归已通过 `56` 项，覆盖 `采集 -> 缓存 -> closed-bar 事件 -> durable event store -> readiness/health/readmodel -> trace recorder`。
- `docs/` 已完成一次分层审计，核心文档已明确“当前实现真相”与“规划/历史方案”的边界。
- 已新增系统启动与 live canary runbook，文档入口、启动入口和巡检流程现在有统一落点。
- 真实启动后的系统探针已验证返回：
  - `storage_writer=ok`
  - `ingestion=ok`
  - `indicator_engine=ok`
- 当前休盘场景下，系统线程存活与探针状态正常；仍存在 `market_data data latency critical` 告警，这属于休盘/无新鲜行情环境下的预期观测，不应与链路断裂混淆。
- 新增实例配置/入口相关定向回归已通过：
  - `tests/config/test_instance_config_overlay.py`
  - `tests/config/test_mt5_multi_account_config.py`
  - `tests/app_runtime/test_runtime_controls_account_topology.py`
  - `tests/trading/test_account_risk_projection.py`
- 回测结果口径已开始从单一“是否成交”改为显式区分“研究型回测”和“可执行性模拟”，后续 Paper Trading / Live Shadow 应继续沿用这套结果语义，而不是回到隐式兼容路径。
- QuantX 相关 API/readmodel/SSE 定向回归已通过 `42` 项，覆盖 `/signals/recent` 新分页契约、`/trade/command-audits` 过滤分页、`/trade/traces` 目录视图，以及 `/trade/state/stream` 的 `state_snapshot -> position_changed` 事件链。
- 控制闭环专项回归已进一步补到 `43` 项，新增覆盖 `trade/control`、`trade/runtime-mode`、`trade/closeout-exposure` 的统一动作结果契约，以及 `/trade/state/stream` 的 `trade_control_changed` 动作 ID 透传。
- 控制闭环与服务端幂等专项回归已通过 `53` 项，新增覆盖三类控制 mutation 的同键回放、冲突复用拒绝，以及 `TradingModule` 进程重启后基于命令审计的 operator action replay。
- 手动平仓/撤单扩展后的定向回归已通过 `74` 项，新增覆盖 `/close`、`/close_all`、`/close/batch`、`/cancel_orders`、`/cancel_orders/batch` 的统一动作结果契约、同键回放、冲突复用拒绝，以及 `TradingModule` 对手动平仓/撤单动作的内存回放与重启后审计回放。
- monitoring/backtest/trade 联合定向回归已通过 `90` 项，新增覆盖 `pending-entry cancel` 两条 mutation 的统一动作结果与冲突拒绝，以及 `/backtest/run` 的统一提交动作结果、同键回放、冲突复用拒绝与 `run_fields` 契约同步。
- `/v1/monitoring/runtime-tasks` 已收口为“默认当前实例作用域”，不再在 worker 侧混入其他实例或历史轮次的任务状态；当显式传入 `instance_id / instance_role / account_key / account_alias` 时，才切换到跨实例查询口径。

39. **Research 特征层已从单文件 God class 重构为 FeatureHub + 6 个模块化 Provider（feat/research-feature-providers）**  
    原 `src/research/features/engineer.py`（~1250 行，~21 个特征）承载了所有特征定义与计算逻辑，职责边界模糊且扩展成本高。本轮把它拆分为：`FeatureHub`（`hub.py`，纯编排入口）+ 6 个独立 Feature Provider（`temporal` ~33 / `microstructure` ~21 / `cross_tf` ~8 / `regime_transition` ~11 / `session_event` ~7 / `intrabar` ~5），总特征数从 ~31 扩展至 ~85，全部 numpy 向量化。  
    **本次改动如何减少边界泄漏**：特征计算从单文件 God class 拆分为 6 个职责清晰的 Provider，通过 `FeatureProviderProtocol` 接口解耦，`FeatureHub` 与各 Provider 之间不存在隐式状态共享；各 Provider 只依赖 `DataMatrix` 输入，不访问其他 Provider 的输出，从根本上消除了原 God class 中跨特征隐式依赖的边界泄漏风险。  
    **配置**：新增 `research.ini [feature_providers] enabled_providers` 控制 Provider 子集；BH-FDR 新增 `fdr_group_by = provider` 支持按 Provider 分组校正，避免跨维度特征稀释显著性阈值。  
    **CLI 扩展**：`--providers temporal,microstructure` 可指定只运行部分 Provider，便于调试和对比实验。  
    **未决兼容项**：无——旧 `engineer.py` 已被 `hub.py` 完整替代，不保留兼容别名或双轨入口。

40. **Research Feature Provider 首轮回归缺陷已修复（2026-04-16）**  
    针对 PR #47 的 Codex review 高优先级问题，本轮补齐了两处会导致“任务成功但无有效特征”的回归：  
    - `src/research/features/candidates.py` 已移除 `indicator_name == "derived"` 的硬编码过滤，`discover_feature_candidates()` 现在会按 `field_name` 识别 provider 命名空间特征（如 `temporal.*`、`microstructure.*`、`cross_tf.*`），避免新架构特征被静默跳过。  
    - `src/research/orchestration/runner.py` 已实现 `_prepare_extra_data()` 的正式跨 TF 数据加载：会按 Provider 声明的 `parent_tf_mapping + parent_indicators` 预加载父 TF DataMatrix，并注入 `parent_bar_times + parent_indicators` 给 `CrossTFFeatureProvider`，不再固定返回 `None`。  
    **本次改动如何减少边界泄漏/兼容分支**：未新增兼容双轨；直接把旧 `derived` 假设与“预留未实现”路径替换为正式 Provider 契约实现，保持 `FeatureHub.required_extra_data() -> Runner._prepare_extra_data() -> Provider.compute()` 单一链路闭环。  
    **验证状态**：已新增定向测试覆盖 provider 前缀特征候选识别与 cross-TF extra_data 准备行为；未引入临时兼容字段或历史别名。

仍需单独关注但不属于本轮代码阻塞项：

1. **外部经济数据源仍可能超时**  
   真实启动时 Jin10 请求出现过 `read operation timed out`，这是外部依赖可用性问题，不是当前 schema/线程契约问题。

2. **市场数据延迟告警需要结合交易时段判断**  
   当前环境在监控中仍会报 `data latency critical`，更像是运行时没有收到足够新鲜行情或目标市场不活跃，需要结合 MT5 连接状态和交易时段继续看，不建议把它和本轮启动故障混为一类。

3. **`/trade/state/stream` 目前仍是轮询快照 diff，尚未具备服务端事件重放缓冲**  
   当前 SSE 已能满足前端状态面板、流水线追踪与告警订阅，但 `Last-Event-ID` 只会返回 `resync_required`，不会做真正的断线续传。若后续要承载更长连接时长、标签页切换恢复或多前端实例游标恢复，需要补服务端 replay buffer / cursor store，而不是继续让前端自己做事件补洞。

4. **危险操作的统一动作合同仍未覆盖到所有长耗时/批处理入口**  
   当前已经接入正式 action contract / replay 语义的包括 `trade/control`、`trade/runtime-mode`、`trade/closeout-exposure`、`close`、`close_all`、`close/batch`、`cancel_orders`、`cancel_orders/batch`、`pending-entry cancel`、`backtest/run`。但 `backtest/optimize`、`backtest/walk-forward` 等同样会触发后台长任务的 mutation 仍然沿用旧的“裸 job 提交”合同；如果后续前端继续扩展实验/回测控制台，这些入口也应复用同一套动作结果与幂等边界，而不是继续在不同子系统保留两套提交语义。

## 1. 总体结论

当前系统已经完成从 legacy 策略到结构化策略的主体迁移，领域目录、运行时装配、信号链路、交易执行、持久化、回测与研究系统都已形成清晰分层。主要问题不在“缺模块”，而在迁移后的工程一致性：

1. **本机 local 配置会优先生效，当前已不再保留 legacy 投票组，但仍会改变结构化策略有效集合**。
2. **部分长期运行组件的 stop/start 生命周期保护不一致**，线程 join 超时后仍清空线程引用，存在重复启动后台线程的风险。
3. **装配层和 API/展示层仍访问若干私有属性/方法**，说明正式端口还不完整。
4. **千行级协调器仍然集中多种职责**，后续性能与并发问题会优先在这些文件中出现。
5. **策略有效性仍处在验证阶段**，当前样本量不足以支持实盘放大或过度调参。

---

## 2. P0 风险

### 2.1 `signal.local.ini` 仍是高优先级事实源，但结构化策略冻结方式已切换到正式部署合同

**当前状态**：
- 当前代码注册策略仍是 8 个结构化策略实例。
- `config/signal.local.ini` 依旧是高优先级事实源，仍可能改变本机有效策略集合。
- 但“全部 `regime_affinity = 0` 即冻结”的隐式语义已退役，当前要求通过 `[strategy_deployment.<strategy>]` 显式声明 `candidate / paper_only / active_guarded / active`。
- `structured_session_breakout` 与 `structured_lowbar_entry` 这类单 TF 候选，现已迁移为 `strategy_deployment` 合同，带 `locked_timeframes / locked_sessions / require_pending_entry / max_live_positions` 等护栏。

**影响**：
- local 覆盖不会出现在仓库默认配置中，但会直接改变本机运行时的有效策略集合。
- 当前缺少“启动摘要/状态端点”来明确标出哪些策略被 local 配置冻结，人工排查时容易把“无交易”误判成策略逻辑问题。

**剩余建议**：
- 在启动阶段输出 effective strategy summary：启用 TF、部署状态、是否 guarded、是否支持 intrabar。
- 对 ignored local 配置提供只读诊断端点或 preflight 检查，避免“本机覆盖改变行为但 API 不可见”。
- 若策略长期停留在 `candidate / paper_only`，需要补对应的研究 provenance、回测与 paper 证据，而不是把状态长期留在 local 文件里。

### 2.2 生命周期防护已有改进，但仍是分散实现，缺统一约束

**涉及位置**：
- `src/signals/orchestration/runtime.py`：`stop()` 在 `join(timeout)` 后直接 `self._thread = None`。
- `src/trading/positions/manager.py`：`stop()` 在 `join(timeout=5.0)` 后直接清空 `_reconcile_thread`。
- `src/trading/pending/manager.py`：`shutdown()` 在 monitor/fill worker join 后直接清空线程引用。
- `src/indicators/manager.py`：`stop()` 会记录未退出线程 warning，但随后仍清空线程引用。

**现状判断**：
- 当前这些组件大多已经改成“线程仍存活则保留引用并在下次 start() 再次等待”，明显优于早期实现。
- 问题不再是单个显式 bug，而是这套策略仍分散在多个类里手写，缺统一 helper、统一测试模板和统一状态契约。

**建议**：
- 抽取统一的 lifecycle helper：`join_and_clear_if_stopped(component_name, thread, timeout)`。
- join 后仅在线程已退出时清空引用；仍 alive 时保留引用并让 `is_running()` 返回真实状态。
- 将该约束加入 ADR，避免后续组件重复实现不一致逻辑。

### 2.3 私有属性依赖已收敛，但还没有完全消失

**证据示例**：
- `src/api/admin_routes/config.py` 已改为直接调用 `UnifiedIndicatorManager.get_intrabar_eligible_names()`，并在 `bar_event_handler.py` 同步移除了对私有 `_get_intrabar_eligible_names` 的兼容回退逻辑。
- 运行时装配层已把部分私有写入替换为正式 setter，方向正确，边界收口正在持续推进。
- `src/indicators/query_services/state_view.py` 已去掉 `Legacy` 回退桥接，`query_services/runtime.py` 与 `runtime/bar_event_handler.py` 统一通过 `manager.state` 获取 `pipeline_event_bus` 与状态数据；测试点同步更新为状态容器契约验证，未再依赖 `_xxx` 字段。

**影响**：
- 私有字段变更会绕过类型检查和契约测试，尤其容易在热重载、运行模式切换和 Studio 展示路径中引入隐性回归。

**建议**：
- 为这些行为补正式端口：`IndicatorManager.get_intrabar_eligible_names()`、`set_pipeline_event_bus()`、`SignalRuntime.set_pipeline_event_bus()`、`set_warmup_ready_fn()`、`enable_intrabar_trading()`、`PaperTradingBridge.current_session()`。
- API/Studio/readmodel 只依赖公开只读方法，不再探测私有实现。

---

## 3. P1 架构与性能问题

### 3.1 协调器仍偏大，职责边界需要继续下沉

热点文件（按当前行数）：

| 文件 | 行数 | 主要风险 |
|------|-----:|----------|
| `src/indicators/manager.py` | 1424 | 4 个后台线程、事件写入、intrabar、reconcile、状态投影混在同一门面 |
| `src/trading/positions/manager.py` | 1279 | 持仓恢复、对账、出场执行、SL/TP 修改、状态投影集中 |
| `src/signals/orchestration/runtime.py` | 1076 | 队列、生命周期、投票、状态、status 投影仍在同一类 |
| `src/signals/service.py` | 992 | 策略注册、评估、持久化、查询与诊断入口混合 |
| `src/trading/execution/executor.py` | 983 | confirmed/intrabar 执行、过滤、熔断、参数计算、状态汇总集中 |
| `src/trading/pending/manager.py` | 620~680 | 价格监控与 MT5 order 管理已下沉，status 聚合已下沉到 `PendingEntrySnapshotService`，`PendingEntryManager` 更偏门面职责 |

**建议拆分顺序**：
1. 先拆生命周期/线程/队列 runner，不动领域算法。
2. 再拆只读投影 builder，避免 `status()` 继续读取内部散落字段。
3. 最后拆业务策略，如 PendingEntry 的超时降级、PositionManager 的 SL/TP 修改端口。

### 3.2 Intrabar 已具备链路，但还不是交易级 SLO 能力

**现状**：
- `IndicatorManager._intrabar_queue` 满时会丢事件。
- `SignalRuntime._intrabar_events` 在交易协调器存在时会短暂等待或替换旧事件；失败仍会丢弃。
- TODO 中已有 intrabar drop rate、queue age、degrade ladder、trace_id 贯通等待办。

**建议**：
- `intrabar_trading.enabled=true` 前必须先完成 SLO 与降级矩阵。
- 状态接口暴露 `intrabar_drop_rate_1m`、`intrabar_queue_age_p95`、`intrabar_to_decision_latency_p95`。
- 从“队列满随机丢弃/替换”升级为明确的 L0-L3 降级策略：全量 intrabar → 白名单策略 → 核心 TF → confirmed-only。

### 3.3 Walk-Forward 结果持久化仍不完整

**证据**：
- `execute_walk_forward()` 仅把 `wf_result` 存入 `backtest_runtime_store.store_walk_forward_result()`。
- 代码会把每个 split 的 OOS `BacktestResult` 单独 `persist_result()`，但没有 WF run/split 的整体 DB 实体与查询恢复路径。
- `BacktestRepository` 目前只有普通 run/trades/evaluations/recommendations 的 fetch/save 方法。

**影响**：
- API 重启后无法按一次 WF 任务完整还原 splits、IS/OOS 对照、best_params、overfitting_ratio、consistency_rate。

**建议**：
- 增加 `backtest_walk_forward_runs` 与 `backtest_walk_forward_splits`。
- Repository 增加 `save_walk_forward_result()`、`fetch_walk_forward_result()`、`list_walk_forward_runs()`。
- API 的 `/results` 和 `/results/{run_id}` 应区分普通 backtest、optimization、walk_forward。

---

## 4. 策略层问题

### 4.1 当前策略样本不足，不应作为实盘放大的依据

`TODO.md` 中 2026-04-08 基线显示：

| TF | 笔数 | 结果 |
|----|----:|------|
| M30 | 16 | WR 68.8%，PnL +271.20，MC p=0.057 |
| H1 | 3 | WR 100%，PnL +276.51 |
| 合计 | 19 | 3 个月约 1.5 笔/周 |

**判断**：
- M30 的 p=0.057 接近但未通过 0.05 显著性门槛。
- H1 只有 3 笔，不能支持任何稳健结论。
- 当前最重要的策略任务不是继续堆功能，而是扩大样本、做 WF、跑 Paper Trading 对比。

### 4.2 单 TF 策略已进入正式 guarded 轨道，但研究闭环仍需继续补齐

当前 `tf_specific` 策略已经有正式部署合同与 live 护栏，避免了过去依赖 zero-affinity freeze 的隐式语义；但 research → candidate → structured strategy → backtest/WF/paper/live 的闭环仍处于第一阶段，尤其还缺：

- 候选结果到正式结构化策略实现的批量晋升流程
- 启动摘要中对 `candidate / paper_only / active_guarded` 的显式可视化
- 基于真实 paper session 的自动晋升/降级台账

**建议**：
- 增加策略运行态摘要：`enabled_timeframes`、`deployment_status`、`locked_sessions`、`is_guarded`。
- Paper Trading/Backtest 输出中记录 active strategy set 和 `research_provenance` 配置快照。
- 继续把单 TF 候选的纸面证据转成可执行策略，而不是长期停留在“被识别但未落地”的状态。

---

## 5. 代码质量问题

### 5.1 broad `except Exception` 分布广，需要区分“可降级”和“必须失败”

代码中大量 `except Exception` 是长期运行系统的防御手段，但部分路径会静默吞掉配置或展示错误，例如 API config 视图、builder 可选组件、回测 DB fallback。建议按语义分层：

- 启动必需组件：失败应阻断启动或进入明确 degraded mode。
- 可选观测组件：可以不中断，但必须在 health/startup status 中可见。
- API 展示补充字段：可以缺省，但应记录 debug/metric，不应完全 `pass`。

### 5.2 文档与代码仍有迁移后残留

本次审查发现 README 曾描述“35 个内置策略”“composites.json”“Regime 亲和度直接跳过”等旧语义；实际代码已经切到结构化策略目录和新的评分/风控语义。本轮已同步 README，后续若策略注册或评分链路变化，应继续同步 README、`docs/signal-system.md` 与本风险台账。

---

## 6. 后续整改顺序

1. **先清配置**：清理 local legacy 策略覆盖，并增加启动校验。
2. **再补生命周期契约**：统一 stop/start helper，修复 join 超时清引用问题。
3. **已完成：公开端口收口（基本完成）**：指标链路已移除 `_get_intrabar_eligible_names` 的外部访问，统一走 `get_intrabar_eligible_names()`；其余接口持续补齐。
4. **阶段 A 进行中（2026-04-10）**：`SignalRuntime` 已将队列/状态/状态机职责切到 `runtime_components.py`，并修复队列清理与 `staticmethod` 越权引用问题。
5. **阶段 A 基本完成（2026-04-10）**：`TradeExecutor` 已补齐 `PreTradePipeline / ExecutionDecisionEngine / ExecutionResultRecorder` 三类组件，并完成队列收口 + 溢出记录统一；`PreTradePipeline` 已下沉 intrabar 门禁分支，`ExecutionDecisionEngine` 接管执行动作分发（市价/挂单）。当前阶段目标已转为“结果可观测性与能力契约”。
5. **阶段 A 增量（2026-04-11，IndicatorManager 最后一轮收口）**：
  - `src/indicators/query_services/runtime.py`：计算/事件/回写/重算链路；
  - `src/indicators/query_services/read.py`：查询/快照/监听器/观测链路；
  - `src/indicators/query_services/storage.py`：快照标准化与内存结果序列化缓存；
  - `src/indicators/runtime/registry_runtime.py`：注册加载与重初始化；
  - `src/indicators/runtime/registry_mutation.py`：配置变更（add/update/remove）入口；
  - `src/indicators/runtime/bar_event_handler.py`：批次处理与单事件处理的事件编排；
  - `src/indicators/runtime/event_loops.py`：事件循环与重算调度；
  - `src/indicators/runtime/event_io.py`：bar close 入库入队与 snapshot 落盘触发；
  - `src/indicators/runtime/lifecycle.py`：启动/停止/运行状态与 listener 生命周期；
  - `src/indicators/runtime/intrabar_queue.py`：intrabar 入队与溢出降级策略；
  - `src/indicators/runtime/pipeline_runner.py`：pipeline 计算分层与优先级结果合并；
  - `src/indicators/manager.py` 保持门面职责，不再承担细粒度查询、注册与循环入口实现；
  - `src/indicators/manager_bindings.py`：统一方法绑定映射；
  - `src/indicators/runtime/loop_adapter.py` 已移除，事件循环与循环入口直接通过 `lifecycle/event_loops` 的组合边界进入。
  - 兼容分支清理：`src/indicators/query_services/` 与 `src/indicators/runtime/` 的 observer/event-store/snapshot 发布路径统一走正式端口（明确契约，不做兼容分支兜底）。
6. **阶段 B 收敛（2026-04-10）**：`SignalModule` 已有能力索引（`strategy_capability_catalog`）并用于回测引擎 confirmed 能力门控；`SignalRuntime` 已同步通过 `SignalPolicy` 注入能力快照，消费字段改为 `valid_scopes`、`needed_indicators`、`needs_intrabar`、`needs_htf`。  
  - 已完成增量（2026-04-10）：  
    - `SignalPolicy` 增加 `strategy_capability_contract()` 作为运行时对账口。  
    - `runtime_warmup.py`/`runtime_status.py` 已改为能力口读取，避免分散兼容回退。  
    - 回测 `BacktestEngine` 已改为按 `strategy_capability_catalog()` 初始化能力快照，策略能力契约与 runtime/backtest 调度语义对齐。  
    - 回测策略评估加一层 `capability.valid_scopes` 门禁，确认与 `SignalRuntime` 的 scope 消费语义一致。  
  - 下一步：把能力快照治理点进一步上移为统一回放一致性检查口（intrabar/丢弃率），并通过 admin 入口 `GET /admin/strategies/capability-reconciliation` 与 `GET /admin/strategies/capability-contract` 固化风险红线。  
  - 当前状态：能力对账已对齐 `regime_affinity / htf_requirements`，并在 runtime 快照与 readmodel 两端都可见。  
  - 已完成增量（2026-04-11）：
    - runtime/readmodel 新增 `strategy_capability_execution_plan`，明确 `configured/scheduled/filtered` 调度边界、scope 覆盖与指标需求并集。
    - 回测 `BacktestResult` 新增同语义 `strategy_capability_execution_plan`，用于与实盘 status 直接对账。
    - 新增 `GET /admin/strategies/capability-execution-plan`，对齐 module/runtime/backtest 三方执行计划差异。
  - 已完成增量（2026-04-11，端口收敛）：
    - `SignalRuntime` 已移除 `_legacy_strategy_capability_contract`，能力加载仅消费 `SignalModule.strategy_capability_catalog()`。
    - `BacktestEngine` 已移除 `_legacy_strategy_capability`，能力加载仅消费 `SignalModule.strategy_capability_catalog()` 并对目标策略做缺失即失败校验。
    - `runtime_status.py` 与 `service_diagnostics.py` 已移除能力契约 `hasattr/getattr` 兼容探测，统一依赖 `strategy_capability_contract()` 正式端口。
    - `runtime_status.py` 与 `backtesting/engine/runner.py` 的 execution plan 已收敛到共享构建器 `src/signals/contracts/execution_plan.py`，`scope/needs/required_indicators` 语义同源。
    - `service_diagnostics.py` 的 module/runtime 对账规范化已收敛到 `normalize_capability_contract(...)`，避免对账口与执行计划口出现字段形态分叉。
    - `api/admin_routes/strategies.py` 的 `module_plan` 已复用共享构建器；`module/runtime/backtest` 对账统一读取 `scheduled_strategies` 语义。
    - 两侧均改为“能力契约非法项/缺失策略直接抛错”，不再通过兼容分支静默降级。
  - 建议验收：新增策略只需通过能力声明/配置驱动，不新增 runtime/private 分散推断。
6. **再做 WF 持久化**：把验证结果从内存缓存提升到 DB 事实源。
7. **最后优化 intrabar**：只有 SLO、降级与 trace 完成后，才把 intrabar 作为真实交易入口。

### 6.A Indicators 目录职责边界巡检清单（2026-04-11）

- [x] `src/indicators/manager.py` 保持门面职责，不承接细粒度计算/查询实现。
- [x] 查询与计算入口集中在 `src/indicators/query_services/`，按职责分离为 runtime 计算/查询与查询服务。
- [x] 运行时细粒度职责集中在 `src/indicators/runtime/`，并按环节拆分：
  - 注册与重初始化：`registry_runtime.py`、`registry_mutation.py`、`lifecycle.py`
  - 事件编排：`bar_event_handler.py`
  - 事件循环：`event_loops.py`、`intrabar_queue.py`
- [x] `src/indicators/runtime/event_io.py` 统一处理 bar close 入队与批量落库流程。
- [x] `src/indicators/runtime/pipeline_runner.py` 统一承载 pipeline 计算分层，不由 `manager.py` 或 `query_services.runtime` 直接承担入口。
- [x] `src/indicators/runtime/loop_adapter.py` 已移除，不保留重复循环适配。
- [x] `manager_bindings.py` 维持显式端口映射，调用走统一绑定方法，避免私有属性直接穿透。
- [x] 已拆除跨模块的双向循环导入：  
  - `query_services.runtime` 与 `runtime.bar_event_handler` 改为懒加载边界；  
  - `runtime.pipeline_runner` 不再顶层反向 import query services。
- [x] 已补齐冒烟测试：`tests/indicators/test_core_functions.py`、`tests/indicators/test_flush_event_batch.py`、`tests/indicators/test_manager_intrabar.py`。
- [ ] 建议后续：补 `tests/indicators/` 层面的职责契约测试（`manager` 与 `runtime/query_services` 的输入输出不变量）。

---

### 6.B Indicators 输入输出与状态归属图（2026-04-11）

#### 文字化边界图（Claude 风格）

```
上游输入
  ├─ api/admin_routes / orchestration caller
  ├─ market_service
  ├─ event_store
  └─ storage_writer
        │
        ▼
UnifiedIndicatorManager（门面）
  ├─ state（唯一运行态，单一所有权）
  ├─ QueryBindingMixin（query_services 端口）
  └─ RegistryBindingMixin（registry 端口）
        │
        ├─ query_services/
        │   ├─ runtime.py
        │   │   ├─ compute / reconcile / eligibility / write-back
        │   │   └─ 对外绑定到 manager 公开方法
        │   ├─ storage.py
        │   │   └─ 快照标准化、持久化输入结构构造
        │   └─ read.py
        │       └─ 读模型 / listeners / 快照读取
        │
        └─ runtime/
            ├─ lifecycle.py：启动/停止/状态机 + listener 生命周期
            ├─ event_loops.py：closed-bar / intrabar / event_writer 的循环
            ├─ event_io.py：enqueue 与批量落盘
            ├─ bar_event_handler.py：事件批次与单事件编排
            ├─ pipeline_runner.py：pipeline 计算分层与优先组合并
            ├─ registry_runtime.py：注册加载与重初始化
            ├─ registry_mutation.py：add/update/remove 配置入口
            └─ intrabar_queue.py：intrabar 入队与回压策略
        │
        └─ market_service cache（指标回写与消费方）
```

边界规则：
- 输入拥有方：`market_data` / `config` / `event_store` / `storage_writer` 仅由外部注入；`runtime` 不重复定义同类输入源。
- 状态所有权：`manager.state` 为单点写入点；`runtime` 与 `query_services` 仅通过方法参数或返回值读取与更新。
  - 调用方向：上游只调用 `UnifiedIndicatorManager`，内部按端口走 `QueryBindingMixin / RegistryBindingMixin`，不直接访问私有字段。
  - 持久化与可观测：事件入库、snapshot 落盘、重算触发通过 `runtime.event_io` + `runtime.event_loops` 的统一链路执行。

### 6.C Trading 输入输出与状态归属图（2026-04-11）

- `position state` 全量写入点是 `PositionManager._positions` + `_tracking`（内存运行态），其职责边界不跨越到 execution/策略评估模块。
- `execution 触发` 由 `TradeExecutor` 与 `TradeExecution*` 组件串联完成，不直接读取 `PositionManager` 的运行态状态；仅消费 `SignalRuntime` 已确认事件。
- `sl/tp 出场` 的唯一计算口是 `src/trading/positions/exit_rules.py`，`PositionManager` 只负责编排评估（`_evaluate_chandelier_exit`）与 MT5 API 执行（`_apply_chandelier_action`），不承载策略语义。
- `pending 入场` 的唯一协调口是 `src/trading/pending/manager.py` + `monitoring.py`，执行入口保持单向入链：`PendingEntryManager -> execute_fn`。
- `trade outcome` 的统一追踪口是 `TradeOutcomeTracker`，下游仅通过 `TradeOutcomeTracker` 回写/消费，不在 `positions` 或 `execution` 中各自重复落库。

边界规则：
- 输入拥有方：
  - 信号事件：`SignalRuntime`（已确认的 signal 事件）
  - 市场快照：`indicator_manager` / `market_service`
  - MT5 交互：`trade_module`（交易网关端口）
  - 共享配置：`SignalConfig`（只读注入）
- 状态写入方：
  - 持仓生命周期：`PositionManager`（reconcile、入场追踪、出场状态）
  - 执行队列：`TradeExecutor` 与 execution 门禁组件
  - 挂单等待：`PendingEntryManager`
- 调用方向（禁止反向）：
  - API / orchestration 不应直接读/改 `_positions`、`_reconcile_thread`、`_fill_monitor_thread` 等私有字段。
  - execution / strategy 模块不应跨过 `exit_rules` 复用 Chandelier 规则细节。
- 可观测性闭环：
  - `PositionManager._build_status`/`SignalRuntime/status` 与 `execution` 需要输出一致的 `decision_id + position_ticket + reason`，用于链路回放定位“是哪个策略/何时被哪个规则阻断或触发”。

### 6.D Trading 冒烟清单（2026-04-11）

- [x] Chandelier profile 命名与注入路径收口为默认语义（`fallback` -> `default`）：
  - `src/config/models/signal.py`
  - `src/app_runtime/factories/signals.py`
  - `src/backtesting/engine/runner.py`
  - `src/trading/positions/exit_rules.py`
  - `config/signal.ini`
- [x] 回测/实盘共用同一 `ChandelierConfig` 字段集合，减少分叉映射语义。
- [ ] 后续建议（不在本轮实现中）：
  - 运行时巡检：打印/上报 `ChandelierConfig` 的 effective profile 解析结果（`regime_aware`、`default_profile`、`aggression_overrides`、`tf_trail_scale`）供 `startup check` 对账。
  - 完整回归：在交易冒烟时补齐 `position_manager` stop/start 与信号出场路径的 trace 断言。

### 6.E 风险模块输入/输出与状态归属清单（2026-04-11）

- `src/risk/service.py`
  - `PreTradeRiskService` 是对外服务口，单一承担“下单前风控评估”语义。
  - 风险规则(`rules.py`)与领域数据模型(`models.py`)不直接持有交易器实例；仅通过 `RuleContext` 读取 `account_service` 与 `economic_provider` 提供的标准化能力。
  - 风险判定只输出 `RiskAssessment`/`checks` 结构，业务方以 `verdict`、`reason`、`blocked`、`checks` 进行决策。
- `src/risk/runtime.py`
  - `wire_margin_guard` 仅承担 margin_guard 的生命周期挂接，不参与规则判定。
  - MarginGuard 实例由 `PositionManager` 与 `TradeExecutor` 消费，遵循“服务注入而非反向读取内部态”的单向边界。
- `src/risk/margin_guard.py`
  - `MarginGuard` 负责“数据→阈值→动作决议”纯计算 + 动作派发，状态归属于 `last_snapshot` 与 action counters。
  - `load_margin_guard_config` 只做 `risk.ini`（含 local 叠加）字段到 dataclass 的映射。
- `src/trading/execution/pre_trade_checks.py`
  - 风控拦截位于执行前门禁列表（步骤 ⑥），“是否 block_new_trades”通过 `MarginGuard.should_block_new_trades()` 显示注入。
- 状态边界与来源：
  - 运行态输入源：
    - 账户状态：`account_service`（来自 MT5 会话）
    - 经济事件：`economic_calendar_service`
    - 风控策略：`RiskConfig` / `EconomicConfig`
  - `position_manager` 仅写入 `PositionManager._margin_guard` 引用；不反向注入规则内部状态。
  - `trade_executor` 仅读 margin guard 快照与决策结果，不直接改写 margin guard 行为状态。

- 冒烟清单（本轮）
- [x] `risk.runtime` 不再直接裸读 `config/risk.ini`，改为 `get_merged_config("risk")` + `load_margin_guard_config`，保证 `risk.local.ini` 覆盖链路。
- [x] `wire_margin_guard` 在 `risk` 配置缺失 `margin_guard` 段时回退到安全默认；当配置源类型异常时抛出结构化配置错误并中断启动，避免 silent fail。
- [x] 风险规则链路保持单向依赖：服务入口 -> rules -> models；execution 侧只消费 `PreTradeRiskService`/`MarginGuard` 接口，不探测私有字段。
- [x] 新增 `MarginGuard` 启动快照日志，`risk.runtime` 统一打印生效的 `margin_guard` 阈值与动作参数，用于 startup 可观测。
- [x] 风险码映射收口：`resolve_risk_failure_key` 改为优先返回 `verdict=block` 的检查项，避免 warning 与 block 混在一起时错误码退化。

### 6.F app_runtime 输入/输出与状态归属清单（2026-04-11）

- `src/app_runtime/container.py`
  - 职责：运行时组件的纯数据承载，所有字段默认 `None`，不承载生命周期动作与域逻辑。
  - 输入来源：`builder.py` 写入；其他层只读。
  - 状态所有权：组件对象内部状态仅归组件自己管理。
- `src/app_runtime/builder.py`
  - 职责：按依赖顺序构建 runtime 所有组件，并完成跨组件连接。
  - 边界：不直接执行业务规则计算，不做回退式兼容分支；装配流程按阶段拆分到 `src/app_runtime/builder_phases/`，保留监控/热更新/可观测连接在装配侧集中处理。
  - 阶段化拆分（本轮完成）：`market.py`、`trading.py`、`signal.py`、`paper_trading.py`、`runtime_controls.py`、`monitoring.py`、`read_models.py`、`studio.py`。
- `src/app_runtime/runtime.py`
  - 职责：启动、停止、错误回退、状态快照的集中编排；不应承担领域参数变换。
  - 状态所有权：`_status` 与停止回调队列由 runtime 本体持有。
- `src/app_runtime/lifecycle.py`
  - 职责：`RuntimeComponentRegistry` 的 `start/stop/is_running` 统一执行模型，提供模式变更顺序化行为。
  - 风险边界：`apply_mode` 对每个组件继续尝试启动，但仍存在部分失败后模式更新为目标状态的行为，依赖上层观测发现。
- `src/app_runtime/mode_controller.py`
  - 职责：运行模式状态机 + 守护线程；不直接操作交易模块运行态，只通过 `TransitionGuard` 和组件注册表做决策。
  - 本轮修复：`stop()` 在 `join(timeout)` 后仅在线程退出时清空引用，线程未退出时保留引用便于被动观测。
- `src/app_runtime/mode_policy.py`
  - 职责：模式策略（策略常量、守卫、EOD 动作）配置化，禁止在控制器中硬编码模式语义。
- `src/app_runtime/factories/*`
  - 职责：对象构造（市场/存储/指标/信号/交易）分离，不应再嵌入 runtime 生命周期判断。

#### 文字化边界图（Claude 风格）

```
上游配置 / 服务
  ├─ config（get_merged_config/专有 model）
  ├─ api/admin（启动入口）
  ├─ monitoring（健康组件）
  └─ persistence（数据库写入口）
        │
        ▼
builder（装配）
  ├─ factories 逐层构造对象
  ├─ runtime/read-models 及可观测链接
  └─ AppContainer（组件图）
        │
        ▼
runtime（编排）
  ├─ 生命周期：start/stop
  ├─ 模式控制：mode_controller + mode_policy + lifecycle registry
  └─ 运行时状态快照 + shutdown callback
        │
        ▼
domain 组件
  ├─ indicators / signals / trading / storage / monitoring
  └─ 各组件只对外提供正式端口
```

边界规则：
- 数据源/配置源不在 domain 内重复定义，统一向内注入。
- 生命周期动作只通过 `RuntimeComponentRegistry` + `RuntimeModeController` 触发；`AppRuntime` 仅做系统级编排与全局清理。
- 运行态状态快照来源保持单向：`AppRuntime` 暴露运行状态，组件内部状态保持私有与封闭。
- 禁止隐式回退补丁：高优先复用失败应向 `status` 和日志暴露，避免通过条件分支静默吞掉模式/组件失败。

#### 冒烟清单（本轮）
- [x] 修复 `RuntimeModeController.stop()` 的线程引用回收：线程未退出时不清空 `_monitor_thread`，避免 “is_running 反映不实”。
- [x] 增补测试：`RuntimeModeController.stop()` 在线程未退出时保留引用（已补充），并覆盖线程退出后清理引用的闭环。
- [x] `AppRuntime.start()` 与 `RuntimeComponentRegistry` 的启动入口已收口：storage/性能预热不再由 runtime 直接触发，统一由 registry 在模式切换链路内执行（保留幂等与一次性 warmup 标志）。

### 6.G 其余包巡检（2026-04-11）

按“职责边界/依赖方向/异常边界/历史兼容”四项复核 `src` 下除 `trading`、`risk` 外的其余包：

- `backtesting`：  
  - 结果：分层还算清晰（data / engine / filtering / optimization / paper_trading / runtime 数据对象），但 `src/backtesting/cli.py` 曾存在入口函数断裂问题（未定义引用、缺少子命令解析与持久化分支）。已完成修复：补齐 `_parse_param`、`_build_components`、`_persist_result`、`_build_parser`、`main()` 分发逻辑，当前 `python -m src.backtesting --help` 可正常给出可执行入口。  
  - 剩余建议：把 `--output`、`--param`、`--timeframes` 等通用参数抽到统一构建器，避免命令与默认行为语义再次发散（当前默认运行时为 `run`）。

- `clients`：  
  - 结果：基本职责清晰，按 MT5 外部系统封装，内部大量 `getattr`/`hasattr` 主要用于跨平台常量兼容与容错，属边界内“适配器策略”而非跨域探测（`clients/base.py` 与 `clients/mt5_trading.py` 明确通过 `_normalize_market_time`、`_get_field` 保证兼容）。  
  - 建议：将常量探测统一到 `clients/base.py` 的常量适配层，逐步在上层策略中改为显式能力字段，减少重复 `getattr` 分散。

- `api`：  
  - 结果：依赖链路完整，负责输入校验与序列化是合理的；监控路由异常防御已收敛为统一入口（`_execute_monitored_call` / `_execute_health_call`），避免散落 `try/except`。  
  - 建议：继续沿用“必须失败/可降级”分层，持续将通用边界策略下沉到业务路由层。

- `ingestion`：  
  - 结果：与 `market`/`persistence` 职责耦合度低，职责边界清晰，`BackgroundIngestor` 与存储写入方向单向。  
  - 建议：保持现状。

- `calendar` / `market_structure`：  
  - 结果：各自以领域能力边界存在，`calendar` 主要负责事件同步与风控告警前置、`market_structure` 负责形态计算，边界清晰。  
  - 建议：保持现状，补充一次策略级契约测试（事件窗口与交易过滤字段映射）即可。

- `monitoring`：  
  - 结果：职责清晰，仍有一定 `except Exception` 保护，建议区分“指标采集可降级”与“生命周期关键指标不可用”。  
  - 建议：为健康检查和关键事件总线增加错误码分级，避免所有异常被同一告警语义吞没。

- `ops`：  
  - 结果：`cli` 与 `scripts` 同名脚本并行分工，历史上形成重复入口。  
  - 执行结论：`src/ops/scripts` 已清理，统一仅保留 `src/ops/cli/*` 作为执行入口。  
  - 变更效果：入口可读性与可追踪性提升，文档入口与实现保持一致。  

- `persistence`：  
  - 结果：分层较正，repositories 与 db/writer 职责分离明显；入口集中在 `repositories/`。  
  - 建议：保留现状，增加一次 `writer/schema` 的 contract 测试（DDL、insert、upsert 的返回值与异常码一致性）。

- `research` / `readmodels` / `studio`：  
  - 结果：三者定位明确（实验计算、读模型投影、前端观测），职责分界清楚。  
  - 建议：保持现状。

#### 该轮结论

- 大部分包职责边界已可接受，不再存在典型“同文件跨域合并”问题。  
- 当前最优先关注点仍在高风险链路：`trading`、`signals`、`indicators`，其余包可以采用低优先级清洁度改造（以异常语义与观测可解释性为主）。  

### 6.H 全量包职责边界清单（2026-04-11）

- `api`：HTTP 适配与路由组织层，职责正确。  
- `app_runtime`：运行时装配与生命周期层，职责正确。  
- `backtesting`：回测与实验运行时，职责正确。  
- `calendar`：经济事件窗口与日历风控前置，职责正确。  
- `clients`：外部系统客户端封装，职责正确。  
- `config`：配置模型与配置加载层，职责正确。  
- `entrypoint`：启动入口层，职责正确。  
- `indicators`：指标计算与状态计算层，职责正确。  
- `ingestion`：行情历史与实时抓取层，职责正确。  
- `market`：市场状态与行情服务层，职责正确。  
- `market_structure`：市场结构特征层，职责正确。  
- `monitoring`：可观测与健康度采集层，职责正确。  
- `ops`：运维工具层，已完成入口统一，仅保留 `cli`。  
- `persistence`：持久化与仓储层，职责正确。  
- `readmodels`：读模型与投影层，职责正确；新增 `decision` 决策摘要逻辑收敛到该层。  
- `research`：研究实验算子层，职责正确。  
- `risk`：风控领域层，职责正确。  
- `signals`：信号与策略调度层，职责正确。  
- `studio`：前端可观测数据服务层，职责正确。  
- `trading`：交易执行与仓位协调层，职责正确。  
- `utils`：通用工具层，职责正确。  

执行落地：  

- [x] `decision` 包职责收口到 `readmodels`。  
- [x] `ops/scripts` 历史重复入口清理。  
- [x] 命令入口文档与实现对齐。  

### 6.I 启动阻塞修复记录（2026-04-11）

- 发现问题：`src/app_runtime/factories/signals.py` 的 `build_executor_config()` 已透传 `htf_conflict_block_timeframes` / `htf_conflict_exempt_categories`，但 `src/trading/execution/executor.py` 的 `ExecutorConfig` 未同步声明同名字段，导致 `deps._ensure_initialized()` 在真实容器初始化阶段直接抛 `TypeError`，服务无法进入 lifespan。  
- 本次修复：恢复装配层与执行层配置契约一致性，`ExecutorConfig` 正式补齐上述两个字段；未新增兼容分支，也未改变现有调用边界。  
- 验证补强：新增 `tests/app_runtime/test_signal_factory.py`，直接覆盖 `SignalConfig -> build_executor_config() -> ExecutorConfig` 的字段对账，避免未来再次出现“入口 smoke 通过，但真实容器初始化失败”的装配断裂。  
- 未决项：这两个 HTF conflict 字段当前已恢复配置契约，但预交易流水线尚未消费它们；如果后续确认该能力需要生效，应在 `pre_trade_pipeline / pre_trade_checks` 中补正式门禁，而不是继续停留在“可配置但未落地”的状态。  

33. **`live-main` 单实例 smoke 已打通，启动期配置空值、schema 时序和风险投影 JSON 缺陷已修复**  
    本轮按 `python -m src.entrypoint.instance --instance live-main` 做了真实单实例烟测，并在 smoke 期间显式关闭自动交易，验证 `/health=200` 与 `/v1/monitoring/health/ready=ready`。过程中修复了三类真实启动问题：  
    1) `src/config/signal.py` 现在会把直接映射到 `SignalConfig` 的空字符串配置视为“未覆盖”，不再因 `signal.ini` 中留空占位字段触发 `ValueError`/Pydantic 校验失败；  
    2) `StorageWriter` 新增正式 `ensure_schema_ready()` 入口，`AppRuntime.start()` 会在 warm-start 之前先确保 schema 与 retention 就绪，避免新库首次启动时 `position_runtime_states / trade_control_state` 查询先于建表；  
    3) `AccountRiskStateProjector` 已对非有限浮点做 JSON 清洗，不再把 `Infinity` 写入 `account_risk_state.metadata` 导致 PostgreSQL JSON 解析失败。  
    当前单实例启动已可进入 `ready`，残余告警主要是数据陈旧导致的 `market_data data latency critical`，属于环境/时段问题而非装配阻塞。

34. **`/v1/trade/accounts` 已补齐正式读模型端口，休盘 smoke 下账户视图不再因私有字段访问而 500**  
    本轮休盘巡检中，`/health`、`ready`、queues、performance、events 均已打通，唯一暴露的真实断点是 `/v1/trade/accounts` 仍直接访问 `RuntimeReadModel.runtime_identity`，而读模型内部只持有私有 `_runtime_identity`，导致账户视图在 live-main 单实例烟测时返回 500。现已在 `RuntimeReadModel` 上补齐正式公开属性 `runtime_identity`，让 `trade/accounts`、风险投影聚合和环境标签统一通过公开只读端口获取，避免 API 为了取状态继续探测私有字段；同时补充了对应 API 回归测试，覆盖新实例配置模型下的账户清单返回。

35. **交易控制烟测已打通：命令审计、状态回放与文本日志三条链路现已一致可追踪**  
    本轮按 `live-main` 单实例、显式不下真实订单的方式对交易控制面做了休盘烟测，实际调用了 `/v1/trade/control` 的安全开关操作，并核对了 `/v1/trade/command-audits` 与 `data/logs/live-main/mt5services.log`。过程中修复了两个真实断点：  
    1) `src/persistence/repositories/trade_repo.py` 中 `write_trade_command_audits()` 的批量写库字段顺序与 `trade_command_audits` 表契约不一致，导致 `account_key` 被错误写入 JSON 列，控制命令虽然返回成功但审计表为空；现已按正式 schema 顺序重排，并补了仓储回归测试。  
    2) `RuntimeReadModel.persisted_trade_control_payload()` 直接返回数据库原始字典，`updated_at` 等 `datetime` 值未做 JSON-safe 归一，导致 `/v1/trade/control` 在持久化状态存在时触发响应验证失败；现已在读模型层统一做递归序列化。  
    同时在 `src.trading.application.module` 中补了正式文本日志：每次 operator action 成功入审计后都会记录 `account / command / status / action_id / actor / reason / idempotency_key`，避免后续只能依赖数据库追查。当前已验证安全开关操作会同时落到 API 状态、审计表与文本日志，说明交易控制主链路的可追踪性已恢复。

36. **`trade/precheck` 准入链已修复账户信息契约缺口，休盘下可返回结构化风险阻断而非 AttributeError**  
    在继续扩大 `live-main` 单实例休盘烟测时，`POST /v1/trade/precheck` 暴露出一个真实契约断点：`src.risk.rules` 的日内亏损规则会读取 `day_start_balance / daily_realized_pnl / daily_pnl`，而 `src.clients.mt5_account.AccountInfo` 适配对象并未暴露这些字段，导致准入接口直接抛出 `AttributeError`，无法返回结构化风险评估。该问题现已通过收口账户信息契约修复：`AccountInfo` 正式补齐上述可空字段，并由 MT5 账户适配层统一返回 `None` 作为“当前 broker 不提供该字段”的显式语义，而不是让下游规则依赖隐式属性探测。修复后已验证 `/v1/trade/precheck` 能在休盘与低保证金场景下稳定返回结构化 `block` 结果，正确给出 `margin_availability` 与 `market_order_protection` 阻断原因，同时文本日志中也会记录对应的风险阻断告警。

37. **economic calendar 健康语义已从“无 last_refresh_at 即 stale”收口为正式启动引导状态，重启后也会恢复上次成功刷新时间**  
    本轮针对休盘 smoke 中出现的 `economic calendar stale` 与 Jin10 TLS 握手超时告警做了根因收口。问题不在风控侧，而在日历服务自身的健康模型：`EconomicCalendarService.is_stale()` 以前只要 `_last_refresh_at is None` 就直接返回 `True`，而 `start_service()` 又会把首次 `calendar_sync` 延后 `startup_calendar_sync_delay_seconds` 执行；同时 `restore_job_state()` 只恢复 job_state，不恢复 `_last_refresh_at`。结果是服务刚启动或刚重启，即使还处于计划中的 bootstrap 窗口，也会被错误标成 stale，并进一步让监控与 Trade Guard 看到“日历健康退化”。现已做三项正式修复：  
    1) `EconomicCalendarService` 新增 `warming_up / staleness_seconds / health_state` 正式健康语义，首次成功刷新前仅在 bootstrap 截止时间之后才视为 stale；  
    2) `restore_job_state()` 现在会恢复最近一次成功刷新的 `_last_refresh_at`，以及最近一次尝试的时间、状态和错误，避免重启后状态丢失；  
    3) 监控侧 `check_economic_calendar()` 改为消费 `staleness_seconds`，在 warming_up 阶段不再把 staleness 记为 `inf`，并补充了经济日历刷新成功日志，便于区分“外部瞬时抖动已恢复”与“持续同步失败”。  
    这次修复保持了边界正确：Trade Guard 继续只读取经济日历正式健康状态，不在风控规则里增加启动期特判。

38. **economic calendar runtime task 持久化已按实例新契约收口，shutdown 不再覆盖最后一次成功刷新结果**  
    在进一步做“停机重启后立即恢复 economic status”的真实 smoke 时，又暴露出第二层设计偏差：economic calendar 的 `runtime_task_row()` 仍按旧 13 列写 `runtime_task_status`，而仓储层已经升级到 17 列实例契约（`instance_id / instance_role / account_key / account_alias`），导致 `persist_job_state()` 实际写库失败但被内部 debug 吃掉；同时 `stop_service()` 会把 `last_status` 改成 `stopped` 并覆盖同一 runtime task 行，使得恢复逻辑即便读到记录，也会把“最后一次成功刷新”误读成 `stopped`。现已收口为：  
    1) `EconomicCalendarService` 正式注入 `runtime_identity`，runtime task row 按实例新契约完整写入；  
    2) `runtime_task_status` 查询支持按 `instance_role/account_key` 过滤，并按 `updated_at desc` 取最新，避免多实例或多轮重启的旧行干扰恢复；  
    3) economic calendar task details 新增 `last_result_status`，shutdown 只更新当前任务状态为 `stopped`，不再抹掉最近一次真实刷新结果；恢复时优先用 `last_result_status + success_count` 还原上次成功刷新时间。  
    经过真实 `live-main` 重启烟测验证，服务刚进入 ready 时即使 `release_watch` 正在新一轮刷新，`/v1/economic/calendar/status` 也已经能恢复出上一轮 `last_refresh_at` 与 `last_refresh_status=ok`，不再出现重启后全部为 `null` 的假 stale 状态。

39. **`/signals/evaluate` 的持久化载荷已收口为 JSON-safe 契约，不再因 metadata 中的 `datetime` 卡死请求**  
    在本轮单实例连通性测试中，`POST /v1/signals/evaluate` 会命中真实持久化路径，而 `SignalModule._persist_signal()` 直接把 context metadata 与 indicator snapshot 原样塞进 `SignalRecord`。当 metadata 中包含 `bar_time / recent_bars[].time` 这类 `datetime` 时，`psycopg2` 在写 `signal_events` 的 JSON 列时会抛 `TypeError: Object of type datetime is not JSON serializable`，接口表现为超时，日志中持续出现 `Failed to persist signal event`。本轮已把“信号持久化载荷必须是 JSON-safe”正式收口到 `SignalRecord.to_row()`：统一递归序列化 `datetime -> ISO8601`、`tuple -> list`、非有限浮点 -> `null`，避免再让 API、runtime 或研究链路各自猜测何时该做 JSON 归一。同时补了 `tests/signals/test_signal_module.py` 回归，明确验证带 `datetime` 的 metadata 能稳定持久化。

40. **`SignalModule` 已在编排边界统一收口信号身份字段，避免策略空串污染 `signal_events` 与 recent/summary 读口**  
    在继续做 `data -> indicators -> signals -> trade` 单实例连通性测试时，又发现 `/signals/evaluate` 虽然已经能稳定持久化，但结构化策略返回的 `SignalDecision.symbol/timeframe` 仍然是空串，导致 `signal_events`、`/signals/recent`、`/signals/summary` 被写入无符号、无周期的事实行。这不是 API 载荷问题，而是结构化策略基类长期把 `SignalDecision` 的身份字段留空，信号编排层也没有在出边界前做统一修正。该问题现已在 `SignalModule.evaluate()` 收口：策略返回后立即以当前请求的 `strategy/symbol/timeframe` 重建正式决策对象，再进入后续置信度修正与持久化流程。这样职责边界更清晰：策略只回答“方向/置信度/原因”，身份字段由编排层统一拥有，避免各策略重复样板代码，也避免 `recent/summary/trade_from_signal` 继续消费被污染的事实。已补 `tests/signals/test_signal_module.py` 回归，明确验证即便策略返回空身份字段，持久化后的 `signal_events` 也会写入正确的 `symbol/timeframe`。

41. **`/v1/trade/dispatch` 已在 API 边界统一归一化交易操作契约，不再让调用方猜测 `submit_trade/trade` 与 `direction/side` 双轨语义**  
    在单实例连通性烟测中，`/v1/trade/dispatch` 暴露出一条典型的接口契约漂移：监控读口会提示使用 `operation=trade`，但历史调用经常仍发 `submit_trade`；同时 payload 中 `direction` 在 `/trade` 主入口可接受，经由 dispatch 走统一调度时却会因底层只认 `side` 而失败。该问题现已在 `TradeAPIDispatcher` 的 API 边界正式收口：`submit_trade / execute_trade` 统一映射到正式操作 `trade`，`precheck_trade` 统一映射到 `trade_precheck`，并且凡是交易类 payload 都先通过 `TradeRequest` 做标准化，再交给 `TradingCommandService.dispatch_operation()`。这样下游只需要面对一套正式载荷，旧别名也不会继续把“语义转换”散落到命令服务内部。对应回归已补到 `tests/api/test_trade_api.py`，明确验证 `submit_trade + direction` 会被稳定归一到正式 `trade + side` 契约。

42. **`/v1/signals/runtime/status` 已补齐执行闸门与过滤摘要，单实例巡检不再只能看到“运行中”而看不到“为什么会放行/过滤”**  
    休盘连通性测试显示 `SignalRuntime` 内部其实已经维护了 `filter_realtime_status / filter_by_scope / filter_window_by_scope`，`TradeExecutor.status()` 也已经有 `execution_gate`，但 `RuntimeReadModel.signal_runtime_summary()` 长期只投影了队列和 warmup 基础状态，导致 `/v1/signals/runtime/status` 返回里 `executor_enabled / execution_gate / active_filters / filter_stats` 都是空值。这个缺口会直接削弱交易链路可审查性：看到 `signal_runtime.running=true` 并不能回答“当前有哪些过滤器启用、执行闸门是否打开、最近是被什么过滤掉的”。本轮已在读模型层把这部分正式投影补齐，统一公开当前执行器启用状态、执行闸门配置、活跃过滤器列表，以及 confirmed/intrabar 两个 scope 的过滤累计/窗口统计。这样单实例 smoke 下即可直接回答“策略在跑，但当前是 session/economic/spread 等哪些门在起作用”，不再依赖读代码或翻日志推断。

43. **`/v1/monitoring/health/ready` 的巡检契约已升级为结构化对象，runbook 已同步修正判读口径**  
    最新单实例 smoke 中，`/v1/monitoring/health/ready` 已返回结构化对象 `{status, checks, startup_phase, timestamp}`，而不是旧 runbook 中记载的裸字符串 `ready`。如果巡检脚本仍按字符串比较，就会误判服务“未就绪”，即使底层 `storage_writer / ingestion / indicator_engine` 都已经达标。本轮已把 runbook 的 ready 判读规则同步修正为：以 `status=ready` 和 `checks.*=ok` 为唯一准则，不再依赖历史的字符串返回语义。这个修正不改变接口实现，但能避免后续运维脚本和 smoke 流程继续按过期契约误报。

44. **双实例 smoke 已打通 `main + executor` 的就绪探针、风险投影与审计链，但 executor 角色边界仍有待继续收紧**  
    本轮按 `python -m src.entrypoint.supervisor --environment live` 对 `live-main + live-exec-a` 做了真实双实例烟测，确认 `8808/8809` 两个实例都能进入 `ready`，并且 `worker` 本地执行控制操作会同时写入 `trade_command_audits`、`account_risk_state` 和 `data/logs/live-exec-a/mt5services.log`，`main` 侧的 `/v1/trade/accounts` 也能正确聚合看到 `live_exec_a` 的最新风险当前态。过程中修复了一个真实观测断点：`/v1/monitoring/health/ready` 过去始终按主实例口径检查 `ingestion + indicator_engine`，导致 executor 明明已经装配好 `PendingEntryManager / PositionManager / AccountRiskStateProjector`，探针仍会返回 503；现已改为按角色判定，executor 的 ready 口径收口为 `storage_writer + pending_entry + position_manager + account_risk_state`。同时，`RuntimeReadModel` 对 executor 的 `storage / indicators / signals` 视图也已改成 `disabled` 语义，避免 `/health` 把“本来就不属于 worker 的共享计算面”误报成 critical；`paper_trading` 也已在 executor 装配阶段直接跳过。  
    但这轮 smoke 也暴露出一个仍待整改的结构性问题：executor 在 build 阶段仍会完整构建 `UnifiedIndicatorManager`、signal factory 与 economic calendar 相关组件，然后仅在 runtime mode 阶段停止其中一部分线程。这说明当前“账户执行面”和“共享计算面”在装配边界上还没有彻底拆干净；本轮只修正了观测契约和非必要的 `paper_trading` 装配，使双实例 smoke 可以成立，但后续仍应把 worker 收口到真正的 `AccountRuntime`，不再在 build 阶段构造不属于它的 shared compute 依赖。

45. **executor 装配边界已前移到 composition root，不再靠 runtime stage 事后停线程**  
    针对上一条暴露出的结构性问题，本轮已把 `executor` 的运行时装配改为真正的角色化组合，而不是“先全量构造，再在 lifecycle 中禁用”。`build_app_container()` 现在会在进入 builder phase 前先识别 `instance_role`：`main` 仍构造 shared compute 路径（`ingestion / UnifiedIndicatorManager / SignalRuntime / economic calendar sync / paper trading`），而 `executor` 只构造本地 `AccountRuntime`，不会再在 build 阶段创建上述 shared compute 组件。与此同时，`build_market_layer()` 已支持 `include_ingestion / include_indicators`，`build_trading_layer()` 已支持 `enable_calendar_sync`，并新增 `build_account_runtime_layer()` 作为 executor/main-account 的统一账户执行装配入口。`runtime_controls` 也同步从“按角色特判”收口为“按组件存在性启停”，避免未来继续把边界错误藏在 lifecycle 分支里。

46. **executor 的指标与经济日历依赖已改为只读适配，不再反向带入 shared compute 堆栈**  
    为了让 `AccountRuntime` 在没有 `UnifiedIndicatorManager` 和本地日历同步线程的情况下仍然具备持仓管理与风控输入，本轮新增了两个正式只读端口：`ConfirmedIndicatorSource` 与 `ReadOnlyEconomicCalendarProvider`。前者直接基于共享库中的确认 OHLC/indicators 快照为 `PositionManager` 提供最新确认指标，后者则只通过 DB 与 `runtime_task_status` 读取 economic calendar 的共享结果，而不会在 executor 本地启动同步线程。这样 executor 既能继续使用 confirmed 指标和经济事件作为本地风控输入，又不会在 build 阶段重新拥有 shared compute 的运行时职责。已新增 composition-root 级测试，直接验证 executor builder 不再调用 `build_signal_layer / build_paper_trading_layer`，而是只走 `build_account_runtime_layer`。

## 7. 验证记录

本次包含 Indicators 收口与测试同步，已执行：

- 已完成一轮职责边界复核，确认 `query_services`、`runtime`、`manager` 的边界已按新目录收口。
- 已修复 import cycle（`runtime.bar_event_handler` 与 `query_services.runtime`、`pipeline_runner` 与 `runtime` 间）。
- 冒烟测试命令执行通过：  
  `pytest -q tests/indicators/test_core_functions.py tests/indicators/test_flush_event_batch.py tests/indicators/test_manager_intrabar.py -q`
- 回测入口自检通过：  
  `python -m src.backtesting --help`

#### 全量回归（2026-04-11）

- 执行命令：`pytest -q tests`
- 结果：`1214 passed in 61.95s (0:01:01)`
- 说明：全量测试套件已通过，当前审查结果无阻断级回归。
- 语法复核：`python -m py_compile src/api/monitoring_routes/health.py src/api/monitoring_routes/runtime.py`
35. 2026-04-13：自动执行资格从“隐式默认放行”改为“显式合同 + 显式绑定”。`signal.ini` 基线默认 `auto_trade_enabled=false`；每个注册策略都必须显式声明 `[strategy_deployment.<name>]`，否则启动失败；`ExecutionIntentPublisher` 不再对 single-account 做“空绑定自动路由当前账户”，且只为 `allows_live_execution()` 的策略发布 intent。未决项：后续仍需收口 economic decay / confidence floor 与 signal filter / trade guard 双口径问题。
36. 2026-04-13：经济事件渐进降权不再被 `confidence_floor` 回抬。`apply_confidence_adjustments()` 现在一旦命中 `economic_event_decay < 1.0`，后续不再施加底线保护，避免事件窗口里的压制信号被重新抬成可执行候选。未决项：signal filter 窗口与最终 trade guard 窗口仍是双口径，需要进一步统一或显式区分。
37. 2026-04-13：executor 的 storage/indicator 读模型语义已改成正式角色语义，不再把“没有 ingestor / indicator_manager”误报为 critical。`RuntimeReadModel` 现在显式接收 `storage_writer`，executor 的 `storage_summary()` 直接基于 writer 线程和队列状态生成摘要，并把 `ingestion=disabled` 作为正式语义返回；`indicator_summary()` 也在 executor 上直接返回 `disabled`，避免 `/health` 和 `/v1/monitoring/health/ready` 继续把不属于账户执行面的共享计算模块误判为故障。未决项：`trade_executor.enabled` 目前仍混合了“执行器运行中”和“自动交易门打开”两个语义，后续应继续拆开。
38. 2026-04-13：`account_risk_state_projector` 的组件注册顺序缺陷已修复，worker 风险当前态现在会在双实例拓扑中稳定落库并被 main 聚合。根因是 `build_runtime_controls()` 之前先构建了 `runtime_component_registry`，而 `account_risk_state_projector` 在 registry 之后才创建，导致 registry 中该组件的 `supported_modes` 永远为空，worker 即使进入 `full` 模式也不会启动本地风险投影。现已把 projector 的创建与 hook 绑定前移到 registry 之前，再构建 component registry，使 `account_risk_state_projection` 成为真正的账户执行面组件。双实例 smoke 已验证：`live-exec-a` 的 `/v1/monitoring/health/ready` 返回 `account_risk_state=ok`，`/v1/trade/accounts` 可从 main 和 worker 两侧同时聚合看到 `live_exec_a` 的最新 `risk_state`。未决项：`/v1/monitoring/runtime-tasks` 目前仍是全局查询口径，worker 侧会看到共享任务历史，后续应按实例默认过滤。
47. 2026-04-13：signal-domain 的经济事件语义已收口到 `economic.ini / EconomicConfig`，不再让 `signal.ini` 和 runtime evaluator 各自维护一套窗口常量。此前 signal filter 使用 `signal.ini` 里的 `economic_*` 键，而 confidence decay 又在 `runtime_evaluator` 内部硬编码 `-20m / +2h` 查询窗口和固定阶梯衰减，形成两套隐藏语义。本轮新增 `SignalEconomicPolicy`，由 `economic.ini` 的 `pre_event_buffer_minutes / post_event_buffer_minutes / high_importance_threshold / release_watch_*` 派生 signal filter 的硬阻断窗口与 confidence decay 的预热窗口；`EconomicEventFilter` 和 `runtime_evaluator` 统一消费这份 policy，不再各自维护 lookahead/lookback/importance 配置。同时，`signal.ini` 中原有的 `economic_filter_enabled / economic_lookahead_minutes / economic_lookback_minutes / economic_importance_min` 已删除，`get_signal_config()` 会对任何遗留 local 覆盖 fail-fast，强制把 signal-domain 经济事件语义收口到 `economic.ini`。按用户确认，账户侧 `trade_guard_calendar_health_mode` 仍保持 `warn_only`，本轮未改变 fail-open/fail-closed 策略。
48. 2026-04-13：deployment 合同已下沉到执行边界复核，`locked_timeframes / locked_sessions` 不再只依赖上游 signal 编排保证。此前 `StrategyDeployment` 虽然正式承载了 `locked_timeframes / locked_sessions`，并由 `SignalPolicy` 注入到策略路由，但 `run_pre_trade_filters()` 在真正执行前只检查 `status / min_final_confidence / max_live_positions / require_pending_entry`，导致 replay、手工注入 signal 或未来跨入口复用事件时，合同约束可能失效。本轮已在执行前过滤链新增 `strategy_locked_timeframe / strategy_locked_session` 两个正式拒绝原因，并使用 `SignalEvent.timeframe` 与 `metadata.session_buckets`（缺失时回退到 `bar_time / generated_at` 推导）做复核。这样 deployment 合同从“上游约定”升级成“执行前强校验”，账户执行面即使接收到外部注入的 signal，也会在本地拒绝合同外时段/周期的交易。
49. 2026-04-13：`/v1/monitoring/health/ready` 当前已验证是“进程/组件就绪”语义，而不是“市场数据已新鲜”语义。最新 `live-main` 单实例 smoke 中，服务在休盘和历史 OHLC 明显陈旧的情况下仍然快速返回 `ready`，同时日志持续出现 `market_data data latency critical` 和 `Warmup skip (stale bar_time)`。这说明当前 ready 探针只要求 `storage_writer / ingestion / indicator_engine` 进入运行态，不会因为休盘或 quote/bar 新鲜度不足而降级；运维上必须把 `ready` 与 `market_data` 告警分开判读，不能把 `ready=true` 误认为“当前已具备实时交易条件”。
50. 2026-04-13：账户风险当前态里的 `quote_stale` 目前只作为可观测 flag，不参与 `should_block_new_trades` 的最终判定。`AccountRiskStateProjector` 会在本地风险投影中标记 `active_risk_flags += quote_stale`，但 `should_block_new_trades` 只受 `runtime_mode / close_only / circuit_breaker / margin_guard` 影响，不会因为 quote 过期而自动阻断新交易。最新单实例 smoke 已验证：在 `auto_entry_enabled=true`、`close_only_mode=false` 的情况下，账户风险当前态仍会是 `quote_stale=true` 且 `should_block_new_trades=false`。这在休盘/断线/MT5 quote 陈旧场景下属于交易语义未决项，后续需要明确是否应将 quote freshness 上升为账户级硬阻断条件。
51. 2026-04-13：`paper_trading` 的正式职责已重新澄清为 `main` 上的策略验证 sidecar，而不是 demo 专属能力。此前把它从 live main 默认运行态里移除，是把“不要混淆真实账户执行”和“不要装配验证能力”混为一谈，方向过度收缩。现已恢复为：`paper_trading` 只要启用就装配在 `main`，用于 shadow execution / 策略验证；`executor` 仍然严格禁止装配。这样边界更符合真实职责：`main` 拥有共享计算与验证 sidecar，`worker` 只拥有账户本地执行与风控。后续真正需要补的是 observability 语义，把 `paper_trading` 明确标成验证支路，而不是继续从 live main 里移除它。
52. 2026-04-13：`paper_trading` 的可观测语义已从“普通 runtime component”正式收口为 `validation_sidecar`。此前虽然 `main` 上已经恢复装配 `paper_trading`，但 `/health`、`/v1/trade/state` 和 runtime 读模型仍只把它混在普通 `components` 视图里，容易把“策略验证 sidecar 正在运行”和“真实账户执行正在运行”混看。本轮已在 `RuntimeReadModel` 中新增 `paper_trading_summary()` 正式投影，并将其暴露到 `runtime_mode.validation_sidecars.paper_trading`、`dashboard_overview.validation.paper_trading`、`trade_state.validation.paper_trading` 以及根 `/health` 的 `runtime.validation_sidecars.paper_trading`。这样既保留 `runtime_mode.components.paper_trading=true` 作为“组件已装配”的事实，又能明确标注其 `kind=validation_sidecar`、`running/status/session_id/signals_received/signals_executed/signals_rejected` 等验证支路状态。最新单实例 smoke 已验证：`live-main` 的 `/v1/monitoring/health/ready` 正常返回 `ready`，同时 `/health` 与 `/v1/trade/state` 都会把 `paper_trading` 标成 `validation_sidecar`，不再与真实账户执行语义混淆。
53. 2026-04-13：`/v1/trade/dispatch` 的交易载荷归一化已补齐最后一处语义泄漏，`submit_trade + direction` 现在能在开盘 canary 中稳定进入真实 dry-run 执行链。此前 `TradeAPIDispatcher` 虽然会用 `TradeRequest` 解析 `direction -> side`，但 `model_dump()` 仍把原始 `direction` 一起带给 `TradingCommandService.execute_trade()`，导致开盘实测中 `dispatch` 在 API 边界之后仍抛出 `unexpected keyword argument 'direction'`，形成“precheck 已 allow、dispatch 却死于接口契约”的假断链。本轮已在 API 调度器里把归一化后的 payload 显式移除 `direction`，只保留下游正式契约 `side/sl/tp/...`；并补回归测试，明确验证 `submit_trade + direction` 最终只会向下游传 `side`。最新开盘单实例 canary 已验证：`precheck` 返回 `allow`，`dispatch(dry_run)` 返回 `success=true`，`requested_operation=submit_trade` 与 `operation=trade` 同时可见，`trade_command_audits` 里也能看到对应的 `precheck_trade / execute_trade` 记录。
54. 2026-04-13：intrabar 主链当前暴露的是**配置闭环缺失**，而不是引擎全断。最新开盘单实例验证中，`/v1/signals/diagnostics/pipeline-trace?scope=intrabar` 已出现 `XAUUSD/H1` 的 `bar_closed -> indicator_computed -> snapshot_published`，`/v1/ohlc/intrabar/series?timeframe=H1` 与 `/v1/indicators/XAUUSD/H1/live` 也都能返回实时快照，说明 intrabar 基础链路本身是工作的；但 `M30/M15/M5` 仍完全无 intrabar 数据。根因有两层：  
    1) `intrabar_trading.trigger` 当前要求 `M30/M15/M5 <- M1`，而共享 `app.ini[trading].timeframes` 仍是 `M5,M15,M30,H1,H4,D1`，并不包含 `M1`，因此这些父级 TF 的 child close 事件在当前有效配置下根本不可能产生；  
    2) 试图通过 `config/instances/live-main/app.local.ini` 临时把 `M1` 只加到单实例，并不会生效，因为配置系统当前只允许 `market.ini / mt5.ini / risk.ini` 参与实例级 overlay，`app.ini` 仍是全局共享事实源。  
    这意味着 intrabar 当前的真实风险点不是“线程没启动”，而是系统缺少对 `trigger_map` 与有效 timeframes 闭环的一致性校验。后续应在启动期对 `intrabar_trading.trigger` 与 `TradingConfig.timeframes` 做 fail-fast 校验，或明确禁止配置出“父级启用但 child TF 不存在”的无效组合。
55. 2026-04-13：closeout 控制链已按 no-op 业务场景验证通过，`API -> 应用服务 -> 状态投影 -> 审计 -> 文本日志` 语义一致。最新验证中，在无持仓、无挂单的 live-main 场景下调用 `/v1/trade/closeout-exposure`，返回 `accepted=true / status=completed`，并明确给出 `remaining_positions=[] / remaining_orders=[]`；`/v1/trade/state/closeout` 能同步看到 `last_reason / last_comment / actor / action_id / audit_id / idempotency_key`，`/v1/trade/command-audits` 也记录了完整的 `closeout_exposure` 请求/响应载荷，主日志中对应的 `Operator action recorded` 也存在。这说明 closeout 链在“没有可平仓对象”时不会卡在中间态，也不会出现 API 成功但状态/审计不一致的问题。未决项仅剩真实持仓/挂单场景下的部分成功、部分失败和 `runtime_mode_after_manual_closeout` 行为验证。
56. 2026-04-13：intrabar trigger 与有效时间框架的闭环校验已从 warning-only 升级为 startup/hot-reload fail-fast。此前 `build_signal_layer()` 只会对缺失 trigger map 发 warning，导致系统即使在 `intrabar_trading.enabled=true`、`enabled_strategies` 已声明、但 child timeframe 根本不在 `app.ini[trading].timeframes` 中时仍然进入 `ready`，运行后只表现为某些父级 TF 永远没有 intrabar 数据。这轮已把校验前移到信号装配边界，并在 `signal.ini` 热重载时复用同一份合同校验：对每个实际启用的 intrabar 策略，都会验证其活动父级 timeframe 是否存在 trigger 映射，以及该 child timeframe 是否属于全局有效 `trading.timeframes`。若不满足，启动直接失败，热重载也拒绝应用新配置。这样系统不再允许“intrabar 父级启用但 child TF 不在有效时间框架集合里”的静默坏配置继续带病运行。
57. 2026-04-13：`/v1/trade/precheck` 与 `/v1/trade/dispatch` 已开始收口到统一 `AdmissionReport`，不再由各入口各自拼装执行原因。本轮新增 `src.trading.admission.TradeAdmissionService`，把 `precheck_trade` 的账户风控结果、运行时 `tradability`、账户风险当前态和 trade control 统一归并成正式准入报告，并在 API 层作为 `/v1/trade/precheck` 的直接返回模型，以及 `/v1/trade/dispatch` 的附属字段返回。当前 `AdmissionReport` 已正式包含 `decision / stage / reasons / economic_guard / quote_health / trade_control / margin_guard / position_limits / trace_id` 等字段，后续 `ExecutionIntentConsumer`、intrabar 执行协调器与其他执行入口将复用同一服务，避免“同一笔交易不同入口给出不同阻断原因”的语义分裂。
58. 2026-04-13：交易链路 trace 已升级到 admission 级业务解释视图，不再只能看到技术事件时间线。`pipeline_trace_events` 早已具备 `admission_report_appended` 事件底座，但 `TradingFlowTraceReadModel` 过去不会把这类事件提升成正式事实，导致 `/v1/trade/traces` 只能看到 pipeline timeline，不能直接回答“这条信号在准入阶段发生了什么”。本轮已把 admission 事件收口进 `facts.admission_reports` 与 `summary.admission`，并同步补齐 `pipeline_admission` 阶段与 trace list 摘要中的最新 admission 决策/阶段。这样 trace 读模型现在可以直接回答：该链路是否生成过 AdmissionReport、最新决策是 `allow/warn/block`、发生在哪个阶段，而不是要求运维再回头手工拼装 pipeline event payload。
59. 2026-04-13：`/v1/trade/state/stream` 已开始直接消费正式 `pipeline_trace_events` 事实源，而不再只依赖本地状态 diff 进行近似推送。此前 SSE 流只能比较 `trade_control / positions / pending / alerts` 等 snapshot 差异，无法感知 `admission_report_appended / command_completed / risk_state_changed` 这类正式业务事件，导致前端和巡检侧仍需轮询 `/v1/trade/traces` 才能知道执行面发生了什么。本轮已在 `RuntimeReadModel` 新增 `recent_trade_pipeline_events_payload()`，按当前 `instance_id + account_key` 过滤正式 pipeline 事件，并在 `/v1/trade/state/stream` 的 snapshot 与增量 diff 中同步透出。这样 SSE 现在既能继续推送传统状态变化，也能直接推送 admission/intent/command/risk/unmanaged-position 关键事件，状态流开始和 trace 事实源对齐。未决项：`ExecutionIntentConsumer` 与 operator command 消费器侧的事件覆盖还需继续补齐，才能让 stream 看到完整的 intent/command 生命周期。
60. 2026-04-13：`TradeExecutor` 已在 confirmed / intrabar 执行边界正式产出 `admission_report_appended`，执行 runtime 的放行/阻断开始和 API 准入语义对齐。此前 `AdmissionReport` 只在 `/v1/trade/precheck` 与 `/v1/trade/dispatch` 入口生成，confirmed 信号和 intrabar 预览在本地执行面被挡住时，只会发 `execution_blocked` 或写 skip reason，导致“API 看得到准入报告，执行 runtime 看不到” 的语义分叉。本轮已把 admission 事件下沉到执行边界：confirmed 流在全部 pre-trade checks 通过后，会先发 `decision=allow` 的 admission 事件再进入 execution_decided/submitted；confirmed/intrabar 在 `reject_signal()`、intrabar guard 缺失、intrabar gate/cost block 等场景下，也都会正式发出 `decision=block` 的 admission 事件。这样 trace、SSE 和执行日志现在能共同回答“执行面为什么挡住了这条信号”，而不再只在 API 层可解释。
61. 2026-04-13：`src.monitoring.pipeline` 的公共导出已补齐 admission / intent / command / risk / unmanaged-position 常量，避免下游测试和读模型继续绕过正式端口直接引用内部 events 模块。此前新增事件类型虽然已经存在于 `src.monitoring.pipeline.events`，但 `__init__` 仍只导出早期的 execution 类常量，导致执行面测试与潜在调用方若想消费 admission/intent/command/risk 事件，只能直接 import 内部文件，公共 API 与实际事件合同出现脱节。本轮已把这些常量全部补入 `src.monitoring.pipeline.__init__` 的导出列表，后续任何读模型、测试或监控适配层都可以经由正式公共端口消费完整 pipeline 事件族，而不是继续扩大内部模块耦合。
62. 2026-04-13：后台消费链的 trace 合同已开始按正式生命周期补齐，`ExecutionIntentConsumer` 与 `OperatorCommandConsumer` 不再只产出“处理成功/失败”的单点事件。此前 intent/command 后台线程即使已经具备 claim、lease、dead-letter 等状态机，pipeline 事件里仍缺少统一的 trace/account/instance 标识，`ExecutionIntentConsumer` 对 reclaim / dead-letter 也没有正式事件，`OperatorCommandService/Consumer` 则缺少对 `command_submitted / command_completed / command_failed(dead_lettered)` 的测试约束。本轮已将仓储 claim 返回值升级为结构化 transitions（`claimed / reclaimed / dead_lettered`），并在消费器侧补齐正式 trace 上下文：intent 事件现在稳定携带 `trace_id / account_key / account_alias / claimed_by_instance_id / claimed_by_run_id / instance_id / instance_role / signal_scope / source_metadata`，command 事件则统一携带 `trace_id / submitted_by_* / claimed_by_* / response_payload`。同时新增 `tests/trading/test_execution_intents.py` 与 `tests/trading/test_operator_commands.py`，正式覆盖 `intent_reclaimed / intent_dead_lettered / command_submitted / command_completed / command_failed(dead_lettered)` 等关键节点，确保后续 `/v1/trade/traces` 与 `/v1/trade/state/stream` 能把同一条业务链从 signal 一路串到后台消费完成，而不是在 intent/command 环节丢失标识或生命周期信息。
63. 2026-04-13：`/health` 的 `paper_trading_summary()` 合同漂移已补齐防御式语义。根 `/health` 现在会把 `paper_trading` 明确作为 `validation_sidecar` 暴露，但不再假设任何测试替身或部分读模型都已经实现 `paper_trading_summary()`；缺失或异常时会回落到 `disabled` 的正式 sidecar 视图，而不是让健康接口直接 500。对应 `tests/api/test_app_health.py` 已恢复并补断言。
64. 2026-04-13：`multi_account` 下 `main` 的本地执行职责已改为显式绑定驱动，不再默认偷偷持有 worker 职责。`build_signal_components()` 现在只在当前 `main` 账户别名被 `account_bindings` 显式绑定到 live-executable 策略时，才装配本地 `trade_executor / pending_entry_manager / position_manager / execution_intent_consumer`；否则 `main` 只保留共享计算与 `ExecutionIntentPublisher`。`RuntimeReadModel` 也同步把这种场景收口为 `status=disabled / execution_scope=remote_executor`，避免 `/health` 把“没有本地执行器”误报成 `critical`。本轮同时把 `config/topology.ini` 的 live 组收口为最小正式双实例 `live-main + live-exec-a`，并新增 transport canary 回归，直接验证 `intent_published -> intent_claimed -> execution_succeeded` 生命周期。
65. 2026-04-13：`ExecutionIntentPublisher -> ExecutionIntentRepository` 的落表 tuple 合同曾发生一位错位，已正式修复并补回归。根因是 `ExecutionIntentPublisher` 生成的 row 结构为 `(..., symbol, timeframe, payload, status, attempt_count, ...)`，但 `ExecutionIntentRepository.write_execution_intents()` 仍按旧顺序把 `row[8]` 当 payload、`row[9]` 当 status，导致 `timeframe/payload/status` 在真实落表时整体左移。这会让 live `main` 即使生成 confirmed intent，也可能写入畸形记录，进而把 transport 问题伪装成“worker 不 claim”或“链路没触发”。现已按正式 schema 顺序修正映射，并新增 `tests/persistence/test_execution_intent_repo.py`，明确锁住 `timeframe -> payload -> status` 三列顺序。
66. 2026-04-13：双实例 live transport canary 已在真实运行态完成，`main -> intent -> exec-a -> execution_skipped` 事实链可通过 `/v1/trade/trace/by-trace/{trace_id}` 直接审计。本轮先定位出 MT5 `IPC timeout` 的真实前置条件是“终端未预热且主终端需要人工登录密码”，在沙箱外预启动并完成终端登录后，`live-main` 与 `live-exec-a` 均可稳定进入 `ready`。随后使用安全的低置信度 confirmed canary 信号，向 `live_exec_a` 写入 execution intent，并在真实运行中的 executor 上看到完整 trace：`intent_published -> intent_claimed -> execution_skipped`。这证明当前共享 intent 表、worker claim、pipeline trace 持久化和 `/v1/trade/traces` 读模型已经打通。未决项：这轮 transport canary 仍是“合成 confirmed signal”，不代表真实策略窗口中已经自然出现可执行信号；另外 MT5 终端登录态仍是开机前置条件，尚未收口成真正无人值守的纯进程级启动。
67. 2026-04-13：MT5 会话启动契约已从“黑盒 initialize/login”收口为正式 session gate，启动入口、预检 CLI 与 `/health` 现在共用同一套状态语义。`MT5BaseClient` 已新增正式 `MT5SessionState`，显式区分 `terminal_reachable / terminal_process_ready / ipc_ready / authorized / account_match / session_ready / interactive_login_required`；`live_preflight`、`src.entrypoint.web` 与 `src.entrypoint.supervisor` 已统一复用该门禁，并在失败时输出 `terminal_not_running / ipc_timeout / interactive_login_required / login_failed / account_mismatch` 等明确错误码，不再把所有问题折叠成笼统的 `Failed to initialize MT5`。同时，根 `/health` 与 runtime readmodel 也补齐了 `runtime.external_dependencies.mt5_session` 正式投影，避免再把 `ready=true` 误读为“账户已可交易”。未决项：当前仍明确不做 GUI 自动输密码；若 broker 终端要求人工解锁，系统行为是 fail-fast 并提示人工处理，而不是尝试旁路自动化。 
68. 2026-04-13：`quote_stale` 已从“仅观测 flag”正式收口为执行侧硬门禁，`trade_state / precheck / TradeExecutor` 三处语义现已一致。此前账户风险投影虽然会把 `quote_stale` 写入 `active_risk_flags`，但 `should_block_new_trades` 与执行前过滤链都不会因此阻断新交易，形成“读口显示不可交易、底层执行链仍可能放行”的双轨语义。本轮已把执行侧 quote freshness 抽成共享正式函数 `src.trading.execution.quote_health.build_execution_quote_health()`，由 `AccountRiskStateProjector` 与 `TradeExecutor` 共用同一套阈值：至少 `3s`，并同时参考 `quote_stale_seconds / stream_interval_seconds` 的 `3x`。现在只要执行侧 quote 判 stale，风险投影就会把它纳入 `should_block_new_trades`，`TradeExecutor.run_pre_trade_filters()` 也会以正式 reason `quote_stale` 本地拒绝 confirmed/intrabar 交易；`TradeAdmissionService` 则避免在 `last_risk_block=quote_stale` 时再追加一条泛化的 `risk_block_new_trades`，减少同一事实在报告中的重复表达。未决项：后续仍需把 bar freshness / intrabar freshness 也收口到同一类 market-tradability gate，避免只有 quote 被硬阻断而 OHLC/child-close 仍停留在观测告警层。 
69. 2026-04-13：`operator command` 结果合同已从“service/consumer/application 三层各自补字段”收口为单一正式构造器。此前 `OperatorCommandConsumer._normalize_existing_action()` 与 `OperatorCommandService._build_existing_response()` 都在用 `setdefault()` 把旧形状结果补齐成当前外观，导致 command queue 的完成态既不是单一状态拥有者，也不是单一结果模型。本轮已将结果合同下沉到 `src.trading.commands.results`，把 `accepted / status / action_id / command_id / audit_id / actor / reason / idempotency_key / request_context / message / error_code / recorded_at / effective_state` 收口为正式 `operator command result`；`TradingModule._build_operator_action_response()`、`OperatorCommandService.enqueue()/existing replay`、`OperatorCommandConsumer` 的本地控制命令与已有 operator action 结果绑定，现已全部复用同一构造器，不再保留 `_normalize_existing_action()` 或 `_build_existing_response()` 这类兼容层。定向回归与扩展切片已通过：`tests/trading/test_operator_commands.py` 以及 `test_signal_executor / test_execution_intents / test_pipeline_event_bus / test_trade_trace` 共 `88 passed`。
70. 2026-04-13：普通交易应用服务的返回合同也已开始从“注入式补字段”迁移到正式结果构造器。此前 `TradingModule._run_trade_with_dispatch_controls()`、`_execute_command()`、`TradeExecutionReplayService` 与 `TradeCommandAuditService.fetch_successful_trade_result()` 都会用 `setdefault()` 或“重放时补字段”的方式给 `execute_trade/precheck_trade` 结果附加 `dispatch_precheck / trace_id / account_alias / operation_id / idempotent_*`，导致普通交易结果虽然可用，但不是单一正式合同。本轮新增 `src.trading.application.results`，并已把这些字段收口成显式结果构造：fresh result 通过 `build_trade_operation_result()` 明确生成，dispatch 通过 `attach_dispatch_precheck()` 明确附加，idempotent replay 通过 `build_idempotent_trade_replay()` 明确构造，不再依赖 `setdefault()` 注入式兼容。未决项：下游 `TradingService.execute_trade()` 的原始 MT5 结果仍是较宽松的基础载荷，后续如继续收口，可再向下抽成更强约束的正式 trade operation result。
71. 2026-04-13：`src/trading/` 顶层散落文件已按职责重新纳入对应子包，不再把命令结果、执行健康、运行时基础设施和 MT5 交易服务平铺在领域根目录。当前目录收口为：`src.trading.commands.results`（命令结果合同）、`src.trading.execution.quote_health / intrabar_health`（执行健康门禁）、`src.trading.runtime.registry / lifecycle`（运行时账户与线程生命周期）、`src.trading.application.trading_service`（MT5 交易业务服务）。根目录保留的 `models / ports / reasons / trade_events` 仍是跨子域共享的领域根契约，本轮没有为了“目录整齐”而强行下沉。相关源码与 `app_runtime` 装配、测试 import 已同步迁移，未保留旧顶层路径转发壳。
72. 2026-04-13：`TradingService.execute_trade / precheck_trade` 的结果合同已从“内部 `update/setdefault` 拼装字典”收口为正式 builder。此前 `TradingService` 虽然已经下沉到 `src.trading.application.trading_service`，但其返回值仍在方法内部边执行边拼接：`execute_trade()` 先拿 MT5 原始返回，再追加 `request_id / estimated_margin / precheck / state_consistency`；`precheck_trade()` 则依赖 `assessment.setdefault("warnings"/"checks")` 与后置字段注入补齐合同。这会导致应用层虽然已经有 `src.trading.application.results`，底层服务仍然保留“宽松字典 + 事后补字段”的半收口状态。本轮已把这两条返回路径统一改成正式构造器：阻断 precheck 走 `build_blocked_trade_precheck_result()`，风险关闭走 `build_disabled_trade_precheck_result()`，风险评估结果走 `build_trade_precheck_result()`，dry-run 与真实执行结果统一走 `build_trade_execution_result()`。这样 `TradingService` 只负责交易语义与调用 MT5，结果字段拥有权回到单一 builder，不再在服务方法内部散落 `update/setdefault`。定向回归已覆盖 `precheck disabled contract / dry_run contract / execute_trade structured result`，当前未决项只剩更底层 `MT5TradingClient.open_trade_details()` 的原始 broker payload 仍是宽松结构，后续若继续收口，可再把 broker adapter 的输出建成正式端口模型。
73. 2026-04-13：broker 执行结果与 trading 子包入口已继续收口，`MT5TradingClient` 不再把 `order_send` 的原始 MT5 返回直接当作应用层基础载荷传播。当前已新增正式领域模型 `src.trading.models.TradeExecutionDetails`，由 `MT5TradingClient.open_trade_details()` 与 `TradingService._recover_trade_from_state()` 共同产出，随后统一经 `src.trading.application.results` 投影为对外结果。这样 `requested_price / fill_price / broker_comment / pending / recovered_from_state` 等 broker 语义已先在领域边界建模，再交给应用层补充 `request_id / precheck / state_consistency`，不再把 broker comment 和应用 comment 混在同一宽松 dict 中。同时，本轮还修正了更底层的包边界问题：`src.trading.__init__`、`src.trading.application.__init__` 与 `src.trading.runtime.__init__` 曾是重型聚合入口，任何人只要 import 一个子模块都会先拉起整棵 trading 树，直接导致 `mt5_trading -> trading.models` 这类正常依赖触发循环导入。现已将三者收口为轻量、无副作用入口，容器、API、executor、consumer 与测试调用方全部改为直接依赖具体模块（如 `application.module/services`、`runtime.registry/lifecycle`），不再经由聚合 `__init__` 间接穿透整层目录。定向与扩展回归已覆盖 `broker result contract + signal executor/intents/API/container` 切片，共 `172 passed`。
74. 2026-04-13：MT5 trade comment 已从“长字符串标签 + 散落匹配规则”收口为正式 broker comment codec，持仓/挂单恢复不再以全文 comment 为主身份键。此前写路径会先在上层生成带 `:` 的逻辑 comment，再在 `MT5TradingClient._normalize_comment()` 中被 broker-safe 过滤成另一种字符串；与此同时，`TradingService._recover_trade_from_state()`、`pending_orders.find_live_position_for_pending_order()`、`PositionManager._is_restorable_comment()`、runtime unmanaged-position 判定又各自维护一套 comment 比对和前缀猜测，导致“人类可读标签”和“broker 恢复身份”混在一起。本轮新增 `src.trading.broker.comment_codec` 作为唯一 comment 合同：写路径统一产出 27 字符以内、只含 broker-safe 字符的短格式 `TF_LABEL_ACTION_TAG`；`TradingService`、market/pending 执行入口都复用同一构造器；恢复与监控则优先按 `request_tag` 和正式状态库匹配，只有在没有强身份时才退回语义级 comment 判定。这样 MT5 comment 现在只是“最小可读标签 + 短请求指纹”，不再承担跨模块主键职责；同时，历史 `auto:/agent:/M15:...` comment 的读路径被集中到同一个 codec 中，避免 legacy 兼容分支继续散落在 execution、positions、readmodel 多处。
75. 2026-04-13：`src.trading.execution` 包入口已从重型聚合模块收口为轻量壳，`PendingEntryManager -> execution.sizing` 的循环导入已从根上移除。此前 `pending.manager` 只想引用 `TradeParameters`，却会因为 `import src.trading.execution.sizing` 先执行 `src.trading.execution.__init__`，而 `__init__` 又会立即 import `executor`；`executor` 反向 import `pending.manager`，最终把 `PendingEntryManager` 重新卷回半初始化状态。现已将 `execution.__init__` 改为无副作用入口，并把仓库内对 `src.trading.execution` 的包级导入全部迁到具体子模块（`executor/gate/sizing/...`）。这样 execution 包的职责边界与 application/runtime 一致：包入口只定义命名空间，不再承担“顺手把整层树都拉起来”的隐式装配职责。
76. 2026-04-14：`/trade/from-signal` 的执行阶段错误映射已补齐，不再只在“准备信号”阶段可解释。此前该路由只会捕获 `SignalTradePreparationError`，一旦 `execute_prepared()` 在执行阶段触发 `PreTradeRiskBlockedError` 或 `MT5TradeError`，异常会直接冒泡成 500，导致 API 侧与普通 `/trade` 路由的错误合同不一致。本轮已把风险阻断与 MT5 执行错误抽成共享响应构造，在 `trade()` 与 `trade_from_signal()` 两条路径上统一复用；`trade_from_signal()` 现在会稳定返回正式 `AIErrorCode`、`account_alias` 与交易载荷详情，而不是把 broker/risk 异常暴露成未处理错误。对应 API 回归已新增 `risk blocked / market closed` 场景断言。
77. 2026-04-14：`src.signals`、`src.signals.orchestration` 与 `src.trading.commands` 的包入口已继续收口为轻量命名空间，不再承担内部聚合导出职责。此前即使 `execution` 包已经完成去重型 `__init__`，`signals.__init__` 仍会在导入 `src.signals.contracts` 这类轻量子模块时顺手拉起 `SignalRuntime` 与 orchestration 全栈；`trading.commands.__init__` 也仍会把 consumer/service/results 一次性拉入 API 与 container 导入路径。这会继续放大启动期依赖扇出，并保留新的隐藏循环导入风险。本轮已把仓库内所有内部调用方迁到具体子模块（如 `orchestration.runtime/policy`、`commands.service/consumer`），并将三个 `__init__` 收口为无副作用轻量壳。这样包入口只保留命名空间职责，不再作为“兼容聚合出口”长期存在。
78. 2026-04-14：`trade_routes/commands.py` 中剩余的批量交易、保证金估算与改单接口已补齐统一错误合同，不再只有 `/trade` 具备正式 broker/risk 错误映射。此前 `/trade/batch` 完全没有异常边界，`/estimate_margin` 会把所有 `MT5TradeError` 一律映射成 `INSUFFICIENT_MARGIN`，`/modify_orders` 与 `/modify_positions` 也只做固定 `TRADE_MODIFICATION_FAILED` 响应，导致交易 API 存在“不同入口同类错误却给出不同语义”的双轨问题。本轮已把这几条路由统一接到共享 MT5 错误映射与兜底异常响应：`market closed / invalid volume / position_not_found / order_not_found` 等现在会稳定返回对应正式 `AIErrorCode`，未知异常也会以结构化错误返回，而不是裸 500。
79. 2026-04-14：`ModifyPositionsRequest` 已补上正式 `ticket` 合同，API 层不再截断下游已有的单持仓改单能力。此前 `TradingService.modify_positions()` 与 `MT5TradingClient.modify_positions()` 已支持按 `ticket` 精准改单，但 FastAPI schema 与路由完全没有暴露该字段，导致上层只能做“按 symbol/magic 批量改单”，无法使用底层已存在的正式能力。本轮已把 `ticket` 加入 `ModifyPositionsRequest`，并在 `/modify_positions` 路由中显式透传到应用服务与响应 metadata，同时新增 API 回归，直接验证 `ticket=404` 会进入底层并映射成 `POSITION_NOT_FOUND`。这样上层合同与下游端口重新对齐，不再依赖隐式能力或后门调用。
80. 2026-04-14：`src.api.trade_routes.commands` 已从“单文件聚合所有写接口实现”收口为纯路由组合入口，写接口按职责拆入正式子模块。此前 `commands.py` 同时承载了直达交易执行、signal trade、operator command、批量写操作和共用错误辅助函数，单文件体量已超过 800 行，后续任何新交易接口都容易继续堆回这个模块，重新形成 API 层的隐式耦合。本轮已将其拆为：`direct_commands.py`（直达交易/预检/批量/改单/对账）、`signal_commands.py`（signal->trade 执行）、`operator_commands.py`（close/cancel/trade_control/closeout 等 operator queue），`execution_common.py` 只保留跨直达交易入口共享的错误映射与 signal trade 构造。`commands.py` 本身现在仅负责 include 子路由，不再承担实现职责；`src.api.trade` 也已改为直接从对应子模块导出函数。这样 API 写接口边界重新和应用层职责对齐，避免 `commands.py` 继续演化成新的重型兼容模块。
81. 2026-04-14：`trade_routes` 的写路由目录已继续按“类型”归类，不再在根目录平铺实现文件。此前虽然 `commands.py` 已拆出 `direct_commands.py / signal_commands.py / operator_commands.py / execution_common.py`，但这些实现仍直接散落在 `trade_routes/` 根目录，目录层级本身无法表达“这是写入命令域”的边界。本轮已将其统一下沉到 `src.api.trade_routes.command_routes/`：`direct.py`、`signal.py`、`operator.py`、`common.py` 各自只负责一类写接口或共享错误映射；`commands.py` 保留为唯一组合入口，负责 include 与对外导出。旧的根目录实现文件已删除，没有保留双轨路径。这样 `trade_routes/` 根目录只留下顶层命名空间入口（`commands/runtime/state/trace`），实际实现则按命令类型进入子包，目录结构与职责边界一致。
82. 2026-04-14：`src.api.trade_routes.state` 已从重型读路由模块收口为”总览/列表/审计/流”四类正式子模块，读侧根入口只保留组合职责。此前 `state.py` 在单文件内同时承载账户总览、positions/orders 查询、pending/position runtime 列表、command audits、SL/TP 历史以及 SSE state stream，混合了多个读模型视图和一整套流式 diff helper，已经演化成读侧的第二个重型聚合点。本轮已将其拆入 `src.api.trade_routes.state_routes/`：`overview.py` 负责总览和账户视图，`lists.py` 负责 runtime 状态列表，`audit.py` 负责命令审计与 SL/TP 历史，`stream.py` 负责 `trade_state_stream` 及其 snapshot/diff 逻辑，`state.py` 只负责组合这些读路由并对外导出正式入口。对应测试也已迁移到新子模块路径，不再依赖对旧重型模块的私有 monkeypatch。这样 `trade_routes` 目录现在在结构上明确区分”写命令组合入口”和”读状态组合入口”，后续继续扩展 API 时不需要再把不同类型的路由平铺回根目录。

---

83. 2026-04-14：**P0 修复：`TrackedPosition.initial_risk` 永不初始化导致 Chandelier Exit 运行时完全失效**。
    审查发现 `PositionManager.track_position()`（`src/trading/positions/manager.py`）构造 `TrackedPosition` 时从未写入 `initial_risk` 或 `initial_stop_loss`，两者持续为默认 0.0；`sync_open_positions()` 恢复路径同样没有写入这两个字段；全 `src/` 目录对 `.initial_risk = ...` 的赋值点为零。而 `_evaluate_chandelier_exit()` 的第一条守卫 `if pos.initial_risk <= 0: return None`（manager.py:698）会直接短路返回，导致 trailing stop、breakeven、锁利梯度、信号反转 N-bar 退出、超时退出、硬上界 TP 等所有实盘监控规则都从未被触发。实盘/Paper 持仓实际仅依赖 broker 端静态 SL/TP 与日终平仓。回测侧 `BacktestPortfolio` 正确计算了 `initial_risk = abs(entry_price - stop_loss)`，所以回测结果看似正常，但这等于"回测结论无法迁移到实盘"。
    本轮修复：
    - `track_position()` 入场时显式写入 `pos.initial_stop_loss = params.stop_loss` 与 `pos.initial_risk = abs(entry_price - initial_stop_loss)`。
    - `sync_open_positions()` 恢复时优先从持久化的 `merged_context["initial_stop_loss"]`（已存在于 `position_runtime_states` schema 与 `TradingStateStore`）读取，只有在从未持久化过的历史持仓上才 fallback 到当前 MT5 SL 作为近似 baseline；随后统一推导 `initial_risk` 与 `sl_atr_mult`。
    - 因 DB schema 已有 `initial_stop_loss` 列，无需扩 schema；`initial_risk` 可从 `initial_stop_loss` 与 `entry_price` 在运行时纯算，不需要独立持久化列。
    - 新增 3 个回归测试守住入场与恢复两条路径（`tests/trading/test_position_manager.py` 末尾）：`test_track_position_initializes_chandelier_baseline`、`test_sync_open_positions_restores_initial_risk_from_persisted_state`、`test_sync_open_positions_falls_back_to_current_sl_when_unpersisted`，防止未来再次潜伏。
    影响范围判定：这是一个长期潜伏 bug，不是重构引入，但直接决定 Paper Trading / 实盘上线前的核心监控是否生效。修复前 Paper Trading 的任何数据都不能等同于回测结果，修复后两侧的 Chandelier Exit 终于走的是同一套状态机。

---

84. 2026-04-14：**Chandelier / exit_profile 配置已从 signal.ini 拆出到 config/exit.ini，支持实例级覆盖**。
    多实例架构评估发现：虽然 `risk.ini` 早已在 `_INSTANCE_SCOPED_CONFIGS`（支持 `config/instances/<name>/risk.ini` 覆盖），但持仓出场参数（`[chandelier]` / `[exit_profile]` / `[exit_profile.tf_scale]`）全部挤在全局 `signal.ini` 里，`signal.ini` 不在 scoped 列表中，实例目录下也没有 `signal.ini` —— 这意味着 main 和 workers 必然共用同一套 Chandelier trail / breakeven / aggression 矩阵，"不同实例走不同 trail 激进度"的诉求在代码层面不可配置。
    本轮按"最小侵入"原则拆分：
    - 新建 `config/exit.ini`，把 `[chandelier]` / `[exit_profile]` / `[exit_profile.tf_scale]` 三段完整迁移过去；`signal.ini` 对应三段一次性删除，不保留双轨兼容。
    - `exit.ini` 加入 `_INSTANCE_SCOPED_CONFIGS`（`src/config/utils.py:11-16`），自动继承已有的 8 层合并机制：全局 `exit.ini` → `exit.local.ini` → `instances/<name>/exit.ini` → `instances/<name>/exit.local.ini`，后者优先。
    - `get_signal_config()` 内部新增一次 `get_merged_config("exit.ini")` 调用，chandelier/exit_profile/tf_scale 三段从 `exit_merged` 读取；`SignalConfig.chandelier_*` 字段与下游 `ChandelierConfig` 构建路径（`factories/signals.py`、`backtesting/engine/runner.py`）完全不动，避免牵连面扩散。
    - 增加启动期 fail-fast 检查 `_assert_exit_sections_moved()`：若 `signal.ini` 仍残留 `[chandelier]` / `[exit_profile]` / `[exit_profile.tf_scale]` 任一 section，直接 raise `ValueError` 并提示迁移到 `exit.ini`，避免"半份配置无感知"的隐性故障。
    - 每个 `config/instances/<name>/` 目录新增 `exit.ini` 和 `exit.local.ini` 模板（全为注释示例，保持默认继承）；`.gitignore` 已有 `config/**/*.local.ini` 递归规则覆盖新增 local 文件。
    - 测试覆盖：`tests/config/test_instance_config_overlay.py` 新增两个用例验证 `exit.ini` 实例级覆盖生效 + `exit.local.ini` 优先级最高；`tests/config/test_signal_config.py` 新增三个用例验证 chandelier 从 exit.ini 读取 + signal.ini 残留段会 fail-fast。
    职责边界改进：出场参数在职责上本就不属于"信号"域，长期在 signal.ini 里是历史遗留。拆出后 `signal.ini` 的语义更收敛（只管信号评估/策略部署/信号过滤链）。后续若进一步把 `ChandelierConfig` 构建从 `factories/signals.py` 搬到 `factories/trading.py` 或独立 `factories/position.py`，边界会更干净，这是 F-3。

---

85. 2026-04-15：**5 分钟实战观察 + 批量运行期问题修复**。
    首次启动 live-main 实例观察 5 分钟（MT5 gate 通过、ingestor/indicator/signal 链路全部健康、Paper Trading 真实开仓 2 持仓浮盈 +12.93），暴露出运行期的一批可观测性与韧性问题：
    (1) **P1.2 Paper Trading 状态不持久化**：虽然 `paper_trading_repo` 的 `upsert_session` 和 `write_trades` 实现完整，但 `PaperTradeTracker.on_trade_opened` 是 `pass`（入场不入队），`save_session` 只在进程 `_stop_paper_trading` 时被调用，`INSERT_TRADE_SQL` 用 `ON CONFLICT DO NOTHING`（后续 close 无法 update 已有 open 记录）。后果：运行中 DB 表始终 0 条，进程崩溃 → 所有 open positions + session metrics 丢失，Paper 对比回测的 P1 验证失去基础。本轮修复：
    - `INSERT_TRADE_SQL` 改为 `ON CONFLICT (trade_id) DO UPDATE SET ...`，支持 open → close 的 upsert。
    - `PaperTradeTracker.on_trade_opened` 入队（不再 pass），利用 upsert 让 open 记录先落库，close 时更新同一行。
    - `PaperTradeTracker` 增加 `set_session_snapshot_provider()` setter，flush 循环主动从 bridge 拉取当前 session 最新 metrics（balance/total_pnl/total_trades），让运行中的 session 也周期性持久化；进程崩溃时至少保留最近一次 flush 的快照。
    - `PaperTradingBridge.snapshot_active_session()` 新端口暴露实时 session 对象（更新 metrics 字段但保持 stopped_at/final_balance=None）。
    - `builder_phases/paper_trading.py` 注入 provider 回调，完成闭环。
    (2) **P2.3 Supervisor 组内韧性**：之前 `_start_initial` 调 `ensure_topology_group_mt5_session_gate_or_raise` 对整组做原子预检，任一 worker MT5 terminal 缺失 → 整组拒绝启动（连 main 都起不来）。生产中 worker terminal 偶发崩了就不能用了。改为 per-instance gate：main 必须通过（fail-fast），workers 失败仅 warn 跳过不阻断 main；`_monitor_loop` 重启路径同样区分 main（不可恢复则终止）与 worker（gate 失败延迟下次 loop 重试）；`_spawn` 不再内部 gate（由调用方负责）。新增 3 个定向回归测试守住 main fail-fast、worker skip、spawn 无副作用。
    (3) **P2.5 economic_calendar 启动瞬态 alert**：HealthMonitor 启动后 service 的 calendar staleness 可能 > critical 阈值（因为上次 refresh 是进程重启前），但 5 秒内 refresh 会让它回归，告警历史仍被污染。加启动 grace 期（60s），仅豁免"启动瞬态类"指标 `economic_calendar_staleness` / `indicator_freshness`；`data_latency` 等运行时数据流指标不豁免。补 3 个回归测试。
    (4) **P2.6 HTF stale warning 洪水降级**：`htf_resolver.py` 里 HTF bar > max_age 时的 log 级别从 WARNING 降到 INFO，前 3 次打 + 每 200 次打（原前 5 + 每 50）。HTF stale 时的实际行为是 skip injection 让策略 fallback / regime 兜底，不是 error。周末市场休市时 H4 自然过时，不应污染 errors.log。
    (5) **P2.4 Windows 乱码**：核实后发现 log 文件本身是正确 UTF-8（RotatingFileHandler 已 `encoding='utf-8'`），乱码只出现在 **Python 进程 stdout**（Windows 默认 cp936/GBK）。`web.py` / `supervisor.py` 启动时调用新增的 `_force_utf8_stdio()`（`sys.stdout/stderr.reconfigure(encoding='utf-8', errors='backslashreplace')`），彻底消除 stdout 乱码，不依赖用户 Windows 系统设置。
    (6) **P3.7 信号质量端点文档漂移**：TODO.md 中 `/signals/monitoring/quality` 缺路径参数，实际端点是 `/signals/monitoring/quality/{symbol}/{timeframe}`。更新 TODO.md。
    (7) **P3.8 multi_account main 状态语义**：`RuntimeReadModel.trade_executor_summary()` / `pending_entries_summary()` / `position_manager_summary()` 在 `_shared_compute_main_without_local_execution()` 下返回 `status="disabled"` 让 dashboard 误以为故障，实际是拓扑正确"委托给 workers"。新增 `state="delegated"` 字段明确语义（保留 `status="disabled"` 兼容旧前端）。
    (8) **P1.1 trade/state/overview 为端点名误写**：5 分钟观察报告中的 "`/v1/trade/state/overview` 返空" 属于我写错端点名，实际 overview 职责由 `/trade/state` 端点承担（`overview.py` 注册的是多个端点 `/trade/state` / `/trade/accounts` / `/positions` 等）。非 bug，无代码变更。
    本轮所有改动 1362 测试通过（原 1357 + 新增 5：3 supervisor 弹性 + 2 monitoring grace）。F-3（ChandelierConfig 职责迁移）仍未解，新增 F-4 / F-5 见下。

---

86. 2026-04-15：**F-4 / F-5 关闭：Paper 持久化完整性 + Supervisor worker 自动补齐**。#85 修了 Paper 持久化主路径后仍遗留两个韧性缺口，本轮一次性收口：
    (1) **F-4.1 首次 flush 窗口**：`PaperTradingBridge.start()` 创建 session 后要等 30s flush_interval 才入库，这段窗口里崩溃会丢起始记录。`PaperTradeTracker.start()` 增加末尾 `_flush_now()` 立即刷一次；同时调整装配顺序 `_start_paper_trading`：bridge 先 start（session 创建）、tracker 后 start（立即 flush 能拉到 session）。
    (2) **F-4.2 open trade 运行时字段同步**：之前 tracker 只在 `on_trade_opened` / `on_trade_closed` 事件点入队，open 期间的 `MFE / MAE / current_sl / bars_held` 变化不进 DB。新增 `PaperTradeTracker.set_open_trades_snapshot_provider()`，flush 前主动从 bridge 拉所有 open records 追加到 pending 队列；`PaperTradingBridge.snapshot_open_trades()` 返回 `dataclasses.replace` 副本避免锁外并发修改。配合 #85 的 `INSERT_TRADE_SQL ON CONFLICT DO UPDATE` 语义，DB 里 open 行的出场相关字段随价格推进定期刷新。
    (3) **F-4.3 进程重启 recovery**：之前进程崩溃后所有 open trades 和 session metrics 内存丢失。新增 `PaperTradingConfig.resume_active_session`（默认 False，兼容现有行为）；开启后 `PaperTradingBridge.__init__` 接受 `recover_fn` 注入，`start()` 先调 `_attempt_resume()`，从 `paper_trading_repo.fetch_latest_active_session()` + `fetch_open_trades()` 读 DB，session_id 复用、`PaperPortfolio.restore_baseline(balance=initial + total_pnl)` + `restore_open_trade(record)` 把老持仓放回 `_open_positions`，active_symbols 随 symbols 重建。`builder_phases/paper_trading.py` 里当 flag 启用且 db_writer 可用时封装 recover_fn 交给 bridge；失败走 fresh start（异常被吞且记 warning）。
    (4) **F-5 Supervisor 未启动 worker 周期重试**：启动期 gate 失败的 workers 之前需要用户手动重启 supervisor 才能补齐。`Supervisor` 新增 `_pending_workers: set[str]` 和 `_PENDING_WORKER_RETRY_INTERVAL_SECONDS = 30`，`_start_initial` 中 worker gate 失败时加入 pending；`_monitor_loop` 每轮末调用 `_retry_pending_workers()`，用 monotonic 节流，gate 通过就 spawn 并从 pending 移除。现在用户启动 MT5 terminal 后 30s 内 supervisor 会自动补启 worker，无需介入。
    测试覆盖：
    - 新增 `tests/backtesting/test_paper_trading_persistence.py` 9 个用例（start 立即 flush / open trades provider / provider 异常容错 / 副本独立性 / resume flag 关闭 / resume 复用 session+open / recover None fresh start / recover 异常 fresh start）
    - 扩展 `tests/entrypoint/test_supervisor.py` 2 个用例（pending worker 重试 spawn / 节流行为）
    全量 1373 通过（原 1362 + 11）。F-4 / F-5 在 Follow-ups 中标记 closed。

---

87. 2026-04-15：**MT5 session 自动建立的设计缺陷修复 — initialize 必须一次性带完整凭据**。
    用户反馈"启动应该自动拉起 MT5 terminal，不应该要求人工开"。审查发现两个层叠问题：
    (1) **gate 层**：`probe_mt5_session_gate` / `MT5BaseClient.connect()` 用 `require_terminal_process=True` 提前在 terminal 未跑时就拒绝，**让 `mt5.initialize(path=...)` 自带的"自动拉起 terminal"能力根本跑不到**。改为默认 `auto_launch_terminal=True` → `require_terminal_process=False`，让 initialize 自己处理 terminal 启动；为 dry-run preflight / runtime health 等不应有副作用的诊断场景保留 `auto_launch_terminal=False` 严格模式。
    (2) **initialize 凭据缺失**（更深层的设计缺陷）：`_initialize_kwargs()` **只传 `path`**，不传 `login/password/server/timeout`。MT5 库的 `mt5.initialize(path, login, password, server, timeout)` 是"完整 session 建立"接口，传入完整凭据会自动完成"拉起 terminal + 自动登录 + 建立 IPC"。当前代码把它**人为拆成两步**：`initialize(path)` → `login(login, password, server)`，结果 terminal 拉起来后**停在登录界面等人工**，IPC 建立不了 → `ipc_timeout`，login 根本走不到。这是补丁式拆分，非单一职责。本轮把 `_initialize_kwargs` 收口为返回完整凭据集 `{path, login, password, server, timeout=60000}`，让 initialize 一次性建立 session；`_login_kwargs` 保留但**职责收窄**为"账户切换"语义（仅当 IPC 已就绪但当前账户不匹配时调用）。
    实战验证（5 分钟跑 supervisor --group live）：
    - **零人工介入**：用户关闭所有 MT5 terminal 进程，supervisor 启动后两个 terminal（TradeMax + TMGM）均被 mt5.initialize 自动拉起 + 自动登录
    - 启动时序：`08:38:19` supervisor → `08:38:22` main gate passed (3s) → `08:38:40` worker gate passed (21s) → 完整双实例就绪
    - F-4.1 立即 flush 验证：Paper session 在启动 14 秒内入库（`paper_trading_sessions` 1 条 active session）
    - F-4.2 open trades 字段同步验证：3 个 OPEN 持仓在 DB，MFE 从 0.21 持续累积到 1.10（运行时持续推送 DB）
    - P0 initial_risk 修复验证：MFE/MAE 持续累积证明 Chandelier 评估在跑（修复前 `initial_risk=0` 直接 return None）
    - P2.5 / P2.6 验证：errors.log 自启动后**仅 1 条 WARNING**（合理 OHLC gap reset），HTF stale 0 条 / economic_calendar critical 0 条（vs 修复前 50+ HTF stale + 1 economic critical）
    - P2.4 验证：日志正确显示 `max=100MB×10`（vs 修复前 `max=100MB��10` 乱码）
    测试覆盖：
    - 新增 `tests/ops/test_mt5_session_gate.py` 4 个用例（默认 auto_launch / strict mode / initialize 失败 fail-fast / auto_launch 参数传递）
    - 新增 `tests/clients/test_mt5_initialize_kwargs.py` 4 个用例（initialize 完整凭据 / login int 强制 / 缺失字段优雅处理 / login_kwargs 仅切换账户）
    - 修正 `tests/core/test_data_integrity_fixes.py::test_mt5_base_client_initializes_session_with_credentials`：旧测试把 "initialize 不带凭据 + 单独 login" 这个有缺陷的旧设计冻结为契约，更新为反映新设计（initialize 一次性带凭据，login 不再被多余调用）
    全量 1381 通过（原 1373 + 8）。F-2 已隐式关闭（auto-launch 让 worker 启动失败的常见路径消失，pending 重试退化为异常路径）。

---

88. 2026-04-15：**F-1 + F-3 一并关闭：DI 重构与 ChandelierConfig 构建职责迁出**。两件事都是"装配层显式注入 / 组件不调全局"的同一原则，合并做避免反复改触动同一区域。
    (1) **F-1 ADR-006 对齐**：`TradingAccountRegistry.__init__()` 增加必需 kwargs `risk_config: RiskConfig` 与 `economic_config: EconomicConfig`；`get_trading_service()` 用 `self._risk_config / self._economic_config` 构造 `PreTradeRiskService`，移除内部 `get_risk_config()` / `get_economic_config()` 全局调用。`build_trading_components()` 工厂签名增加 `risk_config: RiskConfig` 必需参数，`build_trading_layer()` 在装配层显式 `get_risk_config()` 一次性传入。这样：
    - 测试可直接传 stub config（不必 monkeypatch 全局 cache）
    - 两个 registry 实例可持有不同 config 而互不影响（同进程多账户铺路）
    - ADR-006 违规闭环
    (2) **F-3 ChandelierConfig 构建职责迁出**：`factories/signals.py:419-445` 与 `backtesting/engine/runner.py:340-353` 两处重复构造 ChandelierConfig 的代码块抽出为单一构建入口 `src/trading/positions/exit_rules.py:build_chandelier_config(source)`。该函数接受任何带 `chandelier_*` 字段的对象（duck typing；当前 SignalConfig 满足，将来若拆出独立 ExitConfig 也满足）。两处调用点改为单行调用 `build_chandelier_config(signal_config)`。这样：
    - DRY：去掉 ~30 行重复代码
    - 单一职责：出场参数构建归 `exit_rules` 模块（与 `ChandelierConfig` / `profile_from_aggression` 同位）
    - 防漂移：未来改 chandelier_* 字段映射只需改一处，不会出现"实盘改了回测忘改"
    测试覆盖：
    - 新增 `tests/trading/test_account_registry_di.py` 4 个用例（必需 kwargs / 注入对象持有 / 多实例隔离 / 透传到 PreTradeRiskService）
    - 新增 `tests/trading/test_chandelier_config_builder.py` 4 个用例（全字段映射 / mapping 副本独立 / alpha 派生 default_profile / duck typing 接受）
    全量 1389 通过（原 1381 + 8）。F-1 / F-3 在 Follow-ups 中标记 closed。

---

89. 2026-04-15：**T3 + T4 + T5 测试覆盖打包补齐**：之前重构后的覆盖盲区一次性清理。
    - **T3 AdmissionService 零单测 → 18 用例**：之前 `TradeAdmissionService` 仅靠集成测试隐式覆盖，关键决策路径（reasons 形成 / decision allow|warn|block / stage 优先级 / trace_id 兜底链 / pipeline_event_bus emit / deployment_contract / position_limits 字段填充）一旦改动无回归保护。新增 `tests/trading/test_admission_service.py` 18 个用例，每条决策分支（precheck pass/fail / runtime_absent / circuit_open / quote_stale / event_blocked / calendar_health_degraded / account_risk + quote_stale 去重边界）独立验证。
    - **T4 OperatorCommandConsumer 测试 5 → 22 用例**：原 `tests/trading/test_operator_commands.py` 仅覆盖 enqueue / 单条命令 trace / dead-lettered / close_position / cancel_orders 5 个用例，缺失线程生命周期 / 异常恢复 / 命令分支依赖缺失等高频运维场景。新增 `tests/trading/test_operator_command_consumer_lifecycle.py` 17 用例：start/stop/restart 线程 idempotency、_process_command 异常 → command_failed、各种依赖缺失（runtime_mode_controller / trade_executor / exposure_closeout_controller / pending_entry_manager）报错、未知 command_type ValueError、reset_circuit_breaker 状态快照、set_trade_control + reset_circuit 复合调用、runtime_mode partial_failure 路径、heartbeat_fn 调用契约、_worker batch 中途 stop_event 打断。
    - **T5 OwnedThreadLifecycle 零独立测试 → 12 用例**：lifecycle 工具类之前只在 consumer 测试中隐式覆盖，ADR-005 关键 contract（"join 超时后必须保留仍存活线程引用，防止双线程消费"）没有专门守护。新增 `tests/trading/test_owned_thread_lifecycle.py` 12 用例覆盖 is_running 三态 / ensure_running 三场景（idle/alive/dead）/ wait_previous 引用清理 + ADR-005 超时保留 / stop 干净退出 + ADR-005 contract / idempotency。
    本轮纯增量测试，零产线代码改动，零回归风险。全量 1436 通过（原 1389 + 47）。

---

90. 2026-04-15：**架构师全项目审查 — 5 个真问题修复 + 6 项误报识别 + 6 项大重构记 follow-up**。
    用 3 个并行 Explore agent 扫描 + 关键事实核验后修复以下：
    (1) **B1 ADR-006 违反**：`api/research_routes/routes.py:86` 用 `repo._writer` 私有访问；`exp_repo._execute(...)` 直接写 SQL 也是越权。修复：`BacktestRepository` 加公开 `writer` property；`ExperimentRepository` 加 `link_to_mining_run()` 公开方法（与已有 `advance_to_backtest` / `advance_to_paper` 同语义）+ 配套 `LINK_MINING_RUN_SQL` schema 常量；API 路由改为 `repo.writer` + `exp_repo.link_to_mining_run(...)`。
    (2) **B2 PendingEntryManager DRY 违反**：手写两份 50 行线程启停代码（`_monitor_thread` + `_fill_worker_thread`），与 `OwnedThreadLifecycle` 工具类的 ADR-005 contract 重复实现。修复：双 lifecycle 实例（共享 `_stop_event`），`start()` / `shutdown()` 改为调 lifecycle.wait_previous / ensure_running / stop；删除 ~50 行重复的 join/timeout/僵尸清理代码。新增 4 个 lifecycle 集成测试（启停/重启/idempotent/引用清理）。
    (3) **B7 双轨配置补丁**：`indicator_config.py` 的 `ConfigLoader.load()` 自动检测 yaml/yml/json 三种路径 + `from_yaml()` 在 PyYAML 缺失时静默 fallback 到 JSON。这是典型"补丁式兼容"——调用方无法预知 source of truth 切换。grep 全 src 确认 `from_yaml` 无外部调用 + 实际配置只有 `config/indicators.json`。修复：删除 `from_yaml()` 方法；`load()` 收口为单一 JSON 入口；遇 yaml/yml 路径 `raise NotImplementedError` fail-fast。
    (4) **B8 真 Bug 揭示**：审计 `breakeven_applied` 字段时发现它**不是简单冗余**（DB schema `position_runtime_states.breakeven_applied` 列 + TradingStateStore 写入），而是与运行时 `breakeven_activated` 字段语义不同（DB 持久化历史标志 vs 运行时活跃状态）。**真 bug**：`reconciliation.sync_open_positions()` 恢复持仓时只设 `breakeven_applied`，未同步 `breakeven_activated`。后果：`_evaluate_chandelier_exit` 误以为 breakeven 还没激活，可能基于"未激活"前提重复触发 breakeven 移动逻辑（SL 的 max() 会保护实际位置不回退，但 lock_ratio 等下游计算前提错乱）。修复：reconciliation 恢复时若 `breakeven_applied=True` → 同步 `breakeven_activated=True`；`TrackedPosition` 字段加注释明确两者语义差异；新增 2 个回归测试守住"持久化标志同步到运行时活跃状态"+"未持久化时保持默认 False"。
    (5) **supervisor.py:180 注释清理**：将"临时把 restart_count 累加"改为详细说明（与现有指数回退公式一致），避免后续审查再误判为补丁。
    
    **核验后识别的 6 项 Agent 误报**（不修，记录避免重复审）：
    - F1 "compute_breakeven_sl 分母为零"：grep 全 src 零外部调用，全在 `evaluate_exit` 入口 `initial_risk > 0` 守卫内
    - F2 "PendingEntryManager 双线程竞态"：shutdown 用 stop_event 协调，两线程独立 join 设计正确
    - F3 "OwnedThreadLifecycle 线程泄漏"：Agent **反向理解 ADR-005**——实际代码超时**保留**引用（防双线程消费），Agent 误读为"清空导致泄漏"。新增的 12 个测试已守护
    - F4 "modify_sl 先赋值后调用"：实际顺序是 MT5 API + 校验通过后才赋值 `pos.stop_loss`，设计正确
    - F5 "_INSTANCE_SCOPED_CONFIGS 应加 topology.ini"：topology 是全局拓扑定义，实例级覆盖会破坏 group 一致性
    - F6 "supervisor restart_count 'temp' 是补丁"：实际是合法指数回退累加，仅注释措辞问题（B5 已优化）
    
    **本会话不实施 — 记 follow-up F-6 ~ F-11**：
    - F-6 拆分 RuntimeReadModel（1523 行 → 4 facade）
    - F-7 拆分 TradeExecutor（1276 行 → 3 职责类）
    - F-8 factories/signals.py Factory 重组（1076 行 18 函数）
    - F-9 TradingModule 拆分（1051 行）
    - F-10 Config 模块职责分离（centralized.py + signal.py 共 1246 行）
    - F-11 API root `__init__.py` 工厂化（24 子路由 import 重型）
    
    全量 1442 通过（原 1436 + 6：4 lifecycle + 2 breakeven sync）。

---

## Follow-ups（未决项）

### ~~F-1：ADR-006 对齐 —— 风控/经济日历配置改为构造函数注入~~（2026-04-15 由 #88 解决）

<details>
<summary>原 F-1 备份（已解决）</summary>

**现状**：`src/trading/runtime/registry.py:88` 中 `TradingAccountRegistry.get_trading_service()` 内部直接调用 `get_risk_config()` 与 `get_economic_config()` 两个全局函数来构造 `PreTradeRiskService`。这违反 ADR-006（装配层/组件内不应读全局配置函数），也让测试需要 monkeypatch 全局函数而不能直接注入 mock。

**风控隔离现状**：当前 supervisor 多进程架构下，每个实例进程有独立的 `MT5_INSTANCE` 环境变量，`get_merged_config(“risk.ini”)` 通过 `_INSTANCE_SCOPED_CONFIGS` 机制已能正确加载 `config/instances/<name>/risk.ini` 和 `risk.local.ini`。因此**不同实例能配置不同风控参数**，机制已就绪——但目前所有实例目录下的 `risk.ini` 仍是空模板（全注释），实际未利用这个能力。

**待整改**：
1. `TradingAccountRegistry.__init__()` 增加 `risk_config: RiskConfig` 与 `economic_config: EconomicConfig` 构造参数
2. `src/app_runtime/factories/trading.py` 构建时加载并注入
3. 移除 registry 内的全局函数调用
4. 补一个隔离验证测试：模拟两个不同实例名，验证加载出不同的 `RiskConfig`

**触发条件**：下一轮为实例差异化风控写入真实参数时（如 live vs demo 不同的 `daily_loss_limit_pct`）顺手完成。或同进程多实例需求出现时必须完成。

**相关 ADR**：ADR-006（跨模块边界禁止读写私有属性，构造函数注入优先）

</details>

### F-2：`account_bindings` 空配置导致 worker 空转（P0 业务决策）

**现状**：`signal.ini` / `signal.local.ini` 中所有 `[account_bindings.*]` section 的 `strategies =` 都是空的。`ExecutionIntentPublisher._resolve_target_accounts(strategy)` 在 `_account_bindings` 为空时对任何策略都返回空 iterable，意味着 **`execution_intents` 表不会写入任何行**，所有 worker 实例启动后只能 claim 到空集，处于长期空转。

**待整改**：填写 `signal.local.ini` 的 `[account_bindings.<alias>]`，决定策略如何在 main / workers 间分配。两种方案：
- 方案 A（main 只做共享计算）：`live_main.strategies =` 留空，workers 承接所有策略。
- 方案 B（main 也执行部分策略）：main 承接一部分，workers 承接另一部分。

**触发条件**：Paper Trading 即将开跑或上 live canary 前必须完成。否则 workers 是僵尸进程。

**为什么不在本轮代码变更中完成**：策略→账户分配是**业务决策**，不是代码决策，需用户根据策略相关性、风险承担能力等决定。代码架构已经支持。

### ~~F-3：`ChandelierConfig` 构建应从 `factories/signals.py` 挪到 `factories/trading.py` 或 `factories/position.py`~~（2026-04-15 由 #88 解决：抽 `build_chandelier_config()` 到 `exit_rules.py`，与 ChandelierConfig 同模块更符合职责边界，且回测/实盘共用单一入口）

<details>
<summary>原 F-3 备份（已解决）</summary>

**现状**：`src/app_runtime/factories/signals.py:419-445` 中仍在构建 `ChandelierConfig` 并注入 `PositionManager`。但 Chandelier 是持仓出场参数，职责上与"信号评估/策略注册"完全正交，只是历史上 `chandelier_*` 字段挂在 `SignalConfig` 上所以构建在了 signal 工厂里。本轮（#84）把配置文件拆到 `exit.ini` 后，SignalConfig 仅作为数据传递结构，不再有语义绑定。

**待整改**：
1. 可选：把 `SignalConfig.chandelier_*` 字段和 `aggression_overrides` / `tf_trail_scale` 单独抽成 `ExitConfig` pydantic 模型，由独立的 `get_exit_config()` 加载。
2. `factories/signals.py` 中 `_ChandelierConfig` 构建代码块挪到 `factories/trading.py` 或新增 `factories/position.py`。
3. `build_account_runtime_layer` 不再通过 `signal_config` 拿 chandelier 字段，改为直接接收 `ExitConfig` / `ChandelierConfig`。

**触发条件**：下次修改持仓出场逻辑（如新增 exit rule）时顺手做；或 F-1 一起做（都是构造函数注入的重构）。

**相关**：#84（本轮 exit.ini 拆分），F-1（ADR-006 对齐）

</details>

### ~~F-4：Paper Trading session 持久化的完整性审计~~（2026-04-15 由 #86 解决）

### ~~F-5：Supervisor worker gate 失败后的自动恢复~~（2026-04-15 由 #86 解决）

### F-6：拆分 RuntimeReadModel（1523 行上帝类）

**现状**：`src/readmodels/runtime.py` 单文件混合 4 个域读模型（health / signal / execution / storage）。

**待整改**：拆为 `runtime_health.py` / `runtime_signals.py` / `runtime_execution.py` / `runtime_storage.py`；`RuntimeReadModel` 退化为 facade 仅做组合；API 层按需直接 import 子 facade。

**预估**：3-5 天。**触发条件**：下次新增 read model 字段或测试层重构时一并做。

### F-7：拆分 TradeExecutor（1276 行 76 方法）

**现状**：决策 / 执行 / 结果记录 / 生命周期 / 熔断 / intrabar guard 全混在一个类。

**待整改**：提取 `ExecutionDecisionEngine`（`_check_*` / `_decide_*`）+ `ExecutionLifecycle`（start/stop/熔断状态机）；TradeExecutor 退化为协调器。

**预估**：4-6 天（实时交易核心，需严格回归）。**触发条件**：下次修改执行逻辑时拆出来。

### F-8：factories/signals.py Factory 重组（1076 行 18 函数）

**待整改**：合并为 2-3 个高阶函数 `build_signal_layer` / `build_account_runtime_layer`；内部细节函数加 `_` 前缀；可拆为 `factories/signals/builder.py` + `strategies.py` + `runtime.py`。

**预估**：2-3 天。**触发条件**：下次新增信号工厂步骤时一并重组。

### F-9：TradingModule 拆分（1051 行 47 方法）

**待整改**：已有 `TradingCommandService` / `TradingAuditService` / `TradingQueryService` 子组件；TradingModule 退化为 facade，47 个方法按归属下沉。

**预估**：3-4 天。**触发条件**：与 F-7 同期做（trading 域重构）。

### F-10：Config 模块职责分离（centralized.py 648 + signal.py 598 行）

**待整改**：从 `centralized.py` 抽 `ConfigValidator` 到 `config/validator.py`；从 `signal.py` 抽 `_apply_overrides` 到 `config/signal_override_resolver.py`；centralized.py 退化为纯聚合 + 缓存。

**预估**：2-3 天。**触发条件**：下次新增配置 section 时一并做。

### F-11：API root `__init__.py` 工厂化（332 行，24 子路由 import）

**现状**：API 包 `__init__.py` 在 import 时就 import 24 个子路由模块，启动慢 + 循环风险。

**待整改**：改为 `def create_app() → FastAPI` 工厂函数，路由动态注册。

**预估**：1-2 天。**触发条件**：API 层下次重构或启动性能优化时做。

---

## 2026-04-15 断言核验协议确立 + 累计误报教训

### 背景

2026-04-13~15 在 "架构审计 + 数据流/风控量化评审" 两轮审查中，累计识别 **9 处误报**（前轮 6 处 + 本轮 3 处自生 surface-read 误读），其中多条被初次标记为 "P0/FATAL" 的断言经核验后完全不成立。为避免未来重蹈覆辙，沉淀为硬纪律写入 `CLAUDE.md §12`。

### 9 处误报清单

| # | 误报断言 | 真实情况 | 识别途径 |
|---|---|---|---|
| 1 | `compute_breakeven_sl` 分母为零 | 外部零调用，内部入口守卫 | grep 全 src 无外部调用 |
| 2 | PendingEntryManager 双线程竞态 | shutdown 用 stop_event 协调，设计正确 | 读完整 lifecycle 代码 |
| 3 | `OwnedThreadLifecycle.wait_previous` 线程泄漏 | **反向理解 ADR-005**：超时保留引用是契约 | 读 ADR-005 |
| 4 | `modify_sl` 先赋值后调用 | 实际顺序是 API 调用 + 校验通过后才赋值 | 细读函数体 |
| 5 | `_INSTANCE_SCOPED_CONFIGS` 漏加 topology.ini | topology.ini 是全局拓扑不应实例级覆盖 | 理解语义边界 |
| 6 | supervisor 'restart_count 临时累加' 是补丁 | 合法的指数回退累加 | 完整读循环逻辑 |
| 7 | **DailyLossLimit 未接入 auto-trade** | `trading_service.open_trade()` 内调用 `enforce_trade_allowed` | grep 调用链 |
| 8 | **`TradeFrequency.record_trade()` 从未被调用** | `trading_service.py:377` 调用 `record_trade_execution` → rule.record_trade | grep 全 src |
| 9 | **PnL 熔断器 `enabled=` 空→默认禁用** | `_drop_blank_values` 剥离空值，Pydantic 默认 enabled=True | 追 config loader |

### 共同模式

**停在"看到代码片段像有 bug"就下结论，没走完追查闭环**：
- Agent 子代理：单文件片段扫描，看不到跨模块保护（Pydantic 默认 / normalize / setter 注入）
- 主线（我）：看 ini 字面值就判断默认，没追 config loader 的 normalize

### 硬纪律（已写入 `CLAUDE.md §12`）

断言核验协议：任何 "X 默认禁用 / 未接入 / 从未被调用 / 是 bug" 的断言，写下前必须完成 3 步：
1. 追 Pydantic/dataclass 模型默认值（`src/config/models/`）
2. 追 loader normalize 逻辑（`_drop_blank_values` / `_normalize_*`）
3. 追全 src 调用点（`grep -rn <name> src/`）

任一步未完成 → 只能写"疑似"，不得标 P0/FATAL。子代理返回的"FATAL"结论同样需主线 3 步核验后采纳。

### 边界泄漏角度

本次改动**仅新增工作流纪律**，不触碰代码与架构；作用是降低"凭片段下结论"导致的误导成本（用户时间 / 不必要的改动 / 产生补丁式修复）。未决兼容项：无。

---

## 2026-04-15 F-12：挖掘 forward_return 与回测 Chandelier Exit 的语义鸿沟

### 事件经过

按用户要求做首轮策略挖掘。挖掘工具（`src/ops/cli/mining_runner.py`）对 6 个月 XAUUSD/4TF 数据跑出 15 条 top rules。挑选 3 条 top candidates 编码为 `StructuredStrategyBase` 子类并回测：

| 策略 | 挖掘训练集 | 挖掘测试集 | 回测（原）| 回测（--no-filters）|
|---|---|---|---|---|
| weak_momentum_sell (M15 SELL) | 63.5% / n=7286 | 62.3% / n=2829 | 4 / 75% / PF 0.98 | **94 / 17% / PF 0.04** |
| roc_accel_sell (M30 SELL) | 58.4% / n=1823 | 60.8% / n=561 | 8 / 12.5% / PF 0.08 | **45 / 24% / PF 0.09** |
| squeeze_breakout_buy (H1 BUY) | 72.0% / n=164 | 60.4% / n=53 | 12 / 75% / PF 1.15 | 12 / 75% / PF 1.23 |

### 根因

`MiningRunner` 的 `DataMatrix.forward_return` 定义为"入场后 **N bar** (3/5/10) 的收盘价相对入场价收益"——**短期定点观测**。

回测引擎（`src/backtesting/engine/runner.py`）用 `PositionManager + Chandelier Exit`：
- trailing stop
- regime-based exit
- signal reversal exit
- EOD close
- 平均持仓周期 20-50 bar

同一入场条件下，**短期 forward_return 胜率与长期 trailing-exit 胜率几乎无相关性**。这解释了为什么 M15/M30 两个挖掘预期 60%+ 的规则在真实 exit 下胜率只剩 17% / 24%。

唯一幸存的 squeeze_breakout_buy 是因为"盘整末期→趋势突破"的信号类型**天然契合 trailing exit**——突破起爆后趋势延续，trail 跟随；没翻转则 trail 持续收紧至 SL。

### 暴露的真实问题

1. **Research 模块的 forward_return 太朴素**：只测"等 N bar 再看"，不测"假设用实际 exit 模型退出的 return"
2. **候选晋升路径缺验证关**：`StrategyCandidateSpec.promotion_decision` 只看 IC/hit_rate/robustness_tier，没有内建"跟实盘 exit 对齐"的检查
3. **`--no-filters` 不是完整诊断开关**：过滤链可关，但 exit 模型无法在回测 CLI 里关掉

### 本次处置

- **B 方案 执行**：丢弃 `weak_momentum_sell` + `roc_accel_sell`（回测不成立）；保留 `squeeze_breakout_buy` 进 Paper Trading 实时验证
- **D 方案 沉淀**（本条目 + `docs/research-system.md` 追加警告）

### 未决工作（Follow-ups）— 2026-04-15 追踪状态

**F-12a** ✅ **已闭环**（commit `cf838d5`）
- 新模块 `src/research/core/barrier.py`：BarrierConfig / BarrierOutcome / `compute_barrier_returns()` 三件套，Triple-Barrier (AFML) 实现
- `DataMatrix` 新增 `barrier_returns_long/short` + `barrier_configs` 字段；9 组默认 RR 网格
- `build_data_matrix()` 同时填充朴素 forward_return 和 barrier_returns（并列，不替换）
- 12 个单元测试覆盖 TP/SL/Time 三条 barrier × LONG/SHORT × 边界条件

**F-12b** ✅ **已闭环**（commit `c0c0387`）
- `ExitSpec` 新增 `mode` 枚举（CHANDELIER/BARRIER）+ `time_bars` 字段
- BARRIER 模式构造时硬校验 sl_atr/tp_atr/time_bars 齐全 >0
- 新增 `evaluate_barrier_exit()` 纯 TP/SL/Time 分派（不走 trailing/breakeven/signal reversal）
- `evaluate_exit()` 入口按 `exit_spec["mode"]` 分派；`atr_at_entry` 参数传通
- `PositionManager._evaluate_chandelier_exit()` 透传 `pos.atr_at_entry` 给引擎
- 14 个测试覆盖 ExitSpec 校验 + 分派正确性 + 向后兼容

**F-12c** ⏸️ **推迟**
- 原设想：回测 CLI 加 `--exit-mode fixed_bars=N` 诊断开关
- 重新评估：Plan B 完成后策略可在 `_exit_spec()` 直接声明 `ExitSpec(mode=BARRIER, ...)`，回测会自动走 barrier 路径——**F-12c 的诊断价值被 Plan B 实质覆盖**
- 保留标记但优先级降为 P3：仅在未来需要"同一策略用两套 exit 对比"时再实现

**F-12d** ✅ **已闭环**（commit `<pending>`）
- `MinedRule` 新增 `barrier_stats_train` / `barrier_stats_test` 字段（Tuple[BarrierStats, ...]）
- `BarrierStats` dataclass：barrier_key / n_samples / tp_rate / sl_rate / time_rate / mean_return / hit_rate
- `_build_arrays()` 新增返回 `bar_indices`（原 matrix bar index），传递到 `_extract_rules` → `_process_leaf`
- `_process_leaf` 在叶节点规则上调用 `_compute_barrier_stats_for_rule()`：对每组 `DataMatrix.barrier_configs` 汇总该规则触发样本的退出分布
- 按方向分派：buy 规则读 `barrier_returns_long`，sell 读 `barrier_returns_short`
- 统计后按 hit_rate 降序，下游可直接取 top-1 barrier 组合作为策略 `_exit_spec()` 声明参数
- `to_dict()` 输出含 `barrier_stats_train` / `barrier_stats_test` 列表（非空时）
- 向后兼容：matrix 无 barrier_returns → 字段为空 tuple，`to_dict()` 不输出字段
- CV consistency 路径传 matrix=None 跳过 barrier 计算（其只关心 rule condition key）
- 4 个集成测试守护（`tests/research/test_rule_mining_barrier.py`）

### 挖掘 → 策略 的完整闭环

经 F-12a + F-12b + F-12d 三件套，挖掘产出现在可以端到端落地：

```
1. MiningRunner.run() → MinedRule（含 barrier_stats_train[0..n]）
2. 选 hit_rate 最高那组 → (sl_atr, tp_atr, time_bars)
3. 策略 _exit_spec() 声明 ExitSpec(mode=BARRIER, sl_atr=..., tp_atr=..., time_bars=...)
4. 实盘 evaluate_exit() 按 mode=barrier 分派走 evaluate_barrier_exit
5. Exit 语义与挖掘 forward_return 完全一致 → 避免 C-1/I-1 那类失败
```

### 与下次挖掘接入

挖掘侧（`MiningRunner` 产出）应**同时报告**：
- 朴素 forward_return[h] 胜率（现状）
- barrier_returns[(sl, tp, time)] 胜率（新增，选 top-3 组合）

Top candidate 选择时应挑**两者都高**的规则——这就是本条目最初建议的 `exit_model_alignment_score` 的实操化表达（不需要单独存 score，直接在 candidate 产出里并列两组数据供决策）。

### 边界泄漏角度

- 本次改动**不打补丁**：barrier 是独立分派分支，Chandelier 代码零改动
- **向后兼容**：老策略不声明 mode = 默认 CHANDELIER，行为 0 变化
- **职责清晰**：挖掘只负责给出"多种 exit 假设下的候选"；策略自行在 `_exit_spec()` 声明实际使用的 exit；exit 引擎按 mode 分派
- 未决兼容项：无（F-12c 可选）

---

## 2026-04-16 squeeze_breakout_buy 止损删除 + 策略体系清理

### 止损决策（三方独立印证同一结论）

| 评估方法 | 产出 | 结论 |
|---|---|---|
| **24 月 H1 aggression 扫描**（本次 B-5） | α∈{0.30,0.45,0.55,0.70,0.85} 全部 PF 0.18~0.26 / MaxDD 60%+ | ❌ 彻底失败 |
| **barrier_stats H1 test**（F-12d 产出） | top RR 组合 test hit_rate 22.6% vs naive 60.4% | ❌ 跨时段不稳健 |
| **6 月 vs 24 月样本** | 6 月 PF 0.99 vs 24 月 PF 0.26 | ❌ 前次回测 12 笔 PF 1.23 是样本波动幸运 |

**根因**：挖掘规则（纯指标条件 `di_spread>-0.44 AND adx<10.84 AND squeeze_intensity<0.21`）与用户原生 Why/When/Where 三层语义不符，缺乏跨 24 月的结构稳健性。

### 清理动作

**Step 1 — 彻底删除 squeeze_breakout_buy**：
- 删除 `src/signals/strategies/structured/squeeze_breakout_buy.py`
- `catalog.py` / `structured/__init__.py` 移除注册与导出
- `signal.ini` 移除 `strategy_timeframes` + `strategy_deployment` 段
- `signal.local.ini` 移除 live-exec-a + demo-main account_bindings 引用

**Step 2 — 校准冻结策略 deployment status**：
- `structured_session_breakout`：`paper_only → candidate`（2026-04-08 确认冻结，runtime 不应评估）
- `structured_lowbar_entry`：`paper_only → candidate`（2026-04-08 所有 aggression 均亏损）
- `structured_breakout_follow` 保留 paper_only（0 交易是"条件严格设计如此"，未来放宽阈值即可复活）

### 策略体系总览（2026-04-16 清理后）

**🟢 核心活跃（6 个，2026-04-06 原生，有回测基线支撑）**：
- structured_trend_continuation（M30/H1 主盈利源）
- structured_trend_h4（H1, HTF=H4 变体）
- structured_trend_h4_momentum（H1 + momentum_consensus14 增强）
- structured_sweep_reversal（M30）
- structured_range_reversion（M30）
- structured_trendline_touch（H1/H4）

**🟡 设计型保留（1 个）**：
- structured_breakout_follow（paper_only, 0 交易因条件严格）

**⚫ candidate 冻结（2 个，代码资产保留不评估）**：
- structured_session_breakout
- structured_lowbar_entry

**🔴 已删除（1 个）**：
- structured_squeeze_breakout_buy（2026-04-16 止损）

### F-12 价值体现

本次止损是 F-12 (a/b/d/e) 完整闭环的**首次实战检验**：
- 没有 F-12d 的 barrier_stats，我们可能信任 "6 月 PF 1.23" 进 Paper，1-2 周后才发现失败
- 有了 F-12d + aggression scan，**止损提前 2 周**，避免污染 Paper 观察窗口

### 未决工作

- A-3（walk-forward 挖掘改造）：未来挖掘必须产出跨 N 个独立窗口稳健的候选才能进 Paper
- Paper Trading 观察对象回归到 6 个核心策略（trend/sweep/range/trendline 家族）
- 等 1-2 周累积真实数据后再决策晋升 active_guarded

### 边界泄漏角度

本次改动**删除失败策略**（不打补丁保留）+ **校准冻结声明**（runtime 更干净）+ **Paper 观察对象正本清源**。未决兼容项：无。

## 2026-04-17 Intraday 高频化基础设施体检（Step 1）

### 背景

规划 M5/M15 原生高频策略前，按"先压测基础设施、再挖特征"原则做三项事实核查：数据完整性、回测点差建模、regime 切换稳定性。避免在"脆弱基础设施"上做挖掘，否则 Alpha 全是假的。

分支：`feat/research-intraday-m5-m15`（从 main 切出）。

### Step 1.1 — XAUUSD 数据覆盖（live 环境）

| TF | Bars | 起始 | 最新 | 跨度 | 判定 |
|----|------|------|------|------|------|
| D1 | 255 | 2025-04-18 | 2026-04-14 | 12 月 | 充足 |
| H4 | 1,530 | 2025-04-17 | 2026-04-15 | 12 月 | 充足 |
| H1 | 5,824 | 2025-04-17 | 2026-04-15 | 12 月 | 充足 |
| M30 | 11,640 | 2025-04-17 | 2026-04-15 | 12 月 | 充足 |
| M15 | 22,956 | 2025-04-17 | 2026-04-15 | 12 月 | 充足 |
| M5 | **69,841**（回补后） | 2025-04-17 | 2026-04-15 | 12 月 | 充足 |

M5 原 37,550 bar（仅 2025-10-01 起 6.5 月，挖掘样本边缘），本轮通过 `python -m src.ops.cli.backfill_ohlc --tf M5 --start 2025-04-17 --end 2025-10-01` 补齐至 2025-04-17 起，共 69,841 bar，覆盖期完全对齐 M15。两者后续挖掘/回测样本同期可比。

### Step 1.2 — 回测点差建模启用状态

`config/backtest.ini [risk]` 已启用完整动态 spread + swap 模型：
- `dynamic_spread_enabled = true`
- `spread_base_points = 15.0` + session 倍数（亚 1.0 / 伦敦 1.2 / 纽约 1.3）
- `spread_volatility_threshold = 1.8` + `spread_volatility_mult = 2.0`（ATR/90-bar avg ≥ 阈值时点差 ×2）
- `swap_enabled = true` + long −0.3 / short 0.15 USD/lot + 周三三倍
- 附加 `slippage_points = 3.0` 作为非 spread 类滑点

注意：`src/backtesting/models.py` / `engine/runner.py` / `engine/portfolio.py` 中 `dynamic_spread_enabled` 默认值为 `False`，真正生效靠 `backtest.ini` 覆盖——ini 是 SSOT，只读代码片段会产生误判（3 步追查原则：追模型默认值 → 追 loader → 追调用链）。

**存疑但保留**：纽约倍数 (1.3) > 伦敦倍数 (1.2) 反直觉（NY 流动性最好应最低）。判定为保守建模（考虑新闻冲击），M5 场景下会使成本偏高——**是安全方向**，不改。若未来用真实 tick 点差回测校准，再决定是否调整。

### Step 1.3 — M5/M15 Regime 切换频率

使用 `MarketRegimeDetector.detect()`（ADX14 + BB20 + KC20 + RSI 辅助，阈值 ADX≥23 TRENDING / <18 RANGING）全量扫描历史 bar，按日聚合切换次数：

| TF | Bars 分析 | 总天数 | 切换中位数/日 | p75 | p95 | Max | 判定 |
|----|----------|--------|--------------|-----|-----|-----|------|
| M15 | 22,727 | 304 | **13** | 17 | 22 | 31 | **PASS**（远低于 50 阈值） |
| M5 | 69,550 | 308 | **42** | 50 | 58 | 75 | **临界 PASS**（p75 刚好踩线） |

Regime 分布（两 TF 高度一致，detector 语义跨 TF 稳定）：

| Regime | M15 | M5 |
|--------|-----|-----|
| trending | 33.0% | 30.8% |
| breakout | 30.3% | 34.1% |
| ranging | 24.9% | 21.6% |
| uncertain | 11.7% | 13.4% |

**真正的结论**（不是单纯 PASS）：

1. **M5 每个 regime 段平均只持续 6-7 根 bar（30-35 分钟）** — 静态 `regime_affinity` 在 M5 上会频繁用错窗口。**M5 策略必须使用 `SoftRegime` 概率加权，不能用 dominant 硬跳变**。
2. **M15 一个 regime 段平均持续 7-8 根 bar（约 2 小时）** — 静态 affinity 可以工作，但应该监控连续切换日的情况。
3. **XAUUSD 日内以 trending + breakout 为主（63-65%）**，ranging 只有 22-25% — 均值回归类策略的日内窗口有限，主战场是突破 + 趋势延续。现有 `range_reversion` 在 M5/M15 上**不应作为主策略**，只作为 RANGING regime 内的辅助。

### M5 策略开发的硬约束（进入 Step 2/3 前固化）

- **必须用 `SoftRegime` 概率权重**，禁止 dominant regime 硬切
- **特征 lookback ≤ 15-20 根 M5 bar（≈ 1.5 小时）**，超过会跨越多个 regime 段污染统计
- **极端高切换日（当日 > p95 = 58 次）建议降权/不评估**
- 回测必须保持 `dynamic_spread_enabled = true`，禁止为"看着好看"关闭点差建模

### 未决/后续

- Step 2 计划：`python -m src.ops.cli.mining_runner --tf M5,M15,M30 --compare --providers microstructure,session_event,intrabar,regime_transition`，挖 Robust 类原生特征。
- Step 3（远）：在挖到至少 3 个 |IC| > 0.03 且 p < 0.01 的 Robust 特征后，按结构化策略模板落地 M15 原生策略，Paper Trading 至少 2 周。
- `scratch/regime_switch_analysis.py` 为一次性分析脚本，已被 `.gitignore` 覆盖；若未来定期需要跑可迁到 `src/ops/cli/regime_switch.py`。

### 边界泄漏角度

本次为研究型审查，**零代码改动**：仅新增 `.gitignore` 条目（`scratch/`）+ 本文档小节。未引入任何跨模块依赖或兼容分支；scratch 脚本是只读外部调用，不触及 src/。未决兼容项：无。

## 2026-04-17 架构缺口：特征晋升通道未闭环（Step 2.1 派生发现）

### 触发事件

Step 2.1 M15/M30 基线挖掘 Gate FAIL（Robust=0，Top 10 规则全 sell 方向）。结合 XAUUSD 12 月 +45.5% 牛市背景，诊断 sell-only bias 是"算法捕捉到强牛市中的短期回调规律"——**是 exit timing 信号，不是 entry 信号**。该诊断引出更深的职责边界问题。

### 核查结果（见 ADR-007 上下文）

| 发现 | 证据 |
|------|------|
| `src/research/features/promotion.py` 只生成报告对象 | 全文件 37 行，无 file I/O、无 `indicators.json` 写入、无 registry 调用 |
| `mining_runner` 的 `promote_indicator` decision 只用于显示 | `grep "promote_indicator"` → 终端打印 + `--promotable-features-only` 过滤 + JSON 输出 |
| 写入 `indicators.json` 的代码**不存在** | `grep "write.*indicators.json"` → 0 个写入点 |
| 当前"晋升"语义等同于"给人看的 TODO 标签" | 没有任何 CLI / 函数会执行晋升动作 |

### 影响

- Research 输出的 descriptive findings 没有明确"可交易性验证"关卡就进入策略开发入口——Step 2.1 的 sell-only rules 若直接做策略会牛市中爆仓
- 计算型特征（需新增 Python 函数的指标）没有半自动化晋升路径，每次都需 4 步手工（写 core/ / 加 indicators.json / 写测试 / 触发 pipeline）
- 晋升历史无审计记录，一个特征最终走哪条策略是开放问题

### 职责边界（已写入 ADR-007）

- Research：**发现**（新策略候选 A1 / 新特征 A2 / 特征晋升 A3 / 市场特性描绘 A4）——全部 descriptive
- Backtesting：**验证**（参数优化 B1 / 整体验证 B2 / 组合一致性 B3 / 可执行性模拟 B4）——全部 actionable
- 协作契约：Research 终点 = 结构完整的策略 spec（含 exit/filter），Backtesting 起点 = 该 spec + 参数网格

### 修复优先级（非紧急，建议短期启动）

- **短期（1 周内）**：在 `FeatureCandidateSpec` 加 `feature_kind: "derived" | "computed"` 字段；补 `docs/sop/feature-to-indicator-promotion.md`
- **中期（1-2 月）**：实现 `src/ops/cli/promote_feature.py`，半自动化指标代码骨架 + PR 草稿
- **长期**：端到端管线（挖掘 → 策略 spec → 自动回测 → CI 合并）

### 边界泄漏角度

本次**零代码改动**，仅新增 ADR-007（拟定中）+ 本小节。修复该缺口需要新增 CLI 与 SOP 文档，不涉及现有组件私有字段或跨域依赖，是安全的增量扩展。未决兼容项：无（当前手工流程会继续工作，半自动化是优化而非替代）。

## 2026-04-18 挖掘 vs 回测 Gap 根因诊断 + 基线策略问题

### 触发事件

Step 2.2 Top 1 rule mining 显示 test hit rate 73.6%，真实回测（零点差）13.4% win rate，-1.71R/笔净期望。差距 60 个百分点引发 "挖掘效果好、回测不能用" 的系统性质疑。

### 核心发现：三类 Gap

（原快照 `docs/research/2026-04-18-mining-vs-backtest-gap.md` 已于 2026-04-23 重置时删除；下方概要保留作 audit trail。）

| Gap | 本质 | 对 Top 1 的影响 |
|-----|------|---------------|
| **Gap 1 语义** | 挖掘 `hit_rate` = 端点方向正确，回测 `win_rate` = SL/TP 路径先触 TP | ~30 百分点差距（即使零点差也存在） |
| **Gap 2 成本** | 挖掘零成本 vs 回测动态点差/滑点/commission | ~20 百分点差距（XAUUSD M5 上成本占 SL 5-50%） |
| **Gap 3 系统** | 挖掘单信号 vs 回测 11 层管道 | ~10 百分点差距（过滤后信号集中在不利时间） |

### 隐藏的更严重问题：现有策略基线 0 交易

H1 Intrabar 回测（2025-04-17~2026-04-15，5 个启用策略）：
- 总 88 笔交易，PF=0.757（亏损）
- **`structured_trend_continuation` 0 交易**（配置 M30/H1，H1 应运行）
- **`structured_breakout_follow` 0 交易**（配置 M30/H1，H1 应运行）
- 默认 `regime_affinity` 合理（TRENDING=1.00, BREAKOUT=1.00）
- XAUUSD 12 月 trending+breakout 占 63% → 有利 regime 充足

**异常**：在理论上有利的市场 regime 下，两个核心趋势策略 0 交易。

### 推测原因（待验证）

1. 策略 `_why()` / `_when()` / `_where()` 评分机制过严 → raw_confidence 达不到 0.45
2. 某个隐含 filter 在当前牛市数据下屏蔽了所有信号
3. HTF alignment policy 在 H1 层面过严
4. 指标依赖未就绪（某关键指标在 warmup 期间或计算失败）

### 影响评估

当前状态下**推动任何新机制（M5 原生策略 / 挖掘升级 / Intrabar 全量启用）都是在无 alpha 基线上叠加复杂度**。必须先修基线。

### 修复优先级

- **P1（阻塞其他推进）**：诊断 trend_continuation / breakout_follow 在 H1 牛市下 0 交易
- **P2**：在 `mining_runner` 输出中新增 `path_win_rate` / `path_expectancy` 指标（填补 ADR-007 Tradability Filter 缺口的第一步）
- **P3**：按 Regime 子集分层挖掘（消除 sell-only bias）
- **P4**：P1 完成后才评估 Intrabar 链路的真实价值

### 今日已产出的分析文档（均已于 2026-04-23 重置时删除）

- `docs/research/2026-04-18-mining-vs-backtest-gap.md`（本次 Gap 分析）
- `docs/research/2026-04-17-sell-rules-as-exit-signals.md`（Top 10 sell rules 作为 exit timing 的重解读）

### 边界泄漏角度

本次**零代码改动**。新增 1 份 research 文档 + 本小节。诊断用 scratch 脚本（4 个，均在 `.gitignore`）不入库。未决兼容项：无。未决工作项：P1-P4 按优先级推进。

## 2026-04-19 修复：BacktestEngine 聚合 `_required_indicators` 时丢失 `htf_requirements`

### 根因（接续 2026-04-18 P1 诊断）

`BacktestEngine.__init__` 构建 `self._required_indicators` 时仅聚合 `capability.needed_indicators`，**未合并 `capability.htf_requirements`**（`src/backtesting/engine/runner.py:390-403`）。

当策略 `htf_required_indicators` 声明的 HTF 等于主 TF（常见于 H1 策略依赖 H1 级别 supertrend14/ema50 作为 HTF alignment），pipeline 不计算这些指标，进而：

```
pipeline.compute(self._required_indicators)
  → 主 TF indicators 缺 supertrend14 / ema50
  → evaluate_strategies 里 htf_data[timeframe]=indicators 的 fallback 也缺
  → strategy._htf_data(ctx) → htf_dir=None
  → _why() 返回 False, "no_htf"
  → SignalDecision(direction="hold")
  → 0 trades
```

### 诊断路径（逐层排除）

| 层 | 状态 |
|----|------|
| Deployment gate / timeframe / scope filter | ✓ 策略正常进入 `_target_strategies` |
| needed_indicators 可得性 | ✓ 所有策略的 needed 齐全 |
| session locked_sessions 扩展到全时段 | ✓ 仍 0 trades |
| regime_affinity 乘 0（schema 不匹配怀疑） | ✓ 禁用后仍 0 trades（排除） |
| state_machine（min_preview_stable_bars） | ✓ 默认 `enable_state_machine=False`（排除） |
| SM.evaluate 输出 | **最终定位**：3155 次全部返回 direction="hold"（0 buy/sell） |
| capability.htf_requirements | **根因**：未合并到 `_required_indicators`，`engine._required_indicators=['rsi14','atr14','volume_ratio20','adx14','boll20','keltner20']`，缺 `supertrend14` / `ema50` |

### 修复

`runner.py:390-403` 聚合 `_required_indicators` 时追加：

```python
for ind, htf_tf in capability.htf_requirements.items():
    if str(htf_tf).upper() != main_tf:
        continue  # HTF ≠ 主 TF 的场景由 preload_htf_indicators 独立处理
    if ind not in seen:
        seen.add(ind)
        self._required_indicators.append(ind)
```

**仅当 HTF tf == 主 TF 时合并**（自循环 fallback 场景）。HTF 是独立 TF 时由 `preload_htf_indicators` 已经计算全量指标，不受影响。

### 验证

**修复前 → 修复后**（`trend_continuation` 单策略 H1 12 个月回测）：

| 指标 | 修复前 | 修复后 |
|------|-------|-------|
| Trades | 0 | **33** |
| WR | — | 54.5% |
| PnL | 0.0 | **+66.95** |
| PF | — | **1.216** |
| MaxDD | 0 | 6.97% |
| Exit 分布 | — | tp:17 / sl:16 |

`pytest tests/backtesting/ tests/research/` 590 测试全绿，向后兼容。

### 影响面

- **直接受益策略**（`htf_required_indicators` 含主 TF 或 `H1:supertrend14|ema50`）：
  - `structured_trend_continuation`（H1 主场，0 → 33 trades）
  - 可能：`structured_breakout_follow` / `structured_trend_h4` / `structured_trend_h4_momentum` 等（待各自验证）
- **无影响**：`structured_pullback_window` / `structured_open_range_breakout` / `structured_range_reversion`（无 htf_required_indicators 或 HTF ≠ 主 TF）

### 历史数据重释

2026-04-17 Intrabar 回测（5 策略，PF=0.757）中 trend_continuation / breakout_follow 的 0 trades 被误归因为"策略配置与市场不匹配"——**实际是本 bug 导致**。修复后 5 策略 baseline 需要重跑。

### 边界泄漏角度

修改仅涉及 BacktestEngine 的内部指标聚合逻辑，**无跨模块依赖变化**；新增 1 条 conditional append 在已有循环体内；未引入新的私有属性访问或兼容分支。未决兼容项：无。

## 2026-04-19 修复后 baseline 验证 + P2/P3 策略级诊断 + P4 trend_continuation 冻结

### 新 baseline（H1，12 个月，全策略，research mode）

| 维度 | 修复前（2026-04-17 5 策略 Intrabar 回测） | 修复后（全策略） | **冻结 trend_continuation 后** |
|------|---------|---------|---------|
| Trades | 88 | 254 | 201 |
| WR | 39.8% | 45.3% | 44.3% |
| PnL | -$215 | +$4,821 | +$4,575 |
| **PF** | **0.757** | **2.352** | **2.595** |
| Sharpe | -0.771 | +2.041 | **+2.168** |
| Calmar | -0.255 | 4.606 | **5.845** |
| MaxDD | 17.21% | 13.88% | **11.69%** |
| W/L ratio | 1.15 | 2.84 | **3.27** |
| Monte Carlo p | 0.898（无显著性）| 0.0（显著）| 0.0（显著）|

**baseline 反转的本质**：infra bug 修复让 3 个 HTF alignment 策略（`trend_continuation` / `trend_h4` / `trend_h4_momentum`）+ 依赖 supertrend14 的 `breakout_follow` 能被正确评估。其中 `regime_exhaustion`（48 → 52 trades, +$3,316 → +$3,442）和 `strong_trend_follow`（60 → 61 trades, +$1,295 → +$1,216）成为主要盈利来源。

### P2 诊断：trend_continuation 置信度校准反向

| 单策略回测 | Trades | WR | PnL | PF |
|-----------|-------|-----|-----|-----|
| min_conf=0.10 | 33 | 54.5% | +$67 | 1.22 |
| **min_conf=0.45** | **16** | **37.5%** | **-$75** | **0.61** |

**反直觉**：提高 confidence 阈值反而让 WR 从 54.5% → 37.5%（-17 pp），PF 从 1.22 → 0.61。说明策略内部 `raw_confidence = base(0.50) + why×0.15 + when×0.15 + where×0.10 + vol×0.05` 的评分加权**与真实胜率不相关甚至负相关**——高 raw_confidence 的 bar 恰好是过拟合模式的触发点。

**决策**：通过 `signal.local.ini [regime_affinity.structured_trend_continuation]` 全设 0.0 **冻结**该策略。保留代码资产（`strategy_timeframes` 仍注册），便于未来重设计（重新评分或加入 in-sample 校准）后解冻。

### P3 诊断：breakout_follow 参数过严

修复后 `_required_indicators` 已正确包含 `supertrend14`，但 breakout_follow 仍 0 trades。根因是策略 `_why()` 硬条件链过严：

```
adx ∈ [18, 38]       ← 区间窄（trending 但不过强）
adx_d3 >= 1.0        ← ADX 必须上升
|di_diff| >= 3.0
momentum_consensus >= 0.34 (buy) / <= -0.34 (sell)
HTF supertrend14 方向一致
```

在 12 个月牛市数据中，多硬条件 AND 合取罕能同时满足 → 策略设计与当前市场不匹配。**不是 infra bug**。

**未决**：参数调优（放宽 adx 区间、momentum 阈值、di_diff）留作 P5 独立工作。

### 未决工作与决策分级

| 优先级 | 工作 | 说明 |
|-------|------|------|
| **P5**（中）| breakout_follow 参数网格搜索 | 目标找到 PF > 1.3 的参数组合 |
| **P6**（低，长期）| trend_continuation 重新设计 | 可能需要重写 Why/When/Where 评分或加入 in-sample confidence 再校准 |
| **完成**（本次）| P0 bug 修复 + baseline 反转 + trend_continuation 冻结 | 见 git log fix/backtest-htf-required-indicators 分支 |

### signal.local.ini 冻结条目

本机 override（不入 git）：
```ini
[regime_affinity.structured_trend_continuation]
trending = 0.0
ranging = 0.0
breakout = 0.0
uncertain = 0.0
```

runtime 将在 confidence 管线里把该策略所有信号乘 0 → direction 变成 hold。实盘 / Paper 都不会产生 trend_continuation 的入场信号。

### 边界泄漏角度

P4 仅修改 signal.local.ini（gitignored），不涉及正式代码或 signal.ini 基线。保留完整可复原性：未来只需删除 `[regime_affinity.structured_trend_continuation]` section 即可解冻。无新跨模块依赖、无兼容补丁。未决兼容项：无。

---

## 2026-04-19 修复：breakout_follow 对 `adx_d3=None` 硬拒 → 回测 0 trades

### 根因

`StructuredBreakoutFollow._why()` 第 62 行：

```python
if d3 is None or d3 < adx_d3_min:
    return False, None, 0, f"adx_flat:d3={d3}"
```

`adx_d3` 是 ADX 的三阶 delta metric，在 `src/indicators/` 核心**完全没有实现**（grep 无匹配）。只有生产运行时的 delta 计算路径才会输出该字段，**回测管线恒为 None**。

breakout_follow 硬拒 `None` → `_why()` 在回测中 100% 返回 `adx_flat`，后续 di_diff / momentum_consensus / HTF / RSI 四关从未被评估到。

### 诊断路径（逐层排除）

| 步骤 | 方法 | 结论 |
|----|------|----|
| 1 | scratch monkey-patch `_why`，跑 H1 solo 12 个月，统计 fail reason | 3,155 次全 fail：`adx_flat=43%` / `adx_low=31%` / `adx_over=26%` |
| 2 | 写 18 组合网格（放宽 `_adx_min`/`_adx_max`/`_adx_d3_min`） | **全部 0 trades**——说明不是阈值问题 |
| 3 | 写 `verify_param_override.py` 确认 `strategy_params_per_tf` 是否注入 | resolver 正确注入，`get_tf_param(adx_d3_min, H1)=-99` —— 覆盖生效 |
| 4 | 极宽参数（`d3_min=-99`）再跑诊断 | adx_flat **99.7%**，说明恒来自 `d3 is None` 分支 |
| 5 | grep 其他策略对比 | `strong_trend_follow.py:82` / `session_breakout.py:100` / `regime_exhaustion.py:83` 全部用 `if X is not None and X op ...` 正确放行 None |

### 修复

`src/signals/strategies/structured/breakout_follow.py`：
1. `_why()` 第 62 行：`if d3 is None or d3 < adx_d3_min:` → `if d3 is not None and d3 < adx_d3_min:`（与其他策略对齐）
2. 第 114 行 `trend_score = min(d3 / 6.0, 1.0)`：补 None 分支，退化为 ADX 幅度分（避免 `None / 6.0` 崩溃）
3. 日志字符串中 `d3` 格式化补 None 处理（`d3_desc = "n/a"`）

### 全策略 baseline 对比（H1 12 个月 2025-04-17 ~ 2026-04-15）

| 指标 | 旧 baseline（bug 存在） | 新 baseline（bugfix） | 变化 |
|------|---------|---------|------|
| Trades | 201 | 473 | +135% |
| WR | 44.3% | 46.1% | +1.8 pp |
| PF | 2.595 | 2.361 | -0.23 |
| Sharpe | 2.168 | 2.74 | **+0.57** |
| Calmar | 5.845 | 21.77 | compound 放大 |
| MaxDD | 11.69% | **8.0%** | **-3.7 pp** |
| MaxDD duration | — | 75 bars | — |

### PnL 量级解释

新 baseline 账面 PnL $85,101（旧 $4,575）差 18× 并非异常：position_size 动态 compound（`current_balance × risk_pct / stop_distance`），bugfix 新增 163 笔 breakout_follow 正收益让 balance 更快膨胀，后续 regime_exhaustion / strong_trend_follow 的每笔 position size 跟着放大。per-trade `pnl_pct` 维度（每笔约 ±1%）与旧 baseline 基本一致。

### 策略 PnL 分布（新 baseline，按 PnL 降序）

| 策略 | n | WR | PnL |
|------|---|-----|------|
| regime_exhaustion | 52 | 57.7% | +$47,391 |
| strong_trend_follow | 62 | 50.0% | +$25,549 |
| **breakout_follow (bugfix)** | **163** | 44.2% | +$6,491 |
| pullback_window | 46 | 41.3% | +$3,136 |
| open_range_breakout | 72 | 40.3% | +$2,167 |
| trendline_touch | 78 | 47.4% | +$365 |

### Tradeoff

breakout_follow 自身 solo PF 1.23 低于 TODO P5 目标 1.3，拉低了整体 PF（-0.23），但带来更多样化信号（WR/MDD/Sharpe 均改善）。后续可选参数调优（ADX 参数覆盖机制已验证可用），但非阻塞。

### 边界泄漏角度

- 修改限定在单一策略 `_why()` 方法内，无跨模块调用变更
- 对齐现有策略模式（`is not None and` 前置校验），无新设计契约
- 添加一行注释说明回测 vs 生产的差异来源，便于未来维护者理解

### 架构性发现（未决，独立 TODO）

**回测管线缺失 delta metrics**：`adx_d3` / `rsi_d3` 等三阶 delta metric 在 `src/indicators/` 完全没有实现。所有依赖它们的策略（至少 7 处）在回测中相关门控条件**被默默跳过**（通过 `is not None` 短路放行）。生产 vs 回测存在系统性行为差异，具体影响幅度未量化。建议：a) 在 indicator 核心补 delta 计算；或 b) 在文档里显式声明"回测跳过 delta 条件"语义。

---

## 2026-04-19 修复：回测管线缺失 delta metrics（P7）

### 根因（接续上条）

上一条 bugfix 发现 `adx_d3` / `rsi_d3` 等 delta metric 在回测中恒 None。进一步诊断：

- `src/indicators/runtime/delta_metrics.py` **已实现** delta 计算逻辑（`apply_delta_metrics`）
- 但 `UnifiedIndicatorManager.query_services/runtime.py` 是生产路径的入口，依赖 `manager.market_service.get_ohlc_window()` 加载历史 bar
- 回测管线走另一条路：`BacktestEngine._pipeline.compute()` → `OptimizedPipeline.compute()`，**不经过 UnifiedIndicatorManager**，因此 delta 注入点被绕过
- 上一条我在 codebase-review 里写的"delta metric 在 `src/indicators/` 完全没有实现"是核验失败——实际上**是回测路径跳过了已有的 delta 层**，不是 delta 逻辑不存在

### 修复

`src/backtesting/engine/indicators.py`：

- 新增 `_build_delta_config()`：从 `get_global_config_manager()` 读取每个 indicator 的 `delta_bars` 配置，构建 `{name: (delta_offsets)}` 映射
- 新增 `_apply_delta_to_snapshots(snapshots, delta_config)`：snapshot-based delta 注入。数据源是 `snapshots` 列表自身，按 `i - delta` 索引回看前 N 根，就地给 payload 添加 `{metric}_d{N}` 字段。复用 `src/indicators/runtime/delta_metrics.py` 的字段命名约定与 round 精度（6 位），但解耦 MarketService 依赖
- 修改 `precompute_all_indicators()`：在产出全量 snapshots 后一次性应用 delta，单次遍历 O(N × indicators × deltas)，不影响主回测循环性能

### 全策略 baseline 对比（H1 12 个月 2025-04-17 ~ 2026-04-15）

| 指标 | delta 接入前（上一条 bugfix 后） | delta 接入后 | 含义 |
|------|---------|---------|------|
| Trades | 473 | **346** (-27%) | 门控真正生效，减少虚假交易 |
| WR | 46.1% | 46.2% | 整体 WR 持平 |
| PF | 2.361 | 2.041 (-0.32) | 优质 setup 数量减少 |
| Sharpe | 2.74 | 2.508 (-0.23) | 合理下降 |
| MaxDD | 8.0% | **6.93%** (-1.07pp) | 风险更低 |
| MaxDD duration | 75 bars | 61 bars | 恢复更快 |

### 策略级影响（关键质量证据）

| 策略 | delta 接入前 (n/WR/PnL) | delta 接入后 (n/WR/PnL) | 说明 |
|------|------|------|------|
| **regime_exhaustion** | 52/57.7%/+$47k | **15/80.0%/+$14k** | **WR +22.3pp**：`adx_d3 > 0` 门控筛出真正耗竭反转 |
| strong_trend_follow | 62/50%/+$26k | **45/55.6%/+$16k** | WR +5.6pp，`adx_d3_min_strict > 0` 筛出真趋势 |
| breakout_follow | 163/44.2%/+$6k | 115/43.5%/+$2k | 减 48 笔，门控生效 |
| pullback_window | 46/41.3%/+$3k | 20/40.0%/+$0.3k | 减 26 笔（rsi_d3 门控） |
| open_range_breakout | 72/40.3% | 72/40.3% | 不依赖 delta，无变化 |
| trendline_touch | 78/47.4% | 79/46.8% | 不依赖 delta，基本一致 |

### 方法论意义

delta 接入前的 473 笔回测是**假象膨胀**——门控失效放行了非目标场景（例如"ADX 耗竭且下降"以外的场景也被 regime_exhaustion 触发）。delta 接入后的 346 笔是**回测首次与生产行为对齐的真实 baseline**。regime_exhaustion 的 WR 从 57.7% 跳到 80%，直接证明 delta 门控的选择价值。

### 边界泄漏角度

- 修改限定在 `src/backtesting/engine/indicators.py`，无其他模块签名变更
- 策略代码 / indicator 核心 / runtime delta 路径 **零改动**
- 字段命名与生产 `{metric}_d{N}` 完全一致，数值精度对齐（round(6)）
- 新增 8 个单元测试覆盖：基础差值 / 多 delta 共存 / 非数字字段跳过 / 已有 _dN 不递归 / 前值缺失 / 空配置 / 空 snapshots
- 运行时开销：12 个月 H1（~6K bar）delta 注入耗时 < 50ms，相对 indicator 计算主流程可忽略

### 未决项

- **M5/M30 等其他 TF 未回测验证**——当前只对 H1 baseline 做了对比。P5 残留的 breakout_follow 参数调优、FP.2 strong_trend_follow 的 Paper 观察都需要重跑
- **旧 baseline 数据失效**：所有 2026-04-19 之前的回测结果（包括 PR #48 修复后的 201 trades / PF 2.595）都是 delta 门控失效下的数据，不应作为未来参考。TODO 中"新 H1 baseline"栏需更新

---

## 2026-04-25 ADR-010：Paper Trading 模块删除 + Demo 重定位为组合演练账户

### 背景

ADR-010 之前并存两套验证机制：

1. **Paper Trading 模块**（43 文件 / 236 处引用）：在 main 实例（live-main + demo-main）自动装配 PaperTradingBridge，挂 SignalRuntime listener 模拟开仓，绕开 11 层风控堆栈。评估的是"信号无摩擦最优执行"，与生产实盘表现系统性脱节。

2. **demo-main 实例**：与 live-main 装配完全相同的策略集（catalog.py 硬编码 14 条 + signal.ini 全局共享），仅 broker server 不同 → 没有差异化职责。

P9 bug #3 已暴露 paper bypass execution_intent 引发 admission writeback 失配，需专门绕回。当前 13 条策略全部 `paper_only`/`candidate` 状态——意味着 live 实例根本没有真实下单（pre_trade_checks 全拒），所谓"实盘"只是 paper bridge 的影子记录。

### 决策

详见 `docs/design/adr.md` ADR-010 完整决策记录。核心三项：

1. **删除 paper_trading 模块**（43 文件 / 236 处）：bridge / portfolio / tracker / config / schema / repo / API routes / readmodel 字段 / studio agent / 装配胶水。
2. **demo-main 重定位为"组合演练账户"**：装配集合 `demo = ACTIVE ∪ ACTIVE_GUARDED ∪ DEMO_VALIDATION`，`live = ACTIVE ∪ ACTIVE_GUARDED`。demo 永远是 live 的超集，多出来的就是 DEMO_VALIDATION 候选。
3. **状态机改名**：`PAPER_ONLY → DEMO_VALIDATION`（30 处：代码 19 + 配置 11）。

### 改造分 10 个 phase 落地（commit a471a46）

| Phase | 内容 |
|---|---|
| 1 | 装配层 environment-aware 过滤（`_filter_strategies_for_environment` + `allows_demo_validation()` helper） |
| 2 | 状态机改名 30 处（含 `BacktestConfig.include_paper_only → include_demo_validation` / `ValidationDecision` / `REASON_STRATEGY_*` / `paper_verdict` / "paper_shadow" check key / signal.ini 11 处 status）|
| 3 | 删除 paper 核心模块（18 文件 git rm）|
| 4 | 装配胶水清理（container 字段、builder、runtime_controls、persistence/db.py、api/deps.py）|
| 5 | 前端可见 readmodel 改造（lab_impact `paper_sessions → demo_validation_windows`、runtime 删 `paper_trading_summary()`、studio 删 `paper_trader` agent、删 `/v1/paper-trading/*` 路由）|
| 6 | 跨域引用清理（experiment schema 删 `paper_session_id` + 重命名 `paper_sharpe/win_rate → demo_validation_*`、status CHECK constraint 改名）|
| 7 | 新写 `src/ops/cli/demo_vs_backtest.py`（跨 db.live + db.demo 三层切片对账）|
| 8 | DB migration 已在 mt5_live + mt5_demo 执行（DROP 表 + ALTER COLUMN + 旧数据迁移）|
| 9 | 测试更新（删 paper 专属测试 + 改 8+ 交叉测试断言 + 新增 7 用例验证 environment 过滤）|
| 10 | 文档同步：ADR-010 新增 + ADR-009 标 SUPERSEDED + CLAUDE.md / TODO.md / 7 个 docs 同步（本次本节追加） |

### 验证

- 全套单元测试：**2739 通过 / 0 失败 / 6 skipped**
- DB migration 验证：mt5_live + mt5_demo 的 `paper_trading_*` 表已删除，experiments 表列已重命名
- 装配冒烟（实测）：
  - environment="live" → 装配 0 条（当前无 ACTIVE/ACTIVE_GUARDED 策略，符合 P0 待启动现状）
  - environment="demo" → 装配 10 条 demo_validation 策略
  - environment 未知 → 装配全集 14 条 + WARNING（向后兼容）
- 全局 grep `PAPER_ONLY / paper_only / paper_verdict / paper_session_id` 等关键词零代码残留

### 边界泄漏角度

- 净改动 +1743 / -4286 行，72 文件
- 11 层风控堆栈不变，demo 信号也走完整链路真实下单
- `pre_trade_checks` 保留 `allows_live_execution()` 二级护栏，作为 hot-reload 安全网（万一某 ACTIVE 策略 hot-reload 改为 DEMO_VALIDATION，运行时不会自动卸载，二级护栏拒单）

### 前端 breaking change（QuantX 前端方需联动）

- `/v1/lab/impact`: `paper_sessions` → `demo_validation_windows`，`linked_paper_sessions` 字段移除
- `/v1/runtime` + `/v1/health`: `validation_sidecars.paper_trading` 字段删除
- `/v1/studio/stream`: `paper_trader` agent 卡片删除
- `/v1/paper-trading/*`: 6 个端点 404
- `/v1/experiments/{id}`: `phases[].phase=paper_trading` → `phase=demo_validation`

### 未决项

- **portfolio 层**（跨策略相关性 / 同向叠加 / 反向冲突识别）当前缺失但**不在本 ADR 范围**——独立 backlog 项，等 ≥3 条策略并存有真冲突数据后再启动设计
- **多 demo 账户演进**：当并行候选 ≥3 条时，扩展 `topology.ini [group.demo] workers = ...`
- **`paper_shadow_required` dataclass 字段**未改名（语义保留为"必须经过 shadow 验证阶段"），如果未来希望统一命名可在独立 commit 处理
