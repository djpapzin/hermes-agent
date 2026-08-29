#!/usr/bin/python3
"""Root-owned validator for unprivileged Hermes worker scopes."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


WORKER_UID = 996
WORKER_GID = 996
ENV_ROOT = Path("/home/ubuntu/.hermes/state/worker-env")
WORKER_SLICE = "hermes-workers.slice"
MAX_AGGREGATE_MEMORY_BYTES = 10 * 1024 * 1024 * 1024
MIN_MEMORY_BYTES = 64 * 1024 * 1024
MAX_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENV_BYTES = 128 * 1024
_SUFFIX_RE = re.compile(r"[A-Za-z0-9_.-]{1,160}")
_UNIT_RE = re.compile(r"hermes-worker-system-[A-Za-z0-9_.-]{1,160}\.scope")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise ValueError("worker scopes require POSIX effective uid support")
    return int(getter())


def _fail(message: str) -> int:
    print(f"hermes-worker-scope: {message}", file=sys.stderr)
    return 2


def _validate_caller() -> None:
    if _effective_uid() != 0:
        raise ValueError("must run as root through sudo")
    if os.environ.get("SUDO_UID") != str(WORKER_UID):
        raise ValueError("caller is not the Hermes runtime uid")


def _validate_suffix(value: str) -> str:
    if not _SUFFIX_RE.fullmatch(value or ""):
        raise ValueError("invalid worker scope suffix")
    return value


def _validate_unit(value: str) -> str:
    if not _UNIT_RE.fullmatch(value or ""):
        raise ValueError("invalid worker scope unit")
    return value


def _read_environment(path_text: str, *, consume: bool = True) -> dict[str, str]:
    path = Path(path_text)
    if path.parent.resolve() != ENV_ROOT.resolve() or path.suffix != ".json":
        raise ValueError("environment payload is outside the worker-env directory")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("environment payload is not a regular file")
        if info.st_uid != WORKER_UID:
            raise ValueError("environment payload has the wrong owner")
        if info.st_mode & 0o077:
            raise ValueError("environment payload is not private")
        if info.st_size > MAX_ENV_BYTES:
            raise ValueError("environment payload is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            payload = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
        if consume:
            try:
                path.unlink()
            except OSError:
                pass
    if not isinstance(payload, dict) or len(payload) > 256:
        raise ValueError("environment payload must be a bounded object")
    result: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
            raise ValueError("environment payload contains an invalid key")
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError("environment payload contains an invalid value")
        result[key] = value
    return result


def _validate_worker_slice() -> None:
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--system",
            "show",
            WORKER_SLICE,
            "--property=LoadState",
            "--property=MemoryMax",
            "--property=MemoryHigh",
            "--property=MemorySwapMax",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    memory_max = values.get("MemoryMax", "")
    memory_high = values.get("MemoryHigh", "")
    swap_max = values.get("MemorySwapMax", "")
    if (
        result.returncode != 0
        or values.get("LoadState") != "loaded"
        or not memory_max.isdigit()
        or int(memory_max) > MAX_AGGREGATE_MEMORY_BYTES
        or not memory_high.isdigit()
        or int(memory_high) > int(memory_max)
        or swap_max != "0"
    ):
        raise ValueError("aggregate Hermes worker slice is absent or unbounded")


def _run(argv: list[str]) -> int:
    if len(argv) < 6 or argv[4] != "--":
        raise ValueError("run requires SUFFIX MEMORY_BYTES ENV_JSON -- COMMAND")
    suffix = _validate_suffix(argv[1])
    try:
        memory_max = int(argv[2])
    except ValueError as exc:
        raise ValueError("memory limit is not an integer") from exc
    if not MIN_MEMORY_BYTES <= memory_max <= MAX_MEMORY_BYTES:
        raise ValueError("memory limit is outside the approved range")
    command = argv[5:]
    if not command:
        raise ValueError("worker command is empty")
    _validate_worker_slice()
    _read_environment(argv[3], consume=False)
    env_path = Path(argv[3])
    unit = f"hermes-worker-system-{suffix}"
    memory_high = max(MIN_MEMORY_BYTES, memory_max * 9 // 10)
    systemd_argv = [
        "/usr/bin/systemd-run",
        "--system",
        "--scope",
        "--quiet",
        "--unit",
        unit,
        "--slice",
        WORKER_SLICE,
        "--collect",
        "--uid",
        str(WORKER_UID),
        "--gid",
        str(WORKER_GID),
        "--property",
        "MemoryAccounting=yes",
        "--property",
        f"MemoryMax={memory_max}",
        "--property",
        f"MemoryHigh={memory_high}",
        "--property",
        "MemorySwapMax=0",
        "--",
        str(Path(__file__).resolve()),
        "exec",
        str(env_path),
        "--",
        *command,
    ]
    try:
        return subprocess.run(systemd_argv, check=False).returncode
    finally:
        try:
            env_path.unlink()
        except OSError:
            pass


def _exec_worker(argv: list[str]) -> int:
    if _effective_uid() != WORKER_UID:
        raise ValueError("worker exec must run as the Hermes runtime uid")
    if len(argv) < 4 or argv[2] != "--":
        raise ValueError("exec requires ENV_JSON -- COMMAND")
    environment = _read_environment(argv[1])
    command = argv[3:]
    if not command:
        raise ValueError("worker command is empty")
    os.execvpe(command[0], command, environment)
    return 127


def _stop(argv: list[str]) -> int:
    if len(argv) != 2:
        raise ValueError("stop requires exactly one worker scope unit")
    unit = _validate_unit(argv[1])
    return subprocess.run(
        ["/usr/bin/systemctl", "--system", "stop", unit], check=False
    ).returncode


def _oom_evidence(argv: list[str]) -> int:
    """Emit a bounded OOM attestation for one recent worker scope."""

    if len(argv) != 3:
        raise ValueError("evidence requires UNIT SINCE_EPOCH")
    unit = _validate_unit(argv[1])
    try:
        since = int(argv[2])
    except ValueError as exc:
        raise ValueError("evidence timestamp is not an integer") from exc
    now = int(time.time())
    if since < now - 300 or since > now + 5:
        raise ValueError("evidence timestamp is outside the recent proof window")
    result = subprocess.run(
        [
            "/usr/bin/journalctl",
            "--unit",
            unit,
            "--since",
            f"@{since}",
            "--no-pager",
            "--output=cat",
            "--lines=100",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    marker = "A process of this unit has been killed by the OOM killer."
    observed = result.returncode == 0 and any(
        line.strip() in {marker, f"{unit}: {marker}"}
        for line in result.stdout.splitlines()
    )
    print(json.dumps({"unit": unit, "oom_kill": observed}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "exec":
            return _exec_worker(args)
        _validate_caller()
        if not args:
            raise ValueError("expected run or stop")
        if args[0] == "run":
            return _run(args)
        if args[0] == "stop":
            return _stop(args)
        if args[0] == "evidence":
            return _oom_evidence(args)
        raise ValueError("expected run, stop, or evidence")
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
