"""Regression coverage for bounded, resource-aware gateway admission."""

from __future__ import annotations

import asyncio

import pytest

from gateway.admission import (
    AdmissionRejected,
    AgentAdmissionController,
    await_admitted_handoff,
    cgroup_available_memory_mb,
    clear_gateway_admission,
    gateway_admitted_async,
    gateway_admitted_sync,
    install_gateway_admission,
    notify_gateway_admission_changed,
)


def test_cgroup_headroom_uses_memory_max_minus_current(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/system.slice/hermes.service\n", encoding="utf-8")
    cgroup_dir = tmp_path / "root" / "system.slice" / "hermes.service"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "memory.max").write_text(str(3 * 1024 * 1024 * 1024), encoding="utf-8")
    (cgroup_dir / "memory.current").write_text(str(2 * 1024 * 1024 * 1024), encoding="utf-8")

    assert cgroup_available_memory_mb(cgroup_file, tmp_path / "root") == 1024


def test_unbounded_cgroup_defers_to_host_headroom(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/user.slice/test\n", encoding="utf-8")
    cgroup_dir = tmp_path / "root" / "user.slice" / "test"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "memory.max").write_text("max", encoding="utf-8")
    (cgroup_dir / "memory.current").write_text("123", encoding="utf-8")

    assert cgroup_available_memory_mb(cgroup_file, tmp_path / "root") is None


@pytest.mark.asyncio
async def test_cancelled_pre_agent_handoff_releases_admission_slot():
    controller = AgentAdmissionController(max_parallel=1, queue_limit=1)
    await controller.acquire("cancelled")
    waiting = asyncio.Event()

    async def handoff():
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        await_admitted_handoff(
            handoff(), controller=controller, task_id="cancelled"
        )
    )
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert controller.snapshot().active == 0


@pytest.mark.asyncio
async def test_parallel_capacity_queues_and_starts_fifo_after_release():
    notices: list[tuple[str, str]] = []
    controller = AgentAdmissionController(
        max_parallel=2, queue_limit=3, poll_interval_seconds=0.01
    )
    await controller.acquire("agent-1")
    await controller.acquire("agent-2")

    async def queued(task_id: str):
        await controller.acquire(
            task_id, on_queued=lambda text: _record(notices, task_id, text)
        )

    third = asyncio.create_task(queued("agent-3"))
    fourth = asyncio.create_task(queued("agent-4"))
    await asyncio.sleep(0.03)
    assert controller.snapshot().active_task_ids == ("agent-1", "agent-2")
    assert controller.snapshot().queued_task_ids == ("agent-3", "agent-4")
    assert "parallel-agent capacity (2/2)" in notices[0][1]

    await controller.release("agent-1")
    await asyncio.wait_for(third, timeout=0.2)
    assert "agent-3" in controller.snapshot().active_task_ids
    assert not fourth.done()

    await controller.release("agent-2")
    await asyncio.wait_for(fourth, timeout=0.2)


async def _record(rows: list[tuple[str, str]], task_id: str, text: str) -> None:
    rows.append((task_id, text))


@pytest.mark.asyncio
async def test_memory_pressure_queues_without_interrupting_running_agent():
    available = [500]
    controller = AgentAdmissionController(
        max_parallel=3,
        min_headroom_mb=1024,
        queue_limit=2,
        poll_interval_seconds=0.01,
        memory_reader=lambda: available[0],
    )
    waiter = asyncio.create_task(controller.acquire("memory-waiter"))
    await asyncio.sleep(0.03)
    assert controller.snapshot().active == 0
    assert controller.snapshot().queued == 1
    available[0] = 2048
    await asyncio.wait_for(waiter, timeout=0.2)
    assert controller.snapshot().active_task_ids == ("memory-waiter",)


