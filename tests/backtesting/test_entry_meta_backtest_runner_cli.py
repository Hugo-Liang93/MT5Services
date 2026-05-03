from __future__ import annotations

import subprocess
import sys


def test_backtest_runner_help_exposes_entry_meta_options() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.backtest_runner", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--entry-meta-artifact" in completed.stdout
    assert "--entry-meta-mode" in completed.stdout
    assert "--entry-meta-threshold-grid" in completed.stdout
