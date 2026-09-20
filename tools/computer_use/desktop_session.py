"""Resolve the browser desktop environment used by computer use.

The browser desktop and the ``cua-driver`` stdio transport have different
lifetimes.  The desktop is owned by the persistent browser service; this
module only discovers its current environment and supplies that environment
to a short-lived driver child.  It never owns the browser, talks to CDP, or
persists arbitrary process variables.

On Linux the browser process is the authority for ``DISPLAY`` and the session
bus.  A small allow-listed state file written by the desktop supervisor is a
restart/reboot hint, not a hard-coded display configuration.  When a valid
display exists but the inherited bus is missing or stale, the helper creates
the session bus/accessibility plumbing needed by the next driver transport.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple


_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")
_DBUS_PATH_RE = re.compile(r"(?:^|[:,])path=([^,]+)")
_DBUS_ADDRESS_RE = re.compile(r"^unix:(?:path=[^,]+|abstract=[^,]+)(?:,[^,=]+=[^,]*)*$")
_DBUS_OUTPUT_RE = re.compile(
    r"^DBUS_SESSION_BUS_ADDRESS=(?:'([^']*)'|\"([^\"]*)\"|([^\s;]+))",
    re.MULTILINE,
)
_DBUS_PID_RE = re.compile(r"^DBUS_SESSION_BUS_PID=(?:'?(\d+)'?)", re.MULTILINE)

_STATE_KEYS = (
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SESSION_BUS_PID",
    "AT_SPI_BUS_ADDRESS",
    "AT_SPI_BUS_PID",
    "BROWSER_PID",
    "SESSION_PID",
    "GENERATION",
)
_STATE_KEY_SET = set(_STATE_KEYS)
_PROCESS_ENV_KEYS = set(_STATE_KEYS) | {"HOME", "PATH", "XAUTHORITY"}
_DESKTOP_ENV_KEYS = {
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SESSION_BUS_PID",
    "AT_SPI_BUS_ADDRESS",
    "AT_SPI_BUS_PID",
    "XAUTHORITY",
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
}
_BROWSER_BASENAMES = {
    "brave",
    "brave-browser",
    "browser",
    "chromium",
    "chromium-browser",
    "chrome",
    "firefox",
    "microsoft-edge",
}
_EXCLUDED_CGROUP_HINTS = ("test", "pytest", "session-broker")
_BROWSER_CGROUP_HINTS = ("browser", "chrome", "chromium", "firefox")

_AT_SPI_LAUNCH_LOCK = threading.Lock()
_AT_SPI_LAUNCHED_FOR: set[Tuple[str, str]] = set()


@dataclass(frozen=True)
class _BrowserProcess:
    pid: int
    env: Dict[str, str]
    score: Tuple[int, int, int, int]


def _current_uid() -> int:
    getter = getattr(os, "getuid", None)
    return int(getter()) if getter is not None else -1


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        # Import-time fallback only; normal Hermes paths always use
        # get_hermes_home(), which is profile-aware.
        return Path.home() / ".hermes"


def _state_path() -> Path:
    return _hermes_home() / "state" / "computer_use" / "desktop-session.env"


def _parse_display(value: str) -> Optional[int]:
    match = _DISPLAY_RE.fullmatch((value or "").strip())
    return int(match.group(1)) if match else None


def _x11_socket_dirs() -> Tuple[Path, ...]:
    return (Path("/tmp/.X11-unix"), Path(f"/run/user/{_current_uid()}/.X11-unix"))


def _display_socket(display: str) -> Optional[Path]:
    number = _parse_display(display)
    if number is None:
        return None
    for base in _x11_socket_dirs():
        candidate = base / f"X{number}"
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _display_has_socket(display: str) -> bool:
    return _display_socket(display) is not None


def _iter_x11_displays() -> List[Tuple[int, Path, float]]:
    result: List[Tuple[int, Path, float]] = []
    seen: set[Tuple[int, str]] = set()
    uid = _current_uid()
    for base in _x11_socket_dirs():
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith("X") or not entry.name[1:].isdigit():
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            key = (int(entry.name[1:]), str(entry))
            if key in seen or info.st_uid != uid or not entry.is_socket():
                continue
            seen.add(key)
            result.append((key[0], entry, info.st_mtime))
    result.sort(key=lambda item: (item[2], item[0]), reverse=True)
    return result


def _read_state() -> Dict[str, str]:
    path = _state_path()
    try:
        info = path.stat()
        if info.st_uid != _current_uid() or info.st_mode & 0o077:
            return {}
        raw = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return {}
    result: Dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _STATE_KEY_SET:
            continue
        value = value.strip()
        if "\x00" not in value and "\n" not in value and len(value) <= 4096:
            result[key] = value
    return result


def _int_value(value: Optional[str]) -> Optional[int]:
    if not value or not value.isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _process_parent_pid(pid: int) -> Optional[int]:
    """Return a Linux process' parent without trusting the comm field layout."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    _, separator, remainder = raw.partition(")")
    if not separator:
        return None
    fields = remainder.split()
    if len(fields) < 2:
        return None
    try:
        parent = int(fields[1])  # state is fields[0]; ppid is fields[1]
    except ValueError:
        return None
    return parent if parent > 0 else None


