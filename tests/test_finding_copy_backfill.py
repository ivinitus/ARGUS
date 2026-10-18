from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from argus.pipeline import auditor
from argus.pipeline import finding_copy_backfill as backfill


def _write_execution(tmp_path):
    execution_dir = tmp_path / "tester" / "QA-E1"
    execution_dir.mkdir(parents=True)
    audit = {
        "schema_version": 6,
        "overall_verdict": "concerns",
        "summary": "Keep this unchanged.",
        "findings": [
            {
                "severity": "medium",
                "step_index": 3,
                "description": "Original model finding.",
            },
            {
                "source": "rule",
                "rule": "R1",
                "severity": "low",
                "step_index": 3,
                "description": "Original rule finding.",
            },
            {
                "source": "model",
                "severity": "low",
                "step_index": 9,
                "description": "Second model finding.",
            },
        ],
    }
    metadata = {
        "test_results": [{
            "test_case_key": "QA-T1",
            "steps": [{
                "index": 3,
                "description": (
                    "<p>Click <strong>Listen in app</strong>.</p>"
                    "<script>do not include this</script>"
                ),
            }],
        }],
    }
    (execution_dir / "audit.json").write_text(json.dumps(audit))
    (execution_dir / "metadata.json").write_text(json.dumps(metadata))
    (execution_dir / "audit.md").write_text("original markdown")
    return execution_dir, audit


def test_collect_selects_only_model_findings_and_adds_step_context(tmp_path):
    execution_dir, _audit = _write_execution(tmp_path)

    result = backfill.collect_backfill_input(tmp_path)

    assert list(result.audits) == [execution_dir / "audit.json"]
    assert [candidate.finding_index for candidate in result.candidates] == [0, 2]
    assert [candidate.id for candidate in result.candidates] == [
        "finding-0000",
        "finding-0001",
    ]
    assert result.candidates[0].test_step == "Click Listen in app."
    assert result.candidates[1].test_step == ""


def test_extract_rewrites_requires_every_id_exactly_once():
    raw = {
        "content": [{
            "type": "tool_use",
            "name": "rewrite_findings",
            "input": {
                "rewrites": [
                    {"id": "finding-0000", "description": "Clear first finding."},
                    {"id": "finding-0001", "description": "Clear second finding."},
                ],
            },
        }],
    }

    assert backfill._extract_rewrites(
        raw,
        {"finding-0000", "finding-0001"},
    ) == {
        "finding-0000": "Clear first finding.",
        "finding-0001": "Clear second finding.",
    }

    raw["content"][0]["input"]["rewrites"].pop()
    with pytest.raises(ValueError, match="missing rewrite ids"):
        backfill._extract_rewrites(
            raw,
            {"finding-0000", "finding-0001"},
        )


def test_overlong_rewrite_ids_enforces_plain_language_limit():
    rewrites = {
        "short": "The screenshots do not show playback starting.",
        "long": " ".join(["word"] * 61),
    }

    assert backfill._overlong_rewrite_ids(rewrites) == {"long"}


def test_semantic_drift_rejects_copy_that_changes_inferred_severity():
    candidate = backfill.Candidate(
        id="finding-0000",
        audit_path=Path("audit.json"),
        finding_index=0,
        original_description=(
            "The expected confirmation banner is not shown."
        ),
        test_step="Confirm the banner.",
        finding_context={
            "severity": "medium",
            "page": 4,
            "step_index": 1,
            "category": "insufficient_evidence",
            "action": "add_evidence",
            "confidence": "high",
        },
    )

    drift = backfill._semantic_drift_ids(
        [candidate],
        {
            candidate.id: (
                "The screenshot shows a welcome page instead of the expected "
                "confirmation banner."
            ),
        },
    )

    assert drift == {candidate.id}


def test_fresh_audit_rewrites_only_overlong_model_descriptions(
    tmp_path,
    monkeypatch,
):
    long_description = " ".join(["model"] * 61)
    rule_description = " ".join(["rule"] * 61)
    audit = {
        "overall_verdict": "concerns",
        "summary": "Keep.",
        "findings": [
            {
                "severity": "medium",
                "step_index": 3,
                "description": long_description,
            },
            {
                "source": "rule",
                "rule": "R1",
                "severity": "medium",
                "step_index": 3,
                "description": rule_description,
            },
        ],
    }
    metadata = {
        "test_results": [{
            "steps": [{"index": 3, "description": "Play the podcast."}],
        }],
    }

    def _rewrite(candidates, **_kwargs):
        assert len(candidates) == 1
        assert candidates[0].finding_index == 0
        assert candidates[0].test_step == "Play the podcast."
        return {
            candidates[0].id: (
                "The screenshots show only the Play button. "
                "They do not show the podcast playing."
            ),
        }

    monkeypatch.setattr(backfill, "_rewrite_candidate_list", _rewrite)
    updated = backfill.rewrite_overlong_audit_findings(
        audit,
        metadata,
        auditor.AuditConfig(execution_dir=tmp_path),
    )

    assert updated is not audit
    assert updated["findings"][0]["description"].startswith(
        "The screenshots show"
    )
    assert updated["findings"][0]["severity"] == "medium"
    assert updated["findings"][1]["description"] == rule_description
    assert audit["findings"][0]["description"] == long_description


def test_apply_rewrites_preserves_every_non_description_field(tmp_path):
    execution_dir, original = _write_execution(tmp_path)
    collected = backfill.collect_backfill_input(tmp_path)
    rewrites = {
        "finding-0000": "This step checks that Listen in app opens the title.",
        "finding-0001": "ARGUS could not confirm the second check.",
    }

    backup_root = backfill.apply_rewrites(
        collected,
        rewrites,
        api_key_env="test-profile",
        model_api_url="https://api.anthropic.com/v1/messages",
        model_id="test-model",
        usage=None,
    )

    updated = json.loads((execution_dir / "audit.json").read_text())
    expected = copy.deepcopy(original)
    expected["findings"][0]["description"] = rewrites["finding-0000"]
    expected["findings"][2]["description"] = rewrites["finding-0001"]
    assert updated == expected
    assert updated["findings"][1]["description"] == "Original rule finding."
    assert rewrites["finding-0000"] in (execution_dir / "audit.md").read_text()

    original_backup = (
        backup_root
        / "tester"
        / "QA-E1"
        / "audit.original.json"
    )
    assert json.loads(original_backup.read_text()) == original
    manifest = json.loads((backup_root / "manifest.json").read_text())
    assert manifest["audits_updated"] == 1
    assert manifest["descriptions_updated"] == 2
