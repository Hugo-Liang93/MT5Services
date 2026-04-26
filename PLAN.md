# 日内交易架构就绪整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前 MT5Services 从“可做 demo/research 的日内交易验证平台”推进到“具备统计证据、执行一致性、可审查晋级门禁的日内交易模型验证体系”。

**Architecture:** 计划按单向数据流推进：Research 挖掘只负责发现假设，Backtest 负责 live-aligned 验证，Demo 负责真实 broker 验证，Deployment 合同负责阻断未晋级策略进入 live。优先修复统计配置和候选过滤的根因，再跑 fresh mining 和回测闭环，最后处理策略晋级、文档漂移和组合风险层。

**Tech Stack:** Python, FastAPI, TimescaleDB/PostgreSQL, MT5, pytest, configparser, existing `src.research`, `src.backtesting`, `src.trading`, `src.ops.cli` modules.

---

## 当前判断

- 当前系统工程底座可支撑日内交易 demo 验证，但不能直接作为 live 自动交易模型。
- `active` / `active_guarded` 策略数为 0，`live-main` / `live-exec-a` 绑定为空，这是正确的保护状态。
- `demo-main` 绑定 10 个 `demo_validation` 策略，适合真实 demo broker 验证。
- 统计挖掘链路存在两个优先级最高的问题：
  - `src/research/core/config.py` 默认配置目录指向 `src/config`，不是仓库根目录 `config`。
  - `MiningRunner._rank_findings()` 对 barrier findings 只看显著性，没有强制过滤成本后负收益。
- 2026-04-25 综合挖掘记录显示：稳定策略候选为 0，H1 mining walk-forward 稳定规则为 0；现阶段应推进 fresh mining 和验证闭环，而不是开 live。

## 范围门禁

本计划触及高风险链路：

- Research / mining：`src/research/`
- Backtest / validation：`src/backtesting/`
- Signal deployment：`config/signal.ini`, `config/signal.local.ini`
- Trading execution readiness：`src/trading/`, `src/ops/cli/live_preflight.py`
- Runbook / architecture docs：`docs/`

执行约束：

- 不新增兼容旧 paper shadow 路径。
- 不绕过 `StrategyDeployment` 晋级合同。
- 不把 candidate 或 demo_validation 策略绑定到 live-main。
- 不把“统计显著但成本后负收益”的 finding 推入策略候选。
- 每个代码任务先写失败测试，再写最小实现，再跑相关测试。
- 每轮变更必须更新 `docs/codebase-review.md`。

## 文件结构

计划会修改或新增以下文件：

- Modify: `src/research/core/config.py`  
  责任：修复 research 配置默认路径，保证 `config/research.ini` + `config/research.local.ini` 是默认事实源。

- Modify: `tests/research/core/test_config.py`  
  责任：守护默认配置目录、local 覆盖顺序和显式 ini 路径语义。

- Modify: `src/research/orchestration/runner.py`  
  责任：让 top findings 只输出可交易的 barrier findings，过滤成本后负收益和明显不利的 exit distribution。

- Create: `tests/research/orchestration/test_runner_barrier_findings.py`  
  责任：验证 barrier findings 排名只保留正期望、显著、样本量充足的候选。

- Create: `src/research/orchestration/walk_forward.py`  
  责任：把 mining walk-forward 从 CLI 私有实现收口成可测试服务。

- Modify: `src/ops/cli/mining_walk_forward.py`  
  责任：复用完整 `MiningRunner` 能力，支持 `--providers`, `--child-tf`, `--json-output`, `--persist`, `--experiment`。

- Create: `tests/research/orchestration/test_walk_forward.py`  
  责任：验证窗口切分、跨窗口规则聚合、稳定规则筛选和 JSON payload。

- Modify: `docs/codebase-review.md`  
  责任：记录本轮边界变化、根因修复、剩余风险。

- Modify: `docs/research-system.md`  
  责任：同步 research 配置 SSOT、fresh mining 流程、barrier 正期望过滤口径。

- Modify: `docs/design/full-runtime-dataflow.md`  
  责任：移除过期 PaperTradingBridge 描述，保持 ADR-010 后 demo-main 语义一致。

- Modify: `docs/signal-system.md`  
  责任：同步当前策略注册数量、intrabar scope 和部署状态语义。

- Create: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`  
  责任：固化本轮 fresh mining、backtest、demo validation 的实验设计和 go/no-go 标准。

## 成功标准

- `load_research_config()` 默认读取仓库根目录 `config/research.ini`，并按 `research.local.ini` 覆盖。
- barrier top findings 不再包含 `mean_return_pct <= 0` 的显著项。
- mining walk-forward 与 mining runner 使用同一套 provider、child TF、persistence、JSON output 能力。
- fresh mining 产物能追溯到 experiment id、配置快照、数据覆盖和 provider 摘要。
- live-aligned backtest 显式使用 `--include-demo-validation`、`--simulation-mode execution_feasibility`、Monte Carlo、执行可行性指标。
- demo validation 与 backtest 能通过 `demo_vs_backtest` 对账。
- 没有策略进入 `active_guarded`，除非同时满足统计、回测、执行可行性、demo 对账和文档门禁。

---

### Task 1: Baseline Evidence Snapshot

**Files:**
- Read: `config/app.ini`
- Read: `config/signal.ini`
- Read: `config/signal.local.ini`
- Read: `config/backtest.ini`
- Read: `docs/codebase-review.md`
- Read: `docs/research/2026-04-25-comprehensive-mining-round.md`
- Create: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`

