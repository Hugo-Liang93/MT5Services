from __future__ import annotations

import subprocess
import sys


def test_entry_meta_lab_help_exposes_required_options() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.entry_meta_lab", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--baseline" in completed.stdout
    assert "--tf" in completed.stdout
    assert "--backend" in completed.stdout
    assert "--json-output" in completed.stdout
