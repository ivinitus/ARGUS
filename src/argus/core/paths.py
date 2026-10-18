"""Runtime path resolution: project root, output root, and the argus CLI."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_ROOT_MARKERS = ("pyproject.toml", "cron_wrapper.sh")

_ENV_ROOT = "ARGUS_PROJECT_ROOT"

_cached_root: Path | None = None


def project_root() -> Path:
    """Best-effort repository root, in descending order of reliability."""
    global _cached_root
    if _cached_root is not None:
        return _cached_root

    env = os.environ.get(_ENV_ROOT)
    if env:
        _cached_root = Path(env).expanduser().resolve()
        return _cached_root

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            _cached_root = candidate
            return _cached_root

    _cached_root = Path.cwd().resolve()
    return _cached_root


def reset_cache() -> None:
    """Forget the cached root. For tests that manipulate the environment."""
    global _cached_root
    _cached_root = None


def output_root(settings=None) -> Path:
    """The `output/` tree: `settings.extractor.out_dir`, made absolute."""
    if settings is None:
        return project_root() / "output"
    out_dir = Path(settings.extractor.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = project_root() / out_dir
    return out_dir.resolve()


def argus_executable() -> str:
    """Path to the `argus` CLI for spawning a subprocess."""
    venv_script = project_root() / ".venv" / "bin" / "argus"
    if venv_script.exists():
        return str(venv_script)
    found = shutil.which("argus")
    if found:
        return found
    return f"{sys.executable} -m argus"


def argus_argv() -> list[str]:
    """`argus_executable()` as an argv prefix, safe for `shell=False`."""
    return argus_executable().split(" ")


def cron_wrapper_script() -> Path | None:
    """The cron wrapper, or None when it isn't shipped (wheel install)."""
    script = project_root() / "cron_wrapper.sh"
    return script if script.exists() else None
