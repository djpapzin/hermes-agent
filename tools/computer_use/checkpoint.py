"""Durable computer-use checkpoints for dispatcher-owned Hermes jobs.

Computer-use transport state is intentionally short-lived.  When the tool runs
inside a leased Kanban worker, this module records the safe browser target and
the outcome of each input operation in the existing task-event log.  The
gateway remains the owner of the task and run; this is only a narrow bridge to
the already-persistent worker/router state.

Only identifiers, action names, numeric native target ids, approval scopes,
and redacted routing labels are stored.  Tool arguments and tool results are
never persisted because they may contain credentials or page data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.computer_use.checkpoint")

_INPUT_ACTIONS = frozenset({
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "drag",
    "scroll",
    "type",
    "key",
    "set_value",
    "focus_app",
})
_UNCERTAIN_CODES = frozenset({
    "transport_outcome_unknown",
    "timeout_outcome_unknown",
    "session_expired_no_replay",
})
_OPERATION_EVENT = "computer_use_operation"
_CHECKPOINT_EVENT = "computer_use_checkpoint"
_MAX_EVENT_SCAN = 256


@dataclass(frozen=True)
class OperationClaim:
    """Result of the durable pre-action idempotency claim."""

    operation_id: Optional[str] = None
    duplicate: bool = False
    prior_status: Optional[str] = None
    blocked: bool = False


def _worker_context() -> Optional[tuple[str, int, Optional[str]]]:
    """Return the active dispatcher run, or ``None`` for ordinary sessions."""
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw_run_id = str(os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not task_id or not raw_run_id.isdigit() or int(raw_run_id) <= 0:
        return None
    return task_id, int(raw_run_id), os.environ.get("HERMES_KANBAN_BOARD") or None


def _safe_text(value: Any, limit: int = 160) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    # IDs and labels are useful for recovery, but never let a page or provider
    # error become a durable secret-bearing log line.
    return redact_sensitive_text(text[:limit], force=True)


def _safe_approvals(scopes: Iterable[str]) -> list[str]:
    result: list[str] = []
    for scope in scopes:
        value = _safe_text(scope, 80)
        if value and value not in result:
            result.append(value)
    return result


def _safe_route(session_id: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for field, env_name in (
        ("session_id", None),
        ("platform", "HERMES_SESSION_PLATFORM"),
        ("thread_id", "HERMES_SESSION_THREAD_ID"),
        ("scope_id", "HERMES_SESSION_SCOPE_ID"),
        ("profile", "HERMES_PROFILE"),
        ("board", "HERMES_KANBAN_BOARD"),
    ):
        raw = session_id if env_name is None else os.environ.get(env_name)
        if value := _safe_text(raw, 160):
            values[field] = value
    return values


def _safe_target(backend: Any) -> Optional[Dict[str, Any]]:
    getter = getattr(backend, "computer_use_target", None)
    try:
        target = getter() if callable(getter) else None
    except Exception:
        target = None
    if not isinstance(target, dict):
        return None
    result: Dict[str, Any] = {}
    if app := _safe_text(target.get("app"), 120):
        result["app"] = app
    for field in ("pid", "window_id"):
        try:
            number = int(target.get(field))
        except (TypeError, ValueError):
            continue
        if number > 0:
            result[field] = number
    return result or None


def _operation_id(task_id: str, run_id: int, session_id: str, tool_call_id: str) -> str:
    """Opaque, deterministic idempotency key; raw call ids are not stored."""
    value = "\0".join((task_id, str(run_id), session_id, tool_call_id))
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _result_payload(result: Any) -> Dict[str, Any]:
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return {}
    else:
        parsed = result
    return parsed if isinstance(parsed, dict) else {}


def _result_status(result: Any, error: Optional[BaseException] = None) -> str:
    if error is not None:
        # An exception after an input was handed to the driver cannot prove that
        # the input did not land.  Recovery must therefore stop before replay.
        return "uncertain"
    payload = _result_payload(result)
    structured = payload.get("structuredContent")
    structured = structured if isinstance(structured, dict) else {}
    code = payload.get("code") or structured.get("code")
    if code in _UNCERTAIN_CODES:
        return "uncertain"
    if (
        payload.get("error")
        or payload.get("isError") is True
        or payload.get("ok") is False
    ):
        return "failed"
    return "completed"


def _transport_reconnected(result: Any) -> bool:
    payload = _result_payload(result)
    return payload.get("transport_reconnected") is True


def _append_event(payload: Dict[str, Any], *, kind: str = _CHECKPOINT_EVENT) -> bool:
    context = _worker_context()
    if context is None:
        return False
    task_id, run_id, board = context
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=board)
        try:
            with kb.write_txn(conn):
                row = conn.execute(
                    "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                # A reclaimed/superseded worker must not write a checkpoint into
                # its successor's run.
                if (
                    row is None
                    or row["status"] != "running"
                    or int(row["current_run_id"] or 0) != run_id
                ):
                    return False
                kb._append_event(conn, task_id, kind, payload, run_id=run_id)
            return True
        finally:
            conn.close()
    except Exception:
        # Checkpointing is safety bookkeeping, never a reason to break the
        # actual computer-use call.  Do not log payloads or exception text.
        logger.debug("computer-use checkpoint write failed", exc_info=True)
        return False


def _claim_operation_event(
    *,
    task_id: str,
    run_id: int,
    board: Optional[str],
    operation_id: str,
    raw_call_id: Optional[str],
    payload: Dict[str, Any],
) -> OperationClaim:
    """Atomically deduplicate and append the pending operation event."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=board)
        try:
            with kb.write_txn(conn):
                row = conn.execute(
                    "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "running"
                    or int(row["current_run_id"] or 0) != run_id
                ):
                    return OperationClaim(operation_id, True, "superseded")
                # A pending/uncertain operation is a durable no-replay fence.
                # It remains in force across worker/run replacement until the
                # successor records a fresh, safe read checkpoint. This keeps
                # a new model tool-call id from replaying an input whose remote
                # outcome was not proven.
                latest = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND kind IN (?, ?) "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, _OPERATION_EVENT, _CHECKPOINT_EVENT),
                ).fetchone()
                if latest is not None:
                    try:
                        latest_payload = json.loads(latest["payload"] or "{}")
                    except (TypeError, ValueError):
                        latest_payload = {}
                    latest_checkpoint = (
                        latest_payload.get("checkpoint")
                        if isinstance(latest_payload, dict)
                        else None
                    )
                    latest_status = (
                        str(latest_checkpoint.get("status") or "")
                        if isinstance(latest_checkpoint, dict)
                        else ""
                    )
                    if latest_status in {"pending", "uncertain"}:
                        return OperationClaim(operation_id, True, latest_status, True)
                if raw_call_id:
                    rows = conn.execute(
                        "SELECT payload FROM task_events "
                        "WHERE task_id = ? AND run_id = ? AND kind = ? "
                        "ORDER BY id DESC LIMIT ?",
                        (task_id, run_id, _OPERATION_EVENT, _MAX_EVENT_SCAN),
                    ).fetchall()
                    for event_row in rows:
                        try:
                            prior = json.loads(event_row["payload"] or "{}")
                        except (TypeError, ValueError):
                            continue
                        prior_checkpoint = (
                            prior.get("checkpoint") if isinstance(prior, dict) else None
                        )
                        if (
                            not isinstance(prior_checkpoint, dict)
                            or prior_checkpoint.get("operation_id") != operation_id
                        ):
                            continue
                        prior_status = str(prior_checkpoint.get("status") or "")
                        if prior_status in {"pending", "completed", "uncertain"}:
                            return OperationClaim(operation_id, True, prior_status)
                        break
                kb._append_event(
                    conn, task_id, _OPERATION_EVENT, payload, run_id=run_id
                )
            return OperationClaim(operation_id)
        finally:
            conn.close()
    except Exception:
        # Without the atomic claim there is no duplicate-action guarantee. Do
        # not send a mutating input to the desktop; the parent job stays alive
        # and can retry after the gateway's durable store recovers.
        logger.debug("computer-use idempotency claim failed", exc_info=True)
        return OperationClaim(operation_id, True, "checkpoint_unavailable", True)


