"""Resolve trusted Jira Test Player references for manual audits."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal
from urllib.parse import urlparse

from argus.operations.teams import AuditTeam, resolve_jira_team


ReferenceKind = Literal["run", "execution", "plan", "case"]
AuditKind = Literal["folder", "run", "key", "plan", "case"]

_TEST_RUN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-C[0-9]+$")
_EXECUTION_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-E[0-9]+$")
_TEST_PLAN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-P[0-9]+$")
_TEST_CASE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-T[0-9]+$")
FOLDER_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./()-]{0,127}$")
_JIRA_TESTS_PATH = "/secure/Tests.jspa"
MAX_MANUAL_REFERENCES = 20
MAX_MANUAL_TARGET_CHARS = MAX_MANUAL_REFERENCES * 130 - 2


class JiraReferenceError(ValueError):
    """A pasted Jira reference cannot be safely resolved."""


@dataclass(frozen=True)
class ParsedJiraReference:
    kind: ReferenceKind
    key: str


@dataclass(frozen=True)
class JiraAuditTarget:
    reference_kind: ReferenceKind
    key: str
    project_id: str
    folder_id: int
    folder_name: str
    team: AuditTeam
    audit_kind: AuditKind
    audit_value: str
    source_name: str


def split_manual_references(value: str) -> list[str]:
    """Split a comma-separated manual launch request with bounded fanout."""
    references = [part.strip() for part in value.split(",")]
    if not references or any(not part for part in references):
        raise JiraReferenceError(
            "comma-separated audit references cannot contain empty entries"
        )
    if len(references) > MAX_MANUAL_REFERENCES:
        raise JiraReferenceError(
            f"at most {MAX_MANUAL_REFERENCES} references may be launched at once"
        )
    return references


def parse_manual_targets(value: str) -> list[tuple[AuditKind, str]]:
    """Validate a team-scoped launch without loading worker or Jira dependencies."""
    if len(value) > MAX_MANUAL_TARGET_CHARS:
        raise JiraReferenceError("audit targets are too long")
    targets: list[tuple[AuditKind, str]] = []
    for reference in split_manual_references(value):
        parsed = parse_jira_reference(reference)
        if parsed is not None:
            if len(parsed.key) > 128:
                raise JiraReferenceError("Jira keys must be at most 128 characters")
            kind: AuditKind = "key" if parsed.kind == "execution" else parsed.kind
            targets.append((kind, parsed.key))
        elif FOLDER_TARGET_RE.fullmatch(reference):
            targets.append(("folder", reference))
        else:
            raise JiraReferenceError(
                "Enter folder names or Jira execution, test-case, run, or test-plan keys, separated by commas."
            )
    return list(dict.fromkeys(targets))


def _unwrap_slack_link(value: str) -> str:
    raw = value.strip()
    if not (raw.startswith("<") and raw.endswith(">")):
        return raw
    body = raw[1:-1]
    return body.partition("|")[0].strip()


def _trusted_test_player_parts(value: str, base_url: str | None = None) -> list[str]:
    raw = _unwrap_slack_link(value)
    if base_url is None:
        from argus.config import load
        base_url = load().extractor.base_url
    trusted = urlparse(base_url)
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise JiraReferenceError("malformed Jira Test Player URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != trusted.hostname or parsed.port != trusted.port
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path.rstrip("/") != _JIRA_TESTS_PATH
        or parsed.query
    ):
        raise JiraReferenceError(
            "Jira URL must be an HTTPS Test Player link on the configured Jira host"
        )
    return [part for part in parsed.fragment.split("/") if part]


def parse_jira_reference(value: str, *, base_url: str | None = None) -> ParsedJiraReference | None:
    """Parse a trusted C-run, E-execution, T-case, or P-plan reference.

    URL input is never fetched directly. Only an exact HTTPS Test Player URL
    on the configured Jira host is accepted; API calls later use a configured base URL
    and the validated Jira key.
    """
    raw = _unwrap_slack_link(value)
    upper = raw.upper()
    if _TEST_RUN_KEY_RE.fullmatch(upper):
        return ParsedJiraReference("run", upper)
    if _EXECUTION_KEY_RE.fullmatch(upper):
        return ParsedJiraReference("execution", upper)
    if _TEST_PLAN_KEY_RE.fullmatch(upper):
        return ParsedJiraReference("plan", upper)
    if _TEST_CASE_KEY_RE.fullmatch(upper):
        return ParsedJiraReference("case", upper)

    looks_like_url = "://" in raw or "Tests.jspa" in raw
    if not looks_like_url:
        return None

    parts = _trusted_test_player_parts(raw, base_url)
    if parts and parts[0] == "v2":
        parts = parts[1:]
    if (
        len(parts) == 2
        and parts[0] == "testCase"
        and _TEST_CASE_KEY_RE.fullmatch(parts[1].upper())
    ):
        return ParsedJiraReference("case", parts[1].upper())
    if (
        len(parts) == 2
        and parts[0] == "testPlan"
        and _TEST_PLAN_KEY_RE.fullmatch(parts[1].upper())
    ):
        return ParsedJiraReference("plan", parts[1].upper())
    if (
        len(parts) == 2
        and parts[0] == "testPlayer"
        and _TEST_RUN_KEY_RE.fullmatch(parts[1].upper())
    ):
        return ParsedJiraReference("run", parts[1].upper())
    if (
        len(parts) == 3
        and parts[0] == "testPlayer"
        and parts[1] == "testExecution"
        and _EXECUTION_KEY_RE.fullmatch(parts[2].upper())
    ):
        return ParsedJiraReference("execution", parts[2].upper())
    raise JiraReferenceError(
        "expected #/testPlayer/PROJECT-C123 or "
        "#/testPlayer/testExecution/PROJECT-E123, #/testCase/PROJECT-T123, "
        "or #/testPlan/PROJECT-P123"
    )


def _positive_int(value: object, *, label: str, key: str) -> int:
    if not isinstance(value, (int, str)):
        raise JiraReferenceError(f"{key} is missing {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JiraReferenceError(f"{key} is missing {label}") from exc
    if parsed <= 0:
        raise JiraReferenceError(f"{key} has invalid {label}")
    return parsed


class JiraAuditTargetResolver:
    """Resolve Jira references to a safe team-specific audit target."""

    def __init__(self, extractor_settings: Any) -> None:
        self.settings = extractor_settings

    def resolve(self, reference: str) -> JiraAuditTarget:
        from argus.pipeline import extractor, test_cases, test_plans

        parsed = parse_jira_reference(reference, base_url=self.settings.base_url)
        if parsed is None:
            raise JiraReferenceError("not a Jira run, execution, test-case, or test-plan reference")

        session = extractor.build_session(self.settings)
        if parsed.kind == "case":
            case = test_cases.find_case_by_key(session, self.settings.base_url, parsed.key)
            if case is None:
                raise JiraReferenceError(f"Jira test case {parsed.key} was not found")
            results = test_cases.get_case_results(session, self.settings.base_url, case["id"])
            resolved = [
                self.resolve(str((result.get("testRun") or {}).get("key") or ""))
                for result in results
            ]
            if not resolved or len({target.team.slug for target in resolved}) != 1:
                raise JiraReferenceError(
                    f"{parsed.key} has no unique execution team; run it from the selected team's dashboard"
                )
            target = resolved[0]
            return JiraAuditTarget(
                reference_kind="case", key=parsed.key,
                project_id=target.project_id, folder_id=target.folder_id,
                folder_name=target.folder_name, team=target.team,
                audit_kind="case", audit_value=parsed.key,
                source_name=str(case.get("name") or parsed.key),
            )
        if parsed.kind == "plan":
            record = test_plans.find_plan_by_key(session, self.settings.base_url, parsed.key)
            if record is None:
                raise JiraReferenceError(f"Jira test plan {parsed.key} was not found")
            source_name = str(record.get("name") or parsed.key)
        elif parsed.kind == "run":
            record = extractor.find_test_run_by_key(
                session,
                self.settings.base_url,
                parsed.key,
            )
            if record is None:
                raise JiraReferenceError(
                    f"Jira test run {parsed.key} was not found"
                )
            source_name = str(record.get("name") or parsed.key)
        else:
            result = extractor.find_test_result_context(
                session,
                self.settings.base_url,
                parsed.key,
            )
            if result is None:
                raise JiraReferenceError(
                    f"Jira execution {parsed.key} was not found"
                )
            test_run = result.get("testRun")
            if not isinstance(test_run, dict):
                raise JiraReferenceError(
                    f"Jira execution {parsed.key} is missing test-run metadata"
                )
            record = test_run
            source_name = str(record.get("name") or parsed.key)

        project_id = str(record.get("projectId") or "")
        if not project_id:
            raise JiraReferenceError(f"{parsed.key} is missing projectId")
        folder_id = _positive_int(
            record.get("folderId"),
            label="folderId",
            key=parsed.key,
        )
        folder_options = {"folder_type": "testplan"} if parsed.kind == "plan" else {}
        folder_name = extractor.find_folder_name_by_id(
            session,
            self.settings.base_url,
            project_id,
            folder_id,
            **folder_options,
        )
        if not folder_name:
            raise JiraReferenceError(
                f"could not resolve folder {folder_id} for {parsed.key}"
            )

        team = resolve_jira_team(project_id, folder_name)
        if team is None:
            raise JiraReferenceError(
                f"folder {folder_name!r} in Jira project {project_id} does "
                "not map unambiguously to an ARGUS team"
            )

        audit_kind: AuditKind = (
            "plan" if parsed.kind == "plan" else "run" if parsed.kind == "run" else "key"
        )
        audit_value = parsed.key
        return JiraAuditTarget(
            reference_kind=parsed.kind,
            key=parsed.key,
            project_id=project_id,
            folder_id=folder_id,
            folder_name=folder_name,
            team=team,
            audit_kind=audit_kind,
            audit_value=audit_value,
            source_name=source_name,
        )