- [ ] **Step 1: Record current deployment state**

Run:

```powershell
python -m src.ops.cli.confidence_check --tf H1
python -m src.ops.cli.confidence_check --tf M30
```

Expected:

```text
NO LIVE-ELIGIBLE STRATEGIES
```

- [ ] **Step 2: Record demo binding contract**

Run:

```powershell
python -m pytest -q tests/config/test_demo_main_account_bindings.py
```

Expected:

```text
2 passed
```

- [ ] **Step 3: Create the experiment design document**

Create `docs/research/2026-04-27-fresh-mining-and-validation-plan.md` with this content:

```markdown
# 2026-04-27 Fresh Mining And Validation Plan

## Objective

重新建立 research -> backtest -> demo_validation -> active_guarded 的证据链，不复用 2026-04-23 已作废结果。

## Scope

- Symbol: XAUUSD
- Timeframes: H4, H1, M30, M15
- Data source: live DB OHLC + indicators
- Strategy state before run: no active / active_guarded strategies
- Execution target before promotion: demo-main only

## Required Gates

1. Research config loader reads repo `config/research.ini` and optional `config/research.local.ini`.
2. Barrier findings must be significant and cost-after positive.
3. Mining walk-forward must produce stable candidates in multiple windows.
4. Backtest must run with live-aligned costs and execution feasibility.
5. Demo validation must reconcile with backtest before any active_guarded promotion.

## Go / No-Go

- Go to demo_validation only when fresh mining and backtest both pass validation gates.
- Go to active_guarded only after demo_vs_backtest drift is within the thresholds documented in the run output.
- No live activation is allowed in this round without explicit operator approval.
```

- [ ] **Step 4: Commit baseline documentation**

Run:

```powershell
git add docs/research/2026-04-27-fresh-mining-and-validation-plan.md
git commit -m "docs: add fresh mining validation plan"
```

Expected:

```text
[main <sha>] docs: add fresh mining validation plan
```

---

### Task 2: Fix Research Config SSOT Path

**Files:**
- Modify: `src/research/core/config.py`
- Modify: `tests/research/core/test_config.py`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Write failing tests for default config path**

Append these tests to `tests/research/core/test_config.py`:

```python
class TestDefaultResearchConfigPath:
    def test_default_config_dir_points_to_repo_config(self) -> None:
        """默认 research 配置目录必须是仓库根目录 config/。"""
        from src.research.core import config as research_config_module

        expected = Path(__file__).resolve().parents[3] / "config"
        actual = Path(research_config_module._CONFIG_DIR)

        assert actual == expected
        assert (actual / "research.ini").exists()
        assert actual.name == "config"
        assert actual.parent.name == "MT5Services"

    def test_default_loader_uses_repo_research_ini(self) -> None:
        """默认 loader 应读取 config/research.ini 中的显式统计口径。"""
        cfg = load_research_config()

        assert cfg.overfitting.correction_method == "bh_fdr"
        assert cfg.feature_providers.fdr_grouping == "by_provider"

    def test_default_loader_merges_local_after_base(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """research.local.ini 必须覆盖 research.ini。"""
        from src.research.core import config as research_config_module

        (tmp_path / "research.ini").write_text(
            textwrap.dedent(
                """
                [overfitting]
                correction_method = bh_fdr
                [feature_providers]
                fdr_grouping = by_provider
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "research.local.ini").write_text(
            textwrap.dedent(
                """
                [overfitting]
                correction_method = bonferroni
                [feature_providers]
                fdr_grouping = global
                """
            ).strip(),
            encoding="utf-8",
        )

        monkeypatch.setattr(research_config_module, "_CONFIG_DIR", str(tmp_path))

        cfg = load_research_config()

        assert cfg.overfitting.correction_method == "bonferroni"
        assert cfg.feature_providers.fdr_grouping == "global"
```

- [ ] **Step 2: Run tests and verify failure before implementation**

Run:

```powershell
python -m pytest -q tests/research/core/test_config.py
```

Expected before implementation:

```text
FAILED tests/research/core/test_config.py::TestDefaultResearchConfigPath::test_default_config_dir_points_to_repo_config
```

- [ ] **Step 3: Implement the root config path fix**

Modify the import section and `_CONFIG_DIR` in `src/research/core/config.py`:

```python
import configparser
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_CONFIG_DIR = str(Path(__file__).resolve().parents[3] / "config")
```

Remove the unused `import os` if no remaining code uses it.

- [ ] **Step 4: Run config tests**

Run:

```powershell
python -m pytest -q tests/research/core/test_config.py tests/research/features/test_config.py
```

Expected:

```text
passed
```

- [ ] **Step 5: Update codebase review**

Append to `docs/codebase-review.md`:

