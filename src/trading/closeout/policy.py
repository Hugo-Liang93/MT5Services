from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CloseoutRuntimeModeAction(str, Enum):
    DISABLED = "disabled"
    RISK_OFF = "risk_off"
    INGEST_ONLY = "ingest_only"


@dataclass(frozen=True)
class ExposureCloseoutPolicy:
    """风险收口后的附加动作策略。

    当前只处理人工/API 触发的 closeout 完成后，是否自动切换运行模式。
    """

    after_manual_closeout_action: CloseoutRuntimeModeAction = (
        CloseoutRuntimeModeAction.DISABLED
    )

    def resolve_runtime_mode_target(
        self,
        *,
        reason: str,
        completed: bool,
    ) -> str | None:
        if not completed:
            return None
        if not self._is_manual_reason(reason):
            return None
        if self.after_manual_closeout_action == CloseoutRuntimeModeAction.DISABLED:
            return None
        return self.after_manual_closeout_action.value

    @staticmethod
    def _is_manual_reason(reason: str) -> bool:
        normalized = str(reason or "").strip().lower()
        if not normalized:
            return False
        return normalized.startswith("manual")

    def snapshot(self) -> dict[str, Any]:
        return {
            "after_manual_closeout_action": self.after_manual_closeout_action.value,
        }
