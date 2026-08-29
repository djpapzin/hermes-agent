"""Behavior-level worker cgroup isolation tests for the VM downstream."""

import json
import threading
from unittest.mock import MagicMock, patch

import psutil
import pytest


def test_worker_environment_payload_is_private_and_filters_invalid_names(
    monkeypatch, tmp_path
):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "get_hermes_home", lambda: tmp_path)
    path = pr._write_worker_environment(
        {"DISPLAY": ":99", "BAD-NAME": "drop"}, "scope"
    )

    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"DISPLAY": ":99"}


def test_kill_all_reaps_finished_launcher_scope():
    import tools.process_registry as pr

    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="finished-scoped",
        command="true",
        task_id="task",
        started_at=0,
        exited=True,
        exit_code=0,
        systemd_unit="hermes-worker-system-finished-scoped.scope",
    )
    registry._finished[session.id] = session

    with patch.object(pr, "_stop_systemd_unit", return_value=True) as stop:
        assert registry.kill_all() == 1

    stop.assert_called_once_with(session.systemd_unit)


def test_system_scope_routes_through_validating_root_wrapper(monkeypatch, tmp_path):
    import tools.process_registry as pr

    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"systemd-run": "/usr/bin/systemd-run", "sudo": "/usr/bin/sudo"}.get(name),
    )
    wrapper = tmp_path / "hermes-worker-scope"
    wrapper.touch()
    env_path = tmp_path / "worker-env.json"
    monkeypatch.setattr(pr, "_SYSTEM_SCOPE_WRAPPER", wrapper)
    monkeypatch.setattr(pr, "_write_worker_environment", lambda *_args: env_path)
    monkeypatch.setattr(pr, "_worker_memory_max_bytes", lambda: 512 * 1024 * 1024)

    argv = pr._build_systemd_scope_argv(
        ["/bin/bash", "-c", "true"], "test", backend="system"
    )

    assert argv == [
        "/usr/bin/sudo",
        "-n",
        str(wrapper),
        "run",
        "test",
        "536870912",
        str(env_path),
        "--",
        "/bin/bash",
        "-c",
        "true",
    ]


def test_system_scope_serializes_environment_outside_sudo_arguments(
    monkeypatch, tmp_path
):
    import tools.process_registry as pr

    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"systemd-run": "/usr/bin/systemd-run", "sudo": "/usr/bin/sudo"}.get(name),
    )
    wrapper = tmp_path / "hermes-worker-scope"
    wrapper.touch()
    env_path = tmp_path / "worker-env.json"
    write_env = patch.object(
        pr, "_write_worker_environment", return_value=env_path
    )
    monkeypatch.setattr(pr, "_SYSTEM_SCOPE_WRAPPER", wrapper)
    environment = {
        "DISPLAY": ":99",
        "AGENT_BROWSER_SOCKET_DIR": "/tmp/s",
        "PATH": "/safe",
    }
    with write_env as writer:
        argv = pr._build_systemd_scope_argv(
            ["/usr/bin/env"],
            "browser-test",
            backend="system",
            environment=environment,
        )

    writer.assert_called_once_with(environment, "browser-test")
    assert str(env_path) in argv
    assert "DISPLAY" not in " ".join(argv)
    assert "/tmp/s" not in " ".join(argv)


def test_failed_launcher_removes_private_environment_payload(monkeypatch, tmp_path):
    import tools.process_registry as pr

    wrapper = tmp_path / "hermes-worker-scope"
    wrapper.touch()
    payload = tmp_path / "payload.json"
    payload.write_text('{"TOKEN": "secret"}', encoding="utf-8")
    monkeypatch.setattr(pr, "_SYSTEM_SCOPE_WRAPPER", wrapper)
    argv = [
        "/usr/bin/sudo", "-n", str(wrapper), "run", "scope", "67108864",
        str(payload), "--", "/bin/true",
    ]

    class FailedProcess:
        def poll(self):
            return 1

    pr._cleanup_scope_environment_when_done(FailedProcess(), argv)
    for thread in threading.enumerate():
        if thread.name == "hermes-worker-env-cleanup":
            thread.join(timeout=1)

    assert not payload.exists()


def test_gateway_startup_removes_dead_creator_payload(monkeypatch, tmp_path):
    import tools.process_registry as pr

    root = tmp_path / "state" / "worker-env"
    root.mkdir(parents=True)
    payload = root / "999999-scope-secret.json"
    payload.write_text('{"TOKEN": "secret"}', encoding="utf-8")
    monkeypatch.setattr(pr, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: False)

    pr._cleanup_orphaned_worker_environments()

    assert not payload.exists()


def test_gateway_startup_removes_recycled_creator_payload(monkeypatch, tmp_path):
    import tools.process_registry as pr

    root = tmp_path / "state" / "worker-env"
    root.mkdir(parents=True)
    payload = root / "4321-100-scope-secret.json"
    payload.write_text('{"TOKEN": "secret"}', encoding="utf-8")
    monkeypatch.setattr(pr, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(pr, "_host_process_start_time", lambda _pid: 200)

    pr._cleanup_orphaned_worker_environments()

    assert not payload.exists()


def test_foreground_scope_tracker_persists_only_populated_scope(monkeypatch):
    import tools.process_registry as pr

    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "_systemd_unit_has_processes", lambda _unit: True)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)

    assert pr.track_finished_foreground_scope(
        "hermes-worker-system-fg.scope", 4321
    ) is True
    assert next(iter(registry._finished.values())).systemd_unit.endswith("fg.scope")

    monkeypatch.setattr(pr, "_systemd_unit_has_processes", lambda _unit: False)
    assert pr.track_finished_foreground_scope(
        "hermes-worker-system-empty.scope", 4322
    ) is False