def _payload(
    *,
    action: str,
    status: str,
    session_id: str,
    backend: Any,
    approval_scopes: Iterable[str],
    operation_id: Optional[str] = None,
    result: Any = None,
) -> Dict[str, Any]:
    target = _safe_target(backend)
    safe = status in {"completed", "failed", "approval_denied"}
    checkpoint: Dict[str, Any] = {
        "action": _safe_text(action, 60) or "unknown",
        "status": status,
        "safe_to_resume": safe,
    }
    if operation_id:
        checkpoint["operation_id"] = operation_id
    payload: Dict[str, Any] = {
        "checkpoint": checkpoint,
        "browser_target": target,
        "approval_scopes": _safe_approvals(approval_scopes),
        "routing": _safe_route(session_id),
        "recorded_at": int(time.time()),
    }
    if _transport_reconnected(result):
        payload["transport_reconnected"] = True
    return payload


def begin_operation(
    *,
    action: str,
    session_id: str,
    tool_call_id: Optional[str],
    backend: Any,
    approval_scopes: Iterable[str],
) -> OperationClaim:
    """Claim one mutating tool call before it reaches the desktop.

    Repeated calls with the same model tool-call id are refused after a
    completed, pending, or uncertain attempt.  A missing tool-call id still
    gets a durable pending marker, but cannot be matched safely across turns.
    """
    if action not in _INPUT_ACTIONS:
        return OperationClaim()
    context = _worker_context()
    if context is None:
        return OperationClaim()
    task_id, run_id, _ = context
    raw_call_id = _safe_text(tool_call_id, 240)
    operation_id = (
        _operation_id(task_id, run_id, session_id, raw_call_id)
        if raw_call_id
        else uuid.uuid4().hex
    )
    payload = _payload(
        action=action,
        status="pending",
        session_id=session_id,
        backend=backend,
        approval_scopes=approval_scopes,
        operation_id=operation_id,
    )
    return _claim_operation_event(
        task_id=task_id,
        run_id=run_id,
        board=context[2],
        operation_id=operation_id,
        raw_call_id=raw_call_id,
        payload=payload,
    )


def record_operation_result(
    *,
    action: str,
    session_id: str,
    backend: Any,
    approval_scopes: Iterable[str],
    operation_id: Optional[str],
    result: Any = None,
    error: Optional[BaseException] = None,
) -> bool:
    if not operation_id:
        return False
    return _append_event(
        _payload(
            action=action,
            status=_result_status(result, error),
            session_id=session_id,
            backend=backend,
            approval_scopes=approval_scopes,
            operation_id=operation_id,
            result=result,
        ),
        kind=_OPERATION_EVENT,
    )


def record_checkpoint(
    *,
    action: str,
    session_id: str,
    backend: Any,
    approval_scopes: Iterable[str] = (),
    result: Any = None,
    status: Optional[str] = None,
) -> bool:
    """Record a read/transport checkpoint without storing its result body."""
    return _append_event(
        _payload(
            action=action,
            status=status or _result_status(result),
            session_id=session_id,
            backend=backend,
            approval_scopes=approval_scopes,
            result=result,
        )
    )


__all__ = [
    "OperationClaim",
    "begin_operation",
    "record_checkpoint",
    "record_operation_result",
]
