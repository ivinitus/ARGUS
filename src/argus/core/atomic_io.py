"""Atomic file writes shared across the pipeline (stdlib-only)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_json", "atomic_write_text"]


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace `path` with `text` (UTF-8).

    audit.json and state files are read concurrently by the report
    server and Slack bot while writers rewrite them; the temp file lives
    in the destination directory so os.replace is a same-filesystem
    rename — readers see the old or the new complete file, never a
    partial one, even across a SIGKILL mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old_mode: int | None = os.stat(path).st_mode
    except OSError:
        old_mode = None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(path.parent), delete=False,
            prefix=f".{path.name}.", suffix=".tmp",
        ) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        if old_mode is not None:
            os.chmod(tmp_path, old_mode)
        os.replace(tmp_path, path)
        tmp_path = None
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Atomically replace `path` with `data` serialized as JSON."""
    atomic_write_text(path, json.dumps(data, indent=indent))
