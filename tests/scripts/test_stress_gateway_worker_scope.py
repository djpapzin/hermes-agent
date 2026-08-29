import pytest

from scripts.stress_gateway_worker_scope import build_scope_command, validate_bounds


def test_system_scope_command_has_tight_memory_boundary_and_unprivileged_uid():
    command = build_scope_command(
        backend="system",
        unit="hermes-worker-proof-test",
        memory_max_mb=16,
        allocation_mb=64,
        uid=1234,
        gid=5678,
    )

    assert "--system" in command
    assert "MemoryHigh=16777216" in command
    assert "MemoryMax=16777216" in command
    assert "MemorySwapMax=0" in command
    assert command[command.index("--uid") + 1] == "1234"
    assert command[command.index("--gid") + 1] == "5678"


@pytest.mark.parametrize(
    ("memory_max_mb", "allocation_mb"),
    [(15, 64), (65, 96), (32, 32), (64, 129)],
)
def test_scope_proof_refuses_unsafe_or_non_oom_bounds(
    memory_max_mb: int, allocation_mb: int
):
    with pytest.raises(ValueError):
        validate_bounds(memory_max_mb, allocation_mb)
