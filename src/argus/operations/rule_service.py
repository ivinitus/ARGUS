"""Team rule draft, preview, publication, and history API."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

from argus.domain import team_rules
from argus.operations.rule_store import RuleConflict
from argus.operations.teams import resolve_team

REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
EXECUTION_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-E[0-9]+$")


class RuleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def revision_key(revision: int) -> str:
    return f"version#{revision:020d}"


def initialize(store: Any, team: str) -> dict:
    head = store.get(team, "head")
    if head:
        return head
    document = team_rules.validate_document(team_rules.defaults(team))
    baseline = {
        "revision": 1, "document": document, "sha256": team_rules.fingerprint(document),
        "author": "packaged-defaults", "published_at": now(),
        "note": "Initial team defaults", "preview_id": None,
    }
    head = {"version": 1, "active_revision": 1, "draft_key": None}
    try:
        store.commit(team, 0, head, {revision_key(1): baseline})
    except RuleConflict:
        return store.get(team, "head")
    return head


def active_snapshot(store: Any, team: str) -> dict:
    """Workers read a complete immutable revision; initialization is API-owned."""
    head = store.get(team, "head")
    if not head:
        raise RuleError(503, "Team rules have not been initialized. Open Team rules first.")
    snapshot = store.get(team, revision_key(head["active_revision"]))
    if not snapshot or team_rules.fingerprint(snapshot["document"]) != snapshot.get("sha256"):
        raise RuleError(503, "Published team rules are unavailable or invalid.")
    snapshot["document"] = team_rules.validate_document(snapshot["document"])
    return snapshot


class RuleService:
    def __init__(self, store: Any, can_edit: Callable[[str, str], bool],
                 launch_preview: Callable[[str, str, str], Any], reports: Any = None):
        self.store = store
        self.can_edit = can_edit
        self.launch_preview = launch_preview
        self.reports = reports

    def validate_document(self, document: Any) -> dict:
        return team_rules.validate_document(document)

    def state(self, team: str, actor: str) -> dict:
        head = initialize(self.store, team)
        active = self.store.get(team, revision_key(head["active_revision"]))
        editable = self.can_edit(team, actor)
        return {
            "version": head["version"], "active": active,
            "draft": self.store.get(team, head["draft_key"]) if editable and head.get("draft_key") else None,
            "can_edit": editable, "catalog": team_rules.catalog(),
        }

    def handle(self, team: str, route: str, method: str, actor: str,
               body: dict, request_id: str = "", query: dict | None = None) -> dict:
        resolved = resolve_team(team)
        if resolved is None or resolved.slug != team:
            raise RuleError(404, "Unknown team.")
        if method == "GET" and not route:
            return self.state(team, actor)
        if not self.can_edit(team, actor):
            raise RuleError(403, "You don't have permission to edit these rules.")
        if method == "GET":
            if route == "history":
                return {"versions": [
                    {k: v for k, v in row.items() if k != "document"}
                    for row in reversed(self.store.query(team, "version#"))
                ]}
            parts = route.split("/")
            if len(parts) == 2 and parts[0] == "versions" and re.fullmatch(r"[1-9][0-9]{0,19}", parts[1]):
                version = self.store.get(team, revision_key(int(parts[1])))
                if not version:
                    raise RuleError(404, "Published version not found.")
                return version
            if parts[0] == "reports":
                if self.reports is None:
                    raise RuleError(503, "Saved test data is unavailable. Try again after refreshing the report.")
                if len(parts) == 1:
                    return self.reports.reports(team)
                if len(parts) == 3 and parts[2] == "executions":
                    from argus.reporting.rule_data import selection_page
                    return selection_page(self.reports.manifest(team, parts[1]), query or {})
            if len(parts) == 2 and parts[0] == "previews" and REQUEST_ID.fullmatch(parts[1]):
                record = self.store.get(team, "preview#" + parts[1])
                if not record:
                    raise RuleError(404, "Preview not found.")
                return record
            raise RuleError(404, "Unknown rule operation.")
        if method != "POST" or route not in {"draft", "preview", "publish", "restore"}:
            raise RuleError(404, "Unknown rule operation.")
        if not REQUEST_ID.fullmatch(request_id):
            raise RuleError(400, "A request key is required.")
        try:
            request_hash = team_rules.fingerprint([actor, route, body])
        except (ValueError, TypeError) as exc:
            raise RuleError(400, "Send finite JSON values.") from exc
        prior = self.store.get(team, "request#" + request_id)
        if prior:
            if prior["sha256"] != request_hash:
                raise RuleError(409, "This request key was used for different input.")
            if route == "preview":
                record = self.store.get(team, "preview#" + request_id)
                if record.get("phase") == "pending":
                    self.launch_preview(team, request_id, actor)
            return prior["response"]
        try:
            return self._change(team, route, actor, body, request_id, request_hash)
        except RuleConflict as exc:
            prior = self.store.get(team, "request#" + request_id)
            if prior and prior["sha256"] == request_hash:
                return prior["response"]
            raise RuleError(409, str(exc)) from exc
        except team_rules.RuleValidationError as exc:
            raise RuleError(400, str(exc)) from exc

    def _change(self, team: str, route: str, actor: str, body: dict,
                request_id: str, request_hash: str) -> dict:
        head = initialize(self.store, team)
        expected = body.get("version")
        if type(expected) is not int or expected != head["version"]:
            raise RuleError(409, "The rules changed. Refresh and reconcile your draft before saving.")
        next_head = {**head, "version": expected + 1}
        writes: dict[str, dict] = {}
        allowed = {
            "draft": {"version", "document"},
            "restore": {"version", "revision"},
            "preview": {"version", "executions"},
            "publish": {"version", "preview_id", "note"},
        }
        optional = {"report"} if route == "preview" else set()
        if set(body) - optional != allowed[route]:
            raise RuleError(400, "Missing or unsupported request fields.")
        if route in {"draft", "restore"}:
            if route == "restore":
                revision = body["revision"]
                if type(revision) is not int or revision < 1:
                    raise RuleError(400, "Choose a published revision.")
                previous = self.store.get(team, revision_key(revision))
                if not previous:
                    raise RuleError(404, "Published revision not found.")
                document = previous["document"]
            else:
                document = body["document"]
            document = self.validate_document(document)
            draft = {
                "document": document, "sha256": team_rules.fingerprint(document),
                "author": actor, "saved_at": now(), "base_revision": head["active_revision"],
            }
            key = f"draft#{expected + 1:020d}"
            draft["id"] = key
            next_head["draft_key"] = key
            writes[key] = draft
            response = {"version": expected + 1, "draft": draft}
        else:
            draft = self.store.get(team, head["draft_key"]) if head.get("draft_key") else {}
            if not draft:
                raise RuleError(409, "Save a draft first.")
            if route == "preview":
                executions = body["executions"]
                if not isinstance(executions, list) or not 1 <= len(executions) <= 10 or any(
                    not isinstance(key, str) or not EXECUTION_KEY.fullmatch(key) for key in executions
                ) or len(set(executions)) != len(executions):
                    raise RuleError(400, "Select 1–10 distinct saved execution keys.")
                preview = {
                    "id": request_id, "phase": "pending", "actor": actor,
                    "created_at": now(), "executions": executions,
                    "draft_key": head["draft_key"], "draft_sha256": draft["sha256"],
                    "active_revision": head["active_revision"],
                }
                if "report" in body:
                    selection = body["report"]
                    if (
                        not isinstance(selection, dict) or set(selection) != {"folder", "revision"}
                        or any(not isinstance(value, str) for value in selection.values())
                        or self.reports is None
                    ):
                        raise RuleError(400, "Choose a saved report.")
                    manifest = self.reports.manifest(team, selection["folder"])
                    if manifest["revision"] != selection["revision"]:
                        raise RuleError(409, "This report changed. Reload its tests and select them again.")
                    items = {row["key"]: row for row in manifest["items"]}
                    if any(key not in items for key in executions):
                        raise RuleError(400, "Choose tests from the selected report.")
                    preview["report"] = selection
                    preview["sources"] = {
                        key: {field: items[key][field] for field in ("source", "input_sha256", "title")}
                        for key in executions
                    }
                writes["preview#" + request_id] = preview
                response = {"version": expected + 1, "preview": preview}
            else:
                preview_id = body["preview_id"]
                if not isinstance(preview_id, str) or not REQUEST_ID.fullmatch(preview_id):
                    raise RuleError(400, "Choose a completed preview.")
                preview = self.store.get(team, "preview#" + preview_id)
                if (
                    preview.get("phase") != "complete"
                    or preview.get("draft_key") != head["draft_key"]
                    or preview.get("draft_sha256") != draft["sha256"]
                    or preview.get("active_revision") != head["active_revision"]
                ):
                    raise RuleError(409, "Complete a preview of this exact draft before publishing.")
                note = team_rules._text(body["note"], "Change summary", 1000)
                revision = head["active_revision"] + 1
                snapshot = {
                    "revision": revision, "document": draft["document"], "sha256": draft["sha256"],
                    "author": actor, "published_at": now(), "note": note, "preview_id": preview_id,
                }
                writes[revision_key(revision)] = snapshot
                next_head.update(active_revision=revision, draft_key=None)
                response = {"version": expected + 1, "active": snapshot}
        writes["request#" + request_id] = {"sha256": request_hash, "response": response}
        self.store.commit(team, expected, next_head, writes)
        if route == "preview":
            # The record is durable before dispatch. An uncertain launch remains
            # visible and cannot authorize publication; retry uses the same task token.
            try:
                self.launch_preview(team, request_id, actor)
            except Exception as exc:
                pending = writes["preview#" + request_id]
                failed = {**pending, "phase": "failed",
                          "diagnostic": f"Could not start preview ({type(exc).__name__}). Start a new preview."}
                try:
                    self.store.put(team, "preview#" + request_id, failed, phase="pending")
                except RuleConflict:
                    pass
        return response
