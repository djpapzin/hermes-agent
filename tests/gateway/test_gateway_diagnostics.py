from pathlib import Path
import subprocess

from gateway.shutdown_forensics import snapshot_shutdown_context
from scripts import hermes_gateway_diagnostics as diagnostics
from scripts.hermes_gateway_diagnostics import collect


def test_shutdown_snapshot_contains_resource_and_task_evidence():
    snapshot = snapshot_shutdown_context(
        shutdown_reason="test",
        active_task_ids=["a", "b"],
        queued_task_ids=["c"],
        worker_pids=[123],
    )

    assert snapshot["shutdown_reason"] == "test"
    assert snapshot["active_agent_count"] == 2
    assert snapshot["queued_task_count"] == 1
    assert snapshot["worker_pids"] == [123]
    assert "gateway_rss" in snapshot
    assert "host_mem_available_kb" in snapshot
    assert "cgroup" in snapshot


def test_diagnostic_groups_worker_cgroup_memory_and_oom_events(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    cgroups = tmp_path / "cgroup"
    proc.mkdir()
    (proc / "meminfo").write_text("MemAvailable: 4096 kB\n", encoding="utf-8")
    for pid, rss, relative in (
        (100, 1000, "system.slice/hermes.service"),
        (200, 2000, "hermes-workers.slice/hermes-worker-browser.scope"),
        (201, 3000, "hermes-workers.slice/hermes-worker-browser.scope"),
    ):
        root = proc / str(pid)
        root.mkdir()
        (root / "status").write_text(f"VmRSS: {rss} kB\n", encoding="utf-8")
        (root / "cgroup").write_text(f"0::/{relative}\n", encoding="utf-8")

    for relative, current, events in (
        ("system.slice/hermes.service", "100", "oom 0\noom_kill 0\n"),
        (
            "hermes-workers.slice/hermes-worker-browser.scope",
            "500",
            "oom 1\noom_kill 1\n",
        ),
    ):
        root = cgroups / relative
        root.mkdir(parents=True)
        (root / "memory.current").write_text(current, encoding="utf-8")
        (root / "memory.high").write_text("1000", encoding="utf-8")
        (root / "memory.max").write_text("2000", encoding="utf-8")
        (root / "memory.events").write_text(events, encoding="utf-8")
    worker_slice = cgroups / "hermes-workers.slice"
    (worker_slice / "memory.current").write_text("500", encoding="utf-8")
    (worker_slice / "memory.high").write_text("800", encoding="utf-8")
    (worker_slice / "memory.max").write_text("1000", encoding="utf-8")
    (worker_slice / "memory.events").write_text(
        "high 1\noom 1\noom_kill 0\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_identity",
        lambda _unit, _manager: (100, "system"),
    )
    result = collect(
        hermes_home=tmp_path,
        proc_root=proc,
        cgroup_root=cgroups,
    )

    assert result["worker_count"] == 1
    assert result["worker_process_count"] == 2
    assert result["worker_cgroups"][0]["rss_kb"] == 5000
    assert result["worker_cgroups"][0]["memory_events"]["oom_kill"] == 1
    assert result["worker_slices"][0]["memory_max"] == "1000"
    assert result["worker_slices"][0]["memory_events"]["high"] == 1
    assert len(result["workers"]) == 2


def test_diagnostic_reads_bounded_admission_runtime_status(tmp_path, monkeypatch):
    (tmp_path / "gateway_state.json").write_text(
        '{"gateway_state":"running","active_agents":2,"admission":'
        '{"active_workers":2,"queued_tasks":1,"queued_task_ids":["task-3"]},'
        '"updated_at":"now","secret":"must-not-copy"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_identity",
        lambda _unit, _manager: (None, None),
    )

    result = collect(hermes_home=tmp_path)

    assert result["runtime_status"]["active_agents"] == 2
    assert result["runtime_status"]["admission"]["queued_tasks"] == 1
    assert "secret" not in result["runtime_status"]


def test_diagnostic_scans_orphan_worker_when_gateway_is_absent(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    cgroups = tmp_path / "cgroup"
    worker = "system.slice/hermes-worker-orphan.scope"
    proc.mkdir()
    (proc / "meminfo").write_text("MemAvailable: 4096 kB\n", encoding="utf-8")
    worker_proc = proc / "222"
    worker_proc.mkdir()
    (worker_proc / "status").write_text("VmRSS: 2048 kB\n", encoding="utf-8")
    (worker_proc / "cgroup").write_text(f"0::/{worker}\n", encoding="utf-8")
    worker_cgroup = cgroups / worker
    worker_cgroup.mkdir(parents=True)
    (worker_cgroup / "memory.current").write_text("100", encoding="utf-8")
    (worker_cgroup / "memory.high").write_text("200", encoding="utf-8")
    (worker_cgroup / "memory.max").write_text("300", encoding="utf-8")
    (worker_cgroup / "memory.events").write_text("oom 0\noom_kill 0\n", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_identity",
        lambda _unit, _manager: (None, None),
    )

    result = collect(
        hermes_home=tmp_path,
        proc_root=proc,
        cgroup_root=cgroups,
    )

    assert result["gateway_pid"] is None
    assert result["worker_count"] == 1
    assert result["workers"][0]["pid"] == 222


def test_diagnostic_defaults_to_active_profile_home(tmp_path, monkeypatch, capsys):
    seen = []
    journal_managers = []
    monkeypatch.setattr(diagnostics, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        diagnostics,
        "collect",
        lambda _unit, home, manager="auto": seen.append((home, manager))
        or {"service_manager": "user"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_bounded_incident_journal",
        lambda _unit, _since, manager: journal_managers.append(manager) or [],
    )
    monkeypatch.setattr("sys.argv", ["hermes_gateway_diagnostics.py"])

    assert diagnostics.main() == 0
    assert seen == [(tmp_path, "auto")]
    assert journal_managers == ["user"]
    capsys.readouterr()


def test_service_pid_auto_detects_user_manager(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "--user" in argv:
            return subprocess.CompletedProcess(argv, 0, "4321\n", "")
        return subprocess.CompletedProcess(argv, 0, "0\n", "")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)

    assert diagnostics._service_pid("hermes-gateway.service") == 4321
    assert calls[0][0] == "systemctl" and "--user" not in calls[0]
    assert "--user" in calls[1]


def test_service_exit_status_records_systemd_result_code_and_signal(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(
                [
                    "LoadState=loaded",
                    "ActiveState=failed",
                    "SubState=failed",
                    "Result=signal",
                    "ExecMainCode=2",
                    "ExecMainStatus=9",
                    "NRestarts=1",
                ]
            ),
            "",
        )

    monkeypatch.setattr(diagnostics.subprocess, "run", run)

    assert diagnostics._service_exit_status(
        "hermes-gateway.service", "user"
    ) == {
        "load_state": "loaded",
        "active_state": "failed",
        "sub_state": "failed",
        "result": "signal",
        "exec_main_code": 2,
        "exec_main_status": 9,
        "n_restarts": 1,
    }
    assert calls[0][:2] == ["systemctl", "--user"]


def test_auto_manager_journal_uses_only_selected_manager(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "_service_identity",
        lambda _unit, _manager: (4321, "user"),
    )
    result = diagnostics.collect(hermes_home=Path("/nonexistent"))
    assert result["service_manager"] == "user"
