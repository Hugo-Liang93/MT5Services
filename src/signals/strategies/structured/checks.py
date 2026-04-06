"""策略检查工具函数集。

纯函数，无状态，供各策略 _why/_when/_where 按需调用。
不强制使用——策略可以自由组合或写自己的逻辑。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


# ═══════════════════════════════════════════════════════════════
# HTF（高时间框架）
# ═══════════════════════════════════════════════════════════════


def htf_direction(
    htf_data: Dict[str, Any],
    min_adx: float = 16.0,
) -> Tuple[Optional[str], float, str]:
    """HTF 方向确认。

    Returns:
        (direction, score, reason)
        direction: "buy" / "sell" / None（无数据或趋势不够强）
        score: 0~1（ADX 越高越确定）
    """
    st = htf_data.get("supertrend14", {})
    htf_dir = st.get("direction")
    adx_data = htf_data.get("adx14", {})
    htf_adx = adx_data.get("adx")

    if htf_dir is None:
        return None, 0, "no_htf"
    if htf_adx is None:
        direction = "buy" if int(htf_dir) == 1 else "sell"
        return direction, 0.3, f"htf:{direction},no_adx"
    htf_adx_f = float(htf_adx)
    if htf_adx_f < min_adx:
        return None, 0, f"htf_weak:{htf_adx_f:.0f}<{min_adx}"

    direction = "buy" if int(htf_dir) == 1 else "sell"
    score = min(htf_adx_f / 40.0, 1.0)
    return direction, score, f"htf:{direction},adx={htf_adx_f:.0f}"


def htf_conflict(
    htf_data: Dict[str, Any],
    direction: str,
    min_adx: float = 25.0,
) -> Tuple[bool, str]:
    """HTF 冲突检查。强趋势反向时返回 True（有冲突）。"""
    st = htf_data.get("supertrend14", {})
    htf_dir = st.get("direction")
    adx_data = htf_data.get("adx14", {})
    htf_adx = adx_data.get("adx")

    if htf_dir is None or htf_adx is None:
        return False, ""
    if float(htf_adx) < min_adx:
        return False, ""
    if direction == "buy" and int(htf_dir) == -1:
        return True, f"htf_bearish:adx={htf_adx}"
    if direction == "sell" and int(htf_dir) == 1:
        return True, f"htf_bullish:adx={htf_adx}"
    return False, ""


# ═══════════════════════════════════════════════════════════════
# ADX / 趋势强度
# ═══════════════════════════════════════════════════════════════


def adx_in_range(
    adx_data: Dict[str, Optional[float]],
    min_adx: Optional[float] = None,
    max_adx: Optional[float] = None,
) -> Tuple[bool, float, str]:
    """ADX 范围检查。

    Returns:
        (passed, score, reason)
        score: ADX 在范围内时按比例给分
    """
    adx = adx_data.get("adx")
    if adx is None:
        return False, 0, "no_adx"
    adx_f = float(adx)
    if min_adx is not None and adx_f < min_adx:
        return False, 0, f"adx_low:{adx_f:.0f}"
    if max_adx is not None and adx_f > max_adx:
        return False, 0, f"adx_high:{adx_f:.0f}"
    # score: 离中点越近越高
    if min_adx is not None and max_adx is not None:
        mid = (min_adx + max_adx) / 2
        half = (max_adx - min_adx) / 2
        score = max(0.0, 1.0 - abs(adx_f - mid) / half) if half > 0 else 0.5
    elif max_adx is not None:
        score = max(0.0, (max_adx - adx_f) / max_adx)
    else:
        score = min(adx_f / 40.0, 1.0)
    return True, score, f"adx={adx_f:.0f}"


def adx_rising(
    adx_data: Dict[str, Optional[float]],
    min_d3: float = 0.5,
) -> Tuple[bool, float, str]:
    """ADX 上升动量检查。"""
    d3 = adx_data.get("adx_d3")
    if d3 is None:
        return False, 0, "no_d3"
    d3_f = float(d3)
    if d3_f < min_d3:
        return False, 0, f"adx_flat:d3={d3_f:.1f}"
    score = min(d3_f / 6.0, 1.0)
    return True, score, f"d3={d3_f:.1f}"


def di_direction(
    adx_data: Dict[str, Optional[float]],
    min_diff: float = 3.0,
) -> Tuple[Optional[str], float, str]:
    """DI 方向差。

    Returns:
        (direction, score, reason)
    """
    plus_di = adx_data.get("plus_di")
    minus_di = adx_data.get("minus_di")
    if plus_di is None or minus_di is None:
        return None, 0, "no_di"
    spread = float(plus_di) - float(minus_di)
    if abs(spread) < min_diff:
        return None, 0, f"di_flat:{spread:.1f}"
    direction = "buy" if spread > 0 else "sell"
    score = min(abs(spread) / 15.0, 1.0)
    return direction, score, f"di={spread:.1f}"


# ═══════════════════════════════════════════════════════════════
# RSI
# ═══════════════════════════════════════════════════════════════


def rsi_extreme(
    rsi: Optional[float],
    rsi_d3: Optional[float],
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> Tuple[Optional[str], float, str]:
    """RSI 极端区域检测。

    Returns:
        (direction, score, reason)
        direction: "buy"(超卖) / "sell"(超买) / None
        score: 越极端越高
    """
    if rsi is None:
        return None, 0, "no_rsi"
    if rsi <= oversold:
        if rsi_d3 is not None and rsi_d3 < 0:
            return None, 0, "still_falling"
        score = min((oversold - rsi) / 15.0 + 0.5, 1.0)
        return "buy", score, f"oversold:{rsi:.0f}"
    if rsi >= overbought:
        if rsi_d3 is not None and rsi_d3 > 0:
            return None, 0, "still_rising"
        score = min((rsi - overbought) / 15.0 + 0.5, 1.0)
        return "sell", score, f"overbought:{rsi:.0f}"
    return None, 0, f"rsi_mid:{rsi:.0f}"


def rsi_in_zone(
    rsi: Optional[float],
    rsi_d3: Optional[float],
    direction: str,
    buy_range: Tuple[float, float] = (32.0, 55.0),
    sell_range: Tuple[float, float] = (45.0, 68.0),
) -> Tuple[bool, float, str]:
    """RSI 区间检查（趋势策略用：RSI 在回调区间内）。

    Returns:
        (passed, score, reason)
        score: 越接近区间中点越高
    """
    if rsi is None:
        return False, 0, "no_rsi"
    lo, hi = buy_range if direction == "buy" else sell_range
    if not (lo <= rsi <= hi):
        return False, 0, f"rsi={rsi:.0f}∉[{lo:.0f},{hi:.0f}]"
    center = (lo + hi) / 2
    score = max(0.0, 1.0 - abs(rsi - center) / 20.0)
    return True, score, f"rsi={rsi:.0f}"


def rsi_momentum_confirmed(
    rsi_d3: Optional[float],
    direction: str,
    threshold: float = 3.0,
) -> Tuple[bool, str]:
    """RSI 动量方向确认。d3 方向需与 direction 一致。"""
    if rsi_d3 is None:
        return True, ""  # 无数据时不拦截
    if direction == "buy" and rsi_d3 < -threshold:
        return False, f"rsi_falling:d3={rsi_d3:.1f}"
    if direction == "sell" and rsi_d3 > threshold:
        return False, f"rsi_rising:d3={rsi_d3:.1f}"
    return True, ""


# ═══════════════════════════════════════════════════════════════
# Bar 形态
# ═══════════════════════════════════════════════════════════════


def bar_close_position(
    bar_stats: Dict[str, Any],
    direction: str,
    buy_max: float = 0.40,
    sell_min: float = 0.60,
) -> Tuple[bool, float, str]:
    """Bar 收盘位置检查。

    Returns:
        (passed, score, reason)
    """
    cp = bar_stats.get("close_position")
    if cp is None:
        return True, 0.3, "no_cp"
    cp_f = float(cp)
    if direction == "buy" and cp_f < buy_max:
        return False, 0, f"bearish_bar:{cp_f:.2f}"
    if direction == "sell" and cp_f > sell_min:
        return False, 0, f"bullish_bar:{cp_f:.2f}"
    # 越极端越好
    if direction == "buy":
        score = min(cp_f / 0.8, 1.0)
    else:
        score = min((1.0 - cp_f) / 0.8, 1.0)
    return True, score, f"cp={cp_f:.2f}"


def bar_body_ratio(
    bar_stats: Dict[str, Any],
    threshold: float = 1.2,
) -> Tuple[bool, float, str]:
    """Bar 实体比检查。"""
    br = bar_stats.get("body_ratio")
    if br is None:
        return True, 0.3, "no_br"
    br_f = float(br)
    score = 0.7 if br_f > threshold else 0.3
    return True, score, f"body={br_f:.2f}"


# ═══════════════════════════════════════════════════════════════
# 量能
# ═══════════════════════════════════════════════════════════════


def volume_ratio(
    volume_ratio_val: Optional[float],
    threshold: float = 1.5,
) -> float:
    """量能比率。返回 0~1 score。"""
    if volume_ratio_val is None:
        return 0.0
    return 1.0 if volume_ratio_val > threshold else 0.0


def mfi_direction(
    mfi: Optional[float],
    direction: str,
    oversold: float = 25.0,
    overbought: float = 75.0,
) -> float:
    """MFI 方向确认。返回 0~1 score。"""
    if mfi is None:
        return 0.0
    if direction == "buy" and mfi < oversold:
        return 1.0
    if direction == "sell" and mfi > overbought:
        return 1.0
    return 0.0