```markdown
### 2026-04-27 — Research config SSOT path fixed

- Fixed `src/research/core/config.py` so default `load_research_config()` reads repo-root `config/research.ini` and `config/research.local.ini`.
- Added tests for default path, base config load, and local override order.
- Boundary impact: research configuration now follows the same root config convention as the rest of the system; no compatibility branch was added.
- Remaining risk: fresh mining results created before this fix must be treated as stale unless the command passed an explicit ini path.
```

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/research/core/config.py tests/research/core/test_config.py docs/codebase-review.md
git commit -m "fix: load research config from repo config"
```

Expected:

```text
[main <sha>] fix: load research config from repo config
```

---

### Task 3: Filter Barrier Findings By Tradability

**Files:**
- Modify: `src/research/orchestration/runner.py`
- Create: `tests/research/orchestration/test_runner_barrier_findings.py`
- Modify: `docs/research-system.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Write tests for barrier finding filters**

Create `tests/research/orchestration/test_runner_barrier_findings.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from src.research.core.contracts import (
    DataSummary,
    IndicatorBarrierPredictiveResult,
    MiningResult,
)
from src.research.orchestration.runner import MiningRunner


def _bp(
    *,
    indicator: str,
    ic: float,
    mean_return_pct: float,
    tp_hit_rate: float = 0.60,
    sl_hit_rate: float = 0.25,
    is_significant: bool = True,
    n_samples: int = 120,
) -> IndicatorBarrierPredictiveResult:
    return IndicatorBarrierPredictiveResult(
        indicator_name=indicator,
        field_name="value",
        regime=None,
        direction="long",
        barrier_key=(1.5, 2.0, 30),
        n_samples=n_samples,
        pearson_r=ic,
        spearman_rho=ic,
        information_coefficient=ic,
        p_value=0.01,
        permutation_p_value=0.01,
        is_significant=is_significant,
        tp_hit_rate=tp_hit_rate,
        sl_hit_rate=sl_hit_rate,
        time_exit_rate=1.0 - tp_hit_rate - sl_hit_rate,
        mean_bars_held=12.0,
        mean_return_pct=mean_return_pct,
    )


def _result(items: list[IndicatorBarrierPredictiveResult]) -> MiningResult:
    return MiningResult(
        run_id="test",
        started_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        data_summary=DataSummary(
            symbol="XAUUSD",
            timeframe="H1",
            n_bars=500,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 1, tzinfo=timezone.utc),
            train_bars=350,
            test_bars=150,
            regime_distribution={},
            available_indicators=[],
        ),
        barrier_predictive_power=items,
    )


def test_barrier_top_findings_exclude_negative_mean_return() -> None:
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result(
            [
                _bp(indicator="bad", ic=0.35, mean_return_pct=-0.04),
                _bp(indicator="good", ic=0.20, mean_return_pct=0.03),
            ]
        )
    )

    summaries = [f.summary for f in findings]
    assert any("good.value" in s for s in summaries)
    assert not any("bad.value" in s for s in summaries)


def test_barrier_top_findings_exclude_significant_but_sl_dominant() -> None:
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result(
            [
                _bp(
                    indicator="sl_dominant",
                    ic=0.35,
                    mean_return_pct=0.02,
                    tp_hit_rate=0.20,
                    sl_hit_rate=0.55,
                ),
                _bp(indicator="balanced", ic=0.18, mean_return_pct=0.02),
            ]
        )
    )

    summaries = [f.summary for f in findings]
    assert any("balanced.value" in s for s in summaries)
    assert not any("sl_dominant.value" in s for s in summaries)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m pytest -q tests/research/orchestration/test_runner_barrier_findings.py
```

Expected before implementation:

```text
FAILED
```

- [ ] **Step 3: Add a local helper in `runner.py`**

Add this helper above `class MiningRunner` in `src/research/orchestration/runner.py`:

```python
def _is_tradeable_barrier_finding(bp: Any) -> bool:
    """Return True only when a barrier finding is statistically and economically usable."""
    if not bp.is_significant:
        return False
    if bp.mean_return_pct <= 0:
        return False
    if bp.tp_hit_rate <= bp.sl_hit_rate:
        return False
    return True
```

- [ ] **Step 4: Use the helper in barrier ranking**

Replace this block in `_rank_findings()`:

```python
for bp in result.barrier_predictive_power:
    if not bp.is_significant:
        continue
```

with:

```python
for bp in result.barrier_predictive_power:
    if not _is_tradeable_barrier_finding(bp):
        continue
```

- [ ] **Step 5: Include mean return in barrier summary**

Update the barrier `summary` string so it includes `ret=...`:

```python
summary=(
    f"{bp.indicator_name}.{bp.field_name} {bp.direction} "
    f"IC={ic:+.3f} barrier={sl_atr}/{tp_atr}/{time_bars} "
    f"tp={bp.tp_hit_rate * 100:.0f}%/"
    f"sl={bp.sl_hit_rate * 100:.0f}% "
    f"ret={bp.mean_return_pct:+.4f}% "
    f"bars={bp.mean_bars_held:.1f} n={bp.n_samples}{regime_str}"
),
```

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m pytest -q tests/research/orchestration/test_runner_barrier_findings.py tests/research/core/test_config.py
```

Expected:

```text
passed
```

- [ ] **Step 7: Update docs**

In `docs/research-system.md`, add this rule to the mining candidate section:

```markdown
Barrier predictive power findings are candidate inputs only when they are both statistically significant and economically usable: `mean_return_pct > 0` after cost and `tp_hit_rate > sl_hit_rate`. Statistically significant negative-return findings are treated as avoidance or reverse-direction evidence, not promotion evidence.
```

Append to `docs/codebase-review.md`:

```markdown
### 2026-04-27 — Barrier finding ranking now filters by tradability

