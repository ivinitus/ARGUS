"""Loopback-only rule editor with isolated SQLite state and labelled sample previews."""
from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import cast
from urllib.parse import parse_qs, urlsplit

from argus.operations import rule_authoring_demo, rule_builder_demo
from argus.operations.rule_builder_demo import DemoRuleService
from argus.operations.rule_service import RuleError, initialize, now, revision_key
from argus.operations.rule_store import RuleConflict, SqliteRuleStore
from argus.operations.teams import TEAMS, resolve_team
from argus.reporting.rules_page import render_page

SAMPLES: dict[str, dict] = {
    "DEMO-E101": {"purchase": True, "email": False, "title": "Purchase an audiobook"},
    "DEMO-E102": {"purchase": True, "email": True, "title": "Purchase a membership"},
    "DEMO-E103": {"purchase": False, "email": False, "title": "Play an audiobook"},
}
_SAMPLE_CHECK_LOCK = threading.Lock()


class DemoRuleReports:
    def reports(self, team: str) -> dict:
        return {"reports": [
            {"folder": folder, "title": title, "count": len(samples), "timestamp": "2026-10-07T00:00:00Z"}
            for folder, title, samples in (
                ("rule-builder", "Rule configuration examples", rule_builder_demo.SAMPLES),
                ("sample-tests", "Purchase and playback", SAMPLES),
            )
        ]}

    def manifest(self, team: str, folder: str) -> dict:
        from argus.domain.team_rules import fingerprint
        if folder not in {"sample-tests", "rule-builder"}:
            raise RuleError(404, "Unknown report.")
        samples = rule_builder_demo.SAMPLES if folder == "rule-builder" else SAMPLES
        title = "Rule configuration examples" if folder == "rule-builder" else "Purchase and playback"
        result = {
            "schema": 1, "team": team, "folder": folder, "title": title,
            "timestamp": "2026-10-07T00:00:00Z",
            "items": [{
                "key": key, "title": sample["title"], "run": "DEMO-C1",
                "run_name": title, "status": sample.get("status", "Pass"), "verdict": "pass",
                "analysis_status": "complete", "execution_date": "2026-10-07",
                "source": f"{folder}/{key}", "input_sha256": fingerprint(sample),
            } for key, sample in samples.items()],
        }
        result["revision"] = fingerprint(result)
        return result


def render_demo_reports(team_slug: str | None, *, detail: bool = False) -> str:
    from html import escape
    from argus.reporting import html_report
    from argus.reporting.ui import ReportUiContext, TeamLink

    team = resolve_team(team_slug) if team_slug else None
    context = ReportUiContext(
        dashboard_name=team.name if team else "ARGUS", dashboard_slug=team.slug if team else "",
        teams=tuple(TeamLink(t.slug, t.name) for t in TEAMS),
        team_base_path="/reports/teams", docs_url="",
    )
    root = f"/reports/teams/{team.slug}/" if team else "/reports/"
    if team:
        rows = "".join(
            f'<tr><td>{key}</td><td>{escape(str(sample["title"]))}</td><td>Pass</td>'
            '<td><span class="rules-verdict pass">✓ Compliant</span></td></tr>'
            for key, sample in SAMPLES.items()
        )
        content = (
            '<div class="cs-container"><div class="cs-container-header"><h2>Saved tests</h2></div>'
            '<table class="argus-table"><thead><tr><th>Execution key</th><th>Test</th>'
            f'<th>Status</th><th>Verdict</th></tr></thead><tbody>{rows}</tbody></table></div>'
        ) if detail else (
            '<div class="cs-container"><div class="cs-container-header"><h2>Audit reports</h2></div>'
            f'<div class="cs-card-body"><a href="{root}sample-tests/">Purchase and playback</a>'
            f'<span>{len(SAMPLES)} saved tests · 7 October 2026</span></div></div>'
        )
        title = "Purchase and playback" if detail else team.name
        action = f'<a class="cs-btn rules-primary" href="{root}rules#test">Test rule changes</a>'
    else:
        title, action = "Teams", ""
        content = '<div class="cs-cards">' + "".join(
            f'<div class="cs-container"><div class="cs-container-header"><h2>{escape(t.name)}</h2></div>'
            f'<div class="cs-card-body"><a href="/reports/teams/{t.slug}/">Audit reports</a>'
            f'<a href="/reports/teams/{t.slug}/rules">Team rules</a></div></div>' for t in TEAMS
        ) + '</div>'
    css = (Path(__file__).parents[1] / "reporting/assets/rules.css").read_text()
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)} · ARGUS</title>{html_report._cs_head_extra(context)}'
        f'{html_report._cs_page_css(context)}<style>{css}</style></head><body>'
        f'{html_report._cs_topnav("/reports/", context)}'
        '<div class="application-layout" id="application-layout">'
        f'{html_report._cs_sidenav(root, "reports" if team else "teams", False, context, hub_home=not team, rules_enabled=True)}'
        '<main class="argus-main main-content rules-workspace">'
        '<div class="rules-alert" id="demo-banner"><strong>Local demo</strong><span>Sample data</span></div>'
        f'<section class="cs-header"><h1>{escape(title)}</h1><div class="cs-actions">{action}</div></section>'
        f'{content}</main></div></body></html>'
    )


