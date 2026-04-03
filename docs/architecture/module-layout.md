# 模块布局规范

本文定义仓库内高频领域模块的目录组织规则，目标是：

- 模块职责单一，边界清晰
- 外部依赖面稳定，内部文件可继续重构
- 避免所有实现文件平铺在单一目录下，降低维护成本

## 总体原则

1. 先按领域拆目录，再按职责拆子包。
2. 外部模块优先依赖子包公开接口，不直接依赖子包内部文件。
3. 包根目录只保留少量跨子域共享对象，不堆放执行细节。
4. 新增文件先判断是否属于现有子包职责，只有确实形成新职责时才新增子包。
5. 禁止为兼容历史路径保留空壳转发文件；重组后统一更新导入。

## `src/trading/` 规范

`trading` 根目录仅保留跨子域共享对象：

- `__init__.py`：领域对外导出
- `models.py`：共享审计/记录模型
- `ports.py`：跨边界正式端口协议
- `registry.py`：账户注册与切换
- `trading_service.py`：底层 MT5 交易适配服务

其余实现按职责进入子包：

- `application/`
  - 命令/查询应用服务
  - 交易模块聚合根
  - 交易控制状态
  - 交易审计与日内统计
  - 幂等回放与执行结果复用
- `execution/`
  - 执行门禁
  - 下单执行器
  - 仓位 sizing 与交易参数计算
  - `executor.py` 保留执行协调器
  - `params.py` 负责交易参数、价格与成本估算
  - `pending_orders.py` 负责挂单提交流程、重复防重与成交识别
  - `eventing.py` 负责执行事件、提交结果与成交记录
- `pending/`
  - 挂单追踪、过期、成交衔接
- `positions/`
  - 持仓管理
  - 持仓规则
- `closeout/`
  - 风险收口和平仓/撤单
- `tracking/`
  - 信号质量跟踪
  - 成交结果跟踪
- `state/`
  - 持久化状态模型
  - 状态恢复
  - 状态告警
  - 状态存储

### `trading` 导入规则

外部模块应优先依赖这些包边界：

- `src.trading.application`
- `src.trading.execution`
- `src.trading.pending`
- `src.trading.positions`
- `src.trading.closeout`
- `src.trading.tracking`
- `src.trading.state`

不应直接依赖子包内部文件名，除非在同子包内部实现细节中使用相对导入。

## `src/monitoring/` 规范

`monitoring` 根目录只保留：

- `__init__.py`
- `manager.py`

其余实现按职责进入：

- `health/`
  - 健康检查规则
  - 健康存储
  - 健康报告
- `pipeline/`
  - pipeline 事件总线
  - pipeline 结构化事件分类
  - pipeline trace 持久化 recorder

### `monitoring` 导入规则

外部模块优先依赖：

- `src.monitoring.health`
- `src.monitoring.pipeline`

## `src/api/` 规范

`api` 根目录按“协议适配 + 子域路由”组织：

- 根目录保留：
  - `__init__.py`：FastAPI 应用装配
  - `deps.py`：DI 适配
  - `schemas.py`：共享请求/基础响应模型
  - `<domain>.py`：子域组合根
- 复杂子域应继续拆成子包，例如：
  - `trade.py`：交易 API 组合根
  - `trade_routes/`
    - `commands.py`：交易命令路由
    - `state.py`：状态与只读查询路由
    - `trace.py`：链路追踪路由
    - `runtime.py`：运行模式路由
    - `view_models.py`：前端核心只读 schema
    - `common.py`：仅限子域内复用的路由辅助函数
  - `signal.py`：信号 API 组合根
  - `signal_routes/`
    - `catalog.py`：策略目录、近期信号与汇总
    - `runtime.py`：运行态、仓位与市场结构
    - `diagnostics.py`：质量、冲突与 trace 诊断
    - `view_models.py`：信号运行态与诊断视图 schema
  - `monitoring.py`：监控 API 组合根
  - `monitoring_routes/`
    - `health.py`：健康、组件、性能与指标
    - `runtime.py`：运行态监控、挂单管理与配置热加载
    - `view_models.py`：监控只读视图 schema
  - `market.py`：行情 API 组合根
  - `market_routes/`
    - `query.py`：报价、K 线、tick 与品种查询
    - `stream.py`：流式订阅
  - `indicators.py`：指标 API 组合根
  - `indicators_routes/`
    - `catalog.py`：指标目录、依赖图、缓存与性能
    - `values.py`：指标值查询与计算
    - `models.py`：子域专属 schema
  - `account.py`：账户查询 API 组合根
  - `account_routes/`
    - `queries.py`：账户、持仓、挂单与账户列表查询
    - `common.py`：账户子域内 dataclass 到响应模型的转换
  - `decision.py`：决策摘要 API 组合根
  - `decision_routes/`
    - `brief.py`：决策摘要生成入口
  - `admin.py`：后台 API 组合根
  - `admin_routes/`
    - `dashboard.py`：后台概览
    - `config.py`：配置查看
    - `strategies.py`：策略与绩效报表
    - `streams.py`：事件流与 pipeline 统计
    - `common.py`：子域内共享辅助函数
    - `view_models.py`：后台核心只读视图 schema
  - `economic.py`：经济日历 API 组合根
  - `economic_routes/`
    - `calendar.py`：日历、风险窗口与更新流
    - `impact.py`：市场影响分析
    - `common.py`：子域内共享依赖与序列化
    - `view_models.py`：经济日历增强与 impact 视图 schema
  - `studio.py`：Studio API 组合根
  - `studio_routes/`
    - `rest.py`：Studio 面板查询接口
    - `stream.py`：Studio SSE 推送接口
    - `common.py`：Studio 子域内依赖解析

