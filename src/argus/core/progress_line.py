
from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

try:
    from rich.console import Console
    from rich.text import Text

    _console: Console | None = Console(stderr=True, highlight=False)
except ImportError:
    _console = None

_FRAMES = ("|", "/", "-", "\\")

_TAG_STYLES = {"ok": "green", "x": "red", " ": "dim"}


def _emit(tag: str, text: str, stream=None) -> None:
    """One `[tag] text` status line — rich-styled when available."""
    if _console is not None and (stream is None or stream is sys.stderr):
        line = Text("[")
        line.append(tag, style=_TAG_STYLES.get(tag, "cyan"))
        line.append("] ")
        line.append(text)
        _console.print(line)
        return
    out = stream or sys.stderr
    out.write(f"[{tag}] {text}\n")
    out.flush()


class _Spinner:
    def __init__(self, label: str, stream=sys.stderr, interval: float = 0.1):
        self.label = label
        self.stream = stream
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0
        self._tty = stream.isatty()

    def start(self) -> None:
        self._start_time = time.monotonic()
        if not self._tty:
            _emit(" ", self.label, self.stream)
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for frame in itertools.cycle(_FRAMES):
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - self._start_time
            self.stream.write(f"\r[{frame}] {self.label}  {elapsed:0.1f}s")
            self.stream.flush()
            time.sleep(self.interval)

    def finish(self, tag: str, message: str | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        elapsed = time.monotonic() - self._start_time
        text = message if message is not None else self.label
        if self._tty:
            self.stream.write(f"\r{' ' * (len(self.label) + 20)}\r")
            self.stream.flush()
        _emit(tag, f"{text}  ({elapsed:0.1f}s)", self.stream)


@contextmanager
def step(label: str) -> Iterator[_Spinner]:
    """Manage a progress step and its terminal status."""
    sp = _Spinner(label)
    argus_log = logging.getLogger("argus")
    prev_level = argus_log.level
    # Only an interactive spinner competes with log lines for the terminal;
    # unattended scheduled runs must keep every warning.
    if sp._tty:
        argus_log.setLevel(logging.CRITICAL)
    sp.start()
    try:
        yield sp
    except BaseException as e:
        sp.finish("x", f"{label} — {e.__class__.__name__}")
        raise
    finally:
        if sp._tty:
            argus_log.setLevel(prev_level)


def done(label: str, detail: str = "") -> None:
    """Standalone terminal line (no spinner), e.g. the final summary."""
    suffix = f"  {detail}" if detail else ""
    _emit("ok", f"{label}{suffix}")