@pytest.mark.asyncio
async def test_worker_crash_releases_only_its_slot_and_queue_continues():
    controller = AgentAdmissionController(
        max_parallel=2, queue_limit=2, poll_interval_seconds=0.01
    )
    await controller.acquire("healthy")
    await controller.acquire("crashing")
    queued = asyncio.create_task(controller.acquire("next"))
    await asyncio.sleep(0.02)

    # The gateway's handler finally block uses this same outcome-independent
    # release path when an agent raises or its worker is OOM-killed.
    await controller.release("crashing", outcome="crash")
    await asyncio.wait_for(queued, timeout=0.2)
    assert set(controller.snapshot().active_task_ids) == {"healthy", "next"}


@pytest.mark.asyncio
async def test_queue_limit_and_restart_reconciliation_are_explicit():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=1, poll_interval_seconds=0.01
    )
    await controller.acquire("active")
    queued = asyncio.create_task(controller.acquire("queued"))
    await asyncio.sleep(0.02)
    with pytest.raises(AdmissionRejected, match="queue is full"):
        await controller.acquire("overflow")

    reconciled = await controller.close("restart reconciliation")
    assert reconciled == ("queued",)
    with pytest.raises(AdmissionRejected, match="restart reconciliation"):
        await queued
    assert controller.snapshot().active_task_ids == ("active",)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_block_following_task():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=2, poll_interval_seconds=0.01
    )
    await controller.acquire("active")
    cancelled = asyncio.create_task(controller.acquire("cancelled"))
    following = asyncio.create_task(controller.acquire("following"))
    await asyncio.sleep(0.02)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await controller.release("active")
    await asyncio.wait_for(following, timeout=0.2)
    assert controller.snapshot().active_task_ids == ("following",)


@pytest.mark.asyncio
async def test_duplicate_task_id_cannot_share_one_set_slot():
    controller = AgentAdmissionController(max_parallel=3)
    await controller.acquire("same-id")

    with pytest.raises(AdmissionRejected, match="already owns an active slot"):
        await controller.acquire("same-id")

    assert controller.snapshot().active == 1


@pytest.mark.asyncio
async def test_async_and_scheduler_surfaces_share_one_global_limit():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=2, poll_interval_seconds=0.01
    )
    install_gateway_admission(controller, asyncio.get_running_loop())
    release_api = asyncio.Event()

    @gateway_admitted_async("api", id_kwargs=("session_id",))
    async def api_turn(*, session_id: str):
        await release_api.wait()
        return session_id

    @gateway_admitted_sync("cron", id_kwargs=("job",))
    def cron_turn(job: dict):
        return job["id"]

    try:
        running = asyncio.create_task(api_turn(session_id="chat-1"))
        await asyncio.sleep(0.02)
        queued = asyncio.create_task(
            asyncio.to_thread(cron_turn, {"id": "daily-report", "prompt": "secret"})
        )
        await asyncio.sleep(0.03)
        snapshot = controller.snapshot()
        assert len(snapshot.active_task_ids) == 1
        assert snapshot.active_task_ids[0].startswith("api:chat-1:")
        assert len(snapshot.queued_task_ids) == 1
        assert snapshot.queued_task_ids[0].startswith("cron:daily-report:")
        assert "secret" not in " ".join(snapshot.queued_task_ids)

        release_api.set()
        assert await asyncio.wait_for(running, timeout=0.2) == "chat-1"
        assert await asyncio.wait_for(queued, timeout=0.2) == "daily-report"
        assert controller.snapshot().active == 0
    finally:
        clear_gateway_admission(controller)


