"""Atomic local report publication."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from argus.core import atomic_io


@dataclass(frozen=True)
class PublishedReport:
    canonical_html: Path
    archived_html: Path
    archived_markdown: Path
    public_url: str






@dataclass(frozen=True)
class ReportAsset:
    path: Path
    relative_path: str
    content_type: str
    content_digest: str


class ReportPublisher:
    def publish(
        self,
        folder_root: Path,
        *,
        stamp: str,
        html: str,
        markdown: str,
        completion_state: str | None = None,
        archive_html: str | None = None,
        detail_chunks: dict[str, dict[str, str]] | None = None,
    ) -> PublishedReport:
        raise NotImplementedError

    def start_audit_run(
        self,
        folder_root: Path,
        *,
        run_id: str,
        expected_keys: list[str],
    ) -> None:
        del folder_root, run_id, expected_keys

    def checkpoint_audit_key(
        self,
        folder_root: Path,
        *,
        run_id: str,
        key: str,
        status: str,
        verdict: str | None,
        findings: int | None,
        reason: str | None,
        elapsed_s: float,
        audit_path: Path | None,
    ) -> None:
        del (
            folder_root,
            run_id,
            key,
            status,
            verdict,
            findings,
            reason,
            elapsed_s,
            audit_path,
        )

    def mark_audit_publishing(
        self,
        folder_root: Path,
        *,
        run_id: str,
    ) -> None:
        del folder_root, run_id

    def mark_audit_failed(
        self,
        folder_root: Path,
        *,
        run_id: str,
        diagnostic: str,
    ) -> None:
        del folder_root, run_id, diagnostic

    def announce_active_audit(
        self,
        *,
        phase: str,
        target: str,
        run_id: str,
        label: str | None = None,
        requested_by: str | None = None,
        task_id: str | None = None,
        total: int | None = None,
        completed: int | None = None,
        diagnostic: str | None = None,
    ) -> None:
        del (
            phase,
            target,
            run_id,
            label,
            requested_by,
            task_id,
            total,
            completed,
            diagnostic,
        )

    def clear_active_audit(self, *, run_id: str) -> bool:
        del run_id
        return False

    def fail_active_audit(self, *, run_id: str, diagnostic: str) -> bool:
        del run_id, diagnostic
        return False

    def stop_active_audit(self, *, run_id: str, diagnostic: str) -> bool:
        del run_id, diagnostic
        return False

    def published_folders(self) -> list[str]:
        """Folder slugs listed in the published dashboard index."""
        return []

    def refresh_dashboard(self) -> bool:
        """Refresh a published team landing page without rebuilding reports."""
        return False

    def active_audit(self) -> dict[str, Any] | None:
        """The published ``active_audit`` status, if any."""
        return None

    def remove_published_folder(self, folder_slug: str) -> dict[str, int]:
        """Delete a folder's published objects and index entry."""
        del folder_slug
        return {"deleted_objects": 0, "index_entries_removed": 0}


# Phases after which the dashboard status must not be cleared or replaced by
# a later, unrelated best-effort write: the operator needs to see them.