- `MiningRunner._rank_findings()` no longer promotes significant barrier findings with non-positive cost-after mean return.
- Added an explicit exit-distribution guard: top findings require `tp_hit_rate > sl_hit_rate`.
- Boundary impact: research ranking now separates statistical significance from tradeability; no strategy code was changed.
- Remaining risk: older mining JSON files may still contain negative-return top findings and must not be used for promotion.
```

- [ ] **Step 8: Commit**

Run:

```powershell
git add src/research/orchestration/runner.py tests/research/orchestration/test_runner_barrier_findings.py docs/research-system.md docs/codebase-review.md
git commit -m "fix: filter barrier findings by positive expectancy"
```

Expected:

```text
[main <sha>] fix: filter barrier findings by positive expectancy
```

---

### Task 4: Unify Mining Walk-Forward With MiningRunner

**Files:**
- Create: `src/research/orchestration/walk_forward.py`
- Modify: `src/ops/cli/mining_walk_forward.py`
- Create: `tests/research/orchestration/test_walk_forward.py`
- Modify: `docs/research-system.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Write tests for walk-forward service**

Create `tests/research/orchestration/test_walk_forward.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.orchestration.walk_forward import (
    aggregate_rules_across_windows,
    split_windows,
    stable_rules,
)


def _condition(indicator: str, field: str, operator: str, threshold: float):
    return SimpleNamespace(
        indicator=indicator,
        field=field,
        operator=operator,
        threshold=threshold,
    )


def _rule(direction: str, indicator: str, threshold: float, hit_rate: float):
    return SimpleNamespace(
        direction=direction,
        conditions=[_condition(indicator, "value", ">=", threshold)],
        train_hit_rate=hit_rate,
        test_hit_rate=hit_rate - 0.02,
        train_n_samples=80,
        barrier_stats_train=[],
    )


def test_split_windows_creates_contiguous_non_overlapping_windows() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 7, tzinfo=timezone.utc)

    windows = split_windows(start, end, 3)

    assert len(windows) == 3
    assert windows[0][0] == start
    assert windows[-1][1] == end
    assert windows[0][1] == windows[1][0]
    assert windows[1][1] == windows[2][0]


def test_stable_rules_require_minimum_window_appearances() -> None:
    per_window = [
        [_rule("long", "adx14", 25.0, 0.58)],
        [_rule("long", "adx14", 25.0, 0.56)],
        [_rule("long", "rsi14", 50.0, 0.57)],
        [_rule("long", "adx14", 25.0, 0.59)],
    ]

    aggregated = aggregate_rules_across_windows(per_window)
    stable = stable_rules(aggregated, n_splits=4, min_consistency=0.60)

    assert [item["key"] for item in stable] == ["adx14.value>=25.00"]
    assert stable[0]["appearances"] == [0, 1, 3]
    assert stable[0]["appearance_count"] == 3
```

- [ ] **Step 2: Create `walk_forward.py` service**

Create `src/research/orchestration/walk_forward.py`:

