# Entry Meta Session Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure Entry Meta dynamic scoring receives a deterministic backtest session context even when no strategy session filter is configured.

**Architecture:** Keep strategy session filtering unchanged, and add a separate Entry Meta session context resolver in `BacktestEngine`. The resolver reuses the public `SessionFilter.current_sessions()` contract only to derive a feature context value, never to block trades.

**Tech Stack:** Python 3.12, pytest, existing `src.signals.execution.filters.SessionFilter`, existing backtest runner and Entry Meta overlay.

---

## File Structure

- Modify `src/backtesting/engine/runner.py`
  - Add `_build_entry_meta_session_context_filter()` as a focused factory for context-only session resolution.
  - Add `_resolve_entry_meta_session()` as the only helper that converts a bar time into the session string passed to Entry Meta.
  - Initialize `self._entry_meta_session_context_filter` independently from `self._session_filter`.
  - Use the new resolver in the decision loop.
- Create `tests/backtesting/test_entry_meta_session_context.py`
  - Directly tests the resolver contract without constructing a full `BacktestEngine`.
  - Verifies no-strategy-session context can still resolve `london`.
  - Verifies empty/invalid resolver outputs downgrade to `"unknown"`.
- Modify `docs/codebase-review.md`
  - Record the boundary cleanup and update the known limitation from “session may be unknown because SessionFilter unavailable” to “session unknown only on resolver failure; fill-time scoring still pending.”

---

### Task 1: Add Failing Tests For Entry Meta Session Context

**Files:**
- Create: `tests/backtesting/test_entry_meta_session_context.py`
- Modify: none
- Test: `tests/backtesting/test_entry_meta_session_context.py`

- [ ] **Step 1: Write the failing resolver tests**

Create `tests/backtesting/test_entry_meta_session_context.py` with:

```python
from __future__ import annotations

from datetime import datetime, timezone

from src.backtesting.engine.runner import (
    _build_entry_meta_session_context_filter,
    _resolve_entry_meta_session,
)


def test_entry_meta_session_context_resolves_without_strategy_session_filter() -> None:
    session_context = _build_entry_meta_session_context_filter()

    session = _resolve_entry_meta_session(
        session_context,
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    )

    assert session == "london"


def test_entry_meta_session_context_downgrades_empty_sessions_to_unknown() -> None:
    class EmptySessionContext:
        def current_sessions(self, at_time: datetime) -> list[str]:
            return []

    session = _resolve_entry_meta_session(
        EmptySessionContext(),
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    )

    assert session == "unknown"


def test_entry_meta_session_context_downgrades_invalid_time_to_unknown() -> None:
    session_context = _build_entry_meta_session_context_filter()

    session = _resolve_entry_meta_session(session_context, "invalid-time")

    assert session == "unknown"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_session_context.py -q
```

Expected: FAIL because `_build_entry_meta_session_context_filter` and `_resolve_entry_meta_session` are not defined yet.

- [ ] **Step 3: Commit the failing tests**

Run:

```powershell
git add -- tests/backtesting/test_entry_meta_session_context.py
git commit -m "test: cover entry meta session context resolution"
```

Expected: commit succeeds and contains only the new test file.

---

### Task 2: Implement The Backtest Session Context Resolver

**Files:**
- Modify: `src/backtesting/engine/runner.py`
- Test: `tests/backtesting/test_entry_meta_session_context.py`

- [ ] **Step 1: Add the helper functions near `_build_equity_curve_filter_config()`**

In `src/backtesting/engine/runner.py`, after `_build_equity_curve_filter_config()`, add:

```python
def _build_entry_meta_session_context_filter() -> Any:
    """Create a context-only session resolver for Entry Meta features."""
    from src.signals.execution.filters import SessionFilter

    return SessionFilter(allowed_sessions=())


def _resolve_entry_meta_session(session_context: Any, bar_time: Any) -> str:
    """Resolve the Entry Meta feature session without applying trade filters."""
    if session_context is None:
        return "unknown"
    try:
        sessions = session_context.current_sessions(bar_time)
    except (AttributeError, TypeError, ValueError):
        logger.debug("Entry Meta session context unavailable", exc_info=True)
        return "unknown"
    if not sessions:
        return "unknown"
    return str(sessions[0] or "unknown")
```

Rationale:

- `Any` is already imported in this file.
- The explicit exception list classifies resolver failures as context downgrade only.
- The helper does not inspect private fields and does not apply an allowlist.

- [ ] **Step 2: Initialize the context resolver independently in `BacktestEngine.__init__`**

Find the block that currently initializes `_session_filter`:

```python
# 用于在 run() 和 _evaluate_strategies() 中查询 bar 的 current_sessions
self._session_filter: Optional[Any] = None
if self._strategy_sessions:
    from src.signals.execution.filters import SessionFilter

    self._session_filter = SessionFilter(
        allowed_sessions=("london", "new_york", "asia")
    )
```

Replace it with:

