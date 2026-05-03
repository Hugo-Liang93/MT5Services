from __future__ import annotations

import pytest

from src.research.state_edge.backends import BackendUnavailableError, resolve_backend


def test_cpu_backend_is_available() -> None:
    backend = resolve_backend("cpu")

    assert backend.name == "cpu"
    backend.assert_available()
    benchmark = backend.readiness_benchmark(rows=32)
    assert benchmark["backend"] == "cpu"
    assert benchmark["rows"] == 32


def test_gpu_backend_fail_fast_when_cuda_stack_is_unavailable(monkeypatch) -> None:
    from src.research.state_edge.backends import GPUBackend

    monkeypatch.setattr(
        GPUBackend,
        "diagnostics",
        lambda self: {
            "backend": "gpu",
            "available": False,
            "checks": ["PyTorch CUDA available=False"],
        },
    )
    backend = resolve_backend("gpu")

    with pytest.raises(BackendUnavailableError) as exc:
        backend.assert_available()

    assert "CUDA" in str(exc.value)
    assert "cpu" not in str(exc.value).lower()