```python
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class MiningWalkForwardWindow:
    index: int
    start: datetime
    end: datetime
    rule_count: int


@dataclass(frozen=True)
class MiningWalkForwardResult:
    timeframe: str
    windows: List[MiningWalkForwardWindow]
    stable: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "windows": [
                {
                    "index": window.index,
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "rule_count": window.rule_count,
                }
                for window in self.windows
            ],
            "stable_rules": self.stable,
        }


def split_windows(
    start: datetime, end: datetime, n_splits: int
) -> List[Tuple[datetime, datetime]]:
    if n_splits < 2:
        raise ValueError(f"n_splits must be >= 2, got {n_splits}")
    total_seconds = (end - start).total_seconds()
    if total_seconds <= 0:
        raise ValueError("end must be after start")
    window_seconds = total_seconds / n_splits
    windows: List[Tuple[datetime, datetime]] = []
    for i in range(n_splits):
        w_start = start + timedelta(seconds=i * window_seconds)
        w_end = end if i == n_splits - 1 else start + timedelta(seconds=(i + 1) * window_seconds)
        windows.append((w_start, w_end))
    return windows


def rule_key(conditions: list) -> str:
    parts: List[str] = []
    for condition in sorted(
        conditions, key=lambda item: (item.indicator, item.field, item.operator)
    ):
        parts.append(
            f"{condition.indicator}.{condition.field}"
            f"{condition.operator}{condition.threshold:.2f}"
        )
    return " & ".join(parts)


def aggregate_rules_across_windows(
    per_window_rules: List[List[Any]],
) -> Dict[str, Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}
    for window_idx, rules in enumerate(per_window_rules):
        for rule in rules:
            key = rule_key(rule.conditions)
            if key not in aggregated:
                aggregated[key] = {
                    "key": key,
                    "direction": rule.direction,
                    "appearances": [],
                    "train_hit_rates": [],
                    "test_hit_rates": [],
                    "train_n_samples": [],
                }
            item = aggregated[key]
            item["appearances"].append(window_idx)
            item["train_hit_rates"].append(float(rule.train_hit_rate))
            if rule.test_hit_rate is not None:
                item["test_hit_rates"].append(float(rule.test_hit_rate))
            item["train_n_samples"].append(int(rule.train_n_samples))
    return aggregated


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stable_rules(
    aggregated: Dict[str, Dict[str, Any]],
    *,
    n_splits: int,
    min_consistency: float,
) -> List[Dict[str, Any]]:
    min_appearances = max(2, math.ceil(n_splits * min_consistency))
    selected: List[Dict[str, Any]] = []
    for key, item in aggregated.items():
        appearance_count = len(item["appearances"])
        if appearance_count < min_appearances:
            continue
        selected.append(
            {
                "key": key,
                "direction": item["direction"],
                "appearances": list(item["appearances"]),
                "appearance_count": appearance_count,
                "avg_train_hit_rate": _mean(item["train_hit_rates"]),
                "avg_test_hit_rate": _mean(item["test_hit_rates"]),
                "avg_train_n_samples": _mean([float(v) for v in item["train_n_samples"]]),
            }
        )
    selected.sort(
        key=lambda item: (
            -int(item["appearance_count"]),
            -float(item["avg_test_hit_rate"]),
            -float(item["avg_train_hit_rate"]),
        )
    )
    return selected


def run_mining_walk_forward(
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
    splits: int,
    min_consistency: float,
    mine_window: Callable[[datetime, datetime], Any],
) -> MiningWalkForwardResult:
    windows = split_windows(start, end, splits)
    per_window_rules: List[List[Any]] = []
    summaries: List[MiningWalkForwardWindow] = []
    for idx, (window_start, window_end) in enumerate(windows):
        result = mine_window(window_start, window_end)
        rules = list(result.mined_rules or [])
        per_window_rules.append(rules)
        summaries.append(
            MiningWalkForwardWindow(
                index=idx,
                start=window_start,
                end=window_end,
                rule_count=len(rules),
            )
        )
    aggregated = aggregate_rules_across_windows(per_window_rules)
    return MiningWalkForwardResult(
        timeframe=timeframe,
        windows=summaries,
        stable=stable_rules(
            aggregated,
            n_splits=splits,
            min_consistency=min_consistency,
        ),
    )
```

- [ ] **Step 3: Run service tests**

Run:

```powershell
python -m pytest -q tests/research/orchestration/test_walk_forward.py
```

Expected:

```text
passed
```

- [ ] **Step 4: Modify CLI to use the service**

Modify `src/ops/cli/mining_walk_forward.py` so it imports and calls:

```python
from src.research.orchestration.walk_forward import run_mining_walk_forward
```

Add parser arguments:

```python
parser.add_argument(
    "--providers",
    type=str,
    default=None,
    help="Feature Providers to enable, comma-separated; 'all' enables all providers.",
)
parser.add_argument(
    "--child-tf",
    default="",
    help="Child TF for intrabar features, e.g. M5 for H1 parent.",
)
parser.add_argument(
    "--json-output",
    default=None,
    help="Write walk-forward payload to JSON file.",
)
```

Also add persistence arguments by reusing the existing helper:

```python
from src.ops.cli._persistence import add_persist_arguments

add_persist_arguments(parser)
```

Update `_mine_window()` signature:

```python
def _mine_window(
    tf: str,
    window_start: datetime,
    window_end: datetime,
    *,
    child_tf: str = "",
    providers: Optional[List[str]] = None,
) -> Any:
```

Inside `_mine_window()`, apply provider overrides with the same `dataclasses.replace` pattern used in `src/ops/cli/mining_runner.py`.

- [ ] **Step 5: Add JSON output**

At the end of `main()` in `src/ops/cli/mining_walk_forward.py`, write:

```python
if args.json_output:
    import json

    output_path = Path(args.json_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(wf_result.to_dict(), fh, ensure_ascii=False, indent=2)
```

- [ ] **Step 6: Run CLI smoke test**

Run:

```powershell
python -m src.ops.cli.mining_walk_forward --environment live --tf H1 --start 2026-01-01 --end 2026-03-01 --splits 3 --providers temporal,microstructure --json-output data/research/wf_smoke_2026-04-27.json
```

Expected:

```text
Walk-Forward Mining
```

And `data/research/wf_smoke_2026-04-27.json` exists with `timeframe`, `windows`, and `stable_rules`.

- [ ] **Step 7: Run tests**

Run:

```powershell
python -m pytest -q tests/research/orchestration/test_walk_forward.py tests/research/orchestration/test_runner_barrier_findings.py tests/research/core/test_config.py
```

Expected:

```text
passed
```

- [ ] **Step 8: Update docs and commit**

Append to `docs/codebase-review.md`:

```markdown
### 2026-04-27 — Mining walk-forward moved behind orchestration service

- Added `src/research/orchestration/walk_forward.py` as the tested service boundary for mining-layer walk-forward.
- `src/ops/cli/mining_walk_forward.py` becomes a CLI adapter instead of owning rule aggregation semantics.
- Boundary impact: walk-forward stability logic is now reusable and testable; CLI-specific JSON/persistence concerns stay outside the service.
- Remaining risk: older walk-forward logs without provider/child-TF metadata should not be used as promotion evidence.
```

Run:

