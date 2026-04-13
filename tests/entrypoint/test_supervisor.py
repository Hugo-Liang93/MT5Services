from __future__ import annotations

from types import SimpleNamespace

from src.entrypoint.supervisor import ManagedProcess, Supervisor


class _DummyProcess:
    pid = 1234

    def poll(self):
        return None


def test_supervisor_start_initial_checks_group_gate_before_spawning(monkeypatch) -> None:
    calls: list[str] = []
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=["live-exec-a"],
        environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_topology_group_mt5_session_gate_or_raise",
        lambda group_name: calls.append(f"group:{group_name}"),
    )
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda instance_name, restart_count=0: calls.append(f"spawn:{instance_name}") or ManagedProcess(
            instance_name=instance_name,
            process=_DummyProcess(),
            restart_count=restart_count,
        ),
    )
    monkeypatch.setattr(
        "src.entrypoint.supervisor._wait_for_ready",
        lambda instance_name, timeout_seconds: calls.append(f"ready:{instance_name}"),
    )

    supervisor._start_initial()

    assert calls == [
        "group:live",
        "spawn:live-main",
        "ready:live-main",
        "spawn:live-exec-a",
    ]


def test_supervisor_spawn_checks_instance_gate_before_launch(monkeypatch) -> None:
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=[],
        environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)
    calls: list[str] = []

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: calls.append(f"gate:{instance_name}"),
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: _DummyProcess(),
    )

    managed = supervisor._spawn("live-main")

    assert managed.instance_name == "live-main"
    assert calls == ["gate:live-main"]
