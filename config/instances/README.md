# Instance Config Layout

正式实例配置模型：

```text
config/
  app.ini                 # 全局共享基线（交易品种、轮询、日志等）
  market.ini              # 全局 API 默认值
  db.ini                  # demo/live 环境分库
  mt5.ini                 # MT5 共享默认值（只放通用项）
  signal.ini              # 共享信号与策略绑定
  risk.ini                # 共享风控默认值
  topology.ini            # 实例编排 SSOT
  instances/
    live-main/
      market.ini          # 该实例自己的 host/port
      mt5.ini             # 该实例自己的账户语义（alias/label）
      mt5.local.ini       # 该实例自己的 login/password/server/path
      risk.ini            # 可选：该账户非敏感风控覆盖
      risk.local.ini      # 可选：该账户敏感/本机风控覆盖
    live-exec-a/
      ...
```

规则：

- `topology.ini` 是实例角色与 single/multi-account 的唯一事实源。
- `config/instances/<instance>/mt5.ini` 直接定义该实例绑定的单一账户。
- 根目录 `config/mt5.ini` 不再维护多账户字典，也不再支持 `default_account`。
- `market.ini` 只负责 HTTP 暴露参数；`worker` 默认绑定 `127.0.0.1`，并建议关闭 docs/redoc。
- `risk.ini` 是唯一允许账户级实例覆盖的业务配置文件；用于每个账户自己的风控阈值。
- `signal.ini` 继续作为共享策略与 `account_bindings` 配置；账户别名来自实例自己的 `mt5.ini`。
- `app.ini`、`db.ini`、`signal.ini`、`topology.ini`、`economic.ini`、`ingest.ini`、`storage.ini`、`cache.ini` 都视为共享配置，实例目录下即使存在同名文件也不会参与合并。
- 运行期本地文件会自动按实例隔离：
  - `data/runtime/<instance>/...`
  - `data/logs/<instance>/...`
- `python -m src.entrypoint.instance --instance <name>` 启动单实例。
- `python -m src.entrypoint.supervisor --environment <live|demo>` 按环境启动整组实例。
- `python -m src.entrypoint.supervisor --group <group>` 按 `topology.ini` 启动整组实例。
