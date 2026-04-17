<!--
severity: critical
tag: CRITICAL
required_vars: instance, queue_name, overflow_count, level
-->
⚠ *队列溢出* — 数据永久丢失

• 实例: {{ instance }}
• 队列: `{{ queue_name }}`
• 溢出: {{ overflow_count }} 次（级别 {{ level }}）

_需要人工排查：后端堵塞或消费滞后_
