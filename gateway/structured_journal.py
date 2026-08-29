"""Small nonblocking native-journal emitter for trusted gateway evidence."""

from __future__ import annotations

import socket


JOURNAL_SOCKET = "/run/systemd/journal/socket"


def emit_native_journal(
    payload: bytes,
    *,
    message_prefix: bytes,
    identifier: str,
    event: str,
    priority: int = 5,
    journal_socket: str = JOURNAL_SOCKET,
) -> bool:
    """Send one bounded native-journal datagram without blocking the caller."""
    if not payload or b"\n" in message_prefix:
        return False
    try:
        safe_identifier = str(identifier).encode("ascii")
        safe_event = str(event).encode("ascii")
        safe_priority = str(int(priority)).encode("ascii")
        message = b"\n".join(
            (
                b"MESSAGE=" + message_prefix + payload,
                b"PRIORITY=" + safe_priority,
                b"SYSLOG_IDENTIFIER=" + safe_identifier,
                b"HERMES_EVENT=" + safe_event,
            )
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.connect(journal_socket)
            sender.send(message)
        return True
    except (OSError, TypeError, ValueError, UnicodeError):
        return False