```powershell
git add src/research/orchestration/walk_forward.py src/ops/cli/mining_walk_forward.py tests/research/orchestration/test_walk_forward.py docs/research-system.md docs/codebase-review.md
git commit -m "refactor: unify mining walk-forward orchestration"
```

Expected:

```text
[main <sha>] refactor: unify mining walk-forward orchestration
```

---

### Task 5: Run Fresh Mining Campaign

**Files:**
- Create: `data/research/fresh_mining_2026-04-27.json`
- Create: `data/research/fresh_mining_wf_h1_2026-04-27.json`
- Create: `data/research/fresh_mining_wf_m30_2026-04-27.json`
- Modify: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run comprehensive mining**

Run:

```powershell
python -m src.ops.cli.mining_runner --environment live --tf H4,H1,M30,M15 --start 2025-04-01 --end 2026-04-26 --compare --emit-feature-candidates --providers all --child-tf M5 --json-output data/research/fresh_mining_2026-04-27.json --persist --experiment fresh-mining-2026-04-27
```

Expected:

```text
XAUUSD Multi-TF Mining Comparison
Feature Candidates
[persist] saved
```

- [ ] **Step 2: Run H1 mining walk-forward**

Run:

```powershell
python -m src.ops.cli.mining_walk_forward --environment live --tf H1 --start 2025-04-01 --end 2026-04-26 --splits 6 --min-consistency 0.60 --providers all --child-tf M5 --json-output data/research/fresh_mining_wf_h1_2026-04-27.json --persist --experiment fresh-mining-wf-2026-04-27
```

Expected:

```text
Walk-Forward Mining
```

- [ ] **Step 3: Run M30 mining walk-forward**

Run:

```powershell
python -m src.ops.cli.mining_walk_forward --environment live --tf M30 --start 2025-04-01 --end 2026-04-26 --splits 6 --min-consistency 0.60 --providers all --child-tf M5 --json-output data/research/fresh_mining_wf_m30_2026-04-27.json --persist --experiment fresh-mining-wf-2026-04-27
```

Expected:

```text
Walk-Forward Mining
```

- [ ] **Step 4: Interpret mining results**

Update `docs/research/2026-04-27-fresh-mining-and-validation-plan.md` with:

```markdown
## Fresh Mining Result

### Files

- `data/research/fresh_mining_2026-04-27.json`
- `data/research/fresh_mining_wf_h1_2026-04-27.json`
- `data/research/fresh_mining_wf_m30_2026-04-27.json`

### Promotion Interpretation

- Feature candidates may proceed to backtest only when they are live-computable and backed by positive barrier evidence.
- Rule candidates may proceed to backtest only when they appear in at least 60% of walk-forward windows.
- If no stable rule exists, strategy mining is considered non-promotable for this round.
```

- [ ] **Step 5: Commit mining evidence index**

Run:

```powershell
git add docs/research/2026-04-27-fresh-mining-and-validation-plan.md docs/codebase-review.md
git commit -m "docs: record fresh mining evidence"
```

Expected:

```text
[main <sha>] docs: record fresh mining evidence
```

---

### Task 6: Live-Aligned Backtest Validation

**Files:**
- Create: `data/artifacts/backtests/live_aligned_demo_validation_2026-04-27.json`
- Create: `data/artifacts/backtests/live_aligned_execution_feasibility_2026-04-27.json`
- Modify: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run demo-validation research-mode backtest**

Run:

```powershell
python -m src.ops.cli.backtest_runner --environment live --tf H4,H1,M30,M15 --start 2025-04-01 --end 2026-04-26 --compare --include-demo-validation --monte-carlo --json-output data/artifacts/backtests/live_aligned_demo_validation_2026-04-27.json --persist --experiment live-aligned-demo-validation-2026-04-27
```

Expected:

```text
Running H4
Running H1
Running M30
Running M15
Done.
```

- [ ] **Step 2: Run execution-feasibility backtest**

Run:

```powershell
python -m src.ops.cli.backtest_runner --environment live --tf H4,H1,M30,M15 --start 2025-04-01 --end 2026-04-26 --compare --include-demo-validation --simulation-mode execution_feasibility --monte-carlo --json-output data/artifacts/backtests/live_aligned_execution_feasibility_2026-04-27.json --persist --experiment live-aligned-execution-feasibility-2026-04-27
```

Expected:

```text
Done.
```

- [ ] **Step 3: Apply promotion gates**

A strategy or feature-backed strategy may proceed only when the artifact shows:

```text
profit_factor >= 1.20
expectancy > 0
max_drawdown_pct <= 15
total_trades >= 80
execution_feasibility.accepted_ratio >= 0.85
monte_carlo.p_value <= 0.10
walk_forward.consistency >= 0.70
```

For demo_validation continuation only:

```text
demo_validation_min_trades >= 20
no below_min_volume rejection cluster
no single-session-only edge unless strategy session lock is explicit
```

- [ ] **Step 4: Update validation document**

Append to `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`:

```markdown
## Live-Aligned Backtest Result

### Artifacts

- `data/artifacts/backtests/live_aligned_demo_validation_2026-04-27.json`
- `data/artifacts/backtests/live_aligned_execution_feasibility_2026-04-27.json`

### Promotion Decision

No strategy may be promoted unless the execution-feasibility artifact passes accepted-ratio, volume, drawdown, Monte Carlo, and trade-count gates.
```

