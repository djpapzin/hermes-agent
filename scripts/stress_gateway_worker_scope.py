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
    if not 16 <= memory_max_mb <= 64:
        raise ValueError("memory-max-mb must be between 16 and 64")
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
) -> list[str]:
    validate_bounds(memory_max_mb, allocation_mb)
    systemd_run = shutil.which("systemd-run") or "/usr/bin/systemd-run"
    prefix = [systemd_run, "--user"]
    if backend == "system":
        sudo = shutil.which("sudo") or "/usr/bin/sudo"
        prefix = [sudo, "-n", systemd_run, "--system"]
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

    unit = f"hermes-worker-boundary-proof-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    uid, gid = _current_posix_ids()
    command = build_scope_command(
        backend=backend,
        unit=unit,
        memory_max_mb=memory_max_mb,
        allocation_mb=allocation_mb,
        uid=uid,
        gid=gid,
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
    after = service_state(gateway_unit)
    kernel_command = [
        "journalctl",
        "--dmesg",
        "--since",
        f"@{int(started_at)}",
        "--no-pager",
        "--output=cat",
    ]
    if backend == "system":
        sudo = shutil.which("sudo") or "/usr/bin/sudo"
        kernel_command = [sudo, "-n", *kernel_command]
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
    worker_oom_observed = result.returncode in {137, -9} or any(
        unit in line and "oom" in line.lower() for line in kernel_rows
    )
    gateway_survived = (
        after.active_state == "active"
        and after.main_pid == before.main_pid
        and after.restarts == before.restarts
    )
    if not worker_oom_observed:
        _stop_scope(backend, unit)
    return {
        "unit": unit + ".scope",
        "backend": backend,
        "memory_max_mb": memory_max_mb,
        "allocation_mb": allocation_mb,
        "worker_returncode": result.returncode,
        "worker_oom_observed": worker_oom_observed,
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
        command = [sudo, "-n", "systemctl", "stop", scope]
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
    parser.add_argument("--memory-max-mb", type=int, default=16)
    parser.add_argument("--allocation-mb", type=int, default=64)
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
