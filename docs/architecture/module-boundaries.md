# 模块边界与职责定义

本文档定义当前代码库的实际模块边界，避免再次出现 API 层装配运行时、展示层直连领域内部状态的问题。

## 1. 模块总览

```text
src/
├─ app_runtime/    运行时容器、工厂、构建、生命周期
├─ api/            HTTP 路由、Schema、中间件、DI 适配
├─ readmodels/     前端与运维统一读模型
├─ studio/         Studio 展示层与事件桥接
├─ market/         内存行情缓存
├─ ingestion/      MT5 数据采集
├─ indicators/     指标计算与快照发布
├─ signals/        信号评估、状态机、投票、跟踪
├─ trading/        交易执行、持仓、结果跟踪
├─ calendar/       经济日历与交易保护
├─ monitoring/     健康监控与告警
├─ persistence/    DB 写入与仓储
├─ config/         配置加载与配置模型
└─ backtesting/    回测、优化、前推
```

## 2. 关键边界

### 2.1 `src/app_runtime/`

职责：

- 创建并装配运行时组件
- 管理启动与关闭顺序
- 注册监控组件

不应做：

- 不应承载 HTTP 路由
- 不应承载前端视图拼装

当前约束：

- 工厂只放在 `src/app_runtime/factories/`
- 不再使用 `src/api/factories/`

### 2.2 `src/api/`

职责：

- 提供 `/v1/...` HTTP 路由
- 处理请求参数与响应模型
- 通过 `deps.py` 访问运行时组件

不应做：

- 不应装配领域组件
- 不应重复聚合运行时摘要
- 不应读取领域模块私有属性
- 运行时状态查询优先依赖 `RuntimeReadModel`，不要继续直接拼 `status()` 返回值

### 2.3 `src/readmodels/`

职责：

- 将运行时状态转换为稳定的查询投影
- 给 `admin`、`monitoring`、后续前端接口复用

适合放入：

- Dashboard 聚合
- Health Report 运行时补充
- 交易监控摘要
- signal runtime / executor / pending entry / tracked positions 统一投影
- 统一 DTO / projection 逻辑

不应做：

- 不应启动线程
- 不应直接执行交易或写库

### 2.4 `src/studio/`

职责：

- 承担 Studio 的展示层装配
- 管理 Studio agent 映射、summary provider、事件桥接

当前约束：

- Studio 装配入口为 `src/studio/runtime.py`
- `builder.py` 不再直接写 Studio mappers
- Studio 只消费领域模块 public API，不直接读取私有属性

### 2.5 `src/signals/`

职责：

- 策略注册与评估
- 状态机、投票、过滤、跟踪
- 暴露稳定的策略元数据接口

当前约束：

- 对外统一使用 `describe_strategy()`、`strategy_catalog()`
- API 层不再探测内部缓存拿 category 或 affinity

### 2.6 `src/monitoring/`

职责：

- 周期性检查组件状态
- 记录健康指标与告警
- 输出组件注册清单

当前约束：

- 组件注册由 `AppRuntime` 完成
- API 层不再手工补 `trading` 组件

## 3. 依赖方向

允许的主要依赖方向：

```text
api -> deps -> app_runtime/container
api -> readmodels
app_runtime -> market/ingestion/indicators/signals/trading/calendar/monitoring
studio -> 领域组件只读适配
readmodels -> 领域组件只读聚合
```

不允许的方向：

```text
app_runtime -> api
readmodels -> api
studio -> api
api -> 领域私有实现
```

## 4. 当前公共抽象

已经抽出的公共层：

- `RuntimeReadModel`
  - 统一运行时投影，消除 admin/monitoring 重复聚合
  - 继续承接 signal runtime、trade executor、pending entries、tracked positions 的统一查询协议
- `SignalModule.strategy_catalog()`
  - 统一策略目录查询
- `SignalModule.describe_strategy()`
  - 统一单策略元数据查询
- `src/studio/runtime.py`
  - 统一 Studio 装配与事件桥接
- `StudioService.build_snapshot()`
  - 同一次快照构建只生成一份 agent 状态，避免 summary 与 agents 前后不一致

## 5. 后续开发规则

- 新增前端查询接口时，先考虑是否应进入 `src/readmodels/`
- 新增 Studio 展示能力时，先改 `src/studio/runtime.py`
- 新增运行时组件时，统一在 `AppRuntime._register_monitoring()` 注册监控
- 新增策略元数据字段时，统一从 `SignalModule` public API 暴露
