import json
from pathlib import Path

from plugins.specialist_router.router import Router, RouterConfig
from plugins.specialist_router import register


def config(tmp_path):
    return RouterConfig(codex_home=tmp_path / "codex", state_path=tmp_path / "state.json")


def test_non_coding_stays_on_coordinator(tmp_path):
    assert Router(config(tmp_path)).classify("What is the weather tomorrow?").route == "coordinator"


def test_bounded_inspection_routes_spark(tmp_path):
    d = Router(config(tmp_path)).classify("Inspect this repository and run focused tests")
    assert d.route == "spark"


def test_high_risk_routes_sol(tmp_path):
    d = Router(config(tmp_path)).classify("Implement a multi-file authentication migration")
    assert d.route == "sol"


def test_spark_failure_escalates_and_sol_is_reviewed_by_spark(tmp_path):
    calls = []
    def runner(cmd, **kwargs):
        calls.append(cmd[cmd.index("-m") + 1])
        model = calls[-1]
        payload = '\n'.join([json.dumps({"type": "thread.started", "thread_id": f"s{len(calls)}"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})])
        return type("P", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
    r = Router(config(tmp_path), runner=runner)
    result = r.execute("Fix this small isolated bug", str(tmp_path), simulate_spark_failure=True)
    assert result["route"] == ["spark", "sol", "spark"]
    assert calls == ["gpt-5.6-sol", "gpt-5.3-codex-spark"]


def test_sol_weekly_reserve_routes_noncritical_work_to_spark(tmp_path):
    r = Router(config(tmp_path))
    quotas = r.quotas()
    quotas["sol"].weekly_remaining = 20
    r._quota_cache = (r._clock(), quotas)
    assert r.reserve_active()


def test_existing_specialist_session_is_resumed(tmp_path):
    commands = []
    def runner(cmd, **kwargs):
        commands.append(cmd)
        payload = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "continued"}})
        return type("P", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
    Router(config(tmp_path), runner=runner).execute(
        "Inspect this repository and run focused tests", str(tmp_path), resume_session_id="thread-123"
    )
    assert "resume" in commands[0]
    assert "thread-123" in commands[0]


def test_quota_rollouts_are_kept_separate_and_cached(tmp_path):
    sessions = tmp_path / "codex" / "sessions" / "2026" / "07" / "11"
    sessions.mkdir(parents=True)
    for name, model, used in (("spark", "gpt-5.3-codex-spark", 70), ("sol", "gpt-5.6-sol", 25)):
        (sessions / f"{name}.jsonl").write_text('\n'.join([
            json.dumps({"type": "session_meta", "payload": {}}),
            json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": {"model": model}}}),
            json.dumps({"type": "event_msg", "payload": {"rate_limits": {"primary": {"used_percent": used}, "secondary": {"used_percent": used}}}}),
        ]))
    q = Router(config(tmp_path)).quotas()
    assert q["spark"].weekly_remaining == 30
    assert q["sol"].weekly_remaining == 75


def test_quota_uses_router_session_pool_when_codex_omits_model(tmp_path):
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    (tmp_path / "state.json").write_text(json.dumps({"attempts": [{"session_id": "spark-session", "pool": "spark"}]}))
    (sessions / "spark.jsonl").write_text('\n'.join([
        json.dumps({"type": "session_meta", "payload": {"session_id": "spark-session"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 12}, "secondary": {"used_percent": 34}}}}),
    ]))
    q = Router(config(tmp_path)).quotas()
    assert q["spark"].five_hour_remaining == 88
    assert q["spark"].weekly_remaining == 66


def test_plugin_registers_gateway_hook_status_and_tool(monkeypatch, tmp_path):
    seen = {"hooks": [], "commands": [], "tools": []}
    class Context:
        def register_hook(self, name, handler): seen["hooks"].append(name)
        def register_command(self, name, **kwargs): seen["commands"].append(name)
        def register_tool(self, name, **kwargs): seen["tools"].append(name)
    monkeypatch.setattr("plugins.specialist_router._config", lambda: config(tmp_path))
    register(Context())
    assert seen == {"hooks": ["pre_gateway_dispatch"], "commands": ["model-route-status"], "tools": ["route_specialist_task"]}
