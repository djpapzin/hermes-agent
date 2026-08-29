"""Browser/Chrome workload cgroup isolation."""

from unittest.mock import patch

from tools.browser_tool import (
    _remember_browser_daemon_scope,
    _scope_browser_workload,
    _stop_scoped_browser_workload,
)


def test_browser_scope_uses_shared_gateway_worker_boundary():
    env = {"AGENT_BROWSER_SOCKET_DIR": "/tmp/ab", "DISPLAY": ":99"}
    with patch(
        "tools.process_registry.build_gateway_worker_scope_argv",
        return_value=(["systemd-run", "--", "agent-browser", "open"], "scope"),
    ) as build:
        argv, unit = _scope_browser_workload(
            ["agent-browser", "open"],
            environment=env,
            session_name="telegram-session",
        )

    assert argv == ["systemd-run", "--", "agent-browser", "open"]
    assert unit == "scope"
    assert build.call_args.kwargs["environment"] is env
    assert build.call_args.kwargs["unit_suffix"].startswith(
        "browser-telegram-session-"
    )


def test_timed_out_browser_stops_complete_scope_before_launcher():
    class Proc:
        _hermes_systemd_unit = "hermes-worker-system-browser.scope"

        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    proc = Proc()
    with patch("tools.process_registry._stop_systemd_unit") as stop:
        _stop_scoped_browser_workload(proc)

    stop.assert_called_once_with("hermes-worker-system-browser.scope")
    assert proc.killed is True


def test_timed_out_browser_also_stops_daemon_origin_scope():
    class Proc:
        _hermes_systemd_unit = "hermes-worker-system-command.scope"

        def poll(self):
            return 0

    with patch("tools.process_registry._stop_systemd_unit") as stop:
        _stop_scoped_browser_workload(
            Proc(),
            {"daemon_systemd_unit": "hermes-worker-system-daemon.scope"},
        )

    assert [call.args[0] for call in stop.call_args_list] == [
        "hermes-worker-system-command.scope",
        "hermes-worker-system-daemon.scope",
    ]


def test_browser_daemon_origin_scope_refreshes_after_idle_exit():
    session = {"daemon_systemd_unit": "hermes-worker-system-old.scope"}
    with patch(
        "tools.process_registry._systemd_unit_has_processes", return_value=False
    ):
        _remember_browser_daemon_scope(
            session, "hermes-worker-system-restarted.scope"
        )

    assert session["daemon_systemd_unit"] == "hermes-worker-system-restarted.scope"
