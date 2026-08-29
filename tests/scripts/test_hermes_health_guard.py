import json

from scripts.hermes_health_guard import GuardSnapshot, evaluate, main


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
