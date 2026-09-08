"""Resolve and repair the Linux desktop session used by computer use.

Hermes has two independent lifetimes on a headless Linux host: the browser
desktop owned by the gateway and the short-lived cua-driver stdio transport.
This module is deliberately limited to the former's environment.  It never
owns a browser, never talks to CDP, and never copies arbitrary process
environment values into a child.

The persistent desktop supervisor writes a small allow-listed state file.  A
Hermes process can also recover a missing session bus for an already-running
X11 desktop; the supervisor remains the source of truth for browser restarts
and reboot recovery.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")
_DBUS_PATH_RE = re.compile(r"(?:^|[:,])path=([^,]+)")
_DBUS_ADDRESS_RE = re.compile(
    r"^unix:(?:path=[^,]+|abstract=[^,]+)(?:,[^,=]+=[^,]*)*$"
)
_DBUS_OUTPUT_RE = re.compile(
    r"^DBUS_SESSION_BUS_ADDRESS=(?:'([^']*)'|\"([^\"]*)\"|([^\s;]+))",
    re.MULTILINE,
)
_DBUS_PID_RE = re.compile(r"^DBUS_SESSION_BUS_PID=(?:'?(\d+)'?)", re.MULTILINE)

# Values written by the supervisor or by the repair path.  In particular,
# command lines, URLs, cookies and provider environment variables are not
# persisted.
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
    "chrome",
    "chromium",
    "chromium-browser",
    "brave",
    "brave-browser",
    "microsoft-edge",
    "firefox",
    "browser",
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


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _state_path() -> Path:
    """Return the gateway-owned state path without introducing new config."""
    return _hermes_home() / "state" / "computer_use" / "desktop-session.env"


def _x11_socket_dirs() -> Tuple[Path, ...]:
    uid = os.getuid()
    return (Path("/tmp/.X11-unix"), Path(f"/run/user/{uid}/.X11-unix"))


def _parse_display(display: str) -> Optional[int]:
    match = _DISPLAY_RE.fullmatch((display or "").strip())
    if not match:
        return None
    return int(match.group(1))


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


def _iter_x11_displays() -> List[Tuple[int, Path, float, int]]:
    entries: List[Tuple[int, Path, float, int]] = []
    seen: set[Tuple[int, str]] = set()
    uid = os.getuid()
    for base in _x11_socket_dirs():
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for entry in children:
            if not entry.name.startswith("X") or not entry.name[1:].isdigit():
                continue
            display = int(entry.name[1:])
            key = (display, str(entry))
            if key in seen:
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            # Do not attach a gateway process to another user's X server.
            if info.st_uid != uid:
                continue
            if not (entry.is_socket() or entry.is_file() or entry.is_char_device()):
                continue
            seen.add(key)
            entries.append((display, entry, info.st_mtime, info.st_uid))
    entries.sort(key=lambda item: (item[2], item[0]), reverse=True)
    return entries


def _read_state() -> Dict[str, str]:
    path = _state_path()
    try:
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return {}
        raw = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return {}

    state: Dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _STATE_KEY_SET:
            continue
        value = value.strip()
        if "\x00" in value or len(value) > 4096:
            continue
        state[key] = value
    return state


def _int_value(value: Optional[str]) -> Optional[int]:
    if not value or not value.isdigit():
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result > 0 else None


def _proc_uid(pid: int) -> Optional[int]:
    try:
        return (Path(f"/proc/{pid}").stat()).st_uid
    except OSError:
        return None


def _read_process_env(pid: int) -> Dict[str, str]:
    if _proc_uid(pid) != os.getuid():
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
    if _proc_uid(pid) != os.getuid():
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
    if _proc_uid(pid) != os.getuid():
        return ""
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return ""


def _process_start_token(pid: int) -> str:
    """Return Linux proc start time so PID reuse changes the fingerprint."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return fields[21] if len(fields) > 21 else ""
    except (OSError, UnicodeError):
        return ""


def _browser_from_pid(pid: Optional[int], rank: int = 1000) -> Optional[_BrowserProcess]:
    if pid is None or _proc_uid(pid) != os.getuid():
        return None
    cmdline = _read_process_cmdline(pid)
    if not cmdline or any(arg.startswith("--type=") for arg in cmdline[1:]):
        return None
    basename = Path(cmdline[0]).name.lower()
    if basename not in _BROWSER_BASENAMES and not any(
        arg.startswith("--remote-debugging-port=") for arg in cmdline
    ):
        return None
    env = _read_process_env(pid)
    cgroup = _read_process_cgroup(pid)
    if any(hint in cgroup for hint in _EXCLUDED_CGROUP_HINTS):
        exclusion = -100
    else:
        exclusion = 0
    service_rank = 1 if any(hint in cgroup for hint in _BROWSER_CGROUP_HINTS) else 0
    remote_rank = 1 if any(arg.startswith("--remote-debugging-port=") for arg in cmdline) else 0
    return _BrowserProcess(
        pid=pid,
        env=env,
        score=(rank + exclusion, service_rank, remote_rank, pid),
    )


