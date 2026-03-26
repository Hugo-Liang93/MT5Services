"""Per-timeframe 策略参数解析器。

将策略参数从全局单一值扩展为 per-TF 可配置：
- 查找优先级：per-TF 值 → 全局默认值 → 策略代码硬编码默认值
- INI 格式：[strategy_params.M5] rsi_reversion__overbought = 72
- 全局格式：[strategy_params] rsi_reversion__overbought = 78（兜底）

设计原则：
- 策略单例不变，参数通过 resolver 查表而非 setattr
- resolver 是不可变查表器，线程安全
- 零性能开销：dict 查表 O(1)
"""

from __future__ import annotations

from typing import Any


class TFParamResolver:
    """Per-TF 策略参数查表器。

    内部结构::

        _params = {
            ("rsi_reversion", "overbought"): {
                "__global__": 78.0,   # 全局默认
                "M5": 72.0,           # M5 覆盖
                "H1": 78.0,           # H1 覆盖
            },
            ...
        }

    查找逻辑::

        get("rsi_reversion", "overbought", "M5")
          → _params[("rsi_reversion","overbought")]["M5"]     # 有 per-TF → 返回
        get("rsi_reversion", "overbought", "H4")
          → _params[("rsi_reversion","overbought")]["__global__"]  # 无 per-TF → 全局
        get("rsi_reversion", "overbought", "D1", default=70)
          → 70.0                                                # 无全局 → default
    """

    _GLOBAL = "__global__"

    def __init__(self) -> None:
        self._params: dict[tuple[str, str], dict[str, float]] = {}

    def set_global(self, strategy: str, param: str, value: float) -> None:
        """设置全局默认值（[strategy_params] section）。"""
        key = (strategy, param)
        if key not in self._params:
            self._params[key] = {}
        self._params[key][self._GLOBAL] = value

    def set_tf(self, strategy: str, param: str, tf: str, value: float) -> None:
        """设置 per-TF 覆盖值（[strategy_params.M5] section）。"""
        key = (strategy, param)
        if key not in self._params:
            self._params[key] = {}
        self._params[key][tf.upper()] = value

    def get(self, strategy: str, param: str, tf: str, *, default: float = 0.0) -> float:
        """查找参数值：per-TF → 全局 → default。"""
        bucket = self._params.get((strategy, param))
        if bucket is None:
            return default
        tf_upper = tf.upper()
        if tf_upper in bucket:
            return bucket[tf_upper]
        if self._GLOBAL in bucket:
            return bucket[self._GLOBAL]
        return default

    def has(self, strategy: str, param: str) -> bool:
        """检查是否有该参数的任何配置。"""
        return (strategy, param) in self._params

    def get_all_for_tf(self, strategy: str, tf: str) -> dict[str, float]:
        """获取某策略在某 TF 下的全部已配置参数。"""
        result: dict[str, float] = {}
        for (s, p), bucket in self._params.items():
            if s != strategy:
                continue
            tf_upper = tf.upper()
            if tf_upper in bucket:
                result[p] = bucket[tf_upper]
            elif self._GLOBAL in bucket:
                result[p] = bucket[self._GLOBAL]
        return result

    def dump(self) -> dict[str, Any]:
        """序列化为可读 dict（用于调试/API）。"""
        out: dict[str, Any] = {}
        for (strategy, param), bucket in sorted(self._params.items()):
            key = f"{strategy}__{param}"
            out[key] = dict(bucket)
        return out

    def __repr__(self) -> str:
        return f"TFParamResolver({len(self._params)} params)"


def build_tf_param_resolver(
    global_params: dict[str, float],
    per_tf_params: dict[str, dict[str, float]],
) -> TFParamResolver:
    """从 INI 加载结果构建 resolver。

    Args:
        global_params: [strategy_params] section → {"supertrend__adx_threshold": 21.0, ...}
        per_tf_params: [strategy_params.M5] etc → {"M5": {"rsi_reversion__overbought": 72}, ...}
    """
    resolver = TFParamResolver()

    # 全局默认
    for compound_key, value in global_params.items():
        parts = compound_key.split("__", 1)
        if len(parts) != 2:
            continue
        resolver.set_global(parts[0], parts[1], float(value))

    # Per-TF 覆盖
    for tf, params in per_tf_params.items():
        for compound_key, value in params.items():
            parts = compound_key.split("__", 1)
            if len(parts) != 2:
                continue
            resolver.set_tf(parts[0], parts[1], tf, float(value))

    return resolver