def sample_account_check(document: dict, sample: dict) -> list[dict]:
    """Exercise the existing account check without changing its implementation."""
    from argus.config.models import PolicySettings
    from argus.domain import workflow_rules

    role = {"failed": "fail", "passed": "pass", "blocked": "blocked"}.get(sample.get("status", ""))
    if role is None:
        return []
    policy = document["policy"]
    metadata = {"test_results": [{
        "status": policy["statuses"].get(role, workflow_rules._DEFAULT_STATUSES[role]),
        "comment": sample.get("comment", ""),
        "steps": [{"comment": text} for text in sample.get("steps", [])],
        "executed_by_name": "Sample tester",
    }]}
    # The demo can run comparisons concurrently. Scope the legacy module's
    # process-wide policy to this one check and restore defaults afterward.
    with _SAMPLE_CHECK_LOCK:
        workflow_rules.configure(PolicySettings(**policy))
        try:
            return workflow_rules.filter_enabled(workflow_rules.check_test_account_in_comments(metadata))
        finally:
            workflow_rules.reset_configuration()


def sample_audit(document: dict, execution: str) -> dict:
    sample = {**SAMPLES, **rule_builder_demo.SAMPLES}[execution]
    audit: dict = {"overall_verdict": "pass", "analysis_status": "complete", "findings": []}
    for finding in sample_account_check(document, sample):
        audit["findings"].append(finding)
        if not finding.get("advisory"):
            audit["overall_verdict"] = {"high": "fail", "medium": "concerns", "low": "pass"}[finding["severity"]]
    for rule in document["custom_rules"]:
        if rule.get("kind") == "structured":
            continue
        assessment = rule_authoring_demo.simulate_evidence(rule, sample)
        if assessment["status"] in {"met", "not_applicable"}:
            continue
        finding = {
            "rule": rule["id"], "title": rule["name"], "severity": rule["severity"],
            "description": assessment["description"],
            "advisory": rule["mode"] == "advisory" or rule["severity"] == "low",
        }
        if assessment["status"] == "uncertain":
            finding["assessment_status"] = "needs_review"
            if not finding["advisory"]:
                audit["review_status"] = "required"
        audit["findings"].append(finding)
        if not finding["advisory"]:
            proposed = {"high": "fail", "medium": "concerns", "low": "pass"}[finding["severity"]]
            if assessment["status"] == "uncertain":
                proposed = "concerns"
            if {"pass": 0, "concerns": 1, "fail": 2}[proposed] > {"pass": 0, "concerns": 1, "fail": 2}[audit["overall_verdict"]]:
                audit["overall_verdict"] = proposed
    return rule_builder_demo.apply(document, sample, audit)


def finish_sample_preview(store: SqliteRuleStore, team: str, identifier: str) -> None:
    key = "preview#" + identifier
    preview = store.get(team, key)
    running = {**preview, "phase": "running"}
    try:
        store.put(team, key, running, phase="pending")
    except RuleConflict:
        return
    try:
        samples = {**SAMPLES, **rule_builder_demo.SAMPLES}
        if any(execution not in samples for execution in preview["executions"]):
            raise ValueError("Choose executions from one of the local sample reports.")
        active = store.get(team, revision_key(preview["active_revision"]))
        draft = store.get(team, preview["draft_key"])
        results = [{
            "execution": execution, "sample": deepcopy(samples[execution]),
            "before": sample_audit(active["document"], execution),
            "after": sample_audit(draft["document"], execution),
        } for execution in preview["executions"]]
        complete = {**running, "phase": "complete", "results": results,
                    "completed_at": now(), "demo": True,
                    "diagnostic": (
                        "Sample preview evaluates the test-account check and saved custom checks. "
                        "Other built-in checks and evidence-policy text are not evaluated. "
                        "Saved LLM requirements use simulated results."
                    )}
        store.put(team, key, complete, phase="running")
    except Exception as exc:
        store.put(team, key, {**running, "phase": "failed", "diagnostic": str(exc)}, phase="running")


