# Gateway resource safety runbook

The messaging gateway is the control plane. Resource pressure must stop new
agent admission before it stops Telegram, sibling sessions, SSH, or systemd.

## One-command evidence

Run this read-only snapshot before changing a service or resource limit:

```bash
python3 scripts/hermes_gateway_diagnostics.py --since-minutes 30
```

It reports the gateway PID/RSS, systemd `Result`/`ExecMainCode`/
`ExecMainStatus`, host `MemAvailable`, the gateway cgroup's
current/high/max values and memory events, independently scoped worker
PIDs/RSS, and each worker cgroup's current/high/max values and OOM events.
The gateway emits an allowlisted shutdown record through a dedicated native
journald datagram; it includes the signal, explicit reason, safe parent fields,
gateway RSS, host headroom, active and queued task IDs, worker PIDs, and cgroup
OOM counters. Parent command lines and unknown fields are excluded. The
diagnostic requires trusted journald PID/unit metadata to match the payload, so
same-UID workers in independent scopes cannot forge gateway evidence.

The same output includes at most 100 filtered lifecycle/resource journal
events from the gateway, health guard, and kernel, plus at most 100
manager-attested structured admission records and 100 manager-attested
structured shutdown records from native journald. Both queries apply exact
event and identifier matches before their bounded result cap. The gateway
writes only controlled fields to these channels; the diagnostic requires the
trusted unit and PID metadata to match each payload, allowlists fields again,
and never parses same-UID files or mixed human-readable chat/command logs.
Gateway lifecycle queries likewise apply systemd-manager provenance matches
before their 300-row input cap, so replacement-process stdout cannot evict the
older exit/watchdog evidence before Python validates it.

Admission uses the tighter of host `MemAvailable` and finite gateway-cgroup
headroom (`MemoryMax - MemoryCurrent`). Structured admission logs expose both
witnesses and the effective value used for the decision.

The persisted top-level `active_agents` count and graceful shutdown drain
include normal chat turns, API runs, cron jobs, and admitted `/background`
agents. Do not treat a host as drained using process counts alone; require both
`active_agents: 0` and `admission.queued_tasks: 0` from the runtime snapshot.

Correlate the timestamp with both service and kernel evidence:

```bash
journalctl -u hermes-gateway.service --since '30 minutes ago' --no-pager
journalctl -k --since '30 minutes ago' --no-pager | grep -Ei 'oom|killed process|memory cgroup'
systemctl show hermes-gateway.service -p MainPID -p Result -p ExecMainCode -p ExecMainStatus -p OOMPolicy -p KillMode
```

Do not label an incident OOM unless kernel or cgroup `memory.events` evidence
supports it. A service-manager SIGTERM, watchdog abort, updater, deployment,
or external health guard is a different failure path.

### Systemd watchdog semantics

The gateway heartbeat reports every event-loop tick that recovers before the
last successful heartbeat reaches `WatchdogSec`. Such a late callback is
recorded as `watchdog delayed: event loop recovered` and must not permanently
latch the heartbeat off. A callback at or beyond the hard deadline records
`watchdog expired` without sending `WATCHDOG=1`, so it cannot race systemd and
re-arm an already-expired watchdog.

This distinction matters under parallel load. A transient delay shorter than
`WatchdogSec` must recover in place; otherwise one late tick becomes a
guaranteed later gateway abort that interrupts healthy, unrelated sessions.
Correlate `Failed with result 'watchdog'` and `status=6/ABRT` with the bounded
admission and memory evidence before changing the watchdog interval.

## Supervisor policy

- SQLite `database is locked`, task count, aggregate memory, or low host
  headroom are admission/alert signals. They must not trigger a whole-gateway
  restart.
- Restart only for a proven gateway control-plane failure. Rate-limit and
  persist the reason; do not create an endless restart loop.
- Keep `KillMode=mixed` and `OOMPolicy=continue` on the gateway. Independently
  scoped workers carry their own finite `MemoryHigh`, `MemoryMax`, and
  `MemorySwapMax=0` so they cannot evade the RAM boundary through swap.
- For production Linux gateways, verify either `systemd-run --user --scope` or
  the same-UID `sudo -n systemd-run --system --scope --uid=<runtime-user>` path
  works before selecting `terminal.worker_cgroup_mode: required`.
