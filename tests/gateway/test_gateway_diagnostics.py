from pathlib import Path

from gateway.shutdown_forensics import snapshot_shutdown_context
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
        (200, 2000, "system.slice/hermes-worker-browser.scope"),
        (201, 3000, "system.slice/hermes-worker-browser.scope"),
    ):
        root = proc / str(pid)
        root.mkdir()
        (root / "status").write_text(f"VmRSS: {rss} kB\n", encoding="utf-8")
        (root / "cgroup").write_text(f"0::/{relative}\n", encoding="utf-8")

    for relative, current, events in (
        ("system.slice/hermes.service", "100", "oom 0\noom_kill 0\n"),
        (
            "system.slice/hermes-worker-browser.scope",
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

    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_pid", lambda _unit: 100
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
    assert len(result["workers"]) == 2


def test_diagnostic_reads_bounded_admission_runtime_status(tmp_path, monkeypatch):
    (tmp_path / "gateway_state.json").write_text(
        '{"gateway_state":"running","active_agents":2,"admission":'
        '{"active_workers":2,"queued_tasks":1,"queued_task_ids":["task-3"]},'
        '"updated_at":"now","secret":"must-not-copy"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_pid", lambda _unit: None
    )

    result = collect(hermes_home=tmp_path)

    assert result["runtime_status"]["active_agents"] == 2
    assert result["runtime_status"]["admission"]["queued_tasks"] == 1
    assert "secret" not in result["runtime_status"]
