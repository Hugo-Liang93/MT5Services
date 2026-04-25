"""TimescaleSignalRepository.update_admission_result() 单元测试（P9 Phase 1.5）。

验证：
- priority 按 actionability 系数自动计算（actionable=1.0, hold=0.5, blocked=0.1）
- guard_category 从 guard_reason_code 自动推导（reason_category 函数）
- signal_id 不存在 → 返回 False（不抛）
- 非法 actionability → ValueError
"""

from __future__ import annotations

import pytest

from src.signals.tracking.repository import TimescaleSignalRepository
from src.trading.execution.reasons import (
    REASON_QUOTE_STALE,
    REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
    REASON_STRATEGY_DEMO_VALIDATION,
    SKIP_CATEGORY_COST_GUARD,
    SKIP_CATEGORY_GOVERNANCE,
    SKIP_CATEGORY_MARKET_DATA,
)


class _StubSignalDB:
    """模拟 TimescaleWriter.signal_repo 接口，记录所有调用。"""

    def __init__(self, *, confidence: float | None = 0.72) -> None:
        self._confidence = confidence
        self.update_calls: list[dict] = []
        self.confidence_calls: list[str] = []

    def fetch_signal_confidence(self, *, signal_id: str) -> float | None:
        self.confidence_calls.append(signal_id)
        return self._confidence

    def update_signal_admission(self, **kwargs) -> bool:
        self.update_calls.append(kwargs)
        return True


def _make_repo(
    confidence: float | None = 0.72,
) -> tuple[TimescaleSignalRepository, _StubSignalDB]:
    db = _StubSignalDB(confidence=confidence)
    # TimescaleSignalRepository 构造时取 db_writer.signal_repo；我们直接传 stub 当作 db_writer
    repo = TimescaleSignalRepository.__new__(TimescaleSignalRepository)
    repo._db = db  # noqa: SLF001 — 测试需要直接注入 stub
    repo._storage_writer = None
    return repo, db


def test_update_admission_actionable_uses_full_confidence_as_priority() -> None:
    repo, db = _make_repo(confidence=0.8)

    ok = repo.update_admission_result(
        signal_id="sig_1",
        actionability="actionable",
        guard_reason_code=None,
    )

    assert ok is True
    assert db.update_calls == [
        {
            "signal_id": "sig_1",
            "actionability": "actionable",
            "guard_reason_code": None,
            "guard_category": None,
            "priority": pytest.approx(0.8),
            "rank_source": "native",
        }
    ]


def test_update_admission_blocked_applies_one_tenth_weight_and_resolves_category() -> (
    None
):
    repo, db = _make_repo(confidence=0.6)

    ok = repo.update_admission_result(
        signal_id="sig_blocked",
        actionability="blocked",
        guard_reason_code=REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
    )

    assert ok is True
    call = db.update_calls[0]
    assert call["actionability"] == "blocked"
    assert call["guard_reason_code"] == REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH
    assert call["guard_category"] == SKIP_CATEGORY_COST_GUARD
    assert call["priority"] == pytest.approx(0.06)
    assert call["rank_source"] == "native"


def test_update_admission_hold_applies_half_weight() -> None:
    repo, db = _make_repo(confidence=0.5)

    ok = repo.update_admission_result(
        signal_id="sig_hold",
        actionability="hold",
        guard_reason_code=REASON_STRATEGY_DEMO_VALIDATION,
    )

    assert ok is True
    call = db.update_calls[0]
    assert call["priority"] == pytest.approx(0.25)
    assert call["guard_category"] == SKIP_CATEGORY_GOVERNANCE


def test_update_admission_returns_false_when_signal_id_missing() -> None:
    repo, db = _make_repo(confidence=None)  # signal 未持久化

    ok = repo.update_admission_result(
        signal_id="not_found",
        actionability="blocked",
        guard_reason_code=REASON_QUOTE_STALE,
    )

    assert ok is False
    assert db.update_calls == []  # 不发起 UPDATE
    # P9 bug #3: storage_writer 异步 race，初始 + 2 次重试（50ms + 100ms）
    # 都返回 None，最终 fetch_confidence 调用 3 次
    assert db.confidence_calls == ["not_found"] * 3


def test_update_admission_succeeds_after_retry_when_storage_catches_up() -> None:
    """Race 修复：fetch_confidence 第一次 None，第二次返回值 → UPDATE 应成功。"""
    db = _StubSignalDB(confidence=0.7)

    # 模拟前两次返回 None，第三次返回 confidence
    confidence_returns = iter([None, 0.7])

    def _flaky_fetch(*, signal_id):
        db.confidence_calls.append(signal_id)
        try:
            return next(confidence_returns)
        except StopIteration:
            return 0.7

    db.fetch_signal_confidence = _flaky_fetch  # type: ignore[method-assign]

    repo = TimescaleSignalRepository.__new__(TimescaleSignalRepository)
    repo._db = db  # noqa: SLF001
    repo._storage_writer = None

    ok = repo.update_admission_result(
        signal_id="sig_late",
        actionability="actionable",
        guard_reason_code=None,
    )

    assert ok is True
    assert len(db.confidence_calls) >= 2
    assert db.update_calls[0]["actionability"] == "actionable"
    assert db.update_calls[0]["priority"] == pytest.approx(0.7)


def test_update_admission_quote_stale_resolves_market_data_category() -> None:
    repo, db = _make_repo(confidence=0.4)

    repo.update_admission_result(
        signal_id="sig_q",
        actionability="blocked",
        guard_reason_code=REASON_QUOTE_STALE,
    )

    assert db.update_calls[0]["guard_category"] == SKIP_CATEGORY_MARKET_DATA


def test_update_admission_rejects_unknown_actionability() -> None:
    repo, _db = _make_repo()

    with pytest.raises(ValueError, match="unsupported actionability"):
        repo.update_admission_result(
            signal_id="sig_x",
            actionability="invalid_state",
            guard_reason_code=None,
        )


def test_update_admission_passes_custom_rank_source() -> None:
    repo, db = _make_repo(confidence=0.5)

    repo.update_admission_result(
        signal_id="sig_r",
        actionability="hold",
        guard_reason_code=REASON_STRATEGY_DEMO_VALIDATION,
        rank_source="confidence_only",
    )

    assert db.update_calls[0]["rank_source"] == "confidence_only"