def test_worker_scope_population_reads_cgroup_membership(monkeypatch, tmp_path):
    import tools.process_registry as pr

    relative = "hermes-workers.slice/hermes-worker-system-test.scope"
    cgroup = tmp_path / relative
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text("4321\n", encoding="utf-8")
    monkeypatch.setattr(
        pr.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": f"LoadState=loaded\nControlGroup=/{relative}\n",
            },
        )(),
    )

    assert pr._systemd_unit_has_processes(
        "hermes-worker-system-test.scope", tmp_path
    ) is True
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    assert pr._systemd_unit_has_processes(
        "hermes-worker-system-test.scope", tmp_path
    ) is False


def test_local_foreground_completion_registers_daemonized_scope():
    from tools.environments.base import BaseEnvironment
    from tools.environments.local import LocalEnvironment

    env = object.__new__(LocalEnvironment)
    proc = MagicMock(pid=4321)
    proc._hermes_systemd_unit = "hermes-worker-system-fg.scope"
    with patch.object(
        BaseEnvironment,
        "_wait_for_process",
        return_value={"output": "ok", "exit_code": 0},
    ), patch(
        "tools.process_registry.track_finished_foreground_scope"
    ) as track:
        result = env._wait_for_process(proc, timeout=10, bounded_capture=True)

    assert result["exit_code"] == 0
    track.assert_called_once_with(proc._hermes_systemd_unit, 4321)


def test_system_scope_negative_probe_retries_after_ttl(monkeypatch, tmp_path):
    import tools.process_registry as pr

    wrapper = tmp_path / "hermes-worker-scope"
    wrapper.touch()
    monkeypatch.setattr(pr, "_SYSTEM_SCOPE_WRAPPER", wrapper)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"systemd-run": "/usr/bin/systemd-run", "sudo": "/usr/bin/sudo"}.get(name),
    )
    monkeypatch.setattr(pr, "_build_systemd_scope_argv", lambda *_args, **_kwargs: ["probe"])
    now = [100.0]
    monkeypatch.setattr(pr.time, "monotonic", lambda: now[0])
    results = iter([1, 0])
    monkeypatch.setattr(
        pr.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": next(results)})(),
    )
    pr._SYSTEMD_SYSTEM_SCOPE_AVAILABLE = None
    pr._SYSTEMD_SYSTEM_SCOPE_PROBED_AT = 0.0

    assert pr._systemd_run_system_scope_available() is False
    assert pr._systemd_run_system_scope_available() is False
    now[0] += pr._SYSTEMD_SCOPE_FAILURE_TTL_SECONDS + 1
    assert pr._systemd_run_system_scope_available() is True


def test_required_mode_prefers_aggregate_system_scope(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_worker_cgroup_mode", lambda: "required")
    monkeypatch.setattr(pr, "_systemd_run_system_scope_available", lambda: True)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)

    assert pr._worker_scope_backend() == "system"


def test_required_mode_refuses_worker_in_gateway_cgroup(monkeypatch, tmp_path):
    import tools.process_registry as pr

    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_worker_cgroup_mode", lambda: "required")
    monkeypatch.setattr(pr, "_worker_scope_backend", lambda: None)

    with patch("subprocess.Popen") as spawn:
        with pytest.raises(RuntimeError, match="cgroup isolation is required"):
            registry.spawn_local("echo unsafe", cwd=str(tmp_path))
    spawn.assert_not_called()


def test_explicit_system_mode_refuses_unbounded_fallback(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_worker_cgroup_mode", lambda: "system")
    monkeypatch.setattr(pr, "_worker_scope_backend", lambda: None)

    with pytest.raises(RuntimeError, match="cgroup isolation is required"):
        pr.build_gateway_worker_scope_argv(["/bin/true"], unit_suffix="test")


def test_foreground_system_scope_receives_sanitized_run_environment(
    monkeypatch, tmp_path
):
    import tools.environments.local as local
    import tools.process_registry as pr

    env = object.__new__(local.LocalEnvironment)
    env.cwd = str(tmp_path)
    env.env = {"HERMES_HOME": "/profiles/friend", "CUSTOM_SETTING": "yes"}
    monkeypatch.setattr(local, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_worker_cgroup_mode", lambda: "system")
    monkeypatch.setattr(pr, "_worker_scope_backend", lambda: "system")
    monkeypatch.setattr(local.os, "getpgid", lambda _pid: 123)

    fake_proc = type("Proc", (), {"pid": 123})()
    with patch.object(pr, "_build_systemd_scope_argv", return_value=["scoped"]) as build, patch.object(
        local.subprocess, "Popen", return_value=fake_proc
    ):
        env._run_bash("true")

    scoped_env = build.call_args.kwargs["environment"]
    assert scoped_env["HERMES_HOME"] == "/profiles/friend"
    assert scoped_env["CUSTOM_SETTING"] == "yes"
