"""Cooperative SIGTERM handling for audit tasks (`/argus stop`, process shutdown).

The operating system delivers SIGTERM to the audit process when an operator runs
``/argus stop`` or the task is drained, then SIGKILL after ``stopTimeout``
(120 s in the beta stack). This module turns that signal into one
predictable sequence:

1. ``requested()`` flips to ``True`` so cooperative loops can poll it.
2. Every callback registered with ``register_on_stop`` runs (best effort,
   each guarded and logged); the orchestrator uses one to write an
   owner-checked ``stopped`` status and clear ``current_audit``.
3. One log line is emitted, stdio is flushed, and the process exits with
   status 143 (128 + SIGTERM) via ``os._exit``.

``os._exit`` is deliberate: worker threads may be mid-model call or
blocked on Jira I/O and cannot be joined within the stop timeout. Every
artefact write in ARGUS is atomic (temp file + rename)
and leases/locks are stale-recoverable, so an abrupt exit leaves no
half-written state behind — only work that the next run redoes.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Callable

log = logging.getLogger("argus.stop_signal")

SIGTERM_EXIT_CODE = 143

_EVENT = threading.Event()
_CALLBACKS: list[Callable[[], None]] = []
_CALLBACKS_LOCK = threading.Lock()
_INSTALLED = False
_exit: Callable[[int], None] = os._exit


def requested() -> bool:
    """Whether SIGTERM has been received; poll this from long loops."""
    return _EVENT.is_set()


def register_on_stop(callback: Callable[[], None]) -> Callable[[], None]:
    """Run ``callback`` on SIGTERM; returns a function that unregisters it."""
    with _CALLBACKS_LOCK:
        _CALLBACKS.append(callback)

    def unregister() -> None:
        with _CALLBACKS_LOCK:
            try:
                _CALLBACKS.remove(callback)
            except ValueError:
                pass

    return unregister


def handle_stop(signum: int = signal.SIGTERM, _frame: object = None) -> None:
    """Signal handler body; tests call this directly instead of signalling."""
    already = _EVENT.is_set()
    _EVENT.set()
    if already:
        # A second SIGTERM while callbacks run: do not re-enter them.
        return
    with _CALLBACKS_LOCK:
        callbacks = list(_CALLBACKS)
    for callback in reversed(callbacks):
        try:
            callback()
        except Exception as exc:
            log.warning(
                "stop callback %s failed: %s: %s",
                getattr(callback, "__qualname__", repr(callback)),
                type(exc).__name__,
                exc,
            )
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    log.warning(
        "[ARGUS] %s received; audit stopped, exiting %d",
        signal_name,
        SIGTERM_EXIT_CODE,
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    _exit(SIGTERM_EXIT_CODE)


def install() -> bool:
    """Install the SIGTERM handler once; only the main thread may do so."""
    global _INSTALLED
    if _INSTALLED:
        return True
    if threading.current_thread() is not threading.main_thread():
        log.debug("SIGTERM handler not installed outside the main thread")
        return False
    signal.signal(signal.SIGTERM, handle_stop)
    _INSTALLED = True
    return True
