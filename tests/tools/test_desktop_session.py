"""Regression coverage for the persistent Linux computer-use desktop."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _browser(desktop_session, pid=4321, display=":7"):
    return desktop_session._BrowserProcess(
        pid=pid,
        env={"DISPLAY": display},
        score=(1000, 1, 1, pid),
    )


def test_state_file_is_allowlisted_atomic_and_private(tmp_path):
    from tools.computer_use import desktop_session

    state_path = tmp_path / "state" / "desktop-session.env"
    with patch.object(desktop_session, "_state_path", return_value=state_path):
        desktop_session._write_state({
            "DISPLAY": ":7",
            "BROWSER_PID": "4321",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/996/bus",
            "PASSWORD": "must-not-persist",
            "GOAL": "must-not-persist",
        })

    assert state_path.stat().st_mode & 0o077 == 0
    content = state_path.read_text()
    assert "DISPLAY=:7" in content
    assert "BROWSER_PID=4321" in content
    assert "PASSWORD" not in content
    assert "GOAL" not in content
    assert "must-not-persist" not in content


def test_stale_state_addresses_are_removed_from_the_next_repair(tmp_path):
    from tools.computer_use import desktop_session

    state_path = tmp_path / "desktop-session.env"
    state_path.write_text(
        "DISPLAY=:7\n"
        "BROWSER_PID=4321\n"
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/missing/bus\n"
        "AT_SPI_BUS_ADDRESS=not-valid\n"
        "GENERATION=old\n"
    )
    os.chmod(state_path, 0o600)
    socket_root = tmp_path / "x11"
    socket_root.mkdir()
    (socket_root / "X7").touch()
    browser = _browser(desktop_session)
    repaired_bus = tmp_path / "bus"
    repaired_bus.touch()

    def repair(env):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={repaired_bus}"
        return True

    with patch.object(desktop_session, "_state_path", return_value=state_path), \
         patch.object(desktop_session, "_x11_socket_dirs", return_value=(socket_root,)), \
         patch.object(desktop_session, "_find_browser", return_value=browser), \
         patch.object(desktop_session, "_launch_session_bus", side_effect=repair), \
         patch.object(desktop_session, "_launch_at_spi"):
        env = desktop_session.desktop_session_child_env({
            "DISPLAY": ":99",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/missing/bus",
        })

    assert env["DISPLAY"] == ":7"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={repaired_bus}"
    persisted = state_path.read_text()
    assert "AT_SPI_BUS_ADDRESS=not-valid" not in persisted
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/missing/bus" not in persisted


def test_dbus_launch_output_is_parsed_without_shell_evaluation(tmp_path):
    from tools.computer_use import desktop_session

    bus_path = tmp_path / "bus"
    bus_path.touch()
    env = {"DISPLAY": ":7", "PATH": "/usr/bin"}
    result = SimpleNamespace(
        returncode=0,
        stdout=(
            f"DBUS_SESSION_BUS_ADDRESS='unix:path={bus_path},guid=abc'; export "
            "DBUS_SESSION_BUS_ADDRESS;\nDBUS_SESSION_BUS_PID=1234; export DBUS_SESSION_BUS_PID;\n"
        ),
    )
    with patch.object(desktop_session.subprocess, "run", return_value=result):
        assert desktop_session._launch_session_bus(env) is True

    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={bus_path},guid=abc"
    assert env["DBUS_SESSION_BUS_PID"] == "1234"


def test_at_spi_repair_receives_only_desktop_environment(monkeypatch):
    from tools.computer_use import desktop_session

    captured = {}
    fake_process = SimpleNamespace(pid=5678)
    monkeypatch.setattr(desktop_session, "_AT_SPI_LAUNCHED_FOR", set())
    monkeypatch.setattr(desktop_session.Path, "exists", lambda _path: True)

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return fake_process

    monkeypatch.setattr(desktop_session.subprocess, "Popen", fake_popen)
    desktop_session._launch_at_spi({
        "DISPLAY": ":7",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/996/bus",
        "TELEGRAM_BOT_TOKEN": "must-not-pass",
    })

    assert captured["args"][-1] == "--launch-immediately"
    assert "TELEGRAM_BOT_TOKEN" not in captured["env"]
    assert captured["env"]["DISPLAY"] == ":7"


def test_supervisor_allocates_display_and_does_not_embed_legacy_numbers():
    script = Path(__file__).parents[2] / "scripts" / "hermes-desktop-session.sh"
    source = script.read_text()
    assert "-displayfd" in source
    assert "Xvfb :99" not in source
    assert "Xvfb :122" not in source
    assert "Xvfb :124" not in source
    assert "XDG_RUNTIME_DIR" in source


def test_fingerprint_changes_when_supervisor_generation_changes():
    from tools.computer_use import desktop_session

    contexts = [
        ({"DISPLAY": ":7"}, 4321, {"GENERATION": "one"}),
        ({"DISPLAY": ":7"}, 4321, {"GENERATION": "two"}),
    ]
    with patch.object(desktop_session, "_resolve_desktop_context", side_effect=contexts), \
         patch.object(desktop_session, "_process_start_token", return_value="start"), \
         patch.object(desktop_session, "_display_socket", return_value=None):
        first = desktop_session.desktop_session_fingerprint({})
        second = desktop_session.desktop_session_fingerprint({})

    assert first != second
    assert first[-1] == "one"
    assert second[-1] == "two"


def test_expired_transport_reconnects_and_keeps_logical_session():
    from anyio import ClosedResourceError
    from tools.computer_use import cua_backend
    from tools.computer_use.cua_backend import _CuaDriverSession

    class Bridge:
        def __init__(self):
            self.calls = 0

        def run(self, _value, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise ClosedResourceError()
            return {"ok": True}

    session = _CuaDriverSession(Bridge())
    session._session = object()
    session._started = True
    session._desktop_fingerprint = ("old",)
    session._call_tool_async = lambda name, args: (name, args)
    reconnects = []

    def reconnect():
        reconnects.append("reconnected")
        # The transport was recreated on the same desktop.
        session._desktop_fingerprint = ("old",)

    # This test exercises the existing closed-transport reconnect contract;
    # the separate fingerprint test below models expiry before a call.
    session._stop_lifecycle_locked = lambda: None
    session._start_lifecycle_locked = lambda: None
    session._on_reconnect = reconnect
    with patch.object(cua_backend, "desktop_session_fingerprint", return_value=("old",)):
        assert session.call_tool("get_accessibility_tree", {}) == {"ok": True}
    assert reconnects == ["reconnected"]
    assert session._started is True


def test_takeover_expiry_at_fifteen_minutes_reconnects_before_next_action(monkeypatch):
    from tools.computer_use.cua_backend import _CuaDriverSession

    class Bridge:
        def __init__(self):
            self.calls = 0

        def run(self, _value, timeout=None):
            self.calls += 1
            return {"call": self.calls}

    bridge = Bridge()
    session = _CuaDriverSession(bridge)
    parent_session = object()
    session._session = parent_session
    session._started = True
    session._desktop_fingerprint = ("old",)
    calls = []

    def restart():
        calls.append("restart")
        session._desktop_fingerprint = ("new",)

    session._restart_session_locked = restart
    fingerprints = iter([("old",), ("new",)])
    monkeypatch.setattr(
        "tools.computer_use.cua_backend.desktop_session_fingerprint",
        lambda: next(fingerprints),
    )
    session._call_tool_async = lambda name, args: (name, args)
    assert session.call_tool("inspect", {}) == {"call": 1}
    session._ensure_current_desktop_session()

    assert calls == ["restart"]
    assert session._desktop_fingerprint == ("new",)
    assert session._session is parent_session
    assert bridge.calls == 1
