"""Tests for query service state access adapter."""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from types import SimpleNamespace

import pytest

from src.indicators.query_services.state_view import state_get, state_set


def test_state_access_prefers_manager_state_container():
    manager = SimpleNamespace(
        state=SimpleNamespace(
            results=OrderedDict(),
            results_lock=RLock(),
            results_max=8,
        )
    )

    assert state_get(manager, "results") == {}
    state_set(manager, "results_max", 16)

    assert manager.state.results_max == 16


def test_state_access_fails_without_state_container():
    manager = SimpleNamespace()

    with pytest.raises(AttributeError):
        state_get(manager, "results")

    with pytest.raises(AttributeError):
        state_set(manager, "results_max", 11)
