#!/usr/bin/env python3
"""Emit one bounded, secret-safe JSON snapshot for gateway pressure incidents."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_INCIDENT_MARKERS = (
    "HERMES_ADMISSION",
    "HERMES_HEALTH",
    "HERMES_RECOVERY",
    "Shutdown context:",
    "Received SIG",
    "Watchdog timeout",
    "watchdog timeout",
    "killed by the OOM killer",
    "Killed process",
    "Out of memory",
    "Stopping Hermes Agent Gateway",
    "Started Hermes Agent Gateway",
)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _fields(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    raw = _read(path)
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            result[parts[0].rstrip(":")] = int(parts[1])
    return result


def _service_pid(unit: str) -> int | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        raw = result.stdout.strip()
        return int(raw) if result.returncode == 0 and raw.isdigit() and raw != "0" else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _runtime_status(path: Path) -> dict[str, Any] | None:
    raw = _read(path)
    if raw is None:
        try:
            result = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            raw = result.stdout if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            raw = None
    try:
        payload = json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _bounded_incident_journal(unit: str, since_minutes: int) -> list[dict[str, str]]:
    """Return only lifecycle/resource evidence, never general chat logs."""
    commands = [
        ["journalctl", "-u", unit],
        ["journalctl", "-u", "hermes-health-guard.service"],
        ["journalctl", "-k"],
    ]
    events: list[dict[str, str]] = []
    for base in commands:
        try:
            result = subprocess.run(
                [
                    *base,
                    "--since",
                    f"{max(1, since_minutes)} minutes ago",
                    "-n",
                    "300",
                    "-o",
                    "json",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            message = str(row.get("MESSAGE") or "")
            if not any(marker in message for marker in _INCIDENT_MARKERS):
                continue
            events.append(
                {
                    "timestamp_realtime_usec": str(
                        row.get("__REALTIME_TIMESTAMP") or ""
                    ),
                    "unit": str(row.get("_SYSTEMD_UNIT") or "kernel"),
                    "message": message[:500],
                }
            )
    return events[-100:]


def collect(
    unit: str = "hermes-gateway.service",
    hermes_home: Path = Path.home() / ".hermes",
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    pid = _service_pid(unit)
    result: dict[str, Any] = {
        "unit": unit,
        "gateway_pid": pid,
        "host_memory_kb": _fields(proc_root / "meminfo"),
    }
    runtime_path = hermes_home / "gateway_state.json"
    runtime = _runtime_status(runtime_path)
    if isinstance(runtime, dict):
        result["runtime_status"] = {
            "gateway_state": runtime.get("gateway_state"),
            "active_agents": runtime.get("active_agents"),
            "admission": runtime.get("admission", {}),
            "updated_at": runtime.get("updated_at"),
        }
    else:
        result["runtime_status"] = None
    if pid:
        status = _fields(proc_root / str(pid) / "status")
        result["gateway_rss_kb"] = status.get("VmRSS")
        cgroup_raw = _read(proc_root / str(pid) / "cgroup") or ""
        relative = next(
            (line.partition("::")[2].lstrip("/") for line in cgroup_raw.splitlines() if line.startswith("0::")),
            None,
        )
        if relative is not None:
            root = cgroup_root / relative
            result["gateway_cgroup"] = "/" + relative
            result["cgroup_memory"] = {
                "current": _read(root / "memory.current"),
                "high": _read(root / "memory.high"),
                "max": _read(root / "memory.max"),
                "events": _fields(root / "memory.events"),
            }
    worker_rows: list[dict[str, Any]] = []
    worker_cgroups: dict[str, dict[str, Any]] = {}
    for proc_status in sorted(proc_root.glob("[0-9]*/status")):
        proc_pid = int(proc_status.parent.name)
        proc_cgroup = _read(proc_status.parent / "cgroup") or ""
        if "hermes-worker-" not in proc_cgroup:
            continue
        fields = _fields(proc_status)
        relative = next(
            (
                line.partition("::")[2].lstrip("/")
                for line in proc_cgroup.splitlines()
                if line.startswith("0::")
            ),
            "",
        )
        rss_kb = fields.get("VmRSS")
        worker_rows.append(
            {"pid": proc_pid, "rss_kb": rss_kb, "cgroup": relative[:300]}
        )
        if relative:
            row = worker_cgroups.setdefault(
                relative,
                {"cgroup": "/" + relative, "pids": [], "rss_kb": 0},
            )
            row["pids"].append(proc_pid)
            row["rss_kb"] += int(rss_kb or 0)

    worker_scope_rows: list[dict[str, Any]] = []
    worker_slice_paths: set[str] = set()
    for relative, row in sorted(worker_cgroups.items()):
        root = cgroup_root / relative
        row.update(
            {
                "memory_current": _read(root / "memory.current"),
                "memory_high": _read(root / "memory.high"),
                "memory_max": _read(root / "memory.max"),
                "memory_events": _fields(root / "memory.events"),
            }
        )
        row["pids"] = sorted(row["pids"])
        worker_scope_rows.append(row)
        parts = Path(relative).parts
        if "hermes-workers.slice" in parts:
            index = parts.index("hermes-workers.slice")
            worker_slice_paths.add(str(Path(*parts[: index + 1])))
    worker_slice_rows: list[dict[str, Any]] = []
    for relative in sorted(worker_slice_paths):
        root = cgroup_root / relative
        worker_slice_rows.append(
            {
                "cgroup": "/" + relative,
                "memory_current": _read(root / "memory.current"),
                "memory_high": _read(root / "memory.high"),
                "memory_max": _read(root / "memory.max"),
                "memory_events": _fields(root / "memory.events"),
            }
        )
    result["workers"] = worker_rows[:100]
    result["worker_process_count"] = len(worker_rows)
    result["worker_cgroups"] = worker_scope_rows[:100]
    result["worker_slices"] = worker_slice_rows
    result["worker_count"] = len(worker_scope_rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", default="hermes-gateway.service")
    parser.add_argument("--hermes-home", type=Path, default=get_hermes_home())
    parser.add_argument("--since-minutes", type=int, default=30)
    args = parser.parse_args()
    result = collect(args.unit, args.hermes_home)
    result["incident_events"] = _bounded_incident_journal(
        args.unit, args.since_minutes
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
