import asyncio
import os
import tempfile
from pathlib import Path

os.environ["HERMES_HOME"] = str(
    Path(tempfile.gettempdir()) / f"hermes-background-drain-test-{os.getpid()}"
)

import pytest

from gateway.admission import AgentAdmissionController
from gateway.run import GatewayRunner


def _runner(controller: AgentAdmissionController) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_admission = controller
    runner._running_agents = {}
    runner._background_agent_refs = {}
    runner._snapshot_running_agents = lambda: {}
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._update_runtime_status = lambda *_args, **_kwargs: None
    return runner


@pytest.mark.asyncio
async def test_background_admission_is_visible_in_total_active_work():
    controller = AgentAdmissionController(max_parallel=2)
    runner = _runner(controller)

    await controller.acquire("background:bg-visible:1234")

    assert runner._active_background_agent_count() == 1
    assert runner._active_work_count() == 1


@pytest.mark.asyncio
async def test_shutdown_drain_waits_for_background_admission_to_release():
    controller = AgentAdmissionController(max_parallel=2)
    runner = _runner(controller)
    task_id = "background:bg-drain:1234"
    await controller.acquire(task_id)

    drain = asyncio.create_task(runner._drain_active_agents(0.5))
    await asyncio.sleep(0.03)
    assert not drain.done()

    await controller.release(task_id)
    _snapshot, timed_out = await asyncio.wait_for(drain, timeout=0.2)

    assert timed_out is False
    assert runner._active_work_count() == 0


def test_shutdown_interrupt_reaches_registered_background_agent(monkeypatch):
    controller = AgentAdmissionController(max_parallel=2)
    runner = _runner(controller)
    interrupted = []

    class Agent:
        def interrupt(self, reason):
            interrupted.append(reason)

    agent = Agent()
    runner._background_agent_refs["bg-interrupt"] = agent
    runner._interrupt_api_server_runs = lambda _reason: 0
    runner._interrupt_running_agents("controlled restart")

    assert interrupted == ["controlled restart"]
