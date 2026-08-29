import json
import subprocess

from scripts.hermes_health_guard import (
    GuardSnapshot,
    _run,
    _telegram_enabled,
    collect_snapshot,
    evaluate,
    main,
)


def _snapshot(**overrides):
    values = {
        "active_state": "active",
        "main_pid": 123,
        "tasks": 20,
        "memory_bytes": 100,
        "wal_bytes": 0,
        "db_locks": 0,
        "telegram_poll_conflicts": 0,
        "telegram_enabled": True,
        "telegram_connected": True,
        "state_db_exists": True,
        "active_agents": 2,
        "queued_tasks": 1,
    }
    values.update(overrides)
    return GuardSnapshot(**values)


def test_pressure_and_db_locks_are_alerts_not_recovery_authority():
    reasons = evaluate(
        _snapshot(tasks=900, memory_bytes=9000, db_locks=20),
        max_tasks=500,
        max_memory=8000,
        max_wal=1000,
        lock_threshold=10,
    )

    assert reasons == ["tasks=900>500", "memory=9000>8000", "db_locks=20>=10"]


def test_main_persists_alert_only_decision_without_restart(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        "scripts.hermes_health_guard.collect_snapshot",
        lambda **_kwargs: _snapshot(db_locks=12),
    )

    assert main(["--hermes-home", str(home), "--state-file", str(state)]) == 0
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["decision"] == "alert_only"
    assert payload["recovery_attempted"] is False
    assert payload["active_agents"] == 2
    assert payload["queued_tasks"] == 1


def test_healthy_snapshot_is_recorded_without_false_degradation(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        "scripts.hermes_health_guard.collect_snapshot",
        lambda **_kwargs: _snapshot(),
    )

    assert main(["--hermes-home", str(home), "--state-file", str(state)]) == 0
    assert json.loads(state.read_text(encoding="utf-8"))["decision"] == "healthy"


def test_disabled_telegram_is_not_reported_disconnected():
    reasons = evaluate(
        _snapshot(telegram_enabled=False, telegram_connected=False),
        max_tasks=500,
        max_memory=8000,
        max_wal=1000,
        lock_threshold=10,
    )

    assert "telegram_polling=disconnected" not in reasons


def test_probe_failure_is_an_explicit_alert():
    reasons = evaluate(
        _snapshot(probe_errors=("systemctl:MainPID", "journalctl")),
        max_tasks=500,
        max_memory=8000,
        max_wal=1000,
        lock_threshold=10,
    )

    assert reasons[:2] == [
        "probe_unavailable=systemctl:MainPID",
        "probe_unavailable=journalctl",
    ]


def test_subprocess_timeout_becomes_unavailable_result(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("systemctl", 5)
        ),
    )

    result = _run("systemctl", "show", "hermes-gateway.service")

    assert result.returncode == 124


def test_unset_systemd_resource_values_are_reported_unavailable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    def runner(*args):
        prop = next((arg for arg in args if arg.startswith("--property=")), "")
        values = {
            "--property=ActiveState": "inactive\n",
            "--property=MainPID": "0\n",
            "--property=TasksCurrent": "[not set]\n",
            "--property=MemoryCurrent": "[not set]\n",
        }
        return subprocess.CompletedProcess(args, 0, values.get(prop, ""), "")

    snapshot = collect_snapshot(
        unit="hermes-gateway.service",
        hermes_home=home,
        lock_window_seconds=60,
        runner=runner,
    )

    assert snapshot.tasks == 0
    assert snapshot.memory_bytes == 0
    assert "systemctl:TasksCurrent:invalid=[not set]" in snapshot.probe_errors
    assert "systemctl:MemoryCurrent:invalid=[not set]" in snapshot.probe_errors


def test_telegram_enablement_resolves_nested_config_and_secret_file(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "gateway:\n  platforms:\n    telegram:\n      enabled: true\n",
        encoding="utf-8",
    )
    assert _telegram_enabled(config) is True

    config.write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    assert _telegram_enabled(config) is True

    config.write_text(
        "platforms:\n  telegram:\n    enabled: false\n",
        encoding="utf-8",
    )
    assert _telegram_enabled(config) is False


def test_runtime_platform_state_recognizes_proxied_telegram_connection(tmp_path):
    from scripts.hermes_health_guard import _runtime_snapshot

    state = tmp_path / "gateway_state.json"
    state.write_text(
        '{"active_agents": 1, "admission": {"queued_tasks": 2}, '
        '"platforms": {"telegram": {"state": "connected"}}}',
        encoding="utf-8",
    )

    assert _runtime_snapshot(state) == (1, 2, True)

    state.write_text("[]", encoding="utf-8")
    assert _runtime_snapshot(state) == (0, 0, None)
