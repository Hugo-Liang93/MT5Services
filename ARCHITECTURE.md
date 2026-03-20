# Architecture

## 目标

本仓库的工程目标不是做一个通用脚本集合，而是做一个可运行、可验证、可服务化的量化交易系统。

开发默认围绕三个层次展开：

1. 交易系统稳定运行
2. 交易能力通过 API 稳定暴露
3. 为上层 agent / 策略系统提供清晰、可机读、可组合的接口

## 领域边界

当前仓库按业务域拆分，而不是按技术杂项堆叠：

- `src/market/`
  - 行情缓存、Quote/Tick/OHLC 读模型、历史窗口读取
- `src/calendar/`
  - 经济日历同步、事件窗口、trade guard
- `src/market_structure/`
  - 前日高低、亚洲区间、开盘区间、压缩/扩张、假突破回收等结构上下文
- `src/indicators/`
  - 指标计算、快照发布、事件驱动计算链
- `src/signals/`
  - Regime、策略评估、投票、信号状态机、信号诊断
- `src/risk/`
  - 前置风控、最终执行闸门、独立可解释的规则
- `src/trading/`
  - 下单、账户、仓位、自动执行、执行跟踪
- `src/monitoring/`
  - 健康检查、运行指标、状态报告
- `src/api/`
  - 服务化暴露层，负责请求/响应与依赖装配
- `src/config/`
  - 唯一运行时配置入口

`src/core/` 不再作为业务服务收纳目录继续扩展。新增领域能力应优先进入明确的顶层域模块。

## 分层约束

每层只做自己的事：

- `market` 提供数据上下文，不做交易决策
- `market_structure` 提供结构上下文，不直接下单
- `indicators` 提供指标上下文，不包含交易执行逻辑
- `signals` 负责判断和状态机，不直接依赖下单细节
- `risk` 负责拦截和解释，不负责造信号
- `trading` 负责执行和仓位生命周期，不回写信号逻辑
- `api` 负责服务化，不承载领域计算

## 开发规则

- 不再引入 `compat` / `fallback` 作为主路径概念
- 新能力先建清晰模块，再接入 runtime，不往大文件里硬塞
- 配置、持久化、诊断、测试必须与功能一起落地
- 新交易逻辑必须说明风险与验证方式
- 对外接口优先 `precheck / preview / execute` 分层

## 面向黄金日内的实现偏好

后续所有黄金日内优化，优先落在以下方向：

1. `session + regime + market_structure` 联合决定策略路由
2. `risk` 与 `trading` 联合控制成本、回撤、熔断与风险预算
3. 评估体系以 `expectancy / drawdown / cost / session-regime behavior` 为核心
4. API 默认暴露结构化、可机读的诊断结果，而不是只返回人看日志

## 下一步扩展标准

当新增一个交易能力时，优先判断它属于哪一类：

- 新行情读模型：放 `market`
- 新结构特征：放 `market_structure`
- 新指标：放 `indicators`
- 新策略逻辑：放 `signals/strategies`
- 新风险规则：放 `risk/rules.py` 或独立规则模块
- 新执行控制：放 `trading`
- 新观测/审计：放 `monitoring` 或 `persistence`

如果一个能力同时跨了多个域，优先新增“上下文层”或“服务层”，不要直接跨层耦合。
