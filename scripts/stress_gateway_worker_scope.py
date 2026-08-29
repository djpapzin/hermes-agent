#!/usr/bin/env python3
"""Bounded cgroup-v2 worker OOM proof that never targets the gateway unit."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


_SYSTEM_SCOPE_WRAPPER = Path("/usr/local/sbin/hermes-worker-scope")


@dataclass(frozen=True)
class ServiceState:
    active_state: str
    main_pid: int
    restarts: int


def _current_posix_ids() -> tuple[int, int]:
    uid_fn = getattr(os, "getuid", None)
    gid_fn = getattr(os, "getgid", None)
    if not callable(uid_fn) or not callable(gid_fn):
        raise RuntimeError("the cgroup proof requires POSIX uid/gid support")
    return int(uid_fn()), int(gid_fn())


def validate_bounds(memory_max_mb: int, allocation_mb: int) -> None:
    if not 64 <= memory_max_mb <= 96:
        raise ValueError("memory-max-mb must be between 64 and 96")
    if not memory_max_mb < allocation_mb <= 128:
        raise ValueError(
            "allocation-mb must exceed memory-max-mb and be at most 128"
        )


def build_scope_command(
    *,
    backend: str,
    unit: str,
    memory_max_mb: int,
    allocation_mb: int,
    uid: int,
    gid: int,
    environment_path: Path | None = None,
) -> list[str]:
    validate_bounds(memory_max_mb, allocation_mb)
    systemd_run = shutil.which("systemd-run") or "/usr/bin/systemd-run"
    prefix = [systemd_run, "--user"]
    if backend == "system":
        if environment_path is None:
            raise ValueError("system proof requires a private environment payload")
        sudo = shutil.which("sudo") or "/usr/bin/sudo"
        memory_max = memory_max_mb * 1024 * 1024
        code = (
            "import time; "
            f"payload=bytearray({allocation_mb}*1024*1024); "
            "time.sleep(0.25); print(len(payload))"
        )
        return [
            sudo,
            "-n",
            str(_SYSTEM_SCOPE_WRAPPER),
            "run",
            unit,
            str(memory_max),
            str(environment_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    elif backend != "user":
        raise ValueError("backend must be user or system")

    memory_max = memory_max_mb * 1024 * 1024
    code = (
        "import time; "
        f"payload=bytearray({allocation_mb}*1024*1024); "
        "time.sleep(0.25); print(len(payload))"
    )
    command = [
        *prefix,
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        unit,
        "--property",
        "MemoryAccounting=yes",
        "--property",
        f"MemoryHigh={memory_max}",
        "--property",
        f"MemoryMax={memory_max}",
        "--property",
        "MemorySwapMax=0",
    ]
    if backend == "system":
        command.extend(["--uid", str(uid), "--gid", str(gid)])
    command.extend(["--", sys.executable, "-c", code])
    return command


def service_state(unit: str) -> ServiceState:
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=ActiveState",
            "--property=MainPID",
            "--property=NRestarts",
            "--no-pager",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return ServiceState(
        active_state=values.get("ActiveState", ""),
        main_pid=int(values.get("MainPID", "0") or 0),
        restarts=int(values.get("NRestarts", "0") or 0),
    )


def worker_oom_observed(
    *, unit: str, kernel_rows: list[str], unit_attested: bool = False
) -> bool:
    """Classify the bounded probe's OOM outcome across launch backends.

    Exit status is deliberately insufficient: the root wrapper converts a
    child SIGKILL to 247, but an administrative SIGKILL has the same status.
    Require an exact kernel row or the wrapper's recent, unit-bound systemd
    journal attestation.
    """

    return unit_attested or any(
        unit in line and "oom" in line.lower() for line in kernel_rows
    )


def _system_scope_oom_evidence(unit: str, started_at: float) -> bool:
    sudo = shutil.which("sudo") or "/usr/bin/sudo"
    result = subprocess.run(
        [
            sudo,
            "-n",
            str(_SYSTEM_SCOPE_WRAPPER),
            "evidence",
            unit + ".scope",
            str(int(started_at)),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return False
    return bool(
        result.returncode == 0
        and isinstance(payload, dict)
        and payload.get("unit") == unit + ".scope"
        and payload.get("oom_kill") is True
    )


def _user_scope_oom_evidence(unit: str, started_at: float) -> bool:
    """Read only the caller's recent exact-unit user-manager journal."""

    result = subprocess.run(
        [
            "journalctl",
            "--user",
            "--unit",
            unit + ".scope",
            "--since",
            f"@{int(started_at)}",
            "--no-pager",
            "--output=cat",
            "--lines=100",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    marker = "A process of this unit has been killed by the OOM killer."
    return result.returncode == 0 and any(
        line.strip() == marker for line in result.stdout.splitlines()
    )


def run_proof(
    *,
    backend: str,
    gateway_unit: str,
    memory_max_mb: int,
    allocation_mb: int,
) -> dict:
    validate_bounds(memory_max_mb, allocation_mb)
    before = service_state(gateway_unit)
    if before.active_state != "active" or before.main_pid <= 0:
        raise RuntimeError(f"refusing proof: {gateway_unit} is not active")

    suffix = f"boundary-proof-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    unit = f"hermes-worker-system-{suffix}" if backend == "system" else f"hermes-worker-{suffix}"
    uid, gid = _current_posix_ids()
    environment_path = None
    if backend == "system":
        from tools.process_registry import _write_worker_environment

        environment_path = _write_worker_environment(dict(os.environ), suffix)
    command = build_scope_command(
        backend=backend,
        unit=suffix if backend == "system" else unit,
        memory_max_mb=memory_max_mb,
        allocation_mb=allocation_mb,
        uid=uid,
        gid=gid,
        environment_path=environment_path,
    )
    started_at = time.time()
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        _stop_scope(backend, unit)
        raise RuntimeError(
            f"worker scope {unit}.scope exceeded the 15-second proof timeout"
        ) from exc
    finally:
        if backend == "system":
            from tools.process_registry import _discard_scope_environment

            _discard_scope_environment(command)
    after = service_state(gateway_unit)
    kernel_command = [
        "journalctl",
        "--dmesg",
        "--since",
        f"@{int(started_at)}",
        "--no-pager",
        "--output=cat",
    ]
    # Kernel journal access is best-effort. The runtime sudo policy
    # intentionally does not grant arbitrary journalctl access.
    kernel = subprocess.run(
        kernel_command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    kernel_rows = [
        line[:500]
        for line in kernel.stdout.splitlines()
        if unit in line or ("oom" in line.lower() and "hermes-worker" in line)
    ][-20:]
    unit_attested = (
        _system_scope_oom_evidence(unit, started_at)
        if backend == "system"
        else _user_scope_oom_evidence(unit, started_at)
    )
    oom_observed = worker_oom_observed(
        unit=unit,
        kernel_rows=kernel_rows,
        unit_attested=unit_attested,
    )
    gateway_survived = (
        after.active_state == "active"
        and after.main_pid == before.main_pid
        and after.restarts == before.restarts
    )
    if not oom_observed:
        _stop_scope(backend, unit)
    return {
        "unit": unit + ".scope",
        "backend": backend,
        "memory_max_mb": memory_max_mb,
        "allocation_mb": allocation_mb,
        "worker_returncode": result.returncode,
        "worker_oom_observed": oom_observed,
        "unit_oom_attested": unit_attested,
        "gateway_before": asdict(before),
        "gateway_after": asdict(after),
        "gateway_survived": gateway_survived,
        "kernel_evidence": kernel_rows,
        "stderr": result.stderr.strip()[:500],
    }


def _stop_scope(backend: str, unit: str) -> None:
    scope = unit + ".scope"
    command = ["systemctl", "--user", "stop", scope]
    if backend == "system":
        sudo = shutil.which("sudo") or "/usr/bin/sudo"
        command = [
            sudo,
            "-n",
            str(_SYSTEM_SCOPE_WRAPPER),
            "stop",
            scope,
        ]
    subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=5,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("user", "system"), default="user")
    parser.add_argument("--gateway-unit", default="hermes-gateway.service")
    parser.add_argument("--memory-max-mb", type=int, default=64)
    parser.add_argument("--allocation-mb", type=int, default=96)
    args = parser.parse_args(argv)
    try:
        record = run_proof(
            backend=args.backend,
            gateway_unit=args.gateway_unit,
            memory_max_mb=args.memory_max_mb,
            allocation_mb=args.allocation_mb,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(record, sort_keys=True))
    return 0 if record["worker_oom_observed"] and record["gateway_survived"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
