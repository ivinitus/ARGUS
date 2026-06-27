
from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_FRAMES = ("|", "/", "-", "\\")


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
            # Non-TTY: just announce, no animation, no in-place updates.
            self.stream.write(f"[ ] {self.label}\n")
            self.stream.flush()
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
        line = f"[{tag}] {text}  ({elapsed:0.1f}s)"
        if self._tty:
            # Overwrite the spinner line, pad to wipe residual chars.
            self.stream.write(f"\r{line}{' ' * 20}\n")
        else:
            self.stream.write(line + "\n")
        self.stream.flush()


@contextmanager
def step(label: str) -> Iterator[_Spinner]:
    """Use as: `with step('doing thing') as s: ...; s.finish('ok', 'done')`

    While the block runs, the `argus` logger is silenced to CRITICAL so stray
    warnings from lower layers don't mangle the spinner line. Original level
    is restored on exit.

    If the block raises, we finish with `[x]` and then the exception
    propagates — the caller can print a clean one-liner or the traceback
    will appear cleanly on a fresh line below.
    """
    sp = _Spinner(label)
    argus_log = logging.getLogger("argus")
    prev_level = argus_log.level
    argus_log.setLevel(logging.CRITICAL)
    sp.start()
    try:
        yield sp
    except BaseException as e:
        sp.finish("x", f"{label} — {e.__class__.__name__}")
        raise
    finally:
        argus_log.setLevel(prev_level)


def done(label: str, detail: str = "") -> None:
    """Standalone terminal line (no spinner), e.g. the final summary."""
    suffix = f"  {detail}" if detail else ""
    sys.stderr.write(f"[ok] {label}{suffix}\n")
    sys.stderr.flush()
