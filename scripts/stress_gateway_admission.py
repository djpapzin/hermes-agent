#!/usr/bin/env python3
"""Controlled admission stress test; defaults stay below 128 MiB total RSS."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil

_ADMISSION_PATH = Path(__file__).resolve().parents[1] / "gateway" / "admission.py"
_SPEC = importlib.util.spec_from_file_location("hermes_gateway_admission", _ADMISSION_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"could not load {_ADMISSION_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
AgentAdmissionController = _MODULE.AgentAdmissionController


async def run(
    workers: int,
    parallel: int,
    memory_mb: int,
    seconds: float,
    crash_worker: int = -1,
) -> dict:
    if workers > 12 or parallel > 6 or memory_mb > 64 or seconds > 15:
        raise ValueError("refusing unsafe stress parameters (workers<=12, parallel<=6, memory_mb<=64, seconds<=15)")
    if workers < 1 or parallel < 1 or memory_mb < 1 or seconds < 0:
        raise ValueError("workers, parallel, and memory must be positive; seconds must be non-negative")
    if crash_worker < -1 or crash_worker >= workers:
        raise ValueError("crash-worker must be -1 or a valid zero-based worker index")
    controller = AgentAdmissionController(
        max_parallel=parallel, queue_limit=workers, poll_interval_seconds=0.05
    )
    control_plane_pid = os.getpid()
    peak_active = 0
    queued_notices = 0
    failures: list[str] = []
    expected_worker_crashes = 0
    started: list[str] = []

    async def one(index: int) -> None:
        nonlocal peak_active, queued_notices, expected_worker_crashes

        async def notice(_message: str) -> None:
            nonlocal queued_notices
            queued_notices += 1

        task_id = f"stress-{index}"
        await controller.acquire(task_id, on_queued=notice)
        started.append(task_id)
        peak_active = max(peak_active, controller.snapshot().active)
        try:
            if index == crash_worker:
                code = "raise SystemExit(42)"
            else:
                code = (
                    "import time; "
                    f"x=bytearray({memory_mb}*1024*1024); "
                    f"time.sleep({seconds!r}); print(len(x))"
                )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
            if index == crash_worker and proc.returncode == 42:
                expected_worker_crashes += 1
            elif proc.returncode != 0:
                failures.append(f"{task_id}: rc={proc.returncode} {stderr.decode(errors='replace')[:120]}")
        finally:
            await controller.release(
                task_id,
                outcome="crashed" if index == crash_worker else "finished",
            )

    await asyncio.gather(*(one(i) for i in range(workers)))
    snapshot = controller.snapshot()
    queue_resumed_and_drained = (
        len(started) == workers and snapshot.active == 0 and snapshot.queued == 0
    )
    control_plane_survived = psutil.pid_exists(control_plane_pid)
    return {
        "workers": workers,
        "parallel_limit": parallel,
        "peak_active": peak_active,
        "queued_notices": queued_notices,
        "failures": failures,
        "expected_worker_crashes": expected_worker_crashes,
        "queue_resumed_and_drained": queue_resumed_and_drained,
        "gateway_process_survived": control_plane_survived,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=16)
    parser.add_argument("--seconds", type=float, default=0.25)
    parser.add_argument(
        "--crash-worker",
        type=int,
        default=-1,
        help="zero-based worker to terminate with exit 42 (-1 disables)",
    )
    args = parser.parse_args()
    result = asyncio.run(
        run(
            args.workers,
            args.parallel,
            args.memory_mb,
            args.seconds,
            args.crash_worker,
        )
    )
    print(json.dumps(result, sort_keys=True))
    expected_crash_missing = (
        args.crash_worker >= 0 and result["expected_worker_crashes"] != 1
    )
    return 1 if (
        result["failures"]
        or result["peak_active"] > args.parallel
        or not result["queue_resumed_and_drained"]
        or not result["gateway_process_survived"]
        or expected_crash_missing
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
