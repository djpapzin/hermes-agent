"""Resource-aware, FIFO admission control for gateway agent turns."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_QUEUE_NOTICE_TIMEOUT_SECONDS = 5.0

_registry_lock = threading.Lock()
_gateway_controller: Optional["AgentAdmissionController"] = None
_gateway_loop: Optional[asyncio.AbstractEventLoop] = None


def install_gateway_admission(
    controller: "AgentAdmissionController",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Publish the gateway-wide controller to cron/API/background surfaces.

    Gateway-owned agent entry points do not all live in ``gateway.run``:
    cron executes in scheduler threads and the API server owns a separate
    runner.  A single installed controller keeps those paths under the same
    global capacity and memory-headroom decision.
    """
    global _gateway_controller, _gateway_loop
    with _registry_lock:
        _gateway_controller = controller
        _gateway_loop = loop


def clear_gateway_admission(
    controller: Optional["AgentAdmissionController"] = None,
) -> None:
    """Clear the registry, optionally only when *controller* still owns it."""
    global _gateway_controller, _gateway_loop
    with _registry_lock:
        if controller is not None and _gateway_controller is not controller:
            return
        _gateway_controller = None
        _gateway_loop = None


def _installed_gateway_admission() -> tuple[
    Optional["AgentAdmissionController"], Optional[asyncio.AbstractEventLoop]
]:
    with _registry_lock:
        return _gateway_controller, _gateway_loop


def notify_gateway_admission_changed() -> None:
    """Refresh gateway status after a surface releases its own bookkeeping."""
    controller, _loop = _installed_gateway_admission()
    if controller is not None:
        controller._notify_change()


def _decorated_task_id(
    prefix: str,
    id_kwargs: tuple[str, ...],
    signature: inspect.Signature,
    args: tuple,
    kwargs: dict,
) -> str:
    try:
        bound = signature.bind_partial(*args, **kwargs)
        for name in id_kwargs:
            value = bound.arguments.get(name)
            if isinstance(value, dict):
                value = value.get("id")
            if value:
                return f"{prefix}:{value}:{uuid.uuid4().hex[:8]}"
    except (TypeError, ValueError):
        pass
    return f"{prefix}:{uuid.uuid4().hex}"


