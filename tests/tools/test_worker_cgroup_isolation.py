"""Behavior-level worker cgroup isolation tests for the VM downstream."""

from unittest.mock import patch

import pytest


def test_system_scope_is_unprivileged_and_memory_bounded(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"systemd-run": "/usr/bin/systemd-run", "sudo": "/usr/bin/sudo"}.get(name),
    )
    monkeypatch.setattr(pr.os, "getuid", lambda: 996)
    monkeypatch.setattr(pr.os, "getgid", lambda: 997)
    monkeypatch.setattr(pr, "_worker_memory_max_bytes", lambda: 512 * 1024 * 1024)

    argv = pr._build_systemd_scope_argv(
        ["/bin/bash", "-c", "true"], "test", backend="system"
    )

    assert argv[:4] == ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run", "--system"]
    assert argv[argv.index("--uid") + 1] == "996"
    assert argv[argv.index("--gid") + 1] == "997"
    assert "MemoryMax=536870912" in argv
    assert "MemoryHigh=483183820" in argv
    assert "MemorySwapMax=0" in argv


def test_system_scope_preserves_only_safe_environment_names(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"systemd-run": "/usr/bin/systemd-run", "sudo": "/usr/bin/sudo"}.get(name),
    )
    argv = pr._build_systemd_scope_argv(
        ["/usr/bin/env"], "browser-test", backend="system",
        environment={"DISPLAY": ":99", "AGENT_BROWSER_SOCKET_DIR": "/tmp/s", "PATH": "/bad"},
    )
    preserve = next(value for value in argv if value.startswith("--preserve-env="))
    assert "DISPLAY" in preserve
    assert "AGENT_BROWSER_SOCKET_DIR" in preserve
    assert "PATH" not in preserve
    assert "/tmp/s" not in " ".join(argv)


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
