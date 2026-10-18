"""Shared test setup: import path, HOME isolation, and a network guard."""
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

# Keep installed browser binaries discoverable after isolating application state.
# CI can supply PLAYWRIGHT_BROWSERS_PATH explicitly.
_BROWSER_CACHE = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    if sys.platform == "win32"
    else Path.home() / "Library/Caches" if sys.platform == "darwin"
    else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
) / "ms-playwright"
if _BROWSER_CACHE.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSER_CACHE))

_FAKE_HOME = tempfile.mkdtemp(prefix="argus-test-home-")
os.environ["HOME"] = _FAKE_HOME

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(
                f"test attempted a network connection to {address!r} — "
                "mock the client (Model API/Jira/Slack) instead")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
