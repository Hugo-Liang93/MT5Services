"""CMEVolumeFeatureProvider — daily CME GC futures volume features.

Joins each intraday bar to its trading-day's CME GC volume via
``bar.time.date()``. Emits volume-relative features that capture
institutional gold market activity (proxy for "real" XAUUSD volume that
broker tick_volume cannot observe).

Daily resolution intentional: CME GC volume only publishes daily totals
publicly. Intraday bars within the same day all share the same daily
volume value (this is fine — we want regime context, not high-freq).

All features emit None when:
  - daily_volume_lookup returns None for the bar's date (pre-data window
    or weekend with no published volume)
  - rolling-window features lack enough history (first 5 / 20 / 60 bars)
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.research.features.protocol import FeatureRole, ProviderDataRequirement

_FEATURE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("cme_volume", "cme_volume_zscore_20d"),
    ("cme_volume", "cme_volume_change_5d"),
    ("cme_volume", "cme_volume_ratio"),
    ("cme_volume", "cme_volume_pct_rank"),
)


class CMEVolumeFeatureProvider:
    """FeatureProvider Protocol implementation; see _FEATURE_KEYS for outputs."""

    def __init__(
        self,
        daily_volume_lookup: Callable[[date], Optional[float]],
    ) -> None:
        """daily_volume_lookup: callable that returns daily CME volume for a
        given date, or None when no data is available. Production callers
        wire a DB-backed lookup; tests inject in-memory lambdas."""
        self._lookup = daily_volume_lookup

    @property
    def name(self) -> str:
        return "cme_volume"

    @property
    def feature_count(self) -> int:
        return len(_FEATURE_KEYS)

    def required_columns(self) -> List[Tuple[str, str]]:
        return []  # No DataMatrix dependencies; we read external data via lookup.

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        return {
            "cme_volume_zscore_20d": FeatureRole.WHEN,
            "cme_volume_change_5d": FeatureRole.WHY,
            "cme_volume_ratio": FeatureRole.WHY,
            "cme_volume_pct_rank": FeatureRole.WHEN,
        }

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        n = matrix.n_bars
        out: Dict[Tuple[str, str], List[Optional[float]]] = {
            key: [None] * n for key in _FEATURE_KEYS
        }

        # Resolve daily volume for each bar's date (cache to avoid N*1d lookups)
        date_volume: Dict[date, Optional[float]] = {}
        per_bar_volumes: List[Optional[float]] = []
        for bt in matrix.bar_times:
            d = bt.date()
            if d not in date_volume:
                date_volume[d] = self._lookup(d)
            per_bar_volumes.append(date_volume[d])

        # Roll daily-distinct values (not bar-by-bar; intraday bars share the day's value)
        ordered_dates: List[date] = []
        ordered_vols: List[Optional[float]] = []
        seen: set[date] = set()
        for bt in matrix.bar_times:
            d = bt.date()
            if d in seen:
                continue
            seen.add(d)
            ordered_dates.append(d)
            ordered_vols.append(date_volume[d])

        # Compute daily-resolution features then broadcast back to bars
        zscore_by_date: Dict[date, Optional[float]] = {}
        change_by_date: Dict[date, Optional[float]] = {}
        ratio_by_date: Dict[date, Optional[float]] = {}
        pctrank_by_date: Dict[date, Optional[float]] = {}

        for i, d in enumerate(ordered_dates):
            v = ordered_vols[i]
            if v is None:
                zscore_by_date[d] = None
                change_by_date[d] = None
                ratio_by_date[d] = None
                pctrank_by_date[d] = None
                continue

            # zscore over last 20 days (excluding today)
            history_20 = [
                vol for vol in ordered_vols[max(0, i - 20) : i] if vol is not None
            ]
            if len(history_20) >= 20:
                mean_20 = sum(history_20) / len(history_20)
                std_20 = statistics.pstdev(history_20)
                zscore_by_date[d] = (v - mean_20) / std_20 if std_20 > 0 else 0.0
            else:
                zscore_by_date[d] = None

            # 5-day change (today / 5d-ago - 1)
            if i >= 5 and ordered_vols[i - 5] is not None and ordered_vols[i - 5] > 0:
                change_by_date[d] = v / ordered_vols[i - 5] - 1.0
            else:
                change_by_date[d] = None

            # ratio today/yesterday
            if i >= 1 and ordered_vols[i - 1] is not None and ordered_vols[i - 1] > 0:
                ratio_by_date[d] = v / ordered_vols[i - 1]
            else:
                ratio_by_date[d] = None

            # percentile rank in trailing 60 days
            history_60 = [
                vol for vol in ordered_vols[max(0, i - 60) : i] if vol is not None
            ]
            if len(history_60) >= 30:
                rank = sum(1 for x in history_60 if x <= v) / len(history_60)
                pctrank_by_date[d] = rank
            else:
                pctrank_by_date[d] = None

        # Broadcast daily values to per-bar arrays
        for bar_i, bt in enumerate(matrix.bar_times):
            d = bt.date()
            out[("cme_volume", "cme_volume_zscore_20d")][bar_i] = zscore_by_date.get(d)
            out[("cme_volume", "cme_volume_change_5d")][bar_i] = change_by_date.get(d)
            out[("cme_volume", "cme_volume_ratio")][bar_i] = ratio_by_date.get(d)
            out[("cme_volume", "cme_volume_pct_rank")][bar_i] = pctrank_by_date.get(d)

        return out
