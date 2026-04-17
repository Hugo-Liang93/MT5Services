<!--
severity: critical
tag: CRITICAL
required_vars: strategy, symbol, direction, reason, instance, trace_id
-->
🚨 *执行失败*

• 策略: `{{ strategy }}` ({{ direction }})
• 品种: `{{ symbol }}`
• 原因: {{ reason }}
• 实例: {{ instance }}
• Trace: `{{ trace_id }}`

_详情: /trace {{ trace_id }}_
