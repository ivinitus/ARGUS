"""Personal review and rule editing services for the loopback report server."""
from __future__ import annotations

from copy import deepcopy
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from argus.core import atomic_io
from argus.operations.review_service import ReviewError, ReviewService
from argus.operations.review_store import SqliteReviewStore
from argus.operations.rule_service import RuleError, RuleService
from argus.operations.rule_runtime import database_path
from argus.operations.rule_store import SqliteRuleStore
from argus.reporting import review_data

_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}$")


class LocalReviewData:
    """Project local reports while retaining immutable reviewed revisions."""

    def __init__(self, roots: dict[str, Path], state_dir: Path, *, hub: bool = False):
        self.roots, self.state_dir, self.hub = roots, state_dir, hub
        self.lock = threading.RLock()

    def _folder(self, team: str, folder: str) -> Path:
        root = self.roots.get(team)
        if root is None or not _SAFE.fullmatch(folder) or folder in {".", ".."}:
            raise ReviewError(404, "Unknown report.")
        path = root / folder
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != root.resolve():
            raise ReviewError(404, "Unknown report.")
        if any(child.is_symlink() for child in path.rglob("*")):
            raise ReviewError(400, "Saved review evidence must not contain symbolic links.")
        return path

    def _cache_asset(self, asset) -> str:
        name = asset.content_digest + asset.path.suffix.lower()
        target = self.state_dir / "assets" / name
        if target.exists():
            return name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as copied:
                temporary = Path(copied.name)
                with asset.path.open("rb") as source:
                    shutil.copyfileobj(source, copied)
                copied.flush()
                os.fsync(copied.fileno())
            with temporary.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != asset.content_digest:
                    raise ReviewError(409, "Evidence changed while loading. Refresh the report.")
            temporary.replace(target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return name

    def _detail_path(self, team: str, folder: str, key: str, revision: str) -> Path:
        if not _SAFE.fullmatch(key) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ReviewError(404, "Unknown review revision.")
        return self.state_dir / team / folder / key / f"{revision}.json"

    def manifest(self, team: str, folder: str) -> dict:
        path = self._folder(team, folder)
        with self.lock:
            manifest, details, assets = review_data.build_documents(path, team)
            evidence_urls = {
                asset.content_digest: f"/_review_assets/{team}/{self._cache_asset(asset)}"
                for asset in assets
            }
            for key, detail in details.items():
                for evidence in detail["evidence"]:
                    evidence["url"] = evidence_urls[evidence["sha256"]]
                target = self._detail_path(team, folder, key, detail["revision"])
                if not target.exists():
                    atomic_io.atomic_write_json(target, detail)
            return manifest

    def detail(self, team: str, folder: str, case: dict) -> dict:
        self._folder(team, folder)
        target = self._detail_path(team, folder, case["key"], case["revision"])
        if not target.is_file():
            self.manifest(team, folder)
        try:
            return json.loads(target.read_text())
        except FileNotFoundError as exc:
            raise ReviewError(404, "This review revision is no longer available.") from exc


class LocalApi:
    def __init__(self, settings, roots: dict[str, Path], *, hub: bool = False):
        from argus import config
        from argus.operations.review_identities import JiraReviewerResolver
        from argus.pipeline.jira_credentials import build_provider
        from argus.reporting.rule_data import LocalRuleReports

        self.actor = os.environ.get("ARGUS_OPERATOR_NAME", "").strip().lower() or getpass.getuser().lower()
        self.roots = roots
        self.hub = hub
        self.reviews = {}
        self.rules = {}
        self.settings = {}
        for team, root in roots.items():
            current = deepcopy(settings) if team == settings.general.team_slug else config.load(profile=team)
            current.general.team_slug = team
            current.extractor.out_dir = root
            current.general.state_dir.mkdir(parents=True, exist_ok=True)
            self.settings[team] = current
            review_store = SqliteReviewStore(str(current.general.state_dir / "reviews.sqlite"))
            self.reviews[team] = ReviewService(
                review_store,
                LocalReviewData({team: root}, current.general.state_dir / "review-sources", hub=hub),
                JiraReviewerResolver(build_provider(current), current.extractor.base_url),
            )
            rule_database = database_path(current)
            rule_database.parent.mkdir(parents=True, exist_ok=True)
            rule_store = SqliteRuleStore(str(rule_database))
            self.rules[team] = RuleService(
                rule_store, lambda _team, actor: actor == self.actor,
                self.launch_preview, LocalRuleReports(roots),
            )

    def asset_path(self, team: str, name: str) -> Path:
        if team not in self.reviews or not re.fullmatch(r"[a-f0-9]{64}\.[a-z0-9]{1,5}", name):
            raise ReviewError(404, "Unknown review evidence.")
        root = self.reviews[team].data.state_dir / "assets"
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root.resolve():
            raise ReviewError(404, "Unknown review evidence.")
        return path

    def launch_preview(self, team: str, identifier: str, _actor: str) -> None:
        settings = self.settings[team]
        log_path = settings.general.state_dir / "rule-preview.log"
        environment = {
            **os.environ,
            "ARGUS_PROFILE": settings.general.profile,
            "ARGUS_TEAM_SLUG": team,
            "ARGUS_STATE_DIR": str(settings.general.state_dir),
            "ARGUS_OUTPUT_DIR": str(settings.extractor.out_dir),
            "ARGUS_RULE_DATABASE": str(database_path(settings)),
        }
        with log_path.open("a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "argus", "rules-preview", "--preview", identifier],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, env=environment,
            )

    def handle(self, method: str, path: str, body: dict, query: dict, request_id: str = "") -> dict:
        if method == "GET" and path == "/session":
            return {"alias": self.actor, "teams": [
                {"slug": team, "name": self.settings[team].general.team_name or team, "can_manage_rules": True}
                for team in self.roots
            ]}
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "teams" or parts[1] not in self.roots:
            raise RuleError(404, "Unknown local operation.")
        team = parts[1]
        if parts[2] == "rules":
            return self.rules[team].handle(team, "/".join(parts[3:]), method, self.actor, body, request_id, query)
        if parts[2] == "reviews" and len(parts) >= 5:
            return self.reviews[team].handle(team, parts[3], "/".join(parts[4:]), method,
                                             self.actor, body, query, request_id)
        raise RuleError(404, "Unknown local operation.")