def _iter_browser_processes() -> Iterable[_BrowserProcess]:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    result: List[_BrowserProcess] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        candidate = _browser_from_pid(int(entry.name), rank=0)
        if candidate is not None:
            result.append(candidate)
    return result


def _find_browser(state: Dict[str, str]) -> Optional[_BrowserProcess]:
    configured = _browser_from_pid(_int_value(state.get("BROWSER_PID")))
    if configured is not None:
        return configured
    candidates = list(_iter_browser_processes())
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[0]


def _address_path(address: str) -> Optional[Path]:
    match = _DBUS_PATH_RE.search(address or "")
    if not match:
        return None
    path = match.group(1)
    if not path or "\x00" in path or "\n" in path:
        return None
    return Path(path)


def _address_is_valid(address: str, *, require_owner: bool = True) -> bool:
    address = (address or "").strip()
    if not _DBUS_ADDRESS_RE.fullmatch(address):
        return False
    path = _address_path(address)
    if path is None:
        # Abstract sockets cannot be checked with Path. The strict syntax
        # check above keeps malformed/stale inherited values out of the
        # child; the driver will perform the actual bus handshake.
        return "abstract=" in address
    try:
        info = path.stat()
    except OSError:
        return False
    return not require_owner or info.st_uid == os.getuid()


def _valid_runtime_dir(value: Optional[str]) -> Optional[str]:
    if not value or "\x00" in value or "\n" in value:
        return None
    path = Path(value)
    try:
        info = path.stat()
    except OSError:
        return None
    if not path.is_dir() or info.st_uid != os.getuid():
        return None
    return str(path)


def _minimal_helper_env(env: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key in _DESKTOP_ENV_KEYS and isinstance(value, str) and "\x00" not in value
    }


