"""Browser/Chrome workload cgroup isolation."""

from unittest.mock import patch

from tools.browser_tool import _scope_browser_workload


def test_browser_scope_uses_shared_gateway_worker_boundary():
    env = {"AGENT_BROWSER_SOCKET_DIR": "/tmp/ab", "DISPLAY": ":99"}
    with patch(
        "tools.process_registry.build_gateway_worker_scope_argv",
        return_value=(["systemd-run", "--", "agent-browser", "open"], "scope"),
    ) as build:
        argv = _scope_browser_workload(
            ["agent-browser", "open"],
            environment=env,
            session_name="telegram-session",
        )

    assert argv == ["systemd-run", "--", "agent-browser", "open"]
    assert build.call_args.kwargs["environment"] is env
    assert build.call_args.kwargs["unit_suffix"].startswith(
        "browser-telegram-session-"
    )