class LocalReportPublisher(ReportPublisher):
    def __init__(self, team_slug: str = "") -> None:
        self.team_slug = team_slug

    def publish(
        self,
        folder_root: Path,
        *,
        stamp: str,
        html: str,
        markdown: str,
        completion_state: str | None = None,
        archive_html: str | None = None,
        detail_chunks: dict[str, dict[str, str]] | None = None,
    ) -> PublishedReport:
        del completion_state
        if self.team_slug and 'class="side-navigation' in html:
            from argus.reporting.review_data import with_review_link
            html = with_review_link(html)
            if archive_html:
                archive_html = with_review_link(archive_html)
        html_canonical = folder_root / "_argus.html"
        html_archive = folder_root / f"_argus_{stamp}.html"
        md_archive = folder_root / f"_report_{stamp}.md"
        details_dir = folder_root / "_argus_details"
        detail_chunks = detail_chunks or {}
        if detail_chunks:
            details_dir.mkdir(parents=True, exist_ok=True)
            for stale in details_dir.glob("*.json"):
                if stale.name not in detail_chunks:
                    stale.unlink()
            for name, payload in detail_chunks.items():
                atomic_io.atomic_write_text(
                    details_dir / name,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            html = html.replace(
                "__ARGUS_DETAIL_BASE__",
                f"{details_dir.name}/",
            )
        atomic_io.atomic_write_text(html_archive, archive_html or html)
        atomic_io.atomic_write_text(md_archive, markdown)
        atomic_io.atomic_write_text(html_canonical, html)
        return PublishedReport(
            canonical_html=html_canonical,
            archived_html=html_archive,
            archived_markdown=md_archive,
            public_url=f"file://{html_canonical.resolve()}",
        )




_publisher: ReportPublisher = LocalReportPublisher()


def refresh_dashboard() -> bool:
    return _publisher.refresh_dashboard()


def configure(settings: Any) -> None:
    global _publisher
    _publisher = LocalReportPublisher(settings.general.team_slug or settings.general.profile)


def publish(
    folder_root: Path,
    *,
    stamp: str,
    html: str,
    markdown: str,
    completion_state: str | None = None,
    archive_html: str | None = None,
    detail_chunks: dict[str, dict[str, str]] | None = None,
) -> PublishedReport:
    return _publisher.publish(
        folder_root,
        stamp=stamp,
        html=html,
        markdown=markdown,
        completion_state=completion_state,
        archive_html=archive_html,
        detail_chunks=detail_chunks,
    )


def start_audit_run(
    folder_root: Path,
    *,
    run_id: str,
    expected_keys: list[str],
) -> None:
    _publisher.start_audit_run(
        folder_root,
        run_id=run_id,
        expected_keys=expected_keys,
    )


def checkpoint_audit_key(
    folder_root: Path,
    *,
    run_id: str,
    key: str,
    status: str,
    verdict: str | None,
    findings: int | None,
    reason: str | None,
    elapsed_s: float,
    audit_path: Path | None,
) -> None:
    _publisher.checkpoint_audit_key(
        folder_root,
        run_id=run_id,
        key=key,
        status=status,
        verdict=verdict,
        findings=findings,
        reason=reason,
        elapsed_s=elapsed_s,
        audit_path=audit_path,
    )


def mark_audit_publishing(
    folder_root: Path,
    *,
    run_id: str,
) -> None:
    _publisher.mark_audit_publishing(
        folder_root,
        run_id=run_id,
    )


def mark_audit_failed(
    folder_root: Path,
    *,
    run_id: str,
    diagnostic: str,
) -> None:
    _publisher.mark_audit_failed(
        folder_root,
        run_id=run_id,
        diagnostic=diagnostic,
    )


def announce_active_audit(
    *,
    phase: str,
    target: str,
    run_id: str,
    label: str | None = None,
    requested_by: str | None = None,
    task_id: str | None = None,
    total: int | None = None,
    completed: int | None = None,
    diagnostic: str | None = None,
) -> None:
    _publisher.announce_active_audit(
        phase=phase,
        target=target,
        run_id=run_id,
        label=label,
        requested_by=requested_by,
        task_id=task_id,
        total=total,
        completed=completed,
        diagnostic=diagnostic,
    )


def clear_active_audit(*, run_id: str) -> bool:
    return _publisher.clear_active_audit(run_id=run_id)


def fail_active_audit(*, run_id: str, diagnostic: str) -> bool:
    return _publisher.fail_active_audit(run_id=run_id, diagnostic=diagnostic)


def stop_active_audit(*, run_id: str, diagnostic: str) -> bool:
    return _publisher.stop_active_audit(run_id=run_id, diagnostic=diagnostic)


def published_folders() -> list[str]:
    return _publisher.published_folders()


def remove_published_folder(folder_slug: str) -> dict[str, int]:
    return _publisher.remove_published_folder(folder_slug)
