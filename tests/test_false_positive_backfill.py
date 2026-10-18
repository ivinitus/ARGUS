from __future__ import annotations

import json

import pytest

from tools import backfill_false_positive_filters as backfill


def _metadata(*, status: str = "Pass", comment: str = "") -> dict:
    return {
        "test_results": [{
            "status": status,
            "comment": comment,
            "test_case_key": "QA-T1",
            "marketplace": "US",
            "tester": "Tester",
            "trace_links": [],
            "steps": [{
                "index": 1,
                "status": "Fail" if status == "Pass" else "In Progress",
                "comment": "Known issue" if status == "Pass" else "",
                "trace_links": ["QA-BUG-1"] if status == "Pass" else [],
            }],
        }],
    }


def test_reconcile_audit_drops_acknowledged_model_finding():
    audit = {
        "overall_verdict": "fail",
        "summary": "Review.",
        "findings": [{
            "severity": "high",
            "step_index": 1,
            "description": (
                "The expected result was not met, matching what the "
                "tester already reported."
            ),
        }],
    }

    result = backfill.reconcile_audit(audit, _metadata())

    assert result.model_findings_removed == 1
    assert result.r21_refreshed is False
    assert result.audit["findings"] == []
    assert result.audit["overall_verdict"] == "pass"


def test_reconcile_preserves_non_dict_historical_finding():
    audit = {
        "overall_verdict": "concerns",
        "summary": "Review.",
        "findings": [
            "legacy malformed finding",
            {
                "severity": "low",
                "description": (
                    "No issue found; included only for completeness."
                ),
            },
        ],
    }

    result = backfill.reconcile_audit(audit, _metadata())

    assert result.model_findings_removed == 1
    assert result.audit["findings"] == ["legacy malformed finding"]


def test_reconcile_audit_refreshes_credentials_only_r21():
    metadata = _metadata(
        status="In Progress",
        comment=(
            "tester+sample@example.com Copy To Clipboard "
            "afxwjny1yqm7718# Copy To Clipboard A15KM1QSDCBNT4"
        ),
    )
    audit = {
        "overall_verdict": "concerns",
        "summary": "Still running.",
        "findings": [{
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": "The tester's comment explains the state.",
            "source": "rule",
            "rule": "R21",
        }],
    }

    result = backfill.reconcile_audit(audit, metadata)

    assert result.model_findings_removed == 0
    assert result.r21_refreshed is True
    finding = result.audit["findings"][0]
    assert finding["severity"] == "high"
    assert "No comment explains" in finding["description"]
    assert backfill.reconcile_audit(
        result.audit,
        result.metadata,
    ).changed is False


def test_backfill_writes_backups_and_is_idempotent(tmp_path):
    execution_dir = tmp_path / "Tester" / "QA-E1"
    execution_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "Review.",
        "findings": [{
            "severity": "low",
            "step_index": 1,
            "description": "No issue found; included only for completeness.",
        }],
    }
    (execution_dir / "audit.json").write_text(json.dumps(audit))
    (execution_dir / "audit.md").write_text("original")
    (execution_dir / "metadata.json").write_text(json.dumps(_metadata()))

    audits, removed, r21_count, backup = backfill.backfill(
        tmp_path,
        apply=True,
    )

    assert (audits, removed, r21_count) == (1, 1, 0)
    assert backup is not None
    assert (
        backup / "Tester" / "QA-E1" / "audit.original.json"
    ).exists()
    assert json.loads((execution_dir / "audit.json").read_text())[
        "findings"
    ] == []

    assert backfill.backfill(tmp_path, apply=True) == (0, 0, 0, None)


def test_reconcile_removes_only_emulation_related_mobile_r19():
    metadata = _metadata()
    metadata["test_results"][0].update({
        "expected_surface": "mobile",
        "expected_device": "Android",
    })
    audit = {
        "overall_verdict": "fail",
        "summary": "Device review.",
        "findings": [{
            "severity": "high",
            "source": "device_check",
            "rule": "R19",
            "description": (
                "The ordinary Mobile run used desktop DevTools emulation."
            ),
        }, {
            "severity": "high",
            "source": "device_check",
            "rule": "R19",
            "description": (
                "The run is assigned Android, but the proven physical "
                "device is iOS."
            ),
        }],
    }

    result = backfill.reconcile_audit(audit, metadata)

    assert [
        finding["description"] for finding in result.audit["findings"]
    ] == [audit["findings"][1]["description"]]
    assert result.transformations["invalid_mobile_r19_removed"] == 1


