import json
import subprocess
from pathlib import Path

from plugins.specialist_router import register
from plugins.specialist_router.router import Router, RouterConfig


def config(tmp_path):
    return RouterConfig(codex_home=tmp_path / "codex", state_path=tmp_path / "state.json")


def _proc(stdout: str, stderr: str = "", returncode: int = 0):
    return type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def _jsonl_message(text: str, thread_id: str = "thread-1", ok: bool = True):
    payload = [json.dumps({"type": "thread.started", "thread_id": thread_id})]
    if ok:
        payload.append(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    return "\n".join(payload)


def test_non_coding_stays_on_coordinator(tmp_path):
    router = Router(config(tmp_path))
    assert router.config.coordinator_model == "openai-api/gpt-5.6"
    assert router.classify("What is the weather tomorrow?").route == "coordinator"


def test_bounded_inspection_routes_spark(tmp_path):
    d = Router(config(tmp_path)).classify("Inspect this repository and run focused tests")
    assert d.route == "spark"


def test_high_risk_routes_sol(tmp_path):
    d = Router(config(tmp_path)).classify("Implement a multi-file authentication migration")
    assert d.route == "sol"


def test_spark_prompt_delivery_uses_piped_stdin(tmp_path):
    seen = {}

    def runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        assert cmd[-1] == "-"
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["input"] == "Line 1\n\"quoted\" line"
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True
        return _proc(_jsonl_message("spark-ready", thread_id="spark-1"))

    router = Router(config(tmp_path), runner=runner)
    result = router._invoke("spark", "Line 1\n\"quoted\" line", tmp_path)
    assert result["ok"] is True
    assert result["session_id"] == "spark-1"
    assert seen["cmd"][seen["cmd"].index("-m") + 1] == "gpt-5.3-codex-spark"


def test_sol_prompt_delivery_uses_piped_stdin(tmp_path):
    seen = {}

    def runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        assert cmd[-1] == "-"
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["input"] == "Line 1\n\"quoted\" line"
        return _proc(_jsonl_message("sol-ready", thread_id="sol-1"))

    router = Router(config(tmp_path), runner=runner)
    result = router._invoke("sol", "Line 1\n\"quoted\" line", tmp_path)
    assert result["ok"] is True
    assert result["session_id"] == "sol-1"
    assert seen["cmd"][seen["cmd"].index("-m") + 1] == "gpt-5.6-sol"


def test_multiline_prompt_round_trips_without_being_rewritten(tmp_path):
    prompt = "First line\n\nSecond line with \"quotes\" and punctuation."

    def runner(cmd, **kwargs):
        assert kwargs["input"] == prompt
        assert kwargs["stdin"] is subprocess.PIPE
        assert cmd[-1] == "-"
        return _proc(_jsonl_message("multiline-ok", thread_id="m-1"))

    router = Router(config(tmp_path), runner=runner)
    result = router._invoke("spark", prompt, tmp_path)
    assert result["ok"] is True
    assert result["message"] == "multiline-ok"


def test_follow_up_reuses_original_failed_task_context(tmp_path):
    state = {
        "task": "Fix the routing failure in specialist invocation",
        "original_task": "Fix the routing failure in specialist invocation",
        "repository": "/repo",
        "routing_reason": "bounded coding task; Spark receives one focused attempt",
        "attempts": [
            {
                "pool": "spark",
                "model": "gpt-5.3-codex-spark",
                "ok": False,
                "message": "Reading additional input from stdin...",
            }
        ],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))

    seen = {}

    def runner(cmd, **kwargs):
        seen["prompt"] = kwargs["input"]
        return _proc(_jsonl_message("continued", thread_id="follow-1"))

    router = Router(config(tmp_path), runner=runner)
    result = router.execute("fix this issue", str(tmp_path))
    assert result["original_task"] == "fix this issue"
    assert result["task"].startswith("Original task:\nFix the routing failure in specialist invocation")
    assert "Fix fix this issue" not in seen["prompt"]
    assert "Previous specialist failure:" in seen["prompt"]
    assert "Reading additional input from stdin..." in seen["prompt"]


def test_spark_failure_escalates_and_sol_is_reviewed_by_spark(tmp_path):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd[cmd.index("-m") + 1])
        payload = _jsonl_message("done", thread_id=f"s{len(calls)}")
        return _proc(payload)

    r = Router(config(tmp_path), runner=runner)
    result = r.execute("Fix this small isolated bug", str(tmp_path), simulate_spark_failure=True)
    assert result["route"] == ["spark", "sol", "spark"]
    assert result["specialist_ok"] is True
    assert result["fallback_used"] is False
    assert calls == ["gpt-5.6-sol", "gpt-5.3-codex-spark"]