def _pid_is_descendant(pid: Optional[int], ancestor: Optional[int]) -> bool:
    """Whether *pid* is still in the desktop supervisor's process tree."""
    if pid is None or ancestor is None or pid <= 0 or ancestor <= 0:
        return False
    current = pid
    seen: set[int] = set()
    for _ in range(64):
        if current == ancestor:
            return True
        if current in seen:
            return False
        seen.add(current)
        parent = _process_parent_pid(current)
        if parent is None or parent == 1:
            return False
        current = parent
    return False


def _browser_belongs_to_state(pid: Optional[int], state: Dict[str, str]) -> bool:
    """Reject a browser PID copied into state by another desktop generation."""
    if pid is None:
        return False
    session_pid = _int_value(state.get("SESSION_PID"))
    if session_pid is not None:
        return _pid_is_descendant(pid, session_pid)
    # Legacy state written before SESSION_PID existed remains usable, but a
    # configured browser must still agree with the state rather than silently
    # claiming a different running desktop.
    configured_pid = _int_value(state.get("BROWSER_PID"))
    return configured_pid is None or configured_pid == pid


def _proc_uid(pid: int) -> Optional[int]:
    try:
        return Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return None


def _read_process_env(pid: int) -> Dict[str, str]:
    if _proc_uid(pid) != _current_uid():
        return {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if not separator:
            continue
        try:
            name = key.decode("utf-8", "strict")
            text = value.decode("utf-8", "strict")
        except UnicodeError:
            continue
        if name in _PROCESS_ENV_KEYS and "\x00" not in text and len(text) <= 4096:
            result[name] = text
    return result


def _read_process_cmdline(pid: int) -> List[str]:
    if _proc_uid(pid) != _current_uid():
        return []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    try:
        return [part.decode("utf-8", "strict") for part in raw.split(b"\0") if part]
    except UnicodeError:
        return []


def _read_process_cgroup(pid: int) -> str:
    if _proc_uid(pid) != _current_uid():
        return ""
    try:
        return (
            Path(f"/proc/{pid}/cgroup")
            .read_text(encoding="utf-8", errors="ignore")
            .lower()
        )
    except OSError:
        return ""


def _process_start_token(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return fields[21] if len(fields) > 21 else ""
    except (OSError, UnicodeError):
        return ""


def _browser_from_pid(pid: Optional[int], rank: int = 0) -> Optional[_BrowserProcess]:
    if pid is None or _proc_uid(pid) != _current_uid():
        return None
    cmdline = _read_process_cmdline(pid)
    if not cmdline or any(arg.startswith("--type=") for arg in cmdline[1:]):
        return None
    if Path(cmdline[0]).name.lower() not in _BROWSER_BASENAMES:
        return None
    env = _read_process_env(pid)
    cgroup = _read_process_cgroup(pid)
    excluded = -100 if any(hint in cgroup for hint in _EXCLUDED_CGROUP_HINTS) else 0
    service_rank = 1 if any(hint in cgroup for hint in _BROWSER_CGROUP_HINTS) else 0
    remote_rank = int(
        any(arg.startswith("--remote-debugging-port=") for arg in cmdline)
    )
    return _BrowserProcess(pid, env, (rank + excluded, service_rank, remote_rank, pid))


def _iter_browser_processes() -> Iterable[_BrowserProcess]:
    # Hermetic tests intentionally run beside the production desktop. Do not
    # import the host browser's environment into an isolated test process.
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    result: List[_BrowserProcess] = []
    for entry in entries:
        if entry.name.isdigit():
            candidate = _browser_from_pid(int(entry.name))
            if candidate is not None:
                result.append(candidate)
    return result


def _find_browser(state: Dict[str, str]) -> Optional[_BrowserProcess]:
    configured_pid = _int_value(state.get("BROWSER_PID"))
    configured = _browser_from_pid(configured_pid, rank=1000)
    if configured is not None and _browser_belongs_to_state(configured.pid, state):
        return configured
    candidates = list(_iter_browser_processes())
    session_pid = _int_value(state.get("SESSION_PID"))
    if session_pid is not None:
        session_candidates = [
            candidate
            for candidate in candidates
            if _pid_is_descendant(candidate.pid, session_pid)
        ]
        if session_candidates:
            session_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
            return session_candidates[0]
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[0] if candidates else None


def _address_path(address: str) -> Optional[Path]:
    match = _DBUS_PATH_RE.search(address or "")
    if not match:
        return None
    path = match.group(1)
    return Path(path) if path and "\x00" not in path and "\n" not in path else None


def _address_is_valid(address: str, *, require_owner: bool = True) -> bool:
    address = (address or "").strip()
    if not _DBUS_ADDRESS_RE.fullmatch(address):
        return False
    path = _address_path(address)
    if path is None:
        return "abstract=" in address
    try:
        info = path.stat()
    except OSError:
        return False
    return not require_owner or info.st_uid == _current_uid()


def _address_is_usable(address: str, env: Optional[Dict[str, str]] = None) -> bool:
    """Check the bus endpoint, not only the socket pathname.

    A restarted session bus can leave a socket path behind while the address
    inherited by Hermes no longer accepts requests.  ``dbus-send`` is used as
    a small, secret-free liveness probe when available; path validation remains
    the compatibility fallback on minimal Linux images without the utility.
    """
    if not _address_is_valid(address):
        return False
    probe = shutil.which("dbus-send")
    if not probe:
        return True
    probe_env = _minimal_helper_env(dict(env or os.environ))
    probe_env["DBUS_SESSION_BUS_ADDRESS"] = address
    try:
        result = subprocess.run(
            [
                probe,
                "--session",
                "--print-reply",
                "--dest=org.freedesktop.DBus",
                "/",
                "org.freedesktop.DBus.ListNames",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            env=probe_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _valid_runtime_dir(value: Optional[str]) -> Optional[str]:
    if not value or "\x00" in value or "\n" in value:
        return None
    path = Path(value)
    try:
        info = path.stat()
    except OSError:
        return None
    return str(path) if path.is_dir() and info.st_uid == _current_uid() else None


def _minimal_helper_env(env: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key in _DESKTOP_ENV_KEYS and isinstance(value, str) and "\x00" not in value
    }


def _merge_path(*values: Optional[str]) -> str:
    entries: List[str] = []
    for value in values:
        for entry in (value or "").split(os.pathsep):
            if entry and entry not in entries:
                entries.append(entry)
    return os.pathsep.join(entries)


def _runtime_bus_address(runtime: Optional[str]) -> str:
    if not runtime:
        return ""
    path = Path(runtime) / "bus"
    try:
        info = path.stat()
    except OSError:
        return ""
    return (
        f"unix:path={path}"
        if path.is_socket() and info.st_uid == _current_uid()
        else ""
    )


def _at_spi_bus_address(runtime: Optional[str]) -> str:
    if not runtime:
        return ""
    try:
        candidates = list((Path(runtime) / "at-spi").glob("bus_*"))
    except OSError:
        return ""
    valid: List[Tuple[float, Path]] = []
    for path in candidates:
        try:
            info = path.stat()
        except OSError:
            continue
        if path.is_socket() and info.st_uid == _current_uid():
            valid.append((info.st_mtime, path))
    if not valid:
        return ""
    return f"unix:path={max(valid, key=lambda item: item[0])[1]}"


def _launch_session_bus(env: Dict[str, str]) -> bool:
    launcher = shutil.which("dbus-launch") or "/usr/bin/dbus-launch"
    try:
        proc = subprocess.run(
            [launcher, "--sh-syntax", "--exit-with-x11"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
            env=_minimal_helper_env(env),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = _DBUS_OUTPUT_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not match:
        return False
    address = next((value for value in match.groups() if value), "")
    if not _address_is_valid(address):
        return False
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    pid_match = _DBUS_PID_RE.search(proc.stdout or "")
    if pid_match:
        env["DBUS_SESSION_BUS_PID"] = pid_match.group(1)
    return True


def _launch_at_spi(env: Dict[str, str]) -> None:
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    key = (env.get("DISPLAY", ""), address)
    runtime = _valid_runtime_dir(env.get("XDG_RUNTIME_DIR"))
    if not address:
        return
    with _AT_SPI_LAUNCH_LOCK:
        if key in _AT_SPI_LAUNCHED_FOR:
            existing = _at_spi_bus_address(runtime)
            if existing and _address_is_usable(existing, env):
                env["AT_SPI_BUS_ADDRESS"] = existing
                return
            # The AT-SPI launcher may have died while DBus and DISPLAY stayed
            # up. Do not let an in-process memo turn that stale bus into a
            # permanent failure; the next transport must repair it.
            _AT_SPI_LAUNCHED_FOR.discard(key)
        launcher = "/usr/libexec/at-spi-bus-launcher"
        if not Path(launcher).exists():
            launcher = shutil.which("at-spi-bus-launcher") or ""
        if not launcher:
            return
        try:
            process = subprocess.Popen(
                [launcher, "--launch-immediately"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_minimal_helper_env(env),
                start_new_session=True,
            )
        except OSError:
            return
        env["AT_SPI_BUS_PID"] = str(process.pid)
        for _ in range(20):
            address = _at_spi_bus_address(runtime)
            if address and _address_is_usable(address, env):
                env["AT_SPI_BUS_ADDRESS"] = address
                _AT_SPI_LAUNCHED_FOR.add(key)
                return
            time.sleep(0.05)


def _write_state(
    values: Dict[str, str], existing: Optional[Dict[str, str]] = None
) -> None:
    path = _state_path()
    data = dict(existing or {})
    for key, value in values.items():
        if key not in _STATE_KEY_SET:
            continue
        if isinstance(value, str) and value:
            data[key] = value
        else:
            data.pop(key, None)
    data = {
        key: str(value)
        for key, value in data.items()
        if key in _STATE_KEY_SET
        and isinstance(value, str)
        and value
        and "\x00" not in value
        and "\n" not in value
        and len(value) <= 4096
    }
    data.setdefault("GENERATION", f"{os.getpid()}-{time.time_ns()}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".desktop-session-", dir=str(path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for key in _STATE_KEYS:
                    if key in data:
                        stream.write(f"{key}={data[key]}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        # State is a reconnect hint; the live environment remains authoritative.
        return


def _resolve_desktop_context(
    base_env: Dict[str, str],
) -> Tuple[Dict[str, str], Optional[int], Dict[str, str]]:
    state = _read_state()
    browser = _find_browser(state)
    session_env = _read_process_env(_int_value(state.get("SESSION_PID")))
    env = dict(base_env)
    browser_pid: Optional[int] = None

    display_candidates = []
    if browser is not None:
        browser_pid = browser.pid
        display_candidates.append(browser.env.get("DISPLAY", ""))
    display_candidates.extend((
        session_env.get("DISPLAY", ""),
        state.get("DISPLAY", ""),
        base_env.get("DISPLAY", ""),
    ))
    display = next(
        (
            candidate
            for candidate in display_candidates
            if _display_has_socket(candidate)
        ),
        "",
    )
    if not display and browser is None:
        displays = _iter_x11_displays()
        if len(displays) == 1:
            display = f":{displays[0][0]}"
    if display:
        env["DISPLAY"] = display
    else:
        env.pop("DISPLAY", None)

    if browser is not None:
        for key in ("XDG_RUNTIME_DIR", "XAUTHORITY", "HOME"):
            if browser.env.get(key):
                env[key] = browser.env[key]
        for key in ("XDG_RUNTIME_DIR", "XAUTHORITY", "HOME"):
            if not env.get(key) and session_env.get(key):
                env[key] = session_env[key]
        path = _merge_path(browser.env.get("PATH"), base_env.get("PATH"))
        if path:
            env["PATH"] = path
    for key in ("XDG_RUNTIME_DIR", "XAUTHORITY", "HOME"):
        if not env.get(key) and session_env.get(key):
            env[key] = session_env[key]
    if session_env.get("PATH"):
        env["PATH"] = _merge_path(env.get("PATH"), session_env.get("PATH"))

    runtime_candidates = []
    if browser is not None:
        runtime_candidates.append(browser.env.get("XDG_RUNTIME_DIR", ""))
    runtime_candidates.append(session_env.get("XDG_RUNTIME_DIR", ""))
    runtime_candidates.extend((
        state.get("XDG_RUNTIME_DIR", ""),
        base_env.get("XDG_RUNTIME_DIR", ""),
    ))
    runtime = next(
        (
            candidate
            for candidate in runtime_candidates
            if _valid_runtime_dir(candidate)
        ),
        None,
    )
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    else:
        env.pop("XDG_RUNTIME_DIR", None)

    bus_candidates = []
    if browser is not None:
        bus_candidates.append(browser.env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    bus_candidates.append(session_env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    bus_candidates.extend((
        state.get("DBUS_SESSION_BUS_ADDRESS", ""),
        base_env.get("DBUS_SESSION_BUS_ADDRESS", ""),
    ))
    bus_candidates.append(_runtime_bus_address(runtime))
    bus = next(
        (
            candidate
            for candidate in bus_candidates
            if _address_is_usable(candidate, env)
        ),
        "",
    )
    if bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    else:
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        env.pop("DBUS_SESSION_BUS_PID", None)

    at_spi_candidates = []
    if browser is not None:
        at_spi_candidates.append(browser.env.get("AT_SPI_BUS_ADDRESS", ""))
    at_spi_candidates.append(session_env.get("AT_SPI_BUS_ADDRESS", ""))
    at_spi_candidates.extend((
        state.get("AT_SPI_BUS_ADDRESS", ""),
        base_env.get("AT_SPI_BUS_ADDRESS", ""),
    ))
    at_spi_candidates.append(_at_spi_bus_address(runtime))
    at_spi = next(
        (
            candidate
            for candidate in at_spi_candidates
            if _address_is_usable(candidate, env)
        ),
        "",
    )
    if at_spi:
        env["AT_SPI_BUS_ADDRESS"] = at_spi
    else:
        env.pop("AT_SPI_BUS_ADDRESS", None)
        env.pop("AT_SPI_BUS_PID", None)
    return env, browser_pid, state


def desktop_session_child_env(
    base_env: Optional[Dict[str, str]] = None,
    *,
    repair: bool = True,
) -> Dict[str, str]:
    """Return the allow-listed desktop context for a cua-driver child."""
    env = dict(base_env if base_env is not None else os.environ)
    if not sys.platform.startswith("linux"):
        return env

    env, browser_pid, state = _resolve_desktop_context(env)
    if (
        repair
        and env.get("DISPLAY")
        and not _address_is_valid(env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    ):
        _launch_session_bus(env)
    if (
        repair
        and env.get("DISPLAY")
        and _address_is_valid(env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    ):
        if not _address_is_valid(env.get("AT_SPI_BUS_ADDRESS", "")):
            _launch_at_spi(env)
        # A launcher may have created the address after the initial resolve.
        if not env.get("AT_SPI_BUS_ADDRESS"):
            at_spi = _at_spi_bus_address(env.get("XDG_RUNTIME_DIR"))
            if at_spi:
                env["AT_SPI_BUS_ADDRESS"] = at_spi

    if env.get("DISPLAY") and browser_pid and _browser_belongs_to_state(browser_pid, state):
        _write_state(
            {
                "DISPLAY": env.get("DISPLAY", ""),
                "XDG_RUNTIME_DIR": env.get("XDG_RUNTIME_DIR", ""),
                "DBUS_SESSION_BUS_ADDRESS": env.get("DBUS_SESSION_BUS_ADDRESS", ""),
                "DBUS_SESSION_BUS_PID": env.get("DBUS_SESSION_BUS_PID", ""),
                "AT_SPI_BUS_ADDRESS": env.get("AT_SPI_BUS_ADDRESS", ""),
                "AT_SPI_BUS_PID": env.get("AT_SPI_BUS_PID", ""),
                "BROWSER_PID": str(browser_pid),
            },
            existing=state,
        )
    return env


def _file_identity(path: Optional[Path]) -> Tuple[int, int, int]:
    if path is None:
        return (0, 0, 0)
    try:
        info = path.stat()
    except OSError:
        return (0, 0, 0)
    return (info.st_dev, info.st_ino, info.st_mtime_ns)


def _bus_identity(address: str) -> Tuple[int, int, int]:
    return _file_identity(_address_path(address))


def desktop_session_fingerprint(
    base_env: Optional[Dict[str, str]] = None,
) -> Tuple[object, ...]:
    """Return a secret-free identity for reconnect detection."""
    env, browser_pid, state = _resolve_desktop_context(dict(base_env or os.environ))
    pid = browser_pid or _int_value(state.get("BROWSER_PID")) or 0
    return (
        env.get("DISPLAY", ""),
        pid,
        _process_start_token(pid) if pid else "",
        _file_identity(_display_socket(env.get("DISPLAY", ""))),
        _bus_identity(env.get("DBUS_SESSION_BUS_ADDRESS", "")),
        _bus_identity(env.get("AT_SPI_BUS_ADDRESS", "")),
        state.get("GENERATION", ""),
    )


__all__ = ["desktop_session_child_env", "desktop_session_fingerprint"]
