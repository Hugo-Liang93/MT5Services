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
