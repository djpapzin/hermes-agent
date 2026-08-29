"""Minimal, optional systemd ``sd_notify`` support for the gateway."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


def _notify_address(raw: str) -> str:
    """Translate systemd's ``@abstract`` notation to Python's address form."""
    return "\0" + raw[1:] if raw.startswith("@") else raw


def notify(message: str) -> bool:
    """Send one nonblocking sd_notify datagram when systemd configured it.

    Notification failures are deliberately non-fatal: a missing socket or an
    older platform must never prevent the gateway from starting.
    """
    address = os.environ.get("NOTIFY_SOCKET", "").strip()
    if not address:
        return False
    if not isinstance(message, str) or not message:
        return False
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        payload = message.encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            # A full receiver buffer must not stall the gateway event loop.
            sender.setblocking(False)
            sender.connect(_notify_address(address))
            sender.send(payload)
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def watchdog_interval_seconds() -> Optional[float]:
    """Return systemd's configured watchdog interval in seconds."""
    if not os.environ.get("NOTIFY_SOCKET", "").strip():
        return None
    if not hasattr(socket, "AF_UNIX"):
        return None
    raw = os.environ.get("WATCHDOG_USEC", "").strip()
    if not raw:
        return None
    try:
        interval = float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(interval) or interval <= 0:
        return None
    return interval