- A systemd-supervised Linux gateway fails closed if no worker-scope backend is
  available, including `auto`, `user`, or `off` configurations. This covers
  terminal, browser, computer-use, local `execute_code`, and configured stdio
  MCP children. Running model-controlled work inside the control-plane cgroup would defeat both
  resource isolation and manager-attested diagnostic provenance. Supervision
  is latched to the gateway PID once at startup; later deletion or replacement
  of same-UID runtime PID, lock, or state files cannot disable isolation.
  Forked/execed children do not inherit that process identity. CLI processes
  outside the systemd-supervised gateway and non-systemd supervisors such as
  the official s6 container retain their existing behavior unless they provide
  their own compatible isolation backend.

### Replace restart-on-pressure health guards

`scripts/hermes_health_guard.py` is the supported alert-only guard. It records
the same pressure, SQLite-lock, WAL, Telegram-connectivity, active-agent, and
queue evidence, but always sets `recovery_attempted=false` and never invokes a
gateway lifecycle command. Install it only inside the controlled deployment
window, after active work has genuinely drained:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo cp --preserve=all /usr/local/sbin/hermes-health-guard \
  "/usr/local/sbin/hermes-health-guard.rollback-${stamp}"
sudo install -o root -g root -m 0755 scripts/hermes_health_guard.py \
  /usr/local/sbin/hermes-health-guard
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/hermes-health-guard.service.d
sudo tee /etc/systemd/system/hermes-health-guard.service.d/alert-only.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /usr/local/sbin/hermes-health-guard --hermes-home /home/ubuntu/.hermes
EOF
sudo systemctl daemon-reload
sudo systemctl start hermes-health-guard.service
sudo jq '{decision,recovery_attempted,reasons,active_agents,queued_tasks}' \
  /var/lib/hermes-health-guard/state.json
```

Pass requires `recovery_attempted: false`; a pressured host may legitimately
report `decision: alert_only`. Do not restart the gateway to make this check
green. Keep the timer enabled so alerts continue.

Rollback restores the timestamped executable, removes only the drop-in above,
reloads systemd, and runs one guard check. Restoring a restart-capable guard is
itself risky and must not occur while unrelated agent work is active:

```bash
sudo install -o root -g root -m 0755 \
  /usr/local/sbin/hermes-health-guard.rollback-<timestamp> \
  /usr/local/sbin/hermes-health-guard
sudo rm /etc/systemd/system/hermes-health-guard.service.d/alert-only.conf
sudo systemctl daemon-reload
sudo systemctl start hermes-health-guard.service
```

## Controlled stress verification

The repository stress command is deliberately capped (`<=12` workers, `<=6`
parallel, `<=64 MiB` per worker, `<=15s`) and does not contact Telegram:

```bash
python3 scripts/stress_gateway_admission.py \
  --workers 6 --parallel 3 --memory-mb 16 --seconds 1 --crash-worker 1
```

Pass requires `peak_active <= parallel_limit`, at least one queue notice when
workers exceed the limit, exactly one expected worker crash, the queued work
resuming and draining, no unexpected subprocess failures, and the
parent/control-plane test process surviving.

On a cgroups-v2 VM, run the separately bounded worker-boundary proof only when
the gateway is active and host `MemAvailable` is comfortably above 1 GiB:

```bash
sudo -u hermes-runtime /usr/bin/python3 \
  scripts/stress_gateway_worker_scope.py \
  --backend system --memory-max-mb 64 --allocation-mb 96
```

The script refuses a worker limit below 64 or above 96 MiB, or an allocation
that does not exceed the worker limit or is above 128 MiB. It stops only its unique disposable scope if the proof times out
or the child unexpectedly survives.
Pass requires an OOM/SIGKILL only in the disposable worker scope and an
unchanged active gateway PID and restart count. It never stops, restarts, or
changes the gateway service.

## Rollout and rollback

1. Capture the diagnostic and recent service/kernel journals.
2. Confirm no unrelated active workloads would be interrupted.
3. Deploy code and config, then perform one controlled gateway restart.
4. Start representative parallel tasks; verify queue notices, FIFO resume,
   Telegram responsiveness, independent worker cgroups, and unchanged gateway
   PID throughout a worker failure.
5. Re-run diagnostics and confirm no gateway cgroup OOM or restart loop.

Rollback is one code/config revert plus one controlled restart. Before that
restart, drain or explicitly reconcile queued and active tasks. Never remove
the worker memory boundary or increase concurrency as an emergency workaround;
set `gateway.admission.max_parallel_agents` lower if pressure remains.
