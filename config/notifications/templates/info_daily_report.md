<!--
severity: info
tag: DAILY
required_vars: instance, report_date_utc, report_time_utc, health_status, active_alerts_count, mode, executor_enabled, circuit_open, signals_running, open_positions, pending_entries, read_model_ok
-->
📊 *每日系统快照*

• 实例: {{ instance }}
• 日期 (UTC): {{ report_date_utc }}
• 生成时刻: {{ report_time_utc }}

*运行状态*
• 健康: {{ health_status }}（活跃告警 {{ active_alerts_count }}）
• 模式: {{ mode }}
• 信号引擎: 运行={{ signals_running }}
• 执行器: 启用={{ executor_enabled }}，熔断={{ circuit_open }}

*仓位*
• 持仓数: {{ open_positions }}
• 待执行挂单: {{ pending_entries }}

{% if read_model_ok %}_读模型正常_{% endif %}
