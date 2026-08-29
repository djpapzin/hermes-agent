import asyncio

import pytest

from scripts.stress_gateway_admission import run


def test_controlled_stress_crash_releases_slot_and_drains_queue():
    result = asyncio.run(
        run(workers=4, parallel=2, memory_mb=1, seconds=0.01, crash_worker=1)
    )

    assert result["peak_active"] == 2
    assert result["queued_notices"] >= 1
    assert result["expected_worker_crashes"] == 1
    assert result["failures"] == []
    assert result["queue_resumed_and_drained"] is True
    assert result["gateway_process_survived"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workers": 13},
        {"parallel": 7},
        {"memory_mb": 65},
        {"seconds": 16},
        {"crash_worker": 4},
    ],
)
def test_controlled_stress_refuses_unsafe_bounds(kwargs):
    values = {
        "workers": 4,
        "parallel": 2,
        "memory_mb": 1,
        "seconds": 0.01,
        "crash_worker": -1,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        asyncio.run(run(**values))
