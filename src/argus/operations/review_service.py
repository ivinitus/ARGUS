"""Authenticated review operations, independent of audit/model execution."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from functools import lru_cache
from typing import Any

from argus.domain.review_assignment import allocate, eligible, reviewer_aliases, validate_roster
from argus.operations.review_identities import IdentityUnavailable
from argus.operations.review_store import ReviewConflict
from argus.operations.teams import resolve_team
from argus.reporting.review_data import digest, review_revision


class ReviewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()




class ReviewService:
    def __init__(self, store: Any, data: Any, resolve_reviewers: Any = None):
        self.store, self.data = store, data
        self.resolve_reviewers = resolve_reviewers
        self._previous_review_revision = lru_cache(maxsize=2048)(self._load_previous_review_revision)

    def _load_previous_review_revision(self, team: str, folder: str, key: str, revision: str) -> str:
        # Immutable source documents need only be fetched once per warm worker.
        return review_revision(self.data.detail(team, folder, {"key": key, "revision": revision}))

    def handle(
        self, team: str, folder: str, action: str, method: str,
        actor: str, body: dict, query: dict, request_id: str = "",
    ) -> dict:
        if resolve_team(team) is None or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", folder):
            raise ReviewError(404, "Unknown review workspace.")
        scope = f"{team}#{folder}"
        fingerprint = digest([actor, method, action, body])
        if method == "POST":
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                raise ReviewError(400, "A review request needs an idempotency key.")
            old = self.store.get(scope, f"request#{request_id}")
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ReviewError(409, "Request key was already used with different input.")
                return old["result"]
        for attempt in range(3):
            try:
                return self._handle(
                    team, folder, scope, action, method, actor, body, query, request_id, fingerprint,
                )
            except ValueError as exc:
                raise ReviewError(400, str(exc)) from exc
            except IdentityUnavailable as exc:
                raise ReviewError(503, str(exc)) from exc
            except ReviewConflict as exc:
                old = self.store.get(scope, f"request#{request_id}") if request_id else {}
                if old and old["fingerprint"] == fingerprint:
                    return old["result"]
                # Re-read after unrelated case activity. The execution's own
                # review version and current assignment still must match.
                if method == "POST" and action.startswith("items/") and attempt < 2:
                    continue
                raise ReviewError(409, str(exc)) from exc
        raise ReviewError(409, "Review changed. Refresh before saving.")

    def _snapshot(self, scope: str, allocation_id: str) -> dict:
        if not allocation_id:
            return {"roster": [], "assignments": {}, "unassigned": {}, "counts": {}}
        snapshot = self.store.get(scope, f"allocation#{allocation_id}")
        snapshot["assignments"] = {}
        for chunk in self.store.query(scope, f"allocation#{allocation_id}#"):
            snapshot["assignments"].update(chunk["assignments"])
        return snapshot

    def _write_snapshot(self, scope: str, allocation_id: str, snapshot: dict) -> None:
        entries = list(snapshot["assignments"].items())
        for offset in range(0, len(entries), 100):
            self.store.put(scope, f"allocation#{allocation_id}#{offset:08d}", {
                "assignments": dict(entries[offset:offset + 100]),
            })
        self.store.put(scope, f"allocation#{allocation_id}", {
            k: v for k, v in snapshot.items() if k != "assignments"
        })

    @staticmethod
    def _current(case: dict, record: dict) -> dict:
        if record.get("revision") == case["revision"] or (
            case.get("review_revision")
            and record.get("review_revision") == case["review_revision"]
        ):
            return record
        return {}

    def _handle(
        self, team: str, folder: str, scope: str, action: str, method: str,
        actor: str, body: dict, query: dict, request_id: str, fingerprint: str,
    ) -> dict:
        manifest = self.data.manifest(team, folder)
        cases = {row["key"]: row for row in manifest["items"]}
        head = self.store.get(scope, "head") or {"version": 0, "allocation_id": ""}
        allocation = self._snapshot(scope, head["allocation_id"])
        reviewers = {
            row["alias"]: row
            for row in [*allocation.get("retained_roster", []), *allocation["roster"]]
        }
        active_aliases = {row["alias"] for row in allocation["roster"]}
        reviews = {row["key"]: row for row in self.store.query(scope, "current#")}
        current_reviews: dict[str, dict] = {}

        def current(key: str) -> dict:
            if key not in current_reviews:
                case, record = cases[key], reviews.get(key, {})
                resolved = self._current(case, record)
                if (
                    not resolved and record.get("revision") and case.get("review_revision")
                    and not record.get("review_revision")
                ):
                    # Existing reviews retain their immutable document revision.
                    # Compare that document instead of resetting all legacy work
                    # when a new publication changes only incidental metadata.
                    try:
                        previous_revision = self._previous_review_revision(
                            team, folder, key, record["revision"],
                        )
                    except ReviewError as exc:
                        if exc.status != 404:
                            raise
                    else:
                        if previous_revision == case["review_revision"]:
                            resolved = record
                current_reviews[key] = resolved
            return current_reviews[key]

        assignments = {
            key: alias for key, alias in allocation["assignments"].items()
            if key in cases and alias in reviewers and eligible(cases[key], reviewers[alias])
            and (alias in active_aliases
                 or current(key).get("state") == "reviewed")
        }
        # A scope-wide write epoch also protects allocation previews from review
        # changes made while the coordinator is inspecting the split.
        if self.store.get(scope, "head").get("version", 0) != head["version"]:
            raise ReviewConflict("Review changed while loading. Refresh.")

        def commit(result: dict, writes: dict | None = None, **changes: Any) -> dict:
            updated = {**head, **changes, "version": head["version"] + 1}
            result["version"] = updated["version"]
            records = {
                **(writes or {}),
                f"request#{request_id}": {"fingerprint": fingerprint, "result": result},
                f"event#{now()}#{request_id}": {
                    "action": action, "actor": actor, "at": now(),
                    "allocation_id": updated["allocation_id"],
                    "request_id": request_id, "result": result,
                },
            }
            self.store.commit(scope, head["version"], updated, records)
            return result

        if method == "GET" and action == "queue":
            filter_reviewer = str(query.get("reviewer") or (actor if query.get("mine") == "1" else "")).strip().lower()
            rows = []
            totals = {"reviewed": 0, "investigation": 0, "pending": 0,
                      "unassigned": int(manifest.get("coverage_missing", 0))}
            loads = {
                alias: {"assigned": 0, "pending": 0, "in_progress": 0, "investigation": 0, "reviewed": 0}
                for alias in reviewers
            }
            for key, case in cases.items():
                record = current(key)
                owner = assignments.get(key)
                review_state = record.get("state", "pending") if owner else "unassigned"
                state = "pending" if review_state == "in_progress" else review_state
                totals[state] += 1
                if owner:
                    loads[owner]["assigned"] += 1
                    loads[owner][review_state] += 1
                if filter_reviewer and owner != filter_reviewer:
                    continue
                if query.get("run") and case.get("run") != query["run"]:
                    continue
                if query.get("state") and state != query["state"]:
                    continue
                if query.get("verdict") and case.get("verdict") != query["verdict"]:
                    continue
                rows.append({
                    **case, "assignee": owner, "state": state, "review_state": review_state,
                    "reason": None if owner else (
                        "Executor identity is unknown." if not case.get("executor")
                        else "No current assignment. Allocate or transfer to an eligible reviewer."
                    ),
                })
            rows.sort(key=lambda row: (
                row["state"] == "reviewed",
                0 if row["analysis"] != "complete" else {"fail": 1, "concerns": 2}.get(row["verdict"], 3),
                row["key"],
            ))
            try:
                offset = max(0, int(query.get("offset") or 0))
                limit = min(100, max(1, int(query.get("limit") or 50)))
            except (TypeError, ValueError):
                raise ReviewError(400, "Offset and limit must be integers.") from None
            expected = query.get("version")
            if expected is not None and str(expected) != str(head["version"]):
                raise ReviewConflict("Queue changed. Reload the first page.")
            return {
                "items": rows[offset:offset + limit], "filtered_total": len(rows),
                "total": manifest["coverage_total"], "coverage_missing": manifest["coverage_missing"],
                "totals": totals, "loads": loads, "roster": allocation["roster"],
                "retained_roster": allocation.get("retained_roster", []),
                "version": head["version"], "report_revision": manifest["revision"],
                "allocation_id": head["allocation_id"], "actor": actor,
                "runs": sorted({c["run"] for c in cases.values() if c.get("run")}),
                "run_names": {
                    c["run"]: c["run_name"]
                    for c in cases.values() if c.get("run") and c.get("run_name")
                },
            }
        if method == "GET" and action.startswith("items/"):
            key = action.removeprefix("items/")
            if key not in cases:
                raise ReviewError(404, "Execution is outside this report.")
            history = self.store.query(scope, f"history#{key}#")
            for event in self.store.query(scope, "reassignment#"):
                change = event["changes"].get(key)
                if change:
                    history.append({
                        **change, "key": key, "action": "transfer",
                        "actor": event["actor"], "at": event["at"],
                        "reason": "Reviewer removed from allocation.",
                    })
            history.sort(key=lambda row: row["at"])
            return {
                "case": self.data.detail(team, folder, cases[key]),
                "review": current(key),
                "history": history[-100:], "assignee": assignments.get(key),
                "version": head["version"], "report_revision": manifest["revision"],
                "review_version": digest(reviews.get(key, {})),
            }
        if method != "POST":
            raise ReviewError(404, "Unknown review operation.")
        if not action.startswith("items/") and (
            body.get("report_revision") != manifest["revision"] or body.get("version") != head["version"]
        ):
            raise ReviewConflict("Report or review changed. Refresh before saving.")
        if action == "preview":
            aliases = reviewer_aliases(body.get("roster"))
            if self.resolve_reviewers is None:
                raise IdentityUnavailable("Jira lookup is unavailable. Try again shortly.")
            roster = validate_roster(self.resolve_reviewers(aliases))
            if [row["alias"] for row in roster] != aliases:
                raise IdentityUnavailable("Jira could not resolve every reviewer. Try again shortly.")
            fixed, completed_removed = {}, {}
            for key, alias in assignments.items():
                saved_review = current(key)
                if saved_review and alias in aliases:
                    fixed[key] = alias
                elif saved_review.get("state") == "reviewed":
                    completed_removed[key] = alias
            # Preserve verified bindings for both active and retained reviewers.
            for row in roster:
                old = reviewers.get(row["alias"])
                if old and old["jira_key"].casefold() != row["jira_key"].casefold():
                    raise ValueError("An existing reviewer's Jira identity cannot be changed in this workspace.")
            preview_id = uuid.uuid4().hex
            result = allocate(
                [case for key, case in cases.items() if key not in completed_removed],
                roster, seed=preview_id, fixed=fixed,
            )
            result["assignments"].update(completed_removed)
            retained_roster = [reviewers[alias] for alias in sorted(set(completed_removed.values()))]
            reassignments = {
                key: {"from": alias, "to": result["assignments"].get(key), "revision": cases[key]["revision"]}
                for key, alias in assignments.items()
                if alias not in aliases and key not in completed_removed
                and current(key)
            }
            snapshot = {
                **result, "roster": roster, "seed": preview_id, "actor": actor,
                "retained_roster": retained_roster, "reassignments": reassignments,
                "report_revision": manifest["revision"], "base_version": head["version"] + 1,
                "created_at": now(),
            }
            self._write_snapshot(scope, preview_id, snapshot)
            return commit({
                "preview_id": preview_id, "counts": result["counts"],
                "unassigned": result["unassigned"], "coverage_missing": manifest["coverage_missing"],
                "assignments": result["assignments"], "roster": roster,
                "reassignments": reassignments, "completed_preserved": len(completed_removed),
                "explanation": (
                    "Counts are balanced while excluding executors. Selected reviewers keep started work; "
                    "unfinished cases from removed reviewers are reassigned with saved notes and history. "
                    "Completed reviews keep their original reviewer. "
                    "Unknown executors and cases with no eligible reviewer remain unassigned."
                ),
            })
        if action == "allocate":
            preview_id = str(body.get("preview_id", ""))
            if not re.fullmatch(r"[a-f0-9]{32}", preview_id):
                raise ValueError("Select a valid allocation preview.")
            preview = self._snapshot(scope, preview_id)
            if (
                preview.get("actor") != actor or preview.get("base_version") != head["version"]
                or preview.get("report_revision") != manifest["revision"]
            ):
                raise ReviewConflict("Allocation preview is stale. Preview again.")
            changes = preview.get("reassignments", {})
            return commit(
                {"allocation_id": preview_id},
                {f"reassignment#{preview_id}": {"changes": changes, "actor": actor, "at": now()}}
                if changes else {},
                allocation_id=preview_id,
            )
        if action == "transfer":
            key, alias = body.get("key"), body.get("assignee")
            if (
                not isinstance(key, str) or key not in cases
                or not isinstance(alias, str) or alias not in active_aliases
                or not eligible(cases[key], reviewers[alias])
            ):
                raise ValueError("Choose an eligible reviewer from the saved roster.")
            saved_review = current(key)
            if saved_review.get("state") == "reviewed":
                raise ValueError("Completed reviews cannot be transferred.")
            reason = str(body.get("reason") or "").strip()
            if not reason or len(reason) > 2000:
                raise ValueError("Explain the transfer in 1–2000 characters.")
            allocation_id = uuid.uuid4().hex
            allocation["assignments"] = {**assignments, key: alias}
            self._write_snapshot(scope, allocation_id, allocation)
            event = {"key": key, "actor": actor, "at": now(), "action": "transfer",
                     "from": assignments.get(key), "to": alias, "reason": reason,
                     "revision": cases[key]["revision"]}
            return commit(
                {"allocation_id": allocation_id},
                {f"history#{key}#{now()}#{request_id}": event},
                allocation_id=allocation_id,
            )
        if action.startswith("items/"):
            key = action.removeprefix("items/")
            if key not in cases:
                raise ReviewError(404, "Execution is outside this report.")
            if assignments.get(key) != actor:
                raise ReviewError(403, "Only the assigned reviewer can save this case.")
            case = cases[key]
            if body.get("revision") != case["revision"]:
                raise ReviewConflict("Audit or evidence changed. Review the current revision.")
            if body.get("review_version") != digest(reviews.get(key, {})):
                raise ReviewConflict("This case's review changed. Refresh and reconcile your draft.")
            detail = self.data.detail(team, folder, case)
            decisions = body.get("decisions", {})
            if not isinstance(decisions, dict):
                raise ValueError("Finding decisions must be an object.")
            finding_ids = {f["id"] for f in detail["findings"]}
            if set(decisions) - finding_ids:
                raise ValueError("A finding does not belong to this audit revision.")
            normalized = {}
            for finding, decision in decisions.items():
                if not isinstance(decision, dict):
                    raise ValueError("Invalid finding decision.")
                value, reason = decision.get("decision"), str(decision.get("reason") or "").strip()
                if value not in {"confirm", "reject", "investigate"} or len(reason) > 2000:
                    raise ValueError("Choose Confirm, Reject or Needs investigation.")
                if value == "reject" and not reason:
                    raise ValueError("Explain rejected findings.")
                normalized[finding] = {"decision": value, "reason": reason}
            state = body.get("state")
            notes = str(body.get("notes") or "").strip()
            if state not in {"in_progress", "investigation", "reviewed"} or len(notes) > 4000:
                raise ValueError("Choose a valid review state and keep notes under 4000 characters.")
            if state == "in_progress" and (
                any(d["decision"] == "investigate" for d in normalized.values())
                or current(key).get("state") == "investigation"
            ):
                state = "investigation"
            if state == "reviewed" and (
                not case["ready"] or set(normalized) != finding_ids
                or any(d["decision"] == "investigate" for d in normalized.values())
                or body.get("attested") is not True
            ):
                raise ValueError("Check all steps and evidence, resolve every finding, and confirm completion.")
            record = {
                "key": key, "revision": case["revision"], "actor": actor, "at": now(),
                "state": state, "decisions": normalized, "notes": notes,
                "allocation_id": head["allocation_id"],
            }
            if case.get("review_revision"):
                record["review_revision"] = case["review_revision"]
            return commit({"saved": True, "key": key, "state": state}, {
                f"current#{key}": record, f"history#{key}#{now()}#{request_id}": record,
            })
        raise ReviewError(404, "Unknown review operation.")