def make_server(port: int, state_path: Path) -> ThreadingHTTPServer:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteRuleStore(str(state_path))
    for team in TEAMS:
        initialize(store, team.slug)

    def launch(team: str, identifier: str, _actor: str) -> None:
        threading.Thread(target=finish_sample_preview, args=(store, team, identifier), daemon=True).start()

    service = DemoRuleService(store, lambda _team, actor: actor == "local-user", launch, DemoRuleReports())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def send(self, code: int, value: object, *, html: bool = False, content_type: str = "") -> None:
            payload = value if isinstance(value, bytes) else str(value).encode() if html else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type or ("text/html; charset=utf-8" if html else "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def handle_request(self, method: str) -> None:
            bound_port = cast(ThreadingHTTPServer, self.server).server_port
            allowed_hosts = {f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"}
            if self.headers.get("Host") not in allowed_hosts:
                self.send(403, {"error": "Use the loopback demo address."})
                return
            path = urlsplit(self.path)
            parts = path.path.strip("/").split("/")
            if method == "GET" and path.path.startswith("/_assets/"):
                from argus.reporting.static_assets import resolve
                asset = resolve(path.path)
                if asset:
                    self.send(200, asset.path.read_bytes(), content_type=asset.content_type)
                else:
                    self.send(404, {"error": "Asset not found."})
                return
            if method == "GET" and path.path.rstrip("/") == "/reports":
                self.send(200, render_demo_reports(None), html=True)
                return
            if method == "GET" and len(parts) in {3, 4} and parts[:2] == ["reports", "teams"]:
                team = resolve_team(parts[2])
                if team is None or team.slug != parts[2]:
                    self.send(404, {"error": "Unknown team."})
                elif len(parts) == 3 or parts[3] == "sample-tests":
                    self.send(200, render_demo_reports(team.slug, detail=len(parts) == 4), html=True)
                elif parts[3] in {"rules", "rules.html"}:
                    self.send(200, render_page(team.slug, "", demo=True), html=True)
                else:
                    self.send(404, {"error": "Page not found."})
                return
            if method == "GET" and path.path in {"/", "/rules"}:
                team_slug = parse_qs(path.query).get("team", ["desktop"])[0]
                if resolve_team(team_slug) is None:
                    self.send(404, {"error": "Unknown team."})
                    return
                self.send(200, render_page(team_slug, "", demo=True), html=True)
                return
            if method == "GET" and path.path == "/session":
                self.send(200, {"alias": "local-user", "teams": [
                    {"slug": team.slug, "name": team.name, "can_manage_rules": True} for team in TEAMS
                ]})
                return
            if len(parts) < 3 or parts[0] != "teams" or parts[2] != "rules":
                self.send(404, {"error": "Unknown demo route."})
                return
            try:
                body = {}
                if method == "POST":
                    if (
                        self.headers.get("Origin") not in {f"http://{host}" for host in allowed_hosts}
                        or self.headers.get("X-Argus-Request") != "1"
                        or self.headers.get_content_type() != "application/json"
                    ):
                        raise RuleError(403, "Submit changes from the local demo page.")
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 131072:
                        raise RuleError(400, "Invalid request size.")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise RuleError(400, "Send a JSON object.")
                payload = service.handle(parts[1], "/".join(parts[3:]), method, "local-user", body,
                                         self.headers.get("Idempotency-Key", ""),
                                         {key: values[0] for key, values in parse_qs(path.query).items()})
                self.send(200, payload)
            except RuleError as exc:
                self.send(exc.status, {"error": str(exc)})
            except (ValueError, TypeError):
                self.send(400, {"error": "Invalid request."})

        def do_GET(self) -> None:
            self.handle_request("GET")

        def do_POST(self) -> None:
            self.handle_request("POST")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=2410)
    parser.add_argument("--state", type=Path, default=Path("/tmp/argus-rules-demo.sqlite"))
    args = parser.parse_args(argv)
    server = make_server(args.port, args.state)
    print(f"ARGUS rule editor demo: http://127.0.0.1:{server.server_port}/rules", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
