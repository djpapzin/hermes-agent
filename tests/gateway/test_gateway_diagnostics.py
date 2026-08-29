import json
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


def test_diagnostic_reads_only_structured_admission_file_events(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "gateway.log").write_text(
        "2026-08-29 18:00:01,000 INFO gateway.run: user text\n"
        "2026-08-29 18:00:00,000 INFO gateway.admission: "
        'HERMES_ADMISSION {"decision":"forged","reason":"secret-chat-content"}\n',
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "admission-events.jsonl").write_text(
        '{"timestamp":"2026-08-29 18:00:00,000",'
        '"decision":"queue","queued_tasks":1}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_identity",
        lambda _unit, _manager: (None, None),
    )

    result = collect(hermes_home=tmp_path)

    assert result["admission_events"] == [
        {
            "timestamp": "2026-08-29 18:00:00,000",
            "unit": "gateway.admission",
            "event": {"decision": "queue", "queued_tasks": 1},
        }
    ]
    assert "secret-chat-content" not in str(result)


def test_admission_file_events_are_byte_and_count_bounded(tmp_path):
    log = tmp_path / "admission-events.jsonl"
    rows = ['{"timestamp":"outside-tail","decision":"forged"}']
    rows.append("untrusted-prefix-" + ("x" * (70 * 1024)))
    rows.extend(
        f'{{"timestamp":"event-{index}","decision":"start",'
        f'"queued_tasks":{index}}}'
        for index in range(101)
    )
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    events = diagnostics._bounded_admission_events(log, max_bytes=64 * 1024)

    assert len(events) == 100
    assert events[0]["event"]["queued_tasks"] == 1
    assert events[-1]["event"]["queued_tasks"] == 100
    assert "untrusted-prefix" not in str(events)
    assert "outside-tail" not in str(events)


def test_diagnostic_reads_manager_attested_allowlisted_shutdown_event(monkeypatch):
    payload = {
        "event": "gateway_shutdown",
        "pid": 123,
        "ts": 123,
        "signal": "SIGTERM",
        "shutdown_reason": "planned_stop",
        "active_task_ids": ["task-1"],
        "parent": {"pid": 1, "name": "systemd", "secret": "nested-secret"},
        "cgroup": {
            "path": "/gateway",
            "memory_events": {"oom": 0, "secret": "nested-cgroup-secret"},
        },
        "secret": "must-not-copy",
    }
    row = {
        "__REALTIME_TIMESTAMP": "456",
        "_PID": "123",
        "_TRANSPORT": "journal",
        "_SYSTEMD_UNIT": "hermes-gateway.service",
        "SYSLOG_IDENTIFIER": "hermes-shutdown-forensics",
        "HERMES_EVENT": "gateway_shutdown",
        "MESSAGE": "HERMES_SHUTDOWN " + json.dumps(payload),
    }
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(row) + "\n", ""
        ),
    )

    events = diagnostics._bounded_shutdown_journal(
        "hermes-gateway.service", 30, "system"
    )

    assert events == [
        {
            "timestamp_realtime_usec": "456",
            "unit": "hermes-gateway.service",
            "event": {
                "event": "gateway_shutdown",
                "pid": 123,
                "ts": 123,
                "signal": "SIGTERM",
                "shutdown_reason": "planned_stop",
                "parent": {"pid": 1, "name": "systemd"},
                "active_task_ids": ["task-1"],
                "cgroup": {"path": "/gateway", "memory_events": {"oom": 0}},
            },
        }
    ]
    assert "must-not-copy" not in str(events)
    assert "nested-secret" not in str(events)
    assert "nested-cgroup-secret" not in str(events)


def test_shutdown_journal_rejects_same_uid_worker_scope_forgery(monkeypatch):
    payload = {"event": "gateway_shutdown", "pid": 999, "signal": "forged"}
    forged = {
        "__REALTIME_TIMESTAMP": "456",
        "_PID": "999",
        "_UID": "996",
        "_TRANSPORT": "journal",
        "_SYSTEMD_UNIT": "hermes-worker-forged.scope",
        "SYSLOG_IDENTIFIER": "hermes-shutdown-forensics",
        "HERMES_EVENT": "gateway_shutdown",
        "MESSAGE": "HERMES_SHUTDOWN " + json.dumps(payload),
    }
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(forged) + "\n", ""
        ),
    )

    assert diagnostics._bounded_shutdown_journal(
        "hermes-gateway.service", 30, "system"
    ) == []


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
    monkeypatch.setattr(
        diagnostics,
        "_bounded_shutdown_journal",
        lambda _unit, _since, manager: journal_managers.append(manager) or [],
    )
    monkeypatch.setattr("sys.argv", ["hermes_gateway_diagnostics.py"])

    assert diagnostics.main() == 0
    assert seen == [(tmp_path, "auto")]
    assert journal_managers == ["user", "user"]
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