def test_direct_fallback_when_specialists_cannot_start_and_no_repeat_loop(tmp_path):
    calls = []

    def runner(cmd, **kwargs):
        calls.append({"model": cmd[cmd.index("-m") + 1], "prompt": kwargs["input"]})
        return _proc(
            _jsonl_message("", thread_id=f"fail-{len(calls)}", ok=False),
            stderr="Quota exceeded. Check your plan and billing details.",
            returncode=1,
        )

    router = Router(config(tmp_path), runner=runner)
    result = router.execute("Fix this small isolated bug", str(tmp_path))
    assert calls[0]["model"] == "gpt-5.3-codex-spark"
    assert calls[0]["prompt"] == "Fix this small isolated bug"
    assert calls[1]["model"] == "gpt-5.6-sol"
    assert calls[1]["prompt"].startswith("Implement and test the original goal. Compact Spark handoff follows:\n")
    assert '"original_goal": "Fix this small isolated bug"' in calls[1]["prompt"]
    assert '"acceptance_criteria": "Fix this small isolated bug"' in calls[1]["prompt"]
    assert result["route"] == ["spark", "sol", "coordinator"]
    assert result["specialist_ok"] is False
    assert result["fallback_used"] is True
    assert result["ok"] is True
    assert "continue directly in the coordinator/manual repo path" in result["attempts"][-1]["message"]
    assert len(result["attempts"]) == 3


def test_sol_weekly_reserve_routes_noncritical_work_to_spark(tmp_path):
    r = Router(config(tmp_path))
    quotas = r.quotas()
    quotas["sol"].weekly_remaining = 20
    r._quota_cache = (r._clock(), quotas)
    assert r.reserve_active()


def test_existing_specialist_session_is_resumed(tmp_path):
    commands = []

    def runner(cmd, **kwargs):
        commands.append({"cmd": cmd, "kwargs": kwargs})
        return _proc(_jsonl_message("continued", thread_id="resume-1"))

    Router(config(tmp_path), runner=runner).execute(
        "Inspect this repository and run focused tests", str(tmp_path), resume_session_id="thread-123"
    )
    assert "resume" in commands[0]["cmd"]
    assert "thread-123" in commands[0]["cmd"]
    assert commands[0]["cmd"][-1] == "-"
    assert commands[0]["kwargs"]["stdin"] is subprocess.PIPE
    assert commands[0]["kwargs"]["input"] == "Inspect this repository and run focused tests"


def test_quota_rollouts_are_kept_separate_and_cached(tmp_path):
    sessions = tmp_path / "codex" / "sessions" / "2026" / "07" / "11"
    sessions.mkdir(parents=True)
    for name, model, used in (("spark", "gpt-5.3-codex-spark", 70), ("sol", "gpt-5.6-sol", 25)):
        (sessions / f"{name}.jsonl").write_text("\n".join([
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
    (sessions / "spark.jsonl").write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"session_id": "spark-session"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 12}, "secondary": {"used_percent": 34}}}}),
    ]))
    q = Router(config(tmp_path)).quotas()
    assert q["spark"].five_hour_remaining == 88
    assert q["spark"].weekly_remaining == 66


def test_status_reports_unknown_external_quota_fields_and_burst_policy(tmp_path):
    router = Router(config(tmp_path))
    status = router.format_status()
    assert "Coordinator: openai-api/gpt-5.6" in status
    assert "Routine route: gpt-5.3-codex-spark" in status
    assert "Complex route: gpt-5.6-sol" in status
    assert "Active specialist sessions: 0 / 2" in status
    assert "Sol five-hour remaining: unknown" in status
    assert "Sol weekly remaining: unknown" in status
    assert "Banked reset availability: unknown" in status
    assert "Banked reset redemption: manual recommendation only; never automatic" in status
    assert "Fresh exact-head GitHub Codex review required: yes" in status
    assert "routine work stays on Spark" in status


def test_sol_reserve_boundary_changes_burst_state(tmp_path):
    router = Router(config(tmp_path))
    quotas = router.quotas()
    quotas["sol"].weekly_remaining = 20
    quotas["sol"].five_hour_remaining = 80
    router._quota_cache = (router._clock(), quotas)
    status = router.format_status()
    assert "Sol reserve active: yes" in status
    assert "reserve mode" in status


def test_banked_reset_config_is_display_only(tmp_path):
    cfg = RouterConfig.from_mapping({"banked_reset_available": "available", "banked_reset_expires_at": 123})
    router = Router(config(tmp_path))
    router.config = cfg
    status = router.format_status()
    assert "Banked reset availability: available" in status
    assert "Banked reset expiry: 123" in status
    assert "never automatic" in status


def test_plugin_registers_gateway_hook_status_and_tool(monkeypatch, tmp_path):
    seen = {"hooks": [], "commands": [], "tools": []}

    class Context:
        def register_hook(self, name, handler):
            seen["hooks"].append(name)

        def register_command(self, name, **kwargs):
            seen["commands"].append(name)

        def register_tool(self, name, **kwargs):
            seen["tools"].append(name)

    monkeypatch.setattr("plugins.specialist_router._config", lambda: config(tmp_path))
    register(Context())
    assert seen == {"hooks": ["pre_gateway_dispatch"], "commands": ["model-route-status"], "tools": ["route_specialist_task"]}
