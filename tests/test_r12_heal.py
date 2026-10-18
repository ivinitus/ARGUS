"""r12_heal probe-error accounting + atomic_io durability contract."""
from __future__ import annotations

import json
import os

from argus.config.models import ArgusConfig
from argus.core import atomic_io
from argus.pipeline import r12_heal


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, params=None, timeout=None):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def test_probe_returns_true_on_evidence():
    session = _Session(_Resp(200, {
        "attachments": [{"name": "session.pdf"}],
    }))
    assert r12_heal.has_new_evidence(session, "https://j", "QA-E1") \
        is True


def test_probe_returns_true_on_xml_evidence():
    session = _Session(_Resp(200, {
        "attachments": [{"name": "automation-results.xml"}],
    }))
    assert r12_heal.has_new_evidence(session, "https://j", "QA-E1") \
        is True


def test_probe_returns_false_when_no_evidence():
    session = _Session(_Resp(200, {"attachments": [],
                                   "testScriptResults": []}))
    assert r12_heal.has_new_evidence(session, "https://j", "QA-E1") \
        is False


def test_probe_returns_none_on_network_error():
    session = _Session(ConnectionError("jira down"))
    assert r12_heal.has_new_evidence(session, "https://j", "QA-E1") \
        is None


def test_probe_returns_none_on_http_error():
    session = _Session(_Resp(503))
    assert r12_heal.has_new_evidence(session, "https://j", "QA-E1") \
        is None


def test_disabled_auto_folder_skips_active_folder_lookup(tmp_path, monkeypatch):
    settings = ArgusConfig()
    settings.folders.auto_folder_enabled = False
    monkeypatch.setattr(
        r12_heal.extractor,
        "build_session",
        lambda unused: (_ for _ in ()).throw(
            AssertionError("disabled auto-folder must not contact Jira")
        ),
    )

    assert r12_heal._resolve_active_folder_dir(tmp_path, settings) is None


def test_heal_counts_probe_errors(tmp_path, monkeypatch):
    exec_dir = tmp_path / "F" / "T" / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns", "summary": "s", "findings": [],
        "variant_name": "no-evidence",
    }))

    monkeypatch.setattr(r12_heal.extractor, "build_session",
                        lambda cfg: _Session(ConnectionError("outage")))

    counters = r12_heal.heal(tmp_path, scope="all", dry_run=True)
    assert counters["r12_total"] == 1
    assert counters["probed"] == 1
    assert counters["probe_errors"] == 1
    assert counters["healed"] == 0


def test_find_r12_audits_includes_unsupported_evidence(tmp_path):
    exec_dir = tmp_path / "F" / "T" / "MOBILE-E1924716"
    exec_dir.mkdir(parents=True)
    audit_path = exec_dir / "audit.json"
    audit_path.write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "unsupported",
        "findings": [],
        "variant_name": "unsupported-evidence",
    }))

    assert r12_heal.find_r12_audits(tmp_path) == [
        ("MOBILE-E1924716", audit_path),
    ]


def test_atomic_write_preserves_existing_mode(tmp_path):
    path = tmp_path / "audit.json"
    path.write_text("{}")
    path.chmod(0o644)

    atomic_io.atomic_write_json(path, {"k": "v"})

    assert json.loads(path.read_text()) == {"k": "v"}
    assert os.stat(path).st_mode & 0o777 == 0o644


def test_atomic_write_new_file_stays_owner_only(tmp_path):
    """No pre-existing file: keep the tempfile's conservative 0600."""
    path = tmp_path / "state.json"
    atomic_io.atomic_write_json(path, {"k": "v"})
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "audit.json"
    atomic_io.atomic_write_text(path, "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["audit.json"]
    assert path.read_text() == "hello"
