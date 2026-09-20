"""Linux computer-use desktop-session environment invariants.

Regression coverage for the Hermes computer-use session boundary: the
browser supervisor owns the desktop, while each cua-driver transport gets a
fresh dynamic X11/DBus/AT-SPI environment.
"""

from __future__ import annotations

import pytest


@pytest.mark.linux_only
def test_browser_context_wins_over_stale_display_and_bus(monkeypatch, tmp_path):
    from tools.computer_use import desktop_session as module

    runtime = str(tmp_path / "runtime")
    browser_bus = "unix:path=/run/user/1000/browser-bus"
    browser = module._BrowserProcess(
        pid=321,
        env={
            "DISPLAY": ":77",
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": browser_bus,
            "PATH": "/browser/bin",
        },
        score=(1000, 1, 1, 321),
    )
    state = {
        "DISPLAY": ":99",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/stale-bus",
        "XDG_RUNTIME_DIR": str(tmp_path / "stale-runtime"),
    }
    written = {}

    monkeypatch.setattr(module, "_read_state", lambda: state)
    monkeypatch.setattr(module, "_find_browser", lambda _state: browser)
    monkeypatch.setattr(module, "_display_has_socket", lambda value: value == ":77")
    monkeypatch.setattr(
        module, "_valid_runtime_dir", lambda value: value if value == runtime else None
    )
    monkeypatch.setattr(
        module,
        "_address_is_valid",
        lambda value, **_kwargs: value == browser_bus,
    )
    monkeypatch.setattr(
        module,
        "_address_is_usable",
        lambda value, *_args, **_kwargs: value == browser_bus,
    )
    monkeypatch.setattr(
        module, "_write_state", lambda values, existing=None: written.update(values)
    )

    env = module.desktop_session_child_env(
        {
            "PATH": "/hermes/bin",
            "DISPLAY": ":124",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/stale-bus",
            "OPENAI_API_KEY": "must-not-be-persisted",
        },
        repair=False,
    )

    assert env["DISPLAY"] == ":77"
    assert env["XDG_RUNTIME_DIR"] == runtime
    assert env["DBUS_SESSION_BUS_ADDRESS"] == browser_bus
    assert written["DISPLAY"] == ":77"
    assert "OPENAI_API_KEY" not in written


@pytest.mark.linux_only
def test_missing_bus_is_repaired_and_atspi_state_is_allowlisted(monkeypatch, tmp_path):
    from tools.computer_use import desktop_session as module

    runtime = str(tmp_path / "runtime")
    repaired_bus = "unix:path=/run/user/1000/repaired-bus"
    repaired_atspi = "unix:path=/tmp/at-spi-bus"
    browser = module._BrowserProcess(
        pid=654,
        env={"DISPLAY": ":88", "XDG_RUNTIME_DIR": runtime, "PATH": "/browser/bin"},
        score=(1000, 1, 1, 654),
    )
    written = {}

    monkeypatch.setattr(module, "_read_state", lambda: {})
    monkeypatch.setattr(module, "_find_browser", lambda _state: browser)
    monkeypatch.setattr(module, "_display_has_socket", lambda value: value == ":88")
    monkeypatch.setattr(
        module, "_valid_runtime_dir", lambda value: value if value == runtime else None
    )
    monkeypatch.setattr(module, "_at_spi_bus_address", lambda _runtime: "")
    monkeypatch.setattr(
        module,
        "_address_is_valid",
        lambda value, **_kwargs: value in {repaired_bus, repaired_atspi},
    )

    def repair_bus(env):
        env["DBUS_SESSION_BUS_ADDRESS"] = repaired_bus
        return True

    def repair_atspi(env):
        env["AT_SPI_BUS_ADDRESS"] = repaired_atspi

    monkeypatch.setattr(module, "_launch_session_bus", repair_bus)
    monkeypatch.setattr(module, "_launch_at_spi", repair_atspi)

    def capture_state(values, existing=None):
        written.update({key: value for key, value in values.items() if value})

    monkeypatch.setattr(module, "_write_state", capture_state)

    env = module.desktop_session_child_env({
        "DISPLAY": ":88",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dead-bus",
        "XDG_RUNTIME_DIR": runtime,
    })

    assert env["DBUS_SESSION_BUS_ADDRESS"] == repaired_bus
    assert env["AT_SPI_BUS_ADDRESS"] == repaired_atspi
    assert written == {
        "DISPLAY": ":88",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": repaired_bus,
        "AT_SPI_BUS_ADDRESS": repaired_atspi,
        "BROWSER_PID": "654",
    }


@pytest.mark.linux_only
def test_state_browser_from_another_desktop_generation_is_ignored(monkeypatch):
    from tools.computer_use import desktop_session as module

    stale = module._BrowserProcess(
        pid=222,
        env={"DISPLAY": ":124"},
        score=(1000, 0, 1, 222),
    )
    active = module._BrowserProcess(
        pid=333,
        env={},
        score=(1, 1, 1, 333),
    )
    monkeypatch.setattr(
        module,
        "_browser_from_pid",
        lambda pid, rank=0: stale if pid == stale.pid else None,
    )
    monkeypatch.setattr(module, "_iter_browser_processes", lambda: [stale, active])
    monkeypatch.setattr(
        module,
        "_pid_is_descendant",
        lambda pid, ancestor: pid == active.pid and ancestor == 111,
    )

    found = module._find_browser(
        {"BROWSER_PID": str(stale.pid), "SESSION_PID": "111"}
    )

    assert found is active


