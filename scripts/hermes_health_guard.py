#!/usr/bin/env python3
"""Alert-only Hermes gateway health guard.

Resource pressure, SQLite contention, task count, and transport degradation are
signals for admission and operator action. They are never authority to restart
the messaging control plane. Genuine process failure remains the service
manager's responsibility through the gateway unit's Restart policy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class GuardSnapshot:
    active_state: str
    main_pid: int
    tasks: int
    memory_bytes: int
    wal_bytes: int
    db_locks: int
    telegram_poll_conflicts: int
    telegram_enabled: bool
    telegram_connected: bool
    state_db_exists: bool
    active_agents: int
    queued_tasks: int
    probe_errors: tuple[str, ...] = ()


def evaluate(snapshot: GuardSnapshot, *, max_tasks: int, max_memory: int, max_wal: int, lock_threshold: int) -> list[str]:
    reasons: list[str] = [f"probe_unavailable={item}" for item in snapshot.probe_errors]
    if snapshot.active_state != "active":
        reasons.append(f"active_state={snapshot.active_state or 'unknown'}")
    if snapshot.tasks > max_tasks:
        reasons.append(f"tasks={snapshot.tasks}>{max_tasks}")
    if snapshot.memory_bytes > max_memory:
        reasons.append(f"memory={snapshot.memory_bytes}>{max_memory}")
    if snapshot.wal_bytes > max_wal:
        reasons.append(f"wal={snapshot.wal_bytes}>{max_wal}")
    if snapshot.db_locks >= lock_threshold:
        reasons.append(f"db_locks={snapshot.db_locks}>={lock_threshold}")
    if snapshot.telegram_poll_conflicts:
        reasons.append(
            f"telegram_poll_conflicts={snapshot.telegram_poll_conflicts}"
        )
    if (
        snapshot.active_state == "active"
        and snapshot.telegram_enabled
        and not snapshot.telegram_connected
    ):
        reasons.append("telegram_polling=disconnected")
    if not snapshot.state_db_exists:
        reasons.append("state_db=missing")
    return reasons


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 124, "", str(exc))


def _runtime_counts(path: Path) -> tuple[int, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        admission = payload.get("admission") if isinstance(payload, dict) else {}
        return (
            max(0, int(payload.get("active_agents", 0))),
            max(0, int((admission or {}).get("queued_tasks", 0))),
        )
    except (OSError, TypeError, ValueError):
        return 0, 0


def _telegram_enabled(path: Path) -> bool:
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        gateway = payload.get("gateway") if isinstance(payload, dict) else {}
        platforms = payload.get("platforms") if isinstance(payload, dict) else {}
        if not isinstance(platforms, dict):
            platforms = {}
        if isinstance(gateway, dict) and isinstance(gateway.get("platforms"), dict):
            platforms = {**platforms, **gateway["platforms"]}
        telegram = platforms.get("telegram") if isinstance(platforms, dict) else {}
        if isinstance(telegram, dict) and "enabled" in telegram:
            return bool(telegram.get("enabled"))
        if isinstance(telegram, dict) and telegram.get("token"):
            return True
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            return True
        env_path = path.parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.removeprefix("export ").strip()
                if key == "TELEGRAM_BOT_TOKEN" and value.strip().strip("'\""):
                    return True
        return False
    except (ImportError, OSError, TypeError, ValueError):
        return False


def collect_snapshot(
    *,
    unit: str,
    hermes_home: Path,
    lock_window_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> GuardSnapshot:
    probe_errors: list[str] = []

    def prop(name: str) -> str:
        proc = runner(
            "systemctl", "show", unit, f"--property={name}", "--value"
        )
        if proc.returncode != 0:
            probe_errors.append(f"systemctl:{name}")
        return proc.stdout.strip()

    active = prop("ActiveState")
    main_pid = int(prop("MainPID") or 0)
    journal_result = runner(
        "journalctl",
        "-u",
        unit,
        "--since",
        f"-{lock_window_seconds} seconds",
        "--no-pager",
        "--output=cat",
    )
    if journal_result.returncode != 0:
        probe_errors.append("journalctl")
    journal = journal_result.stdout.lower()
    socket_result = runner("ss", "-H", "-tnp")
    if socket_result.returncode != 0:
        probe_errors.append("ss")
    sockets = socket_result.stdout
    pid_marker = f"pid={main_pid},"
    telegram_connected = any(
        "ESTAB" in line
        and pid_marker in line
        and ("149.154." in line or "91.108." in line)
        for line in sockets.splitlines()
    )
    active_agents, queued_tasks = _runtime_counts(
        hermes_home / "gateway_state.json"
    )
    wal = hermes_home / "state.db-wal"
    return GuardSnapshot(
        active_state=active,
        main_pid=main_pid,
        tasks=int(prop("TasksCurrent") or 0),
        memory_bytes=int(prop("MemoryCurrent") or 0),
        wal_bytes=wal.stat().st_size if wal.exists() else 0,
        db_locks=journal.count("database is locked"),
        telegram_poll_conflicts=journal.count(
            "conflict: terminated by other getupdates request"
        ),
        telegram_enabled=_telegram_enabled(hermes_home / "config.yaml"),
        telegram_connected=telegram_connected,
        state_db_exists=(hermes_home / "state.db").exists(),
        active_agents=active_agents,
        queued_tasks=queued_tasks,
        probe_errors=tuple(probe_errors),
    )


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", default="hermes-gateway.service")
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("/var/lib/hermes-health-guard/state.json"),
    )
    parser.add_argument("--max-tasks", type=int, default=500)
    parser.add_argument("--max-memory-mb", type=int, default=12288)
    parser.add_argument("--max-wal-mb", type=int, default=4096)
    parser.add_argument("--lock-window-seconds", type=int, default=300)
    parser.add_argument("--lock-threshold", type=int, default=10)
    args = parser.parse_args(argv)

    snapshot = collect_snapshot(
        unit=args.unit,
        hermes_home=args.hermes_home,
        lock_window_seconds=max(1, args.lock_window_seconds),
    )
    reasons = evaluate(
        snapshot,
        max_tasks=max(1, args.max_tasks),
        max_memory=max(1, args.max_memory_mb) * 1024 * 1024,
        max_wal=max(1, args.max_wal_mb) * 1024 * 1024,
        lock_threshold=max(1, args.lock_threshold),
    )
    record = {
        "checked_at": int(time.time()),
        "decision": "alert_only" if reasons else "healthy",
        "recovery_attempted": False,
        "reasons": reasons,
        **asdict(snapshot),
    }
    _write_state(args.state_file, record)
    print("HERMES_HEALTH " + json.dumps(record, sort_keys=True))
    # Monitoring consumes the structured decision. Never make a warning an
    # implicit service restart by exiting through a recovery path.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