### `api` 导入规则

1. 路由函数只做 HTTP 协议适配、错误映射和响应封装。
2. 业务编排必须下沉到应用服务或读模型，禁止在路由中堆积跨域决策逻辑。
3. 复杂子域的根路由文件只作为组合根，不直接承载全部实现。
4. 前端核心只读接口优先提供稳定 schema，避免长期使用 `ApiResponse[dict]` 作为主契约。

## `src/signals/orchestration/` 规范

`signals/orchestration` 负责信号运行时编排，但应保持“薄协调器 + 明确协作者”的结构：

- `runtime.py`
  - 运行时协调器
  - 组件生命周期
  - 队列与后台主循环入口
- `runtime_recovery.py`
  - 运行态恢复
  - confirmed/preview 状态还原
- `runtime_processing.py`
  - 事件出队
  - filter/regime 处理
  - 单事件处理流程
- `runtime_evaluator.py`
  - 策略评估
  - confidence 调整
  - 状态迁移与 signal 发布
- `state_machine.py`
  - confirmed/preview 状态迁移规则
- `vote_processor.py`
  - 投票融合
- `htf_resolver.py`
  - HTF 指标解析与对齐辅助

### `signals/orchestration` 导入规则

1. `runtime.py` 作为门面与协调器，不再承载所有细节实现。
2. 新增运行时逻辑时，优先归入 `runtime_recovery.py`、`runtime_processing.py`、`runtime_evaluator.py` 这类明确职责文件。
3. 状态迁移、投票、HTF 解析等独立规则优先留在独立协作者文件，不回流到 `runtime.py`。

## `src/backtesting/` 规范

`backtesting` 负责离线评估、参数优化与前推验证，但同样应保持“薄协调器 + 明确协作者”的结构：

- `engine.py`
  - 回测协调器
  - 主循环与结果汇总
- `engine_filters.py`
  - 过滤器模拟器构建
  - 经济事件回测数据装配
- `engine_indicators.py`
  - 指标预计算
  - Regime 检测
  - HTF 指标时序加载与查找
- `engine_signals.py`
  - 策略评估
  - 状态机推进
  - 挂起入场模拟
  - 开仓执行与信号评估回填

### `backtesting` 导入规则

1. `engine.py` 只保留主循环编排与结果汇总，不再堆积过滤、HTF、策略评估和执行细节。
2. 回测协作者复用实盘纯函数和领域对象，不单独复制 live 策略逻辑。
3. 新增回测能力时，优先归入现有协作者文件；只有形成稳定新职责时才新增文件。

## 新增文件归位规则

新增实现文件前，按以下顺序判断：

1. 是否属于现有领域目录？
2. 是否属于现有子包职责？
3. 是否只是现有文件过大，应该拆到同子包？
4. 只有在无法归入既有职责时，才允许新增子包。

## 过度设计防线

以下情况不应单独新增子包：

- 只有一个简单 helper 函数
- 只是为了命名好看，没有明确职责边界
- 没有稳定对外接口，只是短期临时逻辑

优先采用：

- 根目录共享模型/端口
- 子包内聚
- 包级 `__init__` 稳定导出

而不是：

- 多层空目录
- 兼容转发文件
- 同一职责被拆成多个并列包
