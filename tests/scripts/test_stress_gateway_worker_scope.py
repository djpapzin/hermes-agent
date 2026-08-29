from pathlib import Path

import pytest

from scripts import stress_gateway_worker_scope as worker_scope
from scripts.stress_gateway_worker_scope import (
    build_scope_command,
    validate_bounds,
    worker_oom_observed,
)


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


def test_worker_oom_classifier_accepts_unit_attestation():
    assert worker_oom_observed(
        unit="hermes-worker-proof",
        kernel_rows=[],
        unit_attested=True,
    )


def test_worker_oom_classifier_rejects_unrelated_failure():
    assert not worker_oom_observed(
        unit="hermes-worker-proof",
        kernel_rows=["another-worker.scope: oom-kill"],
    )


def test_worker_oom_classifier_rejects_bare_sigkill_exit_without_evidence():
    assert not worker_oom_observed(
        unit="hermes-worker-proof",
        kernel_rows=[],
        unit_attested=False,
    )


def test_user_scope_oom_evidence_is_exact_and_bounded(monkeypatch):
    calls = []
    marker = (
        "hermes-worker-proof.scope: A process of this unit has been killed "
        "by the OOM killer.\n"
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": marker})()

    monkeypatch.setattr(worker_scope.subprocess, "run", run)

    assert worker_scope._user_scope_oom_evidence("hermes-worker-proof", 123.9)
    argv, kwargs = calls[0]
    assert argv[:4] == [
        "journalctl",
        "--user-unit",
        "hermes-worker-proof.scope",
        "--since",
    ]
    assert argv[4] == "@123"
    assert kwargs["timeout"] == 8


def test_user_scope_oom_evidence_rejects_non_oom_journal(monkeypatch):
    monkeypatch.setattr(
        worker_scope.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "Killed by SIGKILL\n"}
        )(),
    )

    assert not worker_scope._user_scope_oom_evidence("hermes-worker-proof", 123)
