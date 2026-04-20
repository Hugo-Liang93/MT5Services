"""admission writeback listener 单元测试（P9 Phase 1.5）。"""

from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.factories.admission_writeback import (
    make_intent_published_listener,
    make_skip_listener,
    multicast,
)


class _RecordingRepo:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def update_admission_result(self, **kwargs) -> bool:
        if self._raises:
            raise RuntimeError("DB unavailable")
        self.calls.append(kwargs)
        return True


def test_skip_listener_writes_blocked_with_reason() -> None:
    repo = _RecordingRepo()
    listener = make_skip_listener(repo)

    listener("sig_1", "spread_to_stop_ratio_too_high")

    assert repo.calls == [
        {
            "signal_id": "sig_1",
            "actionability": "blocked",
            "guard_reason_code": "spread_to_stop_ratio_too_high",
        }
    ]


def test_skip_listener_skips_when_signal_id_empty() -> None:
    repo = _RecordingRepo()
    listener = make_skip_listener(repo)

    listener("", "any_reason")
    listener(None, "any_reason")  # type: ignore[arg-type]

    assert repo.calls == []


def test_skip_listener_isolates_repo_exception() -> None:
    repo = _RecordingRepo(raises=True)
    listener = make_skip_listener(repo)

    # 不应抛出
    listener("sig_2", "circuit_open")


def test_skip_listener_normalizes_blank_reason_to_none() -> None:
    repo = _RecordingRepo()
    listener = make_skip_listener(repo)

    listener("sig_3", "")

    assert repo.calls[0]["guard_reason_code"] is None


def _intent_event(signal_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        type="intent_published",
        payload={"signal_id": signal_id} if signal_id is not None else {},
    )


def test_intent_published_listener_writes_actionable() -> None:
    repo = _RecordingRepo()
    listener = make_intent_published_listener(repo)

    event = SimpleNamespace(
        type="intent_published",
        payload={"signal_id": "sig_acc", "intent_id": "i_1"},
    )
    listener(event)

    assert repo.calls == [
        {
            "signal_id": "sig_acc",
            "actionability": "actionable",
            "guard_reason_code": None,
        }
    ]


def test_intent_published_listener_ignores_other_event_types() -> None:
    """add_listener 收到的是全部事件，必须自行按 type 过滤。"""
    repo = _RecordingRepo()
    listener = make_intent_published_listener(repo)

    listener(
        SimpleNamespace(type="bar_closed", payload={"signal_id": "sig_irrelevant"})
    )
    listener(SimpleNamespace(type="execution_failed", payload={"signal_id": "sig_x"}))
    listener(SimpleNamespace(type=None, payload={"signal_id": "sig_y"}))

    assert repo.calls == []


def test_intent_published_listener_handles_missing_payload() -> None:
    repo = _RecordingRepo()
    listener = make_intent_published_listener(repo)

    listener(SimpleNamespace(type="intent_published", payload=None))
    listener(SimpleNamespace(type="intent_published", payload={}))
    listener(_intent_event(""))

    assert repo.calls == []


def test_intent_published_listener_isolates_repo_exception() -> None:
    repo = _RecordingRepo(raises=True)
    listener = make_intent_published_listener(repo)

    listener(_intent_event("sig_x"))


def test_multicast_invokes_all_callbacks_in_order() -> None:
    log: list[str] = []
    cb1 = lambda *_a, **_k: log.append("a")  # noqa: E731
    cb2 = lambda *_a, **_k: log.append("b")  # noqa: E731

    fan = multicast(cb1, cb2)
    fan("ignored", reason="x")

    assert log == ["a", "b"]


def test_multicast_isolates_individual_failure() -> None:
    log: list[str] = []

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    cb_after = lambda *_a, **_k: log.append("after")  # noqa: E731

    fan = multicast(boom, cb_after)
    fan()  # 不应抛出

    assert log == ["after"]


def test_multicast_filters_non_callable_items() -> None:
    log: list[str] = []
    cb_real = lambda *_a, **_k: log.append("ok")  # noqa: E731

    fan = multicast(None, cb_real, "not_callable")  # type: ignore[arg-type]
    fan()

    assert log == ["ok"]
