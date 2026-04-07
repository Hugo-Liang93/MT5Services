"""实验 ID 生成工具。"""

import uuid


def generate_experiment_id() -> str:
    """生成唯一的实验 ID。"""
    return f"exp_{uuid.uuid4().hex[:12]}"