```python
# Entry Meta 的 session 是特征上下文，不是策略过滤条件。
self._entry_meta_session_context_filter = _build_entry_meta_session_context_filter()

# 用于在 run() 和 _evaluate_strategies() 中查询 bar 的 current_sessions
self._session_filter: Optional[Any] = None
if self._strategy_sessions:
    from src.signals.execution.filters import SessionFilter

    self._session_filter = SessionFilter(
        allowed_sessions=("london", "new_york", "asia")
    )
```

This preserves the existing strategy filter behavior while giving Entry Meta a dedicated context source.

- [ ] **Step 3: Use the resolver in the decision loop**

Find the decision loop block:

```python
entry_session = "unknown"
if self._session_filter is not None:
    sessions = self._session_filter.current_sessions(bar.time)
    if sessions:
        entry_session = str(sessions[0])
_process_decision_helper(
    self,
    decision,
    bar,
    bar_index,
    indicators,
    regime,
    entry_session=entry_session,
)
```

Replace it with:

```python
entry_session = _resolve_entry_meta_session(
    self._entry_meta_session_context_filter,
    bar.time,
)
_process_decision_helper(
    self,
    decision,
    bar,
    bar_index,
    indicators,
    regime,
    entry_session=entry_session,
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_session_context.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Run Entry Meta focused regression**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_overlay.py tests\research\entry_meta -q
```

Expected: all selected Entry Meta tests pass.

- [ ] **Step 6: Commit the implementation**

Run:

```powershell
git add -- src/backtesting/engine/runner.py
git commit -m "fix: resolve entry meta sessions in backtests"
```

Expected: commit succeeds and contains only `src/backtesting/engine/runner.py`.

---

### Task 3: Update Review Notes And Run Final Verification

**Files:**
- Modify: `docs/codebase-review.md`
- Test: `tests/backtesting/test_entry_meta_session_context.py`, `tests/backtesting/test_entry_meta_overlay.py`, `tests/research/entry_meta`, `tests/backtesting/test_state_edge_overlay.py`, `tests/research/state_edge`

- [ ] **Step 1: Update the Entry Meta note in `docs/codebase-review.md`**

In section `2026-05-03 Entry Meta-Label Overlay Lab`, replace the existing limitation bullet:

```markdown
- 已知限制：session 无法从当前 SessionFilter 判定时会写为 `unknown`；pending-entry 当前仍在 signal 产生时用 signal bar/time/close 构建 `feature_context` 并打分，不会等到后续 fill time/price 重新打分。
```

With:

```markdown
- 2026-05-03：Entry Meta backtest session context 已与策略 session 过滤职责拆分；即使未配置 `strategy_sessions`，也会从 `bar.time` 推导 `asia/london/new_york/off_hours`，避免动态 scorer 因默认 `unknown` 降低覆盖率。`unknown` 仅保留为 resolver 失败兜底并继续进入 overlay coverage report。
- 已知限制：pending-entry 当前仍在 signal 产生时用 signal bar/time/close 构建 `feature_context` 并打分，不会等到后续 fill time/price 重新打分。该问题属于入场评价时点变更，应另行设计。
```

- [ ] **Step 2: Run focused verification**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_session_context.py tests\backtesting\test_entry_meta_overlay.py tests\research\entry_meta -q
```

Expected: all selected Entry Meta tests pass.

- [ ] **Step 3: Run State Edge regression if existing State Edge tests are runnable**

Run:

```powershell
python -m pytest tests\backtesting\test_state_edge_overlay.py tests\research\state_edge -q
```

Expected: State Edge tests pass. If these paths fail because the pre-existing State Edge worktree files are incomplete, record the failure output and do not modify State Edge files in this task.

- [ ] **Step 4: Run CLI help smoke**

Run:

```powershell
python -m src.ops.cli.backtest_runner --help
```

Expected: command exits 0 and still includes `--entry-meta-artifact`, `--entry-meta-mode`, and `--entry-meta-threshold-grid`.

- [ ] **Step 5: Run whitespace check for touched files**

Run:

```powershell
git diff --check -- src/backtesting/engine/runner.py tests/backtesting/test_entry_meta_session_context.py docs/codebase-review.md
```

Expected: no output.

- [ ] **Step 6: Commit docs**

Run:

```powershell
git add -- docs/codebase-review.md
git commit -m "docs: record entry meta session context boundary"
```

Expected: commit succeeds and contains only `docs/codebase-review.md`.

---

## Self-Review

- Spec coverage: Task 1 covers the new contract; Task 2 implements independent session context resolution and keeps strategy filtering unchanged; Task 3 updates the review ledger and runs Entry Meta/State Edge/CLI verification.
- Red-flag scan: no incomplete tasks remain; every code and verification step includes exact commands and expected outcomes.
- Type consistency: helper names are `_build_entry_meta_session_context_filter` and `_resolve_entry_meta_session` in all tasks; tests import those exact names.
