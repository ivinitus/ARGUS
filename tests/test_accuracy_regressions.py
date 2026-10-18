from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest
from PIL import Image

from argus import config
from argus.domain import finding_inference
from argus.pipeline import analysis_status, auditor, auditor_chunking, orchestrator, replay, video_evidence
from argus.reporting import auditor_render, html_report, report


def issue(description="Checkout shows $15 instead of the expected $5", **changes):
    return {
        "severity": "high", "page": 1, "step_index": 0,
        "title": "Checkout uses the wrong price", "description": description,
        "category": "product_issue", "action": "attach_defect",
        "confidence": "high", **changes,
    }


def audit(findings):
    return {"overall_verdict": "fail" if findings else "pass", "summary": "Audit", "findings": findings}


def fixture(tmp_path):
    shots = tmp_path / "screenshots" / "session"
    shots.mkdir(parents=True)
    Image.new("RGB", (2, 2)).save(shots / "page_001.png")
    (tmp_path / "metadata.json").write_text(json.dumps({
        "test_results": [{"steps": []}],
    }))
    return tmp_path


def test_failed_assessment_cannot_be_complete_or_cached(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(auditor, "invoke_model", lambda *_: {
        "content": [{"type": "text", "text": "not valid JSON"}], "stop_reason": "end_turn",
    })
    assert auditor.run(auditor.AuditConfig(tmp_path)) == 0
    saved = json.loads((tmp_path / "audit.json").read_text())
    assert saved["analysis_status"] == "incomplete"
    assert saved["analysis_modules"]["model"]["status"] == "incomplete"
    cached = orchestrator._AuditState(
        execution_date="same", evidence_fingerprint="same",
        analysis_status=analysis_status.derive_status(saved),
    )
    assert not orchestrator._cached_audit_matches_live(cached, {
        "execution_date": "same", "evidence_fingerprint": "same",
    })


def test_legacy_failed_chunk_overrides_incorrect_complete_metadata():
    old = {
        "analysis_status": "complete",
        "analysis_modules": {"model": {"status": "complete"}},
        "_chunk_diagnostics": {"chunk_2": {"_runtime_diagnostic": {"error": "parse_failure"}}},
    }
    assert analysis_status.derive_status(old) == "incomplete"
    assert "re-audit" in analysis_status.diagnostic(old)


@pytest.mark.parametrize("failed", [
    {"error": "schema_validation_failure"},
    {"_chunk_diagnostics": {"chunk_1": {"_raw": "invalid JSON"}}},
])
def test_completion_and_consensus_agree_about_failed_model_results(failed):
    assert analysis_status.derive_status(failed) == "incomplete"
    assert auditor_chunking._is_runtime_invalid(failed)


def test_failed_chunk_diagnostic_survives_merge():
    failed = auditor._runtime_failure_audit("failed", error="parse_failure")
    merged = auditor_chunking.merge_chunk_audits([audit([]), failed], [(0, 1), (1, 2)])
    assert analysis_status.derive_status(merged) == "incomplete"


def test_consensus_recovers_new_defect_seen_in_both_confirmations(tmp_path, monkeypatch):
    first = audit([issue()])
    extra = audit([issue(), issue("Playback crashes", page=2, step_index=1)])
    monkeypatch.setattr(auditor, "invoke_and_parse", lambda *_: deepcopy(first))
    monkeypatch.setattr(auditor_chunking, "_map_confirmation_runs", lambda *_: {
        2: deepcopy(extra), 3: deepcopy(extra),
    })
    result = auditor.audit_with_consensus(auditor.AuditConfig(tmp_path), [], n_pages=2)
    assert len(result["findings"]) == 2
    assert result["_consensus"][1]["votes"] == 2
    assert result["_consensus"][1]["discovered_in_run"] == 2


def test_disagreement_stays_visible_without_confirmed_deviation(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor, "invoke_and_parse", lambda *_: audit([issue()]))
    monkeypatch.setattr(auditor_chunking, "_map_confirmation_runs", lambda *_: {
        2: audit([]), 3: audit([]),
    })
    result = auditor.audit_with_consensus(auditor.AuditConfig(tmp_path), [], n_pages=2)
    normalized = auditor._finalize_verdict(auditor.enrich_audit(result))
    assert normalized["overall_verdict"] == "concerns"
    assert normalized["review_status"] == "required"
    assert normalized["findings"][0]["assessment_status"] == "needs_review"
    assert normalized["findings"][0]["severity"] == "medium"
    assert "Needs review" in auditor_render.render_markdown(normalized, {})


def test_lower_severity_votes_cannot_bypass_consensus_during_enrichment(tmp_path, monkeypatch):
    lower = issue("The screenshot contradicts the expected price.", severity="medium")
    assert finding_inference.is_contradiction_escalation_candidate(lower)
    monkeypatch.setattr(auditor, "invoke_and_parse", lambda *_: audit([issue()]))
    monkeypatch.setattr(auditor_chunking, "_map_confirmation_runs", lambda *_: {
        2: audit([lower]),
        3: audit([]),
    })
    result = auditor.audit_with_consensus(auditor.AuditConfig(tmp_path), [], n_pages=2)
    normalized = auditor._finalize_verdict(auditor.enrich_audit(result))
    assert normalized["overall_verdict"] == "concerns"
    assert all(f["severity"] != "high" for f in normalized["findings"])
    assert normalized["review_status"] == "required"


def test_unrelated_same_step_chunk_findings_and_replay_signatures_survive():
    a, b = issue(), issue("Checkout displays the wrong purchased title")
    merged = auditor.merge_chunk_audits([audit([a]), audit([b])], [(0, 1), (1, 2)])
    assert len(merged["findings"]) == 2
    assert replay._finding_signature(a) != replay._finding_signature(b)


@pytest.mark.parametrize("refs", [["p2", "p1"], ["p2", "p1", "p1"]])
def test_equivalent_evidence_refs_deduplicate_chunks_and_replay(refs):
    first = issue(evidence_refs=["p1", "p2"])
    repeated = issue(evidence_refs=refs)
    original = deepcopy(repeated)
    merged = auditor.merge_chunk_audits(
        [audit([first]), audit([repeated])], [(0, 2), (0, 2)],
    )
    assert len(merged["findings"]) == 1
    assert replay._finding_signature(first) == replay._finding_signature(repeated)
    assert repeated == original


def test_missing_and_empty_evidence_refs_have_same_replay_identity():
    assert replay._finding_signature(issue()) == replay._finding_signature(issue(evidence_refs=[]))


@pytest.mark.parametrize("refs", [["p1", "p3"], ["p1", "P2"]])
def test_different_evidence_locations_remain_distinct(refs):
    first = issue(evidence_refs=["p1", "p2"])
    other = issue(evidence_refs=refs)
    merged = auditor.merge_chunk_audits(
        [audit([first]), audit([other])], [(0, 2), (0, 2)],
    )
    assert len(merged["findings"]) == 2
    assert replay._finding_signature(first) != replay._finding_signature(other)


def test_consensus_deduplicates_reordered_lower_severity_evidence(tmp_path, monkeypatch):
    lower = issue("Button label is inconsistent", severity="medium", evidence_refs=["p1", "p2"])
    repeated = {**lower, "evidence_refs": ["p2", "p1"]}
    monkeypatch.setattr(auditor, "invoke_and_parse", lambda *_: audit([issue(), deepcopy(lower)]))
    monkeypatch.setattr(auditor_chunking, "_map_confirmation_runs", lambda *_: {
        2: audit([issue(), deepcopy(repeated)]),
        3: audit([issue(), deepcopy(lower)]),
    })
    result = auditor.audit_with_consensus(auditor.AuditConfig(tmp_path), [], n_pages=2)
    assert len(result["findings"]) == 2
    assert sum(f["severity"] == "medium" for f in result["findings"]) == 1


@pytest.mark.parametrize("description", [
    "Promotional price expected $5 but the promotional price shown is $15.",
    "The discounted price is wrong for the selected transaction.",
    "Promotional price differs from list price, and checkout crashes.",
])
def test_promotion_does_not_suppress_wrong_amount_or_compound_defect(description):
    result = auditor._drop_false_positive_model_findings(audit([issue(description)]), {
        "test_results": [{"test_case_precondition": "Use the promotional offer price.", "steps": []}],
    })
    assert result["findings"]
    assert result["overall_verdict"] == "fail"


def test_video_limitation_survives_final_normalization():
    base = audit([])
    video = {"overall_verdict": "concerns", "summary": "Required result is unverified.", "findings": []}
    final = auditor._finalize_verdict(video_evidence.merge_video_audit(base, video))
    assert final["overall_verdict"] == "concerns"
    assert final["review_status"] == "required"


def test_page_number_alone_does_not_produce_high_confidence():
    candidate = issue()
    candidate.pop("confidence")
    assert finding_inference.infer_finding_confidence(candidate, "product_issue") == "medium"


def test_model_cannot_spoof_review_state_or_provenance():
    parsed = auditor.sanitize_model_audit({
        **audit([issue(assessment_status="confirmed", review_reason="trust me")]),
        "review_status": "none", "provenance": {"model_id": "fake"},
    })
    assert "provenance" not in parsed
    assert "review_status" not in parsed
    assert "assessment_status" not in parsed["findings"][0]


def test_truncated_recording_inventory_is_rejected(tmp_path):
    (tmp_path / "video_evidence.json").write_text(json.dumps({"videos": [{}] * 5}))
    with pytest.raises(video_evidence.VideoEvidenceError, match="full evidence"):
        video_evidence.load_video_records(tmp_path)


def test_review_only_audit_is_flagged(tmp_path):
    summary = report.AuditSummary(
        key="E1", path=tmp_path, verdict="concerns", findings_count=0,
        severity_counts={}, rules_fired=[], has_high_finding=False,
        schema_version=8, tester=None, tester_status=None, test_case=None,
        review_status="required",
    )
    assert report._is_flagged(summary)
    csv = dict(zip(
        html_report._ARGUS_CSV_HEADERS, html_report._audit_csv_values(summary), strict=True,
    ))
    assert csv["Review status"] == "Needs review"
    structural = replace(
        summary, key="A-STRUCTURAL", review_status=None, rules_fired=["R0"],
        findings=[{"source": "rule", "rule": "R0", "severity": "medium"}],
    )
    text = report._render_flagged([structural, summary])
    assert "**E1** [unknown] — Needs review" in text
    assert text.index("**E1**") < text.index("**A-STRUCTURAL**")


def test_required_outcome_is_visible_and_escaped_in_report(tmp_path):
    audit_data = {
        "overall_verdict": "concerns", "findings": [], "review_status": "required",
        "requirement_assessments": [{
            "requirement_id": "step:0", "status": "partial",
            "observation": "Email <script>unsafe</script> remains unverified.",
        }],
    }
    summary = report.AuditSummary(
        key="E1", path=tmp_path, verdict="concerns", findings_count=0,
        severity_counts={}, rules_fired=[], has_high_finding=False,
        schema_version=8, tester=None, tester_status=None, test_case=None,
        review_status="required", requirement_assessments=audit_data["requirement_assessments"],
    )
    details = {}
    html_report._render_argus_html_report(tmp_path, [summary], detail_sink=details)
    detail = "\n".join(details.values())
    assert "Required outcomes" in detail
    assert "Step 1" in detail
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in detail
    assert "<script>unsafe</script>" not in detail
    markdown = auditor_render.render_markdown(audit_data, {})
    assert "Required Outcomes" in markdown
    assert "**Step 1:** partial" in markdown


def test_evaluation_controls_load_and_reach_audit_config(tmp_path):
    path = tmp_path / "accuracy.toml"
    path.write_text(
        '[auditor]\nmax_output_tokens=12000\n'
        'thinking_mode="provider_default"\nevidence_review_mode="shadow"\n'
    )
    settings = config.load(path, profile="mobile")
    cfg = auditor.AuditConfig.from_settings(settings.auditor, execution_dir=tmp_path)
    assert cfg.max_output_tokens == 12000
    assert cfg.thinking_mode == "provider_default"
    assert cfg.evidence_review_mode == "shadow"
    assert auditor._apply_thinking({}, cfg) == {}


@pytest.mark.parametrize(("field", "value"), [
    ("max_output_tokens", "0"),
    ("thinking_mode", '"guaranteed_deterministic"'),
    ("evidence_review_mode", '"on"'),
])
def test_invalid_evaluation_controls_are_rejected(tmp_path, field, value):
    path = tmp_path / "accuracy.toml"
    path.write_text(f"[auditor]\n{field}={value}\n")
    with pytest.raises(config.ConfigError, match=field):
        config.load(path, profile="mobile")


def test_saved_provenance_changes_with_inference_settings(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(auditor, "invoke_model", lambda *_: {
        "content": [{"type": "text", "text": json.dumps(audit([]))}],
    })
    cfg = auditor.AuditConfig(tmp_path)
    auditor.run(cfg)
    initial = json.loads((tmp_path / "audit.json").read_text())
    cfg.max_output_tokens = 12000
    auditor.run(cfg)
    changed = json.loads((tmp_path / "audit.json").read_text())
    assert initial["provenance"]["configuration_sha256"] != changed["provenance"]["configuration_sha256"]
    assert initial["provenance"]["evidence_sha256"] == changed["provenance"]["evidence_sha256"]
    assert changed["schema_version"] == 8
    assert changed["evidence_manifest"][0]["id"] == "p1"