- [ ] **Step 5: Commit validation evidence index**

Run:

```powershell
git add docs/research/2026-04-27-fresh-mining-and-validation-plan.md docs/codebase-review.md
git commit -m "docs: record live aligned backtest gates"
```

Expected:

```text
[main <sha>] docs: record live aligned backtest gates
```

---

### Task 7: Demo Validation And Demo-vs-Backtest Reconciliation

**Files:**
- Modify: `docs/runbooks/system-startup-and-live-canary.md`
- Modify: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run live preflight before any demo validation window**

Run:

```powershell
python -m src.ops.cli.live_preflight --environment demo
```

Expected:

```text
No live activation
demo-main
```

If this command reports live binding drift, stop the validation round and fix configuration before continuing.

- [ ] **Step 2: Start demo runtime using the documented entrypoint**

Run:

```powershell
python -m src.entrypoint.web
```

Expected:

```text
uvicorn
```

Keep the process running only during the planned demo validation window.

- [ ] **Step 3: Run health checks during demo window**

Run in a separate PowerShell:

```powershell
python -m src.ops.cli.health_check --environment demo
```

Expected:

```text
ready
```

- [ ] **Step 4: Reconcile demo against persisted backtest run**

After at least 20 eligible demo trades for a strategy, run:

```powershell
python -m src.ops.cli.demo_vs_backtest --backtest-run-id <actual_backtest_run_id> --start 2026-04-27 --end 2026-05-04
```

Expected:

```text
Demo vs Backtest
```

The `<actual_backtest_run_id>` must be copied from the persisted `backtest_runs` output produced in Task 6.

- [ ] **Step 5: Document the reconciliation outcome**

Append to `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`:

```markdown
## Demo Validation Result

### Required Evidence

- Demo validation window start/end
- Strategy names
- Backtest run id
- Demo-vs-backtest drift summary
- Broker rejection reasons, grouped by reason
- Decision: remain demo_validation, reject, or prepare active_guarded proposal

### Promotion Guard

Any strategy with unresolved demo/backtest drift remains in `demo_validation`.
```

- [ ] **Step 6: Commit demo validation record**

Run:

```powershell
git add docs/runbooks/system-startup-and-live-canary.md docs/research/2026-04-27-fresh-mining-and-validation-plan.md docs/codebase-review.md
git commit -m "docs: record demo validation reconciliation gate"
```

Expected:

```text
[main <sha>] docs: record demo validation reconciliation gate
```

---

### Task 8: Active-Guarded Promotion Contract

**Files:**
- Modify only when gates pass: `config/signal.ini`
- Modify only when gates pass: `config/signal.local.ini`
- Modify: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Confirm no promotion happens by default**

Run:

```powershell
python -m src.ops.cli.confidence_check --tf H1
python -m src.ops.cli.confidence_check --tf M30
```

Expected before promotion:

```text
NO LIVE-ELIGIBLE STRATEGIES
```

- [ ] **Step 2: Prepare an active_guarded proposal only after gates pass**

For a strategy to enter `active_guarded`, document:

```markdown
## Active-Guarded Proposal

- Strategy:
- Timeframe lock:
- Session lock:
- Max live positions:
- Pending-entry requirement:
- Backtest run id:
- Demo validation window:
- Demo-vs-backtest result:
- Known failure modes:
- Rollback command:
```

The proposal must live in `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`.

- [ ] **Step 3: Apply config change only for approved strategy**

When an operator approves one strategy, update the relevant strategy deployment in `config/signal.ini` or controlled local override:

```ini
[strategy_deployment.<strategy_name>]
status = active_guarded
locked_tfs = H1
locked_sessions = london,new_york
max_live_positions = 1
require_pending_entry = true
demo_validation_required = true
```

Do not add multiple strategies in the same promotion commit.

- [ ] **Step 4: Run deployment tests**

Run:

```powershell
python -m pytest -q tests/config/test_demo_main_account_bindings.py
python -m src.ops.cli.confidence_check --tf H1
python -m src.ops.cli.live_preflight --environment live
```

Expected:

```text
passed
active_guarded
```

`live_preflight` must not report binding drift, SSOT drift, or missing risk guards.

- [ ] **Step 5: Commit one-strategy promotion**

Run:

```powershell
git add config/signal.ini config/signal.local.ini docs/research/2026-04-27-fresh-mining-and-validation-plan.md docs/codebase-review.md
git commit -m "config: promote one strategy to active guarded"
```

Expected:

```text
[main <sha>] config: promote one strategy to active guarded
```

---

### Task 9: Documentation Drift Cleanup

