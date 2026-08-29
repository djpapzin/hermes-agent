from pathlib import Path

import pytest

from scripts import stress_gateway_worker_scope as worker_scope
from scripts.stress_gateway_worker_scope import build_scope_command, validate_bounds


def test_system_scope_command_has_tight_memory_boundary_and_unprivileged_uid():
    command = build_scope_command(
        backend="system",
        unit="hermes-worker-proof-test",
        memory_max_mb=64,
        allocation_mb=96,
        uid=1234,
        gid=5678,
        environment_path=Path("/tmp/private.json"),
    )

    assert command[:4] == [
        "/usr/bin/sudo",
        "-n",
        "/usr/local/sbin/hermes-worker-scope",
        "run",
    ]
    assert command[5] == str(64 * 1024 * 1024)
    assert command[6] == "/tmp/private.json"
    assert "systemd-run" not in " ".join(command)


@pytest.mark.parametrize(
    ("memory_max_mb", "allocation_mb"),
    [(63, 96), (97, 112), (64, 64), (96, 129)],
)
def test_scope_proof_refuses_unsafe_or_non_oom_bounds(
    memory_max_mb: int, allocation_mb: int
):
    with pytest.raises(ValueError):
        validate_bounds(memory_max_mb, allocation_mb)


def test_documented_system_proof_bounds_reach_real_parser(monkeypatch):
    seen = {}

    def run_proof(**kwargs):
        seen.update(kwargs)
        return {"worker_oom_observed": True, "gateway_survived": True}

    monkeypatch.setattr(worker_scope, "run_proof", run_proof)

    assert worker_scope.main([
        "--backend", "system",
        "--memory-max-mb", "64",
        "--allocation-mb", "96",
    ]) == 0
    assert seen["memory_max_mb"] == 64
    assert seen["allocation_mb"] == 96
