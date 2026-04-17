<!--
severity: critical
tag: CRITICAL
required_vars: instance, consecutive_failures, last_reason, auto_reset_minutes
-->
🔒 *熔断器打开，自动交易停止*

• 实例: {{ instance }}
• 连续失败: {{ consecutive_failures }} 次
• 最近原因: {{ last_reason }}
• 自动恢复: {{ auto_reset_minutes }} 分钟后

_手动恢复: /reset-circuit_