@pytest.mark.asyncio
async def test_background_surface_reports_queue_and_resumes_without_prompt_leak():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=2, poll_interval_seconds=0.01
    )
    install_gateway_admission(controller, asyncio.get_running_loop())
    await controller.acquire("existing")
    notices: list[tuple[str, str, str]] = []

    class BackgroundSurface:
        async def queue_notice(self, message: str, *, source: str, task_id: str):
            notices.append((source, task_id, message))

        @gateway_admitted_async(
            "background",
            id_kwargs=("task_id",),
            queued_notice_method="queue_notice",
            queued_notice_kwargs=("source", "task_id"),
        )
        async def run(self, prompt: str, source: str, task_id: str):
            return prompt

    try:
        queued = asyncio.create_task(
            BackgroundSurface().run("private prompt", "telegram:123", "bg-safe")
        )
        await asyncio.sleep(0.03)
        assert notices == [
            (
                "telegram:123",
                "bg-safe",
                "Queued: system at parallel-agent capacity (1/1); position 1.",
            )
        ]
        assert "private prompt" not in " ".join(controller.snapshot().queued_task_ids)

        await controller.release("existing")
        assert await asyncio.wait_for(queued, timeout=0.2) == "private prompt"
        assert controller.snapshot().active == 0
    finally:
        clear_gateway_admission(controller)


@pytest.mark.asyncio
async def test_slow_queue_notice_does_not_block_fifo_progress(monkeypatch):
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=2, poll_interval_seconds=0.01
    )
    await controller.acquire("existing")
    notice_started = asyncio.Event()

    async def hung_notice(_message: str) -> None:
        notice_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.admission._QUEUE_NOTICE_TIMEOUT_SECONDS", 0.02)
    queued = asyncio.create_task(
        controller.acquire("next", on_queued=hung_notice)
    )
    await notice_started.wait()
    await controller.release("existing")

    await asyncio.wait_for(queued, timeout=0.2)
    assert controller.snapshot().active_task_ids == ("next",)


@pytest.mark.asyncio
async def test_sync_surface_returns_explicit_rejection_result():
    controller = AgentAdmissionController(max_parallel=1, queue_limit=0)
    install_gateway_admission(controller, asyncio.get_running_loop())
    await controller.acquire("existing")

    @gateway_admitted_sync(
        "cron",
        id_kwargs=("job",),
        rejected_result_factory=lambda exc: (False, str(exc)),
    )
    def cron_turn(job: dict):
        return True, job["id"]

    try:
        result = await asyncio.to_thread(cron_turn, {"id": "overflow"})
        assert result[0] is False
        assert "queue is full" in result[1]
    finally:
        clear_gateway_admission(controller)


def test_all_gateway_agent_entry_points_declare_global_admission():
    from cron.scheduler import run_job
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.run import GatewayRunner

    assert GatewayRunner._run_background_task_inner._gateway_admission_surface == "background"
    assert APIServerAdapter._run_agent._gateway_admission_surface == "api"
    assert run_job._gateway_admission_surface == "cron"


@pytest.mark.asyncio
async def test_sync_surface_fails_closed_instead_of_deadlocking_gateway_loop():
    controller = AgentAdmissionController(max_parallel=1)
    install_gateway_admission(controller, asyncio.get_running_loop())

    @gateway_admitted_sync("cron", id_kwargs=("job",))
    def cron_turn(job: dict):
        return job["id"]

    try:
        with pytest.raises(RuntimeError, match="event-loop thread"):
            cron_turn({"id": "unsafe-direct-call"})
    finally:
        clear_gateway_admission(controller)


def test_change_callback_observes_queue_start_and_release():
    changes: list[int] = []
    controller = AgentAdmissionController(
        max_parallel=1,
        memory_reader=lambda: 4096,
        on_change=lambda: changes.append(1),
    )

    async def scenario():
        await controller.acquire("one")
        waiter = asyncio.create_task(controller.acquire("two"))
        await asyncio.sleep(0.02)
        await controller.release("one")
        await waiter
        await controller.release("two")

    asyncio.run(scenario())
    assert len(changes) >= 5


def test_cross_thread_status_refresh_is_marshaled_onto_gateway_loop():
    callbacks = []

    class Loop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            callbacks.append(callback)

    changes: list[int] = []
    controller = AgentAdmissionController(
        max_parallel=1,
        on_change=lambda: changes.append(1),
    )
    install_gateway_admission(controller, Loop())
    try:
        notify_gateway_admission_changed()
        assert changes == []
        assert callbacks == [controller._notify_change]
        callbacks.pop()()
        assert changes == [1]
    finally:
        clear_gateway_admission(controller)
