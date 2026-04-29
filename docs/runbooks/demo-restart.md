# Demo 重启 SOP

> **场景**：拉取新代码后切换 demo runtime；在途持仓由 broker SL/TP 守护，
> 不会丢失。重启时间 = 「对账起点 T0」，之后数据才能进 demo_vs_backtest。

---

## 1. Pre-flight（30 秒）

确认重启前提：

```bash
# 在 demo 主机执行
cd /path/to/MT5Services

# 1.1 当前 git HEAD（用于回滚）
git rev-parse --short HEAD > /tmp/demo-prev-head.txt
echo "Current HEAD: $(cat /tmp/demo-prev-head.txt)"

# 1.2 当前在途持仓（仅观察，不需操作）
python -c "
import psycopg2
conn = psycopg2.connect(host='127.0.0.1', port=5432, user='tsdb',
                         password='tsdb_pass', dbname='mt5_demo')
cur = conn.cursor()
cur.execute('''
SELECT position_ticket, strategy, direction, entry_price,
       current_stop_loss, current_take_profit
FROM position_runtime_states WHERE status='open' ORDER BY opened_at
''')
print(f'活跃持仓 (broker SL/TP 守护，不需手动平):')
for r in cur.fetchall():
    t, s, d, ep, sl, tp = r
    print(f'  ticket={t} {s} {d} entry={ep} sl={sl} tp={tp}')
"
```

---

## 2. 优雅停止（让 flush 队列）

```bash
# 找 demo runtime 进程
ps aux | grep -E "entrypoint.(web|supervisor|instance).*demo" | grep -v grep
# 或 Windows: Get-Process python | Where-Object { $_.CommandLine -match "demo" }

# 发 SIGTERM（不要 SIGKILL；SIGTERM 触发 AppRuntime.stop() 优雅退出：
#   1. 停止信号生成
#   2. flush 持久化队列（trade_outcomes / signal_events）
#   3. 关闭 MT5 IPC + DB 连接
kill -SIGTERM <pid>          # Linux/Mac
# Stop-Process -Id <pid>     # Windows，不加 -Force

# 等待退出（通常 5-15 秒）
while kill -0 <pid> 2>/dev/null; do sleep 1; done
echo "demo runtime stopped"
```

**broker 侧**：所有挂单 SL/TP 仍生效。仓位继续被 broker 服务器守护，
即使 demo 离线期间触发 SL/TP 也会被记录到 broker history_deals，
下次启动 reconciliation 自动识别 + 写 `trade_outcomes`（含 `infer_exit_reason_from_price`
推断的 SL/TP/broker_close）。

---

## 3. 拉取最新代码

```bash
git pull --ff-only

# 验证 HEAD 含必需 commit（截至 2026-04-29）
git log --oneline HEAD~5..HEAD

# 期望见以下关键 commit：
#   1db8475  fix: drop require_pending_entry from three demo_validation deployments
#   c655d92  fix: infer exit_reason from broker close_price in reconciliation
#   ad2d976  fix: gate signal_exit on non-negative r_multiple
#   f3acb4a  fix: per-bar staleness gate
#   3f49513  fix: trading runtime hardening (4 unrelated defects)
#   e318ec5  fix: EF sub-min-volume fallback
```

如缺失关键 commit，**停止重启**，先解决 git 同步问题。

---

## 4. 启动 + 记 T0

```bash
# 启动 demo runtime（前台 / nohup / supervisor 任选）
python -m src.entrypoint.web                          # 单实例
# 或：
python -m src.entrypoint.supervisor --environment demo  # 监督模式
# 或：
nohup python -m src.entrypoint.web > demo.log 2>&1 &

# 立即记录 T0（对账起点 — 之前数据归档为"重启前观测期"）
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$T0" > docs/runbooks/demo-t0.txt
echo "Demo T0: $T0"
```

---

## 5. 启动后验证（5 分钟）

```bash
# 5.1 健康检查
python -m src.ops.cli.health_check --instance demo-main
# 期望：[OK] Ready probe / 无 critical FAIL（除 MT5 path 外环境性问题）

# 5.2 进程内存（启动期 ~300-500MB）
ps aux | grep "entrypoint" | grep -v grep | awk '{print $5}'  # VSZ KB

# 5.3 看是否有错误日志
tail -100 data/logs/demo-main/errors.log 2>/dev/null | head -30

# 5.4 验证 reconciliation 处理离线期间关闭仓位（如有）
python -c "
import psycopg2
from datetime import timedelta
conn = psycopg2.connect(host='127.0.0.1', port=5432, user='tsdb',
                         password='tsdb_pass', dbname='mt5_demo')
cur = conn.cursor()
cur.execute('''
SELECT recorded_at, strategy, direction, metadata->>'exit_reason' as reason
FROM trade_outcomes
WHERE recorded_at >= NOW() - INTERVAL '5 minutes'
ORDER BY recorded_at
''')
for r in cur.fetchall():
    print(r)
"
# 期望：如离线期间有 SL/TP 触发，trade_outcomes.metadata.exit_reason
# 应填充 stop_loss / take_profit / broker_close（c655d92 修复）
```

