from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass
from typing import Protocol


class BackendUnavailableError(RuntimeError):
    """计算后端不可用。"""


class ComputeBackend(Protocol):
    name: str

    def assert_available(self) -> None: ...

    def diagnostics(self) -> dict[str, object]: ...

    def readiness_benchmark(self, *, rows: int = 100_000) -> dict[str, object]: ...


@dataclass(frozen=True)
class CPUBackend:
    name: str = "cpu"

    def assert_available(self) -> None:
        return None

    def diagnostics(self) -> dict[str, object]:
        return {"backend": self.name, "available": True}

    def readiness_benchmark(self, *, rows: int = 100_000) -> dict[str, object]:
        import numpy as np

        n_rows = max(16, min(int(rows), 100_000))
        x = np.ones((n_rows, 16), dtype=np.float32)
        w = np.ones((16, 3), dtype=np.float32)
        started = time.perf_counter()
        _ = x @ w
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "backend": self.name,
            "available": True,
            "rows": n_rows,
            "elapsed_ms": elapsed_ms,
        }


@dataclass(frozen=True)
class GPUBackend:
    name: str = "gpu"

    def assert_available(self) -> None:
        diagnostics = self.diagnostics()
        if diagnostics["available"]:
            return None
        detail = "; ".join(str(item) for item in diagnostics["checks"])
        raise BackendUnavailableError(f"CUDA backend unavailable: {detail}")

    def diagnostics(self) -> dict[str, object]:
        checks: list[str] = []
        available = False

        torch_spec = importlib.util.find_spec("torch")
        if torch_spec is None:
            checks.append("PyTorch not installed")
        else:
            try:
                import torch

                torch_ok = bool(torch.cuda.is_available())
                checks.append(f"PyTorch CUDA available={torch_ok}")
                available = torch_ok
            except Exception as exc:  # pragma: no cover - environment dependent
                checks.append(f"PyTorch check failed: {exc}")

        cupy_spec = importlib.util.find_spec("cupy")
        if cupy_spec is None:
            checks.append("CuPy not installed")
        else:
            try:
                import cupy

                device_count = int(cupy.cuda.runtime.getDeviceCount())
                checks.append(f"CuPy device_count={device_count}")
            except Exception as exc:  # pragma: no cover - environment dependent
                checks.append(f"CuPy check failed: {exc}")

        numba_spec = importlib.util.find_spec("numba")
        if numba_spec is None:
            checks.append("Numba not installed")
        else:
            try:
                from numba import cuda

                numba_ok = bool(cuda.is_available())
                checks.append(f"Numba CUDA available={numba_ok}")
            except Exception as exc:  # pragma: no cover - environment dependent
                checks.append(f"Numba CUDA check failed: {exc}")

        return {"backend": self.name, "available": available, "checks": checks}

    def readiness_benchmark(self, *, rows: int = 100_000) -> dict[str, object]:
        self.assert_available()
        import torch

        n_rows = max(16, min(int(rows), 1_000_000))
        device = torch.device("cuda")
        x = torch.ones((n_rows, 16), dtype=torch.float32, device=device)
        w = torch.ones((16, 3), dtype=torch.float32, device=device)
        torch.cuda.synchronize()
        started = time.perf_counter()
        _ = x @ w
        torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "backend": self.name,
            "available": True,
            "device": torch.cuda.get_device_name(device),
            "rows": n_rows,
            "elapsed_ms": elapsed_ms,
        }


def resolve_backend(name: str) -> ComputeBackend:
    normalized = name.strip().lower()
    if normalized == "cpu":
        return CPUBackend()
    if normalized == "gpu":
        return GPUBackend()
    raise ValueError(f"Unknown compute backend: {name}")
