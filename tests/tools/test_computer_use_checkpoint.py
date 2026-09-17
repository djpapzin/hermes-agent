"""Durable computer-use checkpoint/idempotency tests."""

from __future__ import annotations

import json

import pytest


class _TargetBackend:
    def computer_use_target(self):
        return {"app": "Google Chrome", "pid": 4321, "window_id": 77}


@pytest.fixture
def leased_worker(monkeypatch, tmp_path):
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_SCOPE_ID", "scope-7")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Inspect the browser desktop",
            body="Resume from the latest safe browser checkpoint.",
            assignee="test-worker",
            session_id="telegram-origin-session",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET current_step_key = ? WHERE id = ?",
                ("inspect-browser", task_id),
            )
        task = kb.claim_task(conn, task_id, claimer="test-claim")
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "test-claim")
    return task_id, run_id


def test_checkpoint_persists_safe_target_approvals_and_routing(leased_worker):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.computer_use.checkpoint import begin_operation, record_operation_result

    task_id, run_id = leased_worker
    backend = _TargetBackend()
    claim = begin_operation(
        action="click",
        session_id="telegram-session",
        tool_call_id="tool-call-1",
        backend=backend,
        approval_scopes=["click", "bring_to_front"],
    )
    assert claim.operation_id and not claim.duplicate
    assert record_operation_result(
        action="click",
        session_id="telegram-session",
        backend=backend,
        approval_scopes=["click", "bring_to_front"],
        operation_id=claim.operation_id,
        result=json.dumps({"ok": True}),
    )

    conn = kbc.connect()
    try:
        events = kb.list_events(conn, task_id)
        payloads = [
            event.payload for event in events if event.kind == "computer_use_operation"
        ]
        assert payloads
        completed = payloads[-1]
        assert completed["checkpoint"]["status"] == "completed"
        assert completed["checkpoint"]["safe_to_resume"] is True
        assert completed["browser_target"] == {
            "app": "Google Chrome",
            "pid": 4321,
            "window_id": 77,
        }
        assert completed["approval_scopes"] == ["click", "bring_to_front"]
        assert completed["routing"]["session_id"] == "telegram-session"
        assert completed["routing"]["platform"] == "telegram"
        assert events[-1].run_id == run_id
        context = kb.build_worker_context(conn, task_id)
        assert "Current step: inspect-browser" in context
        assert "Latest computer-use checkpoint" in context
        assert "window_id=77" in context
        assert "Do not replay" not in context
    finally:
        conn.close()


def test_15_minute_takeover_expiry_keeps_parent_job_running_and_blocks_replay(
    leased_worker,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.computer_use.checkpoint import begin_operation, record_operation_result

    task_id, run_id = leased_worker
    backend = _TargetBackend()
    common = {
        "action": "type",
        "session_id": "telegram-session",
        "tool_call_id": "tool-call-2",
        "backend": backend,
        "approval_scopes": ["type"],
    }
    first = begin_operation(**common)
    assert first.operation_id and not first.duplicate
    record_operation_result(
        **{
            key: common[key]
            for key in ("action", "session_id", "backend", "approval_scopes")
        },
        operation_id=first.operation_id,
        result=json.dumps({
            "isError": True,
            "structuredContent": {"code": "session_expired_no_replay"},
        }),
    )

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == run_id
    finally:
        conn.close()

    retry = begin_operation(**common)
    assert retry.duplicate is True
    assert retry.prior_status == "uncertain"
    assert retry.blocked is True

    # A fresh, safe read checkpoint clears the fence; a new input may then be
    # considered by the model without replaying the uncertain operation.
    from tools.computer_use.checkpoint import record_checkpoint

    assert record_checkpoint(
        action="capture",
        session_id="telegram-session",
        backend=backend,
        approval_scopes=(),
        result=json.dumps({"ok": True}),
    )
    fresh = begin_operation(**{**common, "tool_call_id": "tool-call-after-fresh-state"})
    assert fresh.duplicate is False


def test_handler_does_not_dispatch_a_durable_duplicate(monkeypatch):
    import tools.computer_use.checkpoint as checkpoint
    import tools.computer_use.tool as computer_use_tool

    backend = _TargetBackend()
    dispatched = []
    monkeypatch.setattr(computer_use_tool, "_get_backend", lambda **_: backend)
    monkeypatch.setattr(computer_use_tool, "_request_approval", lambda *_args: None)
    monkeypatch.setattr(
        checkpoint,
        "begin_operation",
        lambda **_: checkpoint.OperationClaim("opaque-operation", True, "completed"),
    )
    monkeypatch.setattr(
        computer_use_tool,
        "_dispatch",
        lambda *_args, **_kwargs: dispatched.append(True),
    )

    result = json.loads(
        computer_use_tool.handle_computer_use(
            {"action": "click", "x": 10, "y": 20},
            session_id="telegram-session",
            tool_call_id="tool-call-3",
        )
    )
    assert result["code"] == "duplicate_action_no_replay"
    assert dispatched == []


def test_mutation_fails_closed_when_checkpoint_store_is_unavailable(
    leased_worker, monkeypatch
):
    import hermes_cli.kanban_db_connect as kbc
    from tools.computer_use.checkpoint import begin_operation

    monkeypatch.setattr(
        kbc,
        "connect",
        lambda **_: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    claim = begin_operation(
        action="click",
        session_id="telegram-session",
        tool_call_id="tool-call-db-down",
        backend=_TargetBackend(),
        approval_scopes=["click"],
    )
    assert claim.duplicate is True
    assert claim.blocked is True
    assert claim.prior_status == "checkpoint_unavailable"