---

## 6. 初期监控（首 24 小时）

**期望**：触发频率较 T0 之前明显提升（之前因 require_pending_entry / hedge / signal_exit 浮亏砍仓多重问题压制）。

| 维度 | 预期值（基于 fallback ON backtest）|
|---|---|
| 总 trades / day | ~5-6 笔（4 TF combined）|
| H1 触发的策略数 | 7 个有可能（之前只有 pa + trendline_touch）|
| `regime_exhaustion` 触发 | 应有真实下单（之前 0 fill）|
| `strong_trend_follow` 触发 | 应有真实下单 |
| 持仓时长 | 不再 27-31 分钟集中（signal_exit 浮亏守卫已修）|
| `trade_outcomes.metadata.exit_reason` | 应填充 stop_loss / take_profit / signal_exit / broker_close |
| 同策略 hedge（buy + sell 同时持仓）| **0 次**（duplicate guard 已修）|

**异常告警**：
- 1 小时内 0 trades —— 可能装配 bug，看 `data/logs/demo-main/errors.log`
- hedge 重叠出现 —— `f13b8c7` duplicate guard 没生效（git HEAD 不对）
- `exit_reason=NULL` 占多数 —— `c655d92` 没生效

---

## 7. 对账起点协议

T0 之后的数据进入正式对账流程：

```bash
# 累计 ≥ 20 笔单策略 trades 后跑（约 H1 regime_exhaustion 8 个月，组合 ~6-8 周）
T0=$(cat docs/runbooks/demo-t0.txt)
DAYS_SINCE_T0=$(( ($(date -u +%s) - $(date -u -d "$T0" +%s)) / 86400 ))

python -m src.ops.cli.demo_vs_backtest \
  --backtest-run-id bt_7fbcee08d204 \
  --strategies structured_regime_exhaustion \
  --demo-window ${DAYS_SINCE_T0}d \
  --json-output data/artifacts/demo_vs_backtest/regime_exhaustion_post_T0.json
```

`bt_7fbcee08d204` = H1 regime_exhaustion solo backtest（commit `7bea7ec`，
PF 9.6 / DD 3.87% / Sharpe 2.0 / WF Consistency 83.3%）。

**晋级判定**：drift 在 PLAN.md 阈值内 → 写 active_guarded promotion proposal commit。

---

## 8. Rollback（如发现严重问题）

```bash
# 8.1 立即停 demo
kill -SIGTERM <pid>

# 8.2 回退到重启前 HEAD
git checkout $(cat /tmp/demo-prev-head.txt)

# 8.3 重启
python -m src.entrypoint.web

# 8.4 记新 T0_rollback（对账重置）
date -u +%Y-%m-%dT%H:%M:%SZ > docs/runbooks/demo-t0-rollback.txt
```

回退场景示例：
- 启动 1 小时内出现新代码引入的 P0（如交易链路异常 / 大量错误日志）
- 触发频率反常异常（应该多但完全没触发，或暴涨到 backtest 5x 以上）

---

## 附录：常见问题

**Q: broker 侧仓位会不会因 demo 离线变 orphan？**
A: 不会。MT5 broker 服务器侧保留 SL/TP 挂单，独立于 demo runtime。
demo 离线期间触发 SL/TP 后，下次启动 reconciliation 自动识别（commit
`c655d92` 引入的 `infer_exit_reason_from_price` 推断真实出场原因）。

**Q: demo 重启后 PendingEntry 队列丢失吗？**
A: pending_order_states 表持久化，重启后由 PendingEntryRecovery 自动恢复。
`require_pending_entry=true` 已从 3 处配置移除（commit `1db8475`），现在
demo_validation 阶段策略走 market 入场。

**Q: T0 之前的 trade 数据怎么办？**
A: 归档为「重启前观测期」（含混合代码版本 + hedge bug + signal_exit
浮亏砍仓 bug 等），**不进入正式对账**。可在 `docs/research/` 下做时点
分析参考，但不作为 active_guarded promotion 的证据链。
