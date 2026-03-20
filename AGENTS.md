# AGENTS.md

## 0.1 No Compatibility Rule

- During the current development phase, do not preserve legacy or compatibility paths when fixing bugs or refactoring code.
- Fix the problem directly at the source module instead of adding `compat`, fallback, adapter, dual-path, or transitional wrappers.
- Prefer one clear implementation path over backward-compatible branching.
- If a change requires structural cleanup, do the cleanup now rather than layering compatibility on top of a known-bad design.

## 0. 当前架构修正

- `src/core/` 不再作为新增业务能力的默认落点
- 新增领域能力优先放入 `market / calendar / market_structure / indicators / signals / risk / trading / monitoring / api / config`
- 不再引入新的 `compat` / `fallback` 主路径概念
- 黄金日内相关的新特征优先建设为独立上下文层，再接入策略和风控

## 1. 项目核心目标
协助基于 Python 与 MT5 构建量化交易系统，并逐步沉淀为可供 AI agent 或其他系统调用的 API 服务。

这意味着本仓库中的所有代码修改、设计决策、测试补充、接口扩展，都应优先服务于以下主线目标：

1. 先保证交易系统可运行、可验证、可维护
2. 再保证能力可以通过 API 稳定暴露
3. 最终让 AI agent、策略系统、外部平台能够安全调用这些能力

---

## 2. Agent 在本仓库中的工作职责
当你在本仓库中执行任务时，你的职责不是单纯“写代码”，而是协助推进以下能力建设：

- MT5 连接与运行时稳定性
- 市场数据获取与缓存
- 指标计算与信号生成
- 风控前置校验
- 交易执行与状态跟踪
- 配置管理与运行监控
- API 服务化封装
- 为未来 AI agent / 自动化系统调用预留清晰接口

---

## 3. 最高优先级原则
### 3.1 目标优先级
当出现冲突时，始终按以下优先级决策：

1. 交易正确性
2. 风险可控性
3. 系统稳定性
4. API 一致性
5. 可测试性
6. 可扩展性
7. 代码优雅性

### 3.2 默认决策偏好
- 优先最小改动
- 优先兼容现有结构
- 优先保证 MT5 运行稳定
- 优先保证 API 行为可预测
- 优先选择便于后续 AI agent 调用的实现方式

---

## 4. 本项目的阶段性建设方向
### 第一阶段：交易系统可用
重点是把交易相关能力做扎实：
- MT5 客户端连接稳定
- 行情数据获取可用
- 指标与信号逻辑可复用
- 风控规则可执行
- 下单与仓位逻辑清晰
- 关键失败路径可处理

### 第二阶段：服务层标准化
重点是把量化能力服务化：
- 统一 FastAPI 接口风格
- 统一请求/响应结构
- 把交易、行情、指标、监控能力以 API 暴露
- 明确健康检查、认证、配置、日志、错误码

### 第三阶段：面向 Agent 调用
重点是让 AI agent 或其他系统更容易接入：
- 接口命名清晰
- 输出结构稳定
- 参数约束明确
- 错误信息可机读
- 行为尽量幂等或可解释
- 文档足够让上层系统直接接入

---

## 5. 关键目录认知
- `app.py`
  - 服务启动入口

- `src/api/`
  - FastAPI 应用、路由、中间件、依赖注入
  - 对外 API 暴露层

- `src/config/`
  - 集中式配置读取、合并、校验
  - 所有运行参数的入口

- `src/clients/`
  - MT5 及其他外部依赖客户端封装

- `src/core/`
  - 核心服务逻辑，例如市场数据服务、运行时核心能力

- `src/ingestion/`
  - 后台采集、补数、数据摄取逻辑

- `src/indicators/`
  - 指标计算逻辑

- `src/signals/`
  - 基于指标和行情生成交易信号
  - `strategies/` — 策略实现层（`base.py` Protocol、`trend.py`、`mean_reversion.py`、`breakout.py`、`composite.py`、`registry.py`）
  - `evaluation/` — Regime 分类（`regime.py`）、置信度校准（`calibrator.py`）、投票引擎（`voting.py`）
  - `execution/` — 信号过滤器（`filters.py`）、仓位策略（`policy.py`）、仓位大小计算（`sizing.py`）
  - `tracking/` — 持仓管理（`position_manager.py`）、结果追踪（`outcome_tracker.py`）、信号仓储（`repository.py`）
  - `contracts/` — 接口定义（Protocol / ABC）
  - `analytics/` — 信号分析工具
  - `runtime.py` — 事件驱动主循环（双队列架构）
  - `service.py` — 信号模块单例与策略编排

- `src/risk/`
  - 风控校验逻辑
  - `service.py` — PreTradeRiskService（前置风控，含经济日历 Trade Guard）
  - `rules.py` — 仓位限制、手数、SL/TP 规则

- `src/trading/`
  - 下单、仓位、执行、交易管理
  - `service.py` — TradingModule（账户、持仓、订单生命周期）
  - `trading_service.py` — TradingService（底层下单、平仓、保证金计算）
  - `registry.py` — TradingAccountRegistry（多账户注册与服务工厂）
  - `signal_executor.py` — 信号触发的交易执行器

- `src/monitoring/`
  - 健康检查、运行状态、监控指标
  - `health_monitor.py` — HealthMonitor（SQLite 指标存储、告警、健康报告）
  - `manager.py` — MonitoringManager（定时巡检、组件协调）

