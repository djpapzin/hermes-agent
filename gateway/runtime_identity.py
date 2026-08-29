"""Immutable process-local identity for the gateway control plane."""

from __future__ import annotations

import os
from typing import Optional

_CONTROL_PLANE_PID: Optional[int] = None


def mark_gateway_control_plane_process() -> bool:
    """Latch the current gateway PID once, before model-controlled work starts."""
    global _CONTROL_PLANE_PID
    if _CONTROL_PLANE_PID is not None:
        if _CONTROL_PLANE_PID != os.getpid():
            raise RuntimeError("Gateway control-plane identity is already latched")
        return True
    if os.environ.get("_HERMES_GATEWAY") != "1":
        raise RuntimeError("Gateway control-plane marker is unavailable")
    from gateway.restart import is_gateway_supervisor_process

    if not is_gateway_supervisor_process():
        return False
    _CONTROL_PLANE_PID = os.getpid()
    return True


def is_gateway_control_plane_process() -> bool:
    """Return the immutable identity without consulting runtime state files."""
    return _CONTROL_PLANE_PID == os.getpid()
