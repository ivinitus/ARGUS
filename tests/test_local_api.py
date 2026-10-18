"""The personal dashboard keeps review/rule state local and rejects foreign origins."""
from __future__ import annotations

import functools
import _socket
import socket
import http.client
import http.server
import json
from pathlib import Path
import threading
import uuid

import pytest

from argus.config import ArgusConfig
from argus.operations.local_api import LocalApi, LocalReviewData
from argus.operations.review_service import ReviewError
from argus.operations.rule_service import RuleError
from argus.reporting.rule_data import LocalRuleReports
from argus.reporting.server import _ArgusHandler


def saved_report(root: Path) -> Path:
    folder = root / "Regression"
    execution = folder / "QA-E1"
    execution.mkdir(parents=True)
    (execution / "metadata.json").write_text(json.dumps({"test_results": [{
        "executed_by": "tester", "test_case_name": "Sample", "testrun_key": "QA-C1",
        "status": "Pass", "steps": [{"index": 0, "description": "Open the page", "status": "Pass"}],
    }]}))
    (execution / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass", "analysis_status": "complete", "findings": [],
    }))
    (execution / "evidence.png").write_bytes(b"sample evidence")
    (folder / "_argus.html").write_text("<html>Saved audit</html>")
    return folder


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    settings = ArgusConfig()
    settings.general.team_slug = "web"
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.out_dir = tmp_path / "output"
    saved_report(settings.extractor.out_dir)
    monkeypatch.setenv("ARGUS_OPERATOR_NAME", "reviewer")
    api = LocalApi(settings, {"web": settings.extractor.out_dir})
    api.reviews["web"].resolve_reviewers = lambda aliases: [
        {"alias": alias, "jira_key": alias, "name": alias.title()} for alias in aliases
    ]
    return api


def request(api, method, path, body=None):
    return api.handle(method, path, body or {}, {}, uuid.uuid4().hex)


def test_local_review_assignment_and_rules_survive_reopening(local_api):
    api = local_api
    session = request(api, "GET", "/session")
    assert session["alias"] == "reviewer"
    route = "/teams/web/reviews/Regression/"
    queue = request(api, "GET", route + "queue")
    preview = request(api, "POST", route + "preview", {
        "version": queue["version"], "report_revision": queue["report_revision"],
        "roster": [{"alias": "reviewer"}],
    })
    queue = request(api, "GET", route + "queue")
    request(api, "POST", route + "allocate", {
        "version": queue["version"], "report_revision": queue["report_revision"],
        "preview_id": preview["preview_id"],
    })
    detail = request(api, "GET", route + "items/QA-E1")
    assert detail["assignee"] == "reviewer"
    assert detail["case"]["evidence"][0]["url"].startswith("/_review_assets/web/")
    request(api, "POST", route + "items/QA-E1", {
        "revision": detail["case"]["revision"], "review_version": detail["review_version"],
        "state": "reviewed", "decisions": {}, "attested": True,
    })
    rules = request(api, "GET", "/teams/web/rules")
    document = rules["active"]["document"]
    document["policy"]["completion_threshold"] = 0.77
    request(api, "POST", "/teams/web/rules/draft", {"version": rules["version"], "document": document})
    reopened = LocalApi(api.settings["web"], api.roots)
    assert request(reopened, "GET", route + "queue")["totals"]["reviewed"] == 1
    assert request(reopened, "GET", "/teams/web/rules")["draft"]["document"]["policy"]["completion_threshold"] == 0.77


def test_local_catalog_pins_inputs_and_rejects_symlinks(tmp_path):
    root = tmp_path / "output"
    folder = saved_report(root)
    catalog = LocalRuleReports({"web": root})
    original = catalog.manifest("web", folder.name)
    assert catalog.manifest("web", folder.name) == original
    assert catalog.reports("web")["reports"][0]["count"] == 1
    (folder / "QA-E1/evidence.png").write_bytes(b"changed evidence")
    assert catalog.manifest("web", folder.name)["revision"] != original["revision"]
    with pytest.raises(RuleError):
        catalog.manifest("web", "../output")
    (folder / "escape").symlink_to(tmp_path)
    with pytest.raises(RuleError):
        catalog.manifest("web", folder.name)


def test_review_source_revisions_remain_available_after_new_audit(tmp_path):
    root = tmp_path / "output"
    folder = saved_report(root)
    data = LocalReviewData({"web": root}, tmp_path / "sources")
    original = data.manifest("web", folder.name)["items"][0]
    audit = folder / "QA-E1/audit.json"
    audit.write_text(json.dumps({"overall_verdict": "fail", "analysis_status": "complete", "findings": []}))
    current = data.manifest("web", folder.name)["items"][0]
    assert original["revision"] != current["revision"]
    old_detail = data.detail("web", folder.name, original)
    assert old_detail["verdict"] == "pass"
    (folder / "QA-E1/evidence.png").write_bytes(b"replacement evidence")
    assert (data.state_dir / "assets" / old_detail["evidence"][0]["url"].rsplit("/", 1)[-1]).read_bytes() == b"sample evidence"
    assert data.detail("web", folder.name, current)["verdict"] == "fail"
    with pytest.raises(ReviewError):
        data.manifest("web", "..")


def test_loopback_routes_require_exact_host_origin_and_json(local_api, monkeypatch):
    class Handler(_ArgusHandler):
        pass

    Handler.local_api = local_api
    Handler.out_dir_path = local_api.roots["web"]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        Handler, directory=str(Handler.out_dir_path)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = f"127.0.0.1:{server.server_port}"

    def connect_local_only(connection, target):
        assert target == ("127.0.0.1", server.server_port)
        return _socket.socket.connect(connection, target)

    monkeypatch.setattr(socket.socket, "connect", connect_local_only)

    def exchange(method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, payload
        finally:
            connection.close()

    try:
        assert exchange("GET", "/session")[0] == 200
        assert exchange("GET", "/session", {"Host": f"foreign.example:{server.server_port}"})[0] == 403
        headers = {"Content-Type": "application/json", "X-Argus-Request": "1",
                   "Idempotency-Key": uuid.uuid4().hex}
        assert exchange("POST", "/teams/web/rules/draft", headers, "{}")[0] == 403
        headers["Origin"] = "http://foreign.example"
        assert exchange("POST", "/teams/web/rules/draft", headers, "{}")[0] == 403
        headers["Origin"] = f"http://{address}"
        state = request(local_api, "GET", "/teams/web/rules")
        body = json.dumps({"version": state["version"], "document": state["active"]["document"]})
        assert exchange("POST", "/teams/web/rules/draft", headers, body)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_editor_and_worker_share_explicit_rule_database(local_api, tmp_path, monkeypatch):
    from argus.operations import rule_runtime

    database = tmp_path / "custom" / "policy.sqlite"
    monkeypatch.setenv("ARGUS_RULE_DATABASE", str(database))
    api = LocalApi(local_api.settings["web"], local_api.roots)
    state = request(api, "GET", "/teams/web/rules")
    assert database.is_file()
    try:
        rule_runtime.configure(api.settings["web"])
        assert rule_runtime.current()["sha256"] == state["active"]["sha256"]
    finally:
        rule_runtime.reset()