class SystemdWatchdog:
    """Feed systemd while the asyncio event loop continues to make progress."""

    def __init__(
        self,
        *,
        config_enabled: bool = True,
        lag_tolerance_seconds: Optional[float] = None,
    ) -> None:
        self._config_enabled = bool(config_enabled)
        self.interval_seconds = watchdog_interval_seconds()
        self._lag_tolerance_seconds = lag_tolerance_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._unhealthy = False
        self._expired = False
        self._last_heartbeat_at: Optional[float] = None
        self._stopping = False
        self._stopping_notified = False
        self._shutdown_keepalive: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self._config_enabled and self.interval_seconds is not None

    @property
    def unhealthy(self) -> bool:
        return self._unhealthy

    @property
    def task(self) -> Optional[asyncio.Task[None]]:
        return self._task

    def _lag_tolerance(self) -> float:
        interval = self.interval_seconds or 0.0
        configured = self._lag_tolerance_seconds
        if configured is None:
            return max(0.1, interval * 0.25)
        try:
            value = float(configured)
        except (TypeError, ValueError):
            return max(0.1, interval * 0.25)
        if not math.isfinite(value):
            return max(0.1, interval * 0.25)
        return max(0.0, value)

    def start(self) -> bool:
        """Start the loop-progress sampler when systemd watchdog is enabled."""
        if not self.enabled:
            return False
        if self._task is not None and not self._task.done():
            return True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stopping = False
        self._unhealthy = False
        self._expired = False
        self._last_heartbeat_at = None
        self._stopping_notified = False
        self._task = asyncio.create_task(self._run(), name="hermes-systemd-watchdog")
        return True

    def ready(self, status: str = "Gateway running") -> bool:
        """Tell systemd that startup completed and the gateway is ready."""
        if not self.enabled:
            return False
        safe_status = str(status or "Gateway running").replace("\n", " ")
        sent = notify(f"READY=1\nSTATUS={safe_status}")
        if sent:
            self._last_heartbeat_at = time.monotonic()
        return sent

    def record_tick(self, *, scheduled_at: float, now: float) -> bool:
        """Feed systemd whenever the event loop demonstrates forward progress.

        ``WatchdogSec`` is the authoritative hard deadline.  A callback that
        runs late still proves that the loop recovered before that deadline;
        withholding this heartbeat would turn one transient delay into a
        guaranteed service abort even after the gateway became responsive.
        """
        if not self.enabled or self._stopping or self._expired:
            return False
        try:
            lag = float(now) - float(scheduled_at)
        except (TypeError, ValueError):
            lag = float("inf")
        interval = self.interval_seconds or 0.0
        last_heartbeat_at = self._last_heartbeat_at
        if (
            last_heartbeat_at is not None
            and (
                not math.isfinite(now)
                or now - last_heartbeat_at >= interval
            )
        ):
            self._unhealthy = True
            self._expired = True
            notify("STATUS=watchdog expired: hard deadline exceeded")
            return False

        was_unhealthy = self._unhealthy
        self._unhealthy = not math.isfinite(lag) or lag > self._lag_tolerance()
        if self._unhealthy:
            message = "WATCHDOG=1\nSTATUS=watchdog delayed: event loop recovered"
        elif was_unhealthy:
            message = "WATCHDOG=1\nSTATUS=Hermes Gateway running"
        else:
            message = "WATCHDOG=1"
        sent = notify(message)
        if sent:
            self._last_heartbeat_at = now
        return sent

    def _retry_delay(self, *, now: float, cadence: float) -> float:
        """Retry strictly before the last successful heartbeat deadline."""
        last_success = self._last_heartbeat_at
        interval = self.interval_seconds
        if last_success is None or interval is None:
            return max(0.0, cadence / 4.0)
        remaining = interval - (now - last_success)
        return max(0.0, min(cadence / 4.0, remaining / 2.0))

    def _send_shutdown_heartbeat(
        self,
        *,
        stopping_confirmed: bool,
        now: float,
        cadence: float,
    ) -> tuple[bool, float]:
        """Send STOPPING until confirmed, then heartbeat within the deadline."""
        message = (
            "WATCHDOG=1"
            if stopping_confirmed
            else "STOPPING=1\nWATCHDOG=1\nSTATUS=Hermes Gateway draining"
        )
        sent = notify(message)
        if sent:
            self._last_heartbeat_at = now
            return True, cadence
        return stopping_confirmed, self._retry_delay(now=now, cadence=cadence)

    async def _run(self) -> None:
        interval = self.interval_seconds
        if interval is None:
            return
        cadence = max(0.01, interval / 2.0)
        loop = asyncio.get_running_loop()
        scheduled_at = loop.time() + cadence
        try:
            while not self._stopping:
                await asyncio.sleep(max(0.0, scheduled_at - loop.time()))
                now = loop.time()
                sent = self.record_tick(scheduled_at=scheduled_at, now=now)
                if self._expired:
                    return
                if not sent:
                    retry_delay = self._retry_delay(now=now, cadence=cadence)
                    scheduled_at = now + retry_delay
                    continue
                scheduled_at += cadence
                if scheduled_at < now:
                    scheduled_at = now + cadence
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        """Enter stopping state without letting systemd misclassify a slow drain.

        The normal heartbeat is loop-progress sensitive. During an intentional
        stop the gateway has its own bounded shutdown watchdog, so a daemon
        thread keeps the service-manager watchdog fed while adapters and active
        turns drain. This prevents a planned SIGTERM from becoming SIGABRT.
        """
        self._stopping = True
        task = self._task
        current = asyncio.current_task()
        if task is not None and task is not current:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._task = None
        interval_seconds = self.interval_seconds
        last_heartbeat_at = self._last_heartbeat_at
        if (
            self.enabled
            and not self._expired
            and interval_seconds is not None
            and last_heartbeat_at is not None
            and time.monotonic() - last_heartbeat_at >= interval_seconds
        ):
            self._unhealthy = True
            self._expired = True
            notify("STATUS=watchdog expired: hard deadline exceeded during shutdown")
        if self.enabled and not self._expired and not self._stopping_notified:
            self._stopping_notified = True
            interval = max(0.1, (self.interval_seconds or 1.0) / 2.0)
            stopping_confirmed, initial_delay = self._send_shutdown_heartbeat(
                stopping_confirmed=False,
                now=time.monotonic(),
                cadence=interval,
            )

            def _feed_during_shutdown() -> None:
                # The process-wide shutdown watchdog remains the hard bound.
                # This cap merely prevents a leaked daemon from running forever
                # if a caller invokes stop() outside process teardown.
                deadline = time.monotonic() + 900.0
                delay = initial_delay
                confirmed = stopping_confirmed
                while time.monotonic() < deadline:
                    time.sleep(delay)
                    now = time.monotonic()
                    last_heartbeat_at = self._last_heartbeat_at
                    watchdog_interval = self.interval_seconds
                    if (
                        last_heartbeat_at is not None
                        and watchdog_interval is not None
                        and now - last_heartbeat_at >= watchdog_interval
                    ):
                        self._unhealthy = True
                        self._expired = True
                        notify(
                            "STATUS=watchdog expired: hard deadline exceeded "
                            "during shutdown"
                        )
                        return
                    confirmed, delay = self._send_shutdown_heartbeat(
                        stopping_confirmed=confirmed,
                        now=now,
                        cadence=interval,
                    )

            try:
                self._shutdown_keepalive = threading.Thread(
                    target=_feed_during_shutdown,
                    name="hermes-systemd-shutdown-watchdog",
                    daemon=True,
                )
                self._shutdown_keepalive.start()
            except Exception as exc:
                self._shutdown_keepalive = None
                logger.warning(
                    "Could not start systemd shutdown keepalive; "
                    "continuing bounded teardown: %s",
                    exc,
                )