def test_auto_service_status_returns_both_loaded_manager_candidates(monkeypatch):
    def run(argv, **_kwargs):
        user = "--user" in argv
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(
                [
                    "LoadState=loaded",
                    f"ActiveState={'failed' if user else 'inactive'}",
                    f"SubState={'failed' if user else 'dead'}",
                    f"Result={'signal' if user else 'success'}",
                    f"ExecMainCode={2 if user else 0}",
                    f"ExecMainStatus={9 if user else 0}",
                    "NRestarts=0",
                ]
            ),
            "",
        )

    monkeypatch.setattr(diagnostics.subprocess, "run", run)
    monkeypatch.setattr(
        diagnostics, "_service_identity", lambda _unit, _manager: (None, None)
    )

    result = diagnostics.collect(hermes_home=Path("/nonexistent"))

    assert result["service_status"] is None
    assert result["service_statuses"]["system"]["active_state"] == "inactive"
    assert result["service_statuses"]["user"]["result"] == "signal"
    assert result["service_statuses"]["user"]["exec_main_status"] == 9


def test_incident_journal_preserves_exit_code_after_restart(monkeypatch):
    exit_message = (
        "hermes-gateway.service: Main process exited, "
        "code=killed, status=9/KILL"
    )

    def run(argv, **_kwargs):
        row = {
            "__REALTIME_TIMESTAMP": "123",
            "_PID": "1",
            "_UID": "0",
            "_COMM": "systemd",
            "SYSLOG_IDENTIFIER": "systemd",
            "_TRANSPORT": "journal",
            "_SYSTEMD_UNIT": "init.scope",
            "UNIT": "hermes-gateway.service",
            "MESSAGE": (
                exit_message if "hermes-gateway.service" in argv else "unrelated"
            ),
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(row) + "\n", "")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)

    events = diagnostics._bounded_incident_journal(
        "hermes-gateway.service", 30, "system"
    )

    assert any(event["message"] == exit_message for event in events)


def test_user_incident_journal_uses_user_unit_and_manager_owned_rows(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        row = {
            "__REALTIME_TIMESTAMP": "321",
            "_COMM": "systemd",
            "SYSLOG_IDENTIFIER": "systemd",
            "_TRANSPORT": "journal",
            "_SYSTEMD_USER_UNIT": "init.scope",
            "USER_UNIT": "hermes-gateway.service",
            "MESSAGE": "hermes-gateway.service: Main process exited, status=9/KILL",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(row) + "\n", "")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)

    events = diagnostics._bounded_incident_journal(
        "hermes-gateway.service", 30, "user"
    )

    assert calls[0][:3] == [
        "journalctl",
        "--user-unit",
        "hermes-gateway.service",
    ]
    assert len(events) == 1


def test_incident_journal_rejects_forged_gateway_lifecycle_text(monkeypatch):
    forged = {
        "__REALTIME_TIMESTAMP": "123",
        "_PID": "999",
        "_UID": "996",
        "_COMM": "hermes",
        "_TRANSPORT": "stdout",
        "_SYSTEMD_UNIT": "hermes-gateway.service",
        "MESSAGE": "Failed with result forged-secret-content",
    }
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(forged) + "\n", ""
        ),
    )

    events = diagnostics._bounded_incident_journal(
        "hermes-gateway.service", 30, "system"
    )

    assert events == []


def test_incident_journal_sorts_sources_before_global_cap(monkeypatch):
    gateway = {
        "__REALTIME_TIMESTAMP": "999",
        "_PID": "1",
        "_UID": "0",
        "_COMM": "systemd",
        "SYSLOG_IDENTIFIER": "systemd",
        "_TRANSPORT": "journal",
        "_SYSTEMD_UNIT": "init.scope",
        "UNIT": "hermes-gateway.service",
        "MESSAGE": "hermes-gateway.service: Main process exited, status=9/KILL",
    }
    health = [
        {
            "__REALTIME_TIMESTAMP": str(index),
            "_SYSTEMD_UNIT": "hermes-health-guard.service",
            "MESSAGE": "HERMES_HEALTH {}",
        }
        for index in range(1, 101)
    ]

    def run(argv, **_kwargs):
        rows = [gateway] if "hermes-gateway.service" in argv else health if "hermes-health-guard.service" in argv else []
        return subprocess.CompletedProcess(
            argv, 0, "".join(json.dumps(row) + "\n" for row in rows), ""
        )

    monkeypatch.setattr(diagnostics.subprocess, "run", run)

    events = diagnostics._bounded_incident_journal(
        "hermes-gateway.service", 30, "system"
    )

    assert len(events) == 100
    assert events[-1]["timestamp_realtime_usec"] == "999"
    assert any("Main process exited" in event["message"] for event in events)


def test_auto_manager_journal_uses_only_selected_manager(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "_service_identity",
        lambda _unit, _manager: (4321, "user"),
    )
    result = diagnostics.collect(hermes_home=Path("/nonexistent"))
    assert result["service_manager"] == "user"
