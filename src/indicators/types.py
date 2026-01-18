from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class IndicatorTask:
    """指标任务定义。"""

    name: str
    func_path: str
    params: Dict[str, Any]