**Files:**
- Modify: `docs/design/full-runtime-dataflow.md`
- Modify: `docs/research-system.md`
- Modify: `docs/signal-system.md`
- Modify: `docs/README.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Remove stale PaperTradingBridge description**

In `docs/design/full-runtime-dataflow.md`, replace any runtime diagram edge that routes through `PaperTradingBridge` with:

```text
SignalRuntime -> ExecutionIntentPublisher -> execution_intents -> ExecutionIntentConsumer -> TradeExecutor
```

Document that demo validation uses demo broker execution, not paper shadow.

- [ ] **Step 2: Fix research cross-TF path**

In `docs/research-system.md`, replace:

```text
src/research/core/cross_tf.py
```

with:

```text
src/research/analyzers/multi_tf_aggregator.py
```

- [ ] **Step 3: Sync strategy catalog counts**

Run:

```powershell
python - <<'PY'
from src.signals.strategies.catalog import get_strategy_catalog
catalog = get_strategy_catalog()
print(len(catalog.list_names()))
print("\n".join(sorted(catalog.list_names())))
PY
```

Update `docs/signal-system.md` with the printed count and names.

- [ ] **Step 4: Sync intrabar scope**

Run:

```powershell
python - <<'PY'
from src.signals.strategies.catalog import get_strategy_catalog
catalog = get_strategy_catalog()
for name in sorted(catalog.list_names()):
    strategy = catalog.get(name)
    scopes = getattr(strategy, "signal_scopes", ())
    if "intrabar" in [str(scope) for scope in scopes]:
        print(name)
PY
```

Update `docs/signal-system.md` with the printed intrabar-capable strategies.

- [ ] **Step 5: Run doc-oriented checks**

Run:

```powershell
python -m pytest -q tests/config/test_demo_main_account_bindings.py tests/research/core/test_config.py
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit docs cleanup**

Run:

```powershell
git add docs/design/full-runtime-dataflow.md docs/research-system.md docs/signal-system.md docs/README.md docs/codebase-review.md
git commit -m "docs: sync research and runtime architecture"
```

Expected:

```text
[main <sha>] docs: sync research and runtime architecture
```

---

### Task 10: Final Go / No-Go Review

**Files:**
- Modify: `LIVE_READINESS_EXECUTION_CHECKLIST.md`
- Modify: `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run final test suite subset**

Run:

```powershell
python -m pytest -q tests/research/core/test_config.py tests/research/features/test_config.py tests/research/orchestration/test_runner_barrier_findings.py tests/research/orchestration/test_walk_forward.py tests/config/test_demo_main_account_bindings.py
```

Expected:

```text
passed
```

- [ ] **Step 2: Run final preflight checks**

Run:

```powershell
python -m src.ops.cli.live_preflight --environment demo
python -m src.ops.cli.live_preflight --environment live
```

Expected:

```text
No blocking config drift
```

- [ ] **Step 3: Record final decision**

Append to `docs/research/2026-04-27-fresh-mining-and-validation-plan.md`:

```markdown
## Final Decision

### Decision

- Result: remain demo_validation / promote one strategy to active_guarded / reject current candidates

### Evidence

- Research run id:
- Mining walk-forward artifacts:
- Backtest run id:
- Execution feasibility result:
- Demo validation result:
- Demo-vs-backtest result:

### Operator Notes

- Live activation remains blocked unless this section names exactly one approved active_guarded strategy.
```

- [ ] **Step 4: Update checklist**

In `LIVE_READINESS_EXECUTION_CHECKLIST.md`, add:

```markdown
## 2026-04-27 Research-to-Demo Gate

- [ ] Research config SSOT path verified.
- [ ] Barrier findings filtered by positive cost-after return.
- [ ] Fresh mining artifacts generated after the fixes.
- [ ] Mining walk-forward stable candidates reviewed.
- [ ] Live-aligned backtest artifacts generated.
- [ ] Execution feasibility accepted ratio reviewed.
- [ ] Demo-vs-backtest reconciliation completed.
- [ ] No live strategy enabled without active_guarded approval.
```

- [ ] **Step 5: Commit final review**

Run:

```powershell
git add LIVE_READINESS_EXECUTION_CHECKLIST.md docs/research/2026-04-27-fresh-mining-and-validation-plan.md docs/codebase-review.md
git commit -m "docs: record final trading readiness decision"
```

Expected:

```text
[main <sha>] docs: record final trading readiness decision
```

---

## Execution Order

1. Task 1 creates the evidence frame.
2. Task 2 fixes research config correctness.
3. Task 3 fixes barrier candidate quality.
4. Task 4 makes mining walk-forward reusable and complete.
5. Task 5 runs fresh mining using the fixed stack.
6. Task 6 validates with live-aligned backtests.
7. Task 7 reconciles demo against backtest.
8. Task 8 promotes exactly one strategy only when all gates pass.
9. Task 9 cleans documentation drift.
10. Task 10 records the final go/no-go decision.

## Rollback Rules

- If Task 2 fails, stop. No mining result is trustworthy until research config SSOT is fixed.
- If Task 3 fails, stop. Negative-return significant findings must not enter candidate generation.
- If Task 5 produces no stable candidates, stop at research and document rejection.
- If Task 6 fails execution feasibility, keep strategies in `demo_validation` or `candidate`.
- If Task 7 shows demo/backtest drift, do not promote.
- If Task 8 touches live bindings without one-strategy evidence, revert the promotion commit only.

## Self-Review

- Spec coverage: the plan covers architecture readiness, day-trading execution readiness, statistical mining credibility, backtest/live alignment, demo validation, promotion gates, and documentation drift.
- Placeholder scan: the plan avoids open-ended implementation instructions and names concrete files, commands, expected outputs, and decision gates.
- Type consistency: code snippets use existing objects from `src.research.core.contracts`, existing CLI patterns from `mining_runner.py` and existing promotion semantics from `StrategyDeployment`.