def gateway_admitted_async(
    prefix: str,
    *,
    id_kwargs: tuple[str, ...],
    queued_notice_method: Optional[str] = None,
    queued_notice_kwargs: tuple[str, ...] = (),
):
    """Decorate an async gateway agent entry point with global admission.

    ``queued_notice_method`` names an async method on the decorated object's
    first (``self``) argument.  Only the explicitly named context arguments
    are forwarded, which prevents prompts or other sensitive call arguments
    from leaking into queue-status delivery.
    """
    def decorate(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            controller, _loop = _installed_gateway_admission()
            if controller is None:
                return await func(*args, **kwargs)
            task_id = _decorated_task_id(prefix, id_kwargs, signature, args, kwargs)
            on_queued = None
            if queued_notice_method:
                bound = signature.bind_partial(*args, **kwargs)
                instance = bound.arguments.get("self")
                handler = getattr(instance, queued_notice_method)
                context = {
                    name: bound.arguments.get(name)
                    for name in queued_notice_kwargs
                }

                async def on_queued(message: str) -> None:
                    result = handler(message, **context)
                    if inspect.isawaitable(result):
                        await result

            await controller.acquire(task_id, on_queued=on_queued)
            outcome = "finished"
            try:
                return await func(*args, **kwargs)
            except BaseException:
                outcome = "crashed"
                raise
            finally:
                await controller.release(task_id, outcome=outcome)

        wrapped._gateway_admission_surface = prefix
        return wrapped

    return decorate


def gateway_admitted_sync(
    prefix: str,
    *,
    id_kwargs: tuple[str, ...],
    rejected_result_factory: Optional[Callable[["AdmissionRejected"], object]] = None,
):
    """Decorate a scheduler-thread agent entry point with global admission.

    All controller mutation remains on the gateway event loop.  Standalone
    cron invocations have no installed gateway controller and retain their
    historical behavior.
    """
    def decorate(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            controller, loop = _installed_gateway_admission()
            if controller is None or loop is None or not loop.is_running():
                return func(*args, **kwargs)
            if getattr(loop, "_thread_id", None) == threading.get_ident():
                raise RuntimeError(
                    "Synchronous gateway agent entry point cannot wait for "
                    "admission on the gateway event-loop thread"
                )
            task_id = _decorated_task_id(prefix, id_kwargs, signature, args, kwargs)
            acquired = asyncio.run_coroutine_threadsafe(
                controller.acquire(task_id), loop
            )
            try:
                acquired.result()
            except AdmissionRejected as exc:
                if rejected_result_factory is not None:
                    return rejected_result_factory(exc)
                raise
            outcome = "finished"
            try:
                return func(*args, **kwargs)
            except BaseException:
                outcome = "crashed"
                raise
            finally:
                released = asyncio.run_coroutine_threadsafe(
                    controller.release(task_id, outcome=outcome), loop
                )
                try:
                    released.result(timeout=10)
                except (concurrent.futures.TimeoutError, RuntimeError):
                    logger.error(
                        "Timed out releasing admission slot for %s", task_id
                    )

        wrapped._gateway_admission_surface = prefix
        return wrapped

    return decorate


def host_available_memory_mb(meminfo_path: Path = Path("/proc/meminfo")) -> Optional[int]:
    """Return Linux MemAvailable in MiB, or None when unavailable."""
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def cgroup_available_memory_mb(
    cgroup_file: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Optional[int]:
    """Return remaining cgroup-v2 MemoryMax headroom in MiB."""
    try:
        relative = next(
            line.partition("::")[2].lstrip("/")
            for line in cgroup_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("0::")
        )
        root = cgroup_root / relative
        raw_max = (root / "memory.max").read_text(encoding="utf-8").strip()
        raw_current = (root / "memory.current").read_text(encoding="utf-8").strip()
        if not raw_max.isdigit() or not raw_current.isdigit():
            return None
        return max(0, int(raw_max) - int(raw_current)) // (1024 * 1024)
    except (OSError, StopIteration, ValueError):
        return None


def available_resource_memory_mb() -> Optional[int]:
    """Return the tighter of host and current-cgroup memory headroom."""
    candidates = [host_available_memory_mb(), cgroup_available_memory_mb()]
    finite = [value for value in candidates if value is not None]
    return min(finite) if finite else None


@dataclass(frozen=True)
class AdmissionSnapshot:
    active: int
    queued: int
    max_parallel: Optional[int]
    available_memory_mb: Optional[int]
    host_available_memory_mb: Optional[int]
    cgroup_available_memory_mb: Optional[int]
    min_headroom_mb: int
    active_task_ids: tuple[str, ...]
    queued_task_ids: tuple[str, ...]


class AdmissionRejected(RuntimeError):
    """Raised when the bounded queue is full or the controller is closing."""


class AgentAdmissionController:
    """Admit new turns FIFO without disturbing already-running work."""

    def __init__(
        self,
        *,
        max_parallel: Optional[int],
        min_headroom_mb: int = 0,
        queue_limit: int = 32,
        poll_interval_seconds: float = 2.0,
        memory_reader: Callable[[], Optional[int]] = available_resource_memory_mb,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.max_parallel = max_parallel if max_parallel and max_parallel > 0 else None
        self.min_headroom_mb = max(0, int(min_headroom_mb or 0))
        self.queue_limit = max(0, int(queue_limit or 0))
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds or 2.0))
        self._memory_reader = memory_reader
        self._on_change = on_change
        self._condition = asyncio.Condition()
        self._active: set[str] = set()
        self._queue: list[str] = []
        self._closed_reason: Optional[str] = None

    def set_change_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_change = callback

    def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            logger.debug("Admission change callback failed", exc_info=True)

    def snapshot(self) -> AdmissionSnapshot:
        available = self._memory_reader()
        return AdmissionSnapshot(
            active=len(self._active),
            queued=len(self._queue),
            max_parallel=self.max_parallel,
            available_memory_mb=available,
            host_available_memory_mb=host_available_memory_mb(),
            cgroup_available_memory_mb=cgroup_available_memory_mb(),
            min_headroom_mb=self.min_headroom_mb,
            active_task_ids=tuple(sorted(self._active)),
            queued_task_ids=tuple(self._queue),
        )

    def _capacity_reason(self, available_mb: Optional[int]) -> Optional[str]:
        if self.max_parallel is not None and len(self._active) >= self.max_parallel:
            return f"parallel-agent capacity ({len(self._active)}/{self.max_parallel})"
        if (
            self.min_headroom_mb > 0
            and available_mb is not None
            and available_mb < self.min_headroom_mb
        ):
            return (
                "memory headroom "
                f"({available_mb} MiB available; {self.min_headroom_mb} MiB required)"
            )
        return None

    def _log(self, decision: str, task_id: str, reason: str = "") -> None:
        snap = self.snapshot()
        logger.info(
            "HERMES_ADMISSION %s",
            json.dumps(
                {
                    "decision": decision,
                    "task_id": task_id,
                    "reason": reason,
                    "active_workers": snap.active,
                    "queued_tasks": snap.queued,
                    "max_parallel": snap.max_parallel,
                    "available_memory_mb": snap.available_memory_mb,
                    "host_available_memory_mb": snap.host_available_memory_mb,
                    "cgroup_available_memory_mb": snap.cgroup_available_memory_mb,
                    "min_headroom_mb": snap.min_headroom_mb,
                },
                sort_keys=True,
            ),
        )

    async def acquire(
        self,
        task_id: str,
        *,
        on_queued: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Wait for capacity and reserve a slot for *task_id*."""
        task_id = str(task_id)
        queued_notice_sent = False
        async with self._condition:
            try:
                while True:
                    if self._closed_reason is not None:
                        raise AdmissionRejected(self._closed_reason)
                    if task_id in self._active:
                        raise AdmissionRejected(
                            f"Agent task {task_id} already owns an active slot."
                        )
                    available = self._memory_reader()
                    at_front = not self._queue or self._queue[0] == task_id
                    reason = self._capacity_reason(available)
                    if at_front and reason is None:
                        if self._queue and self._queue[0] == task_id:
                            self._queue.pop(0)
                        self._active.add(task_id)
                        self._log("start", task_id)
                        self._notify_change()
                        return
                    if task_id not in self._queue:
                        if self.queue_limit == 0 or len(self._queue) >= self.queue_limit:
                            self._log("reject", task_id, "queue full")
                            raise AdmissionRejected(
                                f"Agent queue is full ({len(self._queue)}/{self.queue_limit}). "
                                "Existing tasks are still running; please retry later."
                            )
                        self._queue.append(task_id)
                        reason = reason or "waiting for earlier queued task"
                        self._log("queue", task_id, reason)
                        self._notify_change()
                    if not queued_notice_sent and on_queued is not None:
                        queued_notice_sent = True
                        position = self._queue.index(task_id) + 1
                        notice = f"Queued: system at {reason or 'safe capacity'}; position {position}."
                        # Do not hold the controller lock during network I/O.
                        self._condition.release()
                        try:
                            await asyncio.wait_for(
                                on_queued(notice),
                                timeout=_QUEUE_NOTICE_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Timed out delivering admission queue notice for %s",
                                task_id,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Could not deliver admission queue notice for %s: %s",
                                task_id,
                                exc,
                            )
                        finally:
                            await self._condition.acquire()
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(), timeout=self.poll_interval_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
            except BaseException:
                if task_id in self._queue:
                    self._queue.remove(task_id)
                    self._condition.notify_all()
                    self._notify_change()
                raise

    async def release(self, task_id: str, *, outcome: str = "finished") -> None:
        async with self._condition:
            self._active.discard(str(task_id))
            self._log(outcome, str(task_id))
            self._condition.notify_all()
            self._notify_change()

    async def close(self, reason: str) -> tuple[str, ...]:
        """Reject waiters explicitly; running turns remain owned by shutdown."""
        async with self._condition:
            self._closed_reason = str(reason or "Gateway is shutting down; resend after restart.")
            queued = tuple(self._queue)
            self._queue.clear()
            self._condition.notify_all()
            self._notify_change()
            return queued
