from __future__ import annotations

import logging
import stat
import sys
import threading
from pathlib import Path

log = logging.getLogger("argus.secrets")

_warned: set[str] = set()
_warned_lock = threading.Lock()


def _too_open(path: Path) -> int | None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if not mode & (stat.S_IRGRP | stat.S_IROTH
                   | stat.S_IWGRP | stat.S_IWOTH):
        return None
    return mode


def _alert_once(key: str, message: str) -> None:
    with _warned_lock:
        already = key in _warned
        _warned.add(key)
    if already:
        return
    sys.stderr.write(f"\n[ARGUS] ALERT: {message}\n")
    sys.stderr.flush()
    log.warning(message)


def warn_if_world_readable(path: Path) -> bool:
    """Warn once per path and return whether its permissions are too open.

    Advisory only — for files ARGUS does not own (.env, a caller-owned credential file).
    Files that exist solely to hold an ARGUS secret should go through
    refuse_if_world_accessible instead.
    """
    mode = _too_open(path)
    if mode is None:
        return False
    _alert_once(str(path), (
        f"{path} is group/world accessible "
        f"(mode={stat.S_IMODE(mode):o}) — run `chmod 600 {path}`"))
    return True


def refuse_if_world_accessible(path: Path,
                               label: str = "credential file") -> bool:
    """Fail closed on a loose-permission secret file.

    Returns True when the caller must NOT use the file's contents; alerts
    once per path with the remediation.
    """
    mode = _too_open(path)
    if mode is None:
        return False
    _alert_once(str(path), (
        f"{path} is group/world accessible "
        f"(mode={stat.S_IMODE(mode):o}) — ignoring this {label} until you "
        f"run `chmod 600 {path}`"))
    return True


def reset_warned_cache() -> None:
    with _warned_lock:
        _warned.clear()