def test_apply_writes_v3_sanitized_hash_manifest_and_restore(tmp_path):
    execution_dir = tmp_path / "Tester" / "QA-E2"
    execution_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "password: Audit7Secret",
        "findings": [{
            "severity": "low",
            "description": "No issue found; included only for completeness.",
        }],
    }
    metadata = _metadata(
        comment="qa@example.test Fixture9Secret",
    )
    (execution_dir / "audit.json").write_text(json.dumps(audit))
    (execution_dir / "audit.md").write_text(
        "password: Markdown8Secret"
    )
    (execution_dir / "metadata.json").write_text(json.dumps(metadata))

    audits, removed, _, backup_root = backfill.backfill(
        tmp_path,
        apply=True,
    )

    assert (audits, removed) == (1, 1)
    assert backup_root is not None
    manifest = json.loads((backup_root / "manifest.json").read_text())
    assert manifest["version"] == 3
    assert manifest["backup_content_sanitized"] is True
    assert manifest["artifacts"]
    assert all(
        len(artifact[name]) == 64
        for artifact in manifest["artifacts"]
        for name in (
            "before_sha256",
            "backup_sha256",
            "after_sha256",
        )
    )
    backup_text = " ".join(
        path.read_text()
        for path in backup_root.rglob("*")
        if path.is_file()
    )
    assert "Audit7Secret" not in backup_text
    assert "Fixture9Secret" not in backup_text
    assert "Markdown8Secret" not in backup_text

    restored = backfill.restore(backup_root)
    assert restored == 3
    restored_audit = json.loads((execution_dir / "audit.json").read_text())
    restored_metadata = json.loads(
        (execution_dir / "metadata.json").read_text()
    )
    assert restored_audit["findings"]
    assert "Audit7Secret" not in json.dumps(restored_audit)
    assert "Fixture9Secret" not in json.dumps(restored_metadata)


def test_restore_refuses_file_changed_after_migration(tmp_path):
    execution_dir = tmp_path / "Tester" / "QA-E3"
    execution_dir.mkdir(parents=True)
    (execution_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "Review.",
        "findings": [{
            "severity": "low",
            "description": "No issue found; included only for completeness.",
        }],
    }))
    (execution_dir / "audit.md").write_text("original")
    (execution_dir / "metadata.json").write_text(json.dumps(_metadata()))
    *_, backup_root = backfill.backfill(tmp_path, apply=True)
    assert backup_root is not None

    (execution_dir / "audit.json").write_text('{"changed": true}')

    with pytest.raises(RuntimeError, match="changed afterward"):
        backfill.restore(backup_root)


def test_apply_rolls_back_all_touched_artifacts_on_write_failure(
    tmp_path,
    monkeypatch,
):
    execution_dir = tmp_path / "Tester" / "QA-E4"
    execution_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "Review.",
        "findings": [{
            "severity": "low",
            "description": "No issue found; included only for completeness.",
        }],
    }
    metadata = _metadata()
    audit_path = execution_dir / "audit.json"
    metadata_path = execution_dir / "metadata.json"
    markdown_path = execution_dir / "audit.md"
    audit_path.write_text(json.dumps(audit))
    metadata_path.write_text(json.dumps(metadata))
    markdown_path.write_text("original markdown")

    real_write_json = backfill.atomic_io.atomic_write_json
    failed = False

    def fail_metadata_once(path, data, *, indent=2):
        nonlocal failed
        if path == metadata_path and not failed:
            failed = True
            raise OSError("simulated metadata write failure")
        real_write_json(path, data, indent=indent)

    monkeypatch.setattr(
        backfill.atomic_io,
        "atomic_write_json",
        fail_metadata_once,
    )

    with pytest.raises(OSError, match="simulated metadata write failure"):
        backfill.backfill(tmp_path, apply=True)

    assert json.loads(audit_path.read_text()) == audit
    assert json.loads(metadata_path.read_text()) == metadata
    assert markdown_path.read_text() == "original markdown"
