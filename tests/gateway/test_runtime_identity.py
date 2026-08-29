"""Process-lifetime control-plane identity cannot be revoked by worker state."""

from __future__ import annotations

import os


def test_control_plane_identity_is_process_local_and_state_file_independent(
    monkeypatch,
):
    import gateway.runtime_identity as identity
    import gateway.status as status
    import tools.process_registry as process_registry

    monkeypatch.setattr(identity, "_CONTROL_PLANE_PID", os.getpid())
    monkeypatch.setattr(identity, "_SYSTEMD_CONTROL_PLANE_PID", os.getpid())
    monkeypatch.setattr(
        status,
        "get_running_pid",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker-writable gateway state must not be consulted")
        ),
    )

    assert process_registry._is_supervised_gateway_process() is True

    monkeypatch.setattr(identity, "_CONTROL_PLANE_PID", os.getpid() + 1)
    monkeypatch.setattr(identity, "_SYSTEMD_CONTROL_PLANE_PID", os.getpid() + 1)
    assert process_registry._is_supervised_gateway_process() is False


def test_control_plane_marker_latches_only_for_supervised_gateway(monkeypatch):
    import gateway.restart as restart
    import gateway.runtime_identity as identity

    monkeypatch.setattr(identity, "_CONTROL_PLANE_PID", None)
    monkeypatch.setattr(identity, "_SYSTEMD_CONTROL_PLANE_PID", None)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("INVOCATION_ID", "systemd-test")
    monkeypatch.setattr(restart, "is_gateway_supervisor_process", lambda: True)

    assert identity.mark_gateway_control_plane_process() is True
    assert identity.is_gateway_control_plane_process() is True
    assert identity.is_systemd_gateway_control_plane_process() is True


def test_s6_control_plane_does_not_require_systemd_worker_backend(monkeypatch):
    import gateway.restart as restart
    import gateway.runtime_identity as identity
    import tools.process_registry as process_registry

    monkeypatch.setattr(identity, "_CONTROL_PLANE_PID", None)
    monkeypatch.setattr(identity, "_SYSTEMD_CONTROL_PLANE_PID", None)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
    monkeypatch.setattr(restart, "is_gateway_supervisor_process", lambda: True)

    assert identity.mark_gateway_control_plane_process() is True
    assert identity.is_gateway_control_plane_process() is True
    assert identity.is_systemd_gateway_control_plane_process() is False
    assert process_registry._gateway_worker_isolation_required() is False