- `tests/`
  - 单元测试、集成测试、冒烟测试

---

## 6. 修改代码时必须遵守的原则
### 6.1 总原则
- 保持改动最小化
- 优先修复根因，不堆补丁
- 保持现有项目结构一致
- 不为了“更优雅”而大规模重构
- 不破坏已有 API，除非任务明确要求

### 6.2 禁止事项
- 不要硬编码账号、密码、API Key、服务器地址
- 不要在代码中散落新的配置读取方式
- 不要绕开现有配置系统
- 不要无理由改动公共响应结构
- 不要随意重写 MT5 连接模型
- 不要未经说明就改变交易行为
- 不要把调试代码、临时日志直接留在正式实现中

### 6.3 提倡事项
- 优先复用已有模块
- 优先补测试再改逻辑
- 优先让接口输出更稳定、更适合系统调用
- 优先让错误更清晰、可观测、可排查

---

## 7. 面向量化交易系统的特别约束
### 7.1 MT5 相关约束
- 默认认为 MT5 连接可能失败
- 默认认为终端状态可能异常
- 默认认为数据获取可能为空、延迟或中断
- 任何 MT5 调用都要考虑失败恢复路径
- 不要在高频路径中无意义重复初始化

### 7.2 交易逻辑约束
- 任何交易执行改动都必须优先考虑风控
- 任何信号逻辑改动都必须考虑回测/验证影响
- 任何仓位管理改动都必须考虑兼容旧逻辑
- 任何涉及下单的改动都必须说明风险

### 7.3 风控约束
- 风控应优先于信号触发
- 风控应优先于执行优化
- 风控规则要尽量独立、可测试、可解释
- 不允许为了“提高下单成功率”而弱化核心风控

---

## 8. 面向 API 服务化的约束
### 8.1 API 设计原则
所有对外接口都应尽量满足以下要求：

- 输入参数清晰
- 输出结构稳定
- 错误码可识别
- 行为可预测
- 适合程序调用，而不只是适合人工查看

### 8.2 新增 API 时的要求
新增接口前先判断：
1. 现有接口能否扩展解决
2. 是否真的属于服务层职责
3. 是否适合未来 AI agent 调用
4. 是否会引入与 MT5 状态强耦合的问题

### 8.3 面向 Agent 的接口偏好
对于未来可能被 AI agent 调用的接口，优先采用以下风格：
- 参数显式，不依赖隐式状态
- 返回结构标准化
- 失败原因可解释
- 尽量支持 dry-run / precheck / preview
- 对交易类动作优先区分“校验”和“执行”

---

## 9. 配置系统约束
- 所有运行时配置必须优先通过项目现有配置系统读取
- 不新增零散的 ad-hoc 配置读取
- 修改配置行为时，必须评估：
  - 启动是否受影响
  - API 是否受影响
  - MT5 是否受影响
  - 监控是否受影响

### 9.1 编码约束
- 仓库内所有文本文件统一使用 UTF-8 编码
- 通过命令行写入或追加文本文件时，必须显式指定 UTF-8 编码
- 优先使用补丁方式修改文本文件，避免使用未指定编码的 shell 重定向直接写文件
- 如果出现乱码，先区分“文件实际编码错误”和“终端显示编码错误”，不要在未确认前盲目重写文件

涉及以下内容时必须同步更新文档：
- 新配置项
- 默认值变化
- 启动流程变化
- API 行为变化
- 交易/风控行为变化

---

## 10. 测试要求
### 10.1 基本规则
- 修 bug：优先补回归测试
- 新功能：至少补单元测试或集成测试
- 非 trivial 改动：必须说明验证方式
- 不允许只凭“本地看起来能跑”作为结论

### 10.2 测试优先级
1. 单元测试
2. 集成测试
3. 冒烟测试
4. 真实 MT5 环境验证

### 10.3 MT5 限制下的测试策略
- 无法使用真实 MT5 时，优先 mock / stub
- 不能因为 MT5 不可用就放弃测试设计
- 平台限制必须明确写出，不要隐含跳过

---

## 11. 文档同步规则
以下内容变化时，必须同步更新文档：
- 启动方式
- 配置项
- API 路径或响应结构
- 认证与 CORS 行为
- 监控与健康检查语义
- 指标、信号、交易、风控的重要行为

优先同步：
- `README.md`
- `QUICK_START.md`
- `CONFIG_GUIDE.md`

---

## 12. 输出结果格式要求
当完成任务时，优先按下面格式输出：

### 变更概述
- 本次改动目标
- 修改文件
- 核心变化点

### 对核心目标的影响
- 是否提升交易系统稳定性
- 是否提升 API 服务化程度
- 是否更适合 AI agent 调用

### 风险说明
- MT5 运行风险
- 兼容性风险
- 配置风险
- 性能风险

### 验证方式
- 运行命令
- 测试命令
- 手工验证步骤

---

## 13. 默认实现偏好
当需求不够明确时，默认采用以下实现偏好：

1. 选择最小改动方案
2. 选择更稳的方案，而不是更炫的方案
3. 选择更容易测试的方案
4. 选择更适合 API 调用的方案
5. 选择更利于未来 AI agent 调用的结构
6. 选择更容易观测和排错的实现

---

## 14. 一句话执行准则
始终围绕这个目标工作：

先把 Python + MT5 量化交易系统做稳，
再把能力沉淀成清晰、稳定、可测试的 API，
最终让 AI agent 与其他系统可以安全、可靠地调用。