@pytest.mark.linux_only
def test_active_session_environment_beats_overwritten_display_state(
    monkeypatch, tmp_path
):
    from tools.computer_use import desktop_session as module

    runtime = str(tmp_path / "runtime")
    active = module._BrowserProcess(
        pid=333,
        env={},
        score=(1, 1, 1, 333),
    )
    state = {
        "DISPLAY": ":124",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/stale-bus",
        "AT_SPI_BUS_ADDRESS": "unix:path=/tmp/stale-atspi",
        "BROWSER_PID": "222",
        "SESSION_PID": "111",
    }
    session_env = {
        "DISPLAY": ":77",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/active-bus",
    }
    written = {}

    monkeypatch.setattr(module, "_read_state", lambda: state)
    monkeypatch.setattr(module, "_find_browser", lambda _state: active)
    monkeypatch.setattr(
        module,
        "_read_process_env",
        lambda pid: session_env if pid == 111 else {},
    )
    monkeypatch.setattr(module, "_display_has_socket", lambda value: value == ":77")
    monkeypatch.setattr(
        module, "_valid_runtime_dir", lambda value: value if value == runtime else None
    )
    monkeypatch.setattr(
        module,
        "_address_is_usable",
        lambda value, *_args, **_kwargs: value == session_env["DBUS_SESSION_BUS_ADDRESS"],
    )
    monkeypatch.setattr(
        module, "_address_is_valid", lambda value, **_kwargs: value == session_env["DBUS_SESSION_BUS_ADDRESS"]
    )
    monkeypatch.setattr(
        module,
        "_pid_is_descendant",
        lambda pid, ancestor: pid == active.pid and ancestor == 111,
    )
    monkeypatch.setattr(
        module, "_write_state", lambda values, existing=None: written.update(values)
    )

    env = module.desktop_session_child_env({"DISPLAY": ":124"}, repair=False)

    assert env["DISPLAY"] == ":77"
    assert env["XDG_RUNTIME_DIR"] == runtime
    assert env["DBUS_SESSION_BUS_ADDRESS"] == session_env["DBUS_SESSION_BUS_ADDRESS"]
    assert written["BROWSER_PID"] == str(active.pid)


@pytest.mark.linux_only
def test_stale_bus_socket_is_rejected_by_liveness_probe(monkeypatch, tmp_path):
    from tools.computer_use import desktop_session as module

    stale_socket = tmp_path / "stale-bus"
    stale_socket.touch()
    stale = f"unix:path={stale_socket}"
    calls = []

    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: "/usr/bin/dbus-send" if name == "dbus-send" else None,
    )
    monkeypatch.setattr(
        module, "_address_is_valid", lambda value, **_kwargs: value == stale
    )

    def failed_probe(argv, **kwargs):
        calls.append((argv, kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"]))
        return module.subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(module.subprocess, "run", failed_probe)

    assert (
        module._address_is_usable(stale, {"DBUS_SESSION_BUS_ADDRESS": stale}) is False
    )
    assert calls == [
        (
            [
                "/usr/bin/dbus-send",
                "--session",
                "--print-reply",
                "--dest=org.freedesktop.DBus",
                "/",
                "org.freedesktop.DBus.ListNames",
            ],
            stale,
        )
    ]


@pytest.mark.linux_only
def test_stale_atspi_launcher_memo_is_repaired(monkeypatch, tmp_path):
    from tools.computer_use import desktop_session as module

    runtime = str(tmp_path / "runtime")
    display = ":88"
    session_bus = "unix:path=/run/user/1000/session-bus"
    stale_atspi = "unix:path=/run/user/1000/at-spi/stale"
    repaired_atspi = "unix:path=/run/user/1000/at-spi/bus_0"
    addresses = iter([stale_atspi, "", repaired_atspi])

    monkeypatch.setattr(
        module, "_valid_runtime_dir", lambda value: value == runtime and value
    )
    monkeypatch.setattr(module, "_at_spi_bus_address", lambda _runtime: next(addresses))
    monkeypatch.setattr(
        module,
        "_address_is_usable",
        lambda value, *_args, **_kwargs: value == repaired_atspi,
    )
    monkeypatch.setattr(
        module,
        "_AT_SPI_LAUNCHED_FOR",
        {(display, session_bus)},
    )
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: type("Process", (), {"pid": 987})(),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    env = {
        "DISPLAY": display,
        "DBUS_SESSION_BUS_ADDRESS": session_bus,
        "XDG_RUNTIME_DIR": runtime,
    }
    module._launch_at_spi(env)

    assert env["AT_SPI_BUS_ADDRESS"] == repaired_atspi
    assert env["AT_SPI_BUS_PID"] == "987"


@pytest.mark.linux_only
def test_persisted_desktop_state_is_private_and_allowlisted(monkeypatch, tmp_path):
    from tools.computer_use import desktop_session as module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    module._write_state(
        {
            "DISPLAY": ":88",
            "BROWSER_PID": "654",
            "OPENAI_API_KEY": "must-not-be-written",
        },
        existing={"GENERATION": "generation-1", "COOKIE": "must-not-be-written"},
    )

    path = tmp_path / "hermes" / "state" / "computer_use" / "desktop-session.env"
    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0
    content = path.read_text(encoding="utf-8")
    assert "DISPLAY=:88" in content
    assert "BROWSER_PID=654" in content
    assert "GENERATION=generation-1" in content
    assert "OPENAI_API_KEY" not in content
    assert "COOKIE" not in content