def _launch_session_bus(env: Dict[str, str]) -> bool:
    launcher = shutil.which("dbus-launch") or "/usr/bin/dbus-launch"
    helper_env = _minimal_helper_env(env)
    try:
        proc = subprocess.run(
            [launcher, "--sh-syntax", "--exit-with-x11"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
            env=helper_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    match = _DBUS_OUTPUT_RE.search(proc.stdout or "")
    if not match:
        return False
    address = next((value for value in match.groups() if value), "")
    if not _address_is_valid(address, require_owner=True):
        return False
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    pid_match = _DBUS_PID_RE.search(proc.stdout or "")
    if pid_match:
        env["DBUS_SESSION_BUS_PID"] = pid_match.group(1)
    return True


def _launch_at_spi(env: Dict[str, str]) -> None:
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    key = (env.get("DISPLAY", ""), address)
    if not address or key in _AT_SPI_LAUNCHED_FOR:
        return
    with _AT_SPI_LAUNCH_LOCK:
        if key in _AT_SPI_LAUNCHED_FOR:
            return
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
        # The launcher normally daemonises. Retain only a PID as an
        # allow-listed diagnostic; never retain its output or command line.
        env["AT_SPI_BUS_PID"] = str(process.pid)
        _AT_SPI_LAUNCHED_FOR.add(key)


def _write_state(values: Dict[str, str], existing: Optional[Dict[str, str]] = None) -> None:
    path = _state_path()
    data = dict(existing or {})
    for key, value in values.items():
        if key not in _STATE_KEY_SET:
            continue
        if isinstance(value, str) and value:
            data[key] = value
        else:
            # A stale address/PID must not survive a repair pass merely
            # because the state file was used as the merge base.
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
    if "GENERATION" not in data:
        data["GENERATION"] = f"{os.getpid()}-{time.time_ns()}"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".desktop-session-", dir=str(path.parent))
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
        # State is an optimization and a reconnect hint. The live env still
        # goes to cua-driver when persistence is unavailable.
        return


def _resolve_desktop_context(base_env: Dict[str, str]) -> Tuple[Dict[str, str], Optional[int], Dict[str, str]]:
    state = _read_state()
    browser = _find_browser(state)
    env = dict(base_env)
    browser_pid: Optional[int] = None

    # A browser process is the authoritative source for DISPLAY. This avoids
    # selecting a newer but unrelated X socket (for example a test desktop).
    if browser is not None:
        browser_pid = browser.pid
        process_env = browser.env
        display = process_env.get("DISPLAY", "")
        if not _display_has_socket(display):
            display = state.get("DISPLAY", "")
        if _display_has_socket(display):
            env["DISPLAY"] = display
        else:
            env.pop("DISPLAY", None)
        for key in ("XDG_RUNTIME_DIR", "XAUTHORITY", "HOME", "PATH"):
            value = process_env.get(key)
            if value:
                env[key] = value
    else:
        display = env.get("DISPLAY", "")
        if not _display_has_socket(display):
            env.pop("DISPLAY", None)
            displays = _iter_x11_displays()
            # A socket-only fallback is safe only when there is one eligible
            # desktop. Never use mtime to guess among multiple sessions.
            if len(displays) == 1:
                env["DISPLAY"] = f":{displays[0][0]}"

    # Prefer the browser's bus, then the supervisor state, then the caller's
    # bus. All path buses must belong to this uid; this rejects another user's
    # /run/user/<uid>/bus even when it exists.
    bus_candidates = []
    if browser is not None:
        bus_candidates.append(browser.env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    bus_candidates.append(state.get("DBUS_SESSION_BUS_ADDRESS", ""))
    bus_candidates.append(base_env.get("DBUS_SESSION_BUS_ADDRESS", ""))
    bus = next((candidate for candidate in bus_candidates if _address_is_valid(candidate)), "")
    if bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    else:
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        env.pop("DBUS_SESSION_BUS_PID", None)

    runtime_candidates = []
    if browser is not None:
        runtime_candidates.append(browser.env.get("XDG_RUNTIME_DIR", ""))
    runtime_candidates.extend((state.get("XDG_RUNTIME_DIR", ""), base_env.get("XDG_RUNTIME_DIR", "")))
    runtime = next((candidate for candidate in runtime_candidates if _valid_runtime_dir(candidate)), None)
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    else:
        env.pop("XDG_RUNTIME_DIR", None)

    at_spi_candidates = []
    if browser is not None:
        at_spi_candidates.append(browser.env.get("AT_SPI_BUS_ADDRESS", ""))
    at_spi_candidates.extend((state.get("AT_SPI_BUS_ADDRESS", ""), base_env.get("AT_SPI_BUS_ADDRESS", "")))
    at_spi = next((candidate for candidate in at_spi_candidates if _address_is_valid(candidate)), "")
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
    """Return a safe desktop environment for a cua-driver child.

    On Linux, browser process state wins over inherited values. Missing buses
    are repaired only after a valid display is found, and helper processes are
    spawned with an allow-listed environment. Other platforms retain the
    caller's environment unchanged.
    """
    env = dict(base_env if base_env is not None else os.environ)
    if os.name != "posix" or not sys_platform_linux():
        return env

    env, browser_pid, state = _resolve_desktop_context(env)
    if repair and env.get("DISPLAY") and not _address_is_valid(env.get("DBUS_SESSION_BUS_ADDRESS", "")):
        _launch_session_bus(env)
        if env.get("DBUS_SESSION_BUS_ADDRESS"):
            _launch_at_spi(env)
    if env.get("DISPLAY"):
        values = {
            "DISPLAY": env["DISPLAY"],
            "BROWSER_PID": str(browser_pid) if browser_pid else state.get("BROWSER_PID", ""),
            "XDG_RUNTIME_DIR": env.get("XDG_RUNTIME_DIR", ""),
            "DBUS_SESSION_BUS_ADDRESS": env.get("DBUS_SESSION_BUS_ADDRESS", ""),
            "DBUS_SESSION_BUS_PID": env.get("DBUS_SESSION_BUS_PID", ""),
            "AT_SPI_BUS_ADDRESS": env.get("AT_SPI_BUS_ADDRESS", ""),
            "AT_SPI_BUS_PID": env.get("AT_SPI_BUS_PID", ""),
        }
        if browser_pid:
            _write_state(values, existing=state)
    return env


def sys_platform_linux() -> bool:
    # Kept as a function for tests and to avoid importing platform-specific
    # modules in the computer-use optional dependency path.
    return os.sys.platform.startswith("linux")


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


def desktop_session_fingerprint(base_env: Optional[Dict[str, str]] = None) -> Tuple[object, ...]:
    """Return a cheap identity for the desktop transport binding.

    The fingerprint contains no secret values and changes when the browser
    PID, X socket, DBus socket, AT-SPI socket, or supervisor generation
    changes. It is used to reconnect cua-driver without restarting the parent
    Hermes job.
    """
    env, browser_pid, state = _resolve_desktop_context(
        dict(base_env if base_env is not None else os.environ)
    )
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
