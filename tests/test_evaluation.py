from __future__ import annotations

from copy import deepcopy
import json

import pytest

from argus.pipeline import auditor, model_client, evaluation


@pytest.fixture(autouse=True)
def isolate_runtime_configuration(monkeypatch):
    # These runner tests replace auditor.run; no real runtime policy is needed.
    # In particular, a mobile fixture must not alter later coverage tests.
    monkeypatch.setattr(evaluation.runtime, "configure", lambda _: None)


def case(**changes):
    return {
        "id": "case1", "profile": "mobile", "case_family": "checkout",
        "split": "holdout", "execution_dir": "source", "reviewed_by": ["qa-reviewer"],
        "expected_findings": [{
            "id": "wrong_price", "severity": "high", "category": "product_issue",
            "step_index": 0, "description_pattern": "(?i)wrong price",
            "evidence_refs": ["p1"],
        }], **changes,
    }


def finding(**changes):
    return {
        "severity": "high", "category": "product_issue",
        "step_index": 0, "description": "The wrong price appears at checkout.",
        "page": 1, **changes,
    }


def manifest(cases):
    return {
        "version": 1, "cases": cases,
        "configurations": {"baseline": {}, "candidate": {"evidence_review_mode": "enforce"}},
    }


def test_labels_require_review_and_split_isolation(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest([case(reviewed_by=[])])))
    with pytest.raises(ValueError, match="human-reviewed"):
        evaluation.load_manifest(path)
    path.write_text(json.dumps(manifest([case(), case(id="case2", split="development")])))
    with pytest.raises(ValueError, match="leaks"):
        evaluation.load_manifest(path)


def test_duplicate_findings_do_not_increase_recall():
    metrics = evaluation.score(case(), {
        "overall_verdict": "fail", "findings": [finding(), finding()],
    })
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 1
    assert metrics["high_predictions"] == 2
    assert metrics["high_true_positives"] == 1


@pytest.mark.parametrize("source", ["rule", "env_check", "device_check"])
def test_final_audit_scoring_includes_deterministic_false_positives(source):
    metrics = evaluation.score(case(), {
        "overall_verdict": "fail", "findings": [
            finding(source="model"),
            finding(source=source, description="Unsupported environment mismatch"),
        ],
    })
    assert metrics["true_positives"] == metrics["false_positives"] == 1
    assert metrics["high_predictions"] == 2
    assert metrics["high_true_positives"] == 1


def test_review_detections_are_not_confirmed_true_positives():
    metrics = evaluation.score(case(), {
        "overall_verdict": "concerns", "review_status": "required",
        "findings": [finding(severity="medium", assessment_status="needs_review")],
    })
    assert metrics["true_positives"] == 0
    assert metrics["review_detections"] == 1
    assert metrics["false_clean"] == 0


def test_wrong_citation_is_measured_separately_from_detection():
    metrics = evaluation.score(case(), {
        "overall_verdict": "fail", "findings": [finding(page=9)],
    })
    assert metrics["true_positives"] == 1
    assert metrics["citation_checks"] == 1
    assert metrics["correct_citations"] == 0


def test_false_clean_and_incomplete_are_distinct():
    clean = evaluation.score(case(), {"overall_verdict": "pass", "findings": []})
    failed = evaluation.score(case(), {
        "overall_verdict": "pass", "findings": [], "analysis_status": "incomplete",
    })
    assert clean["false_clean"] == 1
    assert failed["false_clean"] == 0
    assert failed["incomplete"] == 1


def test_one_to_one_matching_handles_overlapping_label_patterns():
    labels = case(expected_findings=[
        {"id": "a", "severity": "high", "description_pattern": "price|title"},
        {"id": "b", "severity": "high", "description_pattern": "price"},
    ])
    metrics = evaluation.score(labels, {
        "overall_verdict": "fail",
        "findings": [finding(description="price"), finding(description="title")],
    })
    assert metrics["true_positives"] == 2


def test_small_compatibility_sample_cannot_pass_promotion_gate():
    score = evaluation.score(case(), {"overall_verdict": "fail", "findings": [finding()]})
    rows = [
        {
            "case_id": "case1", "profile": "mobile", "split": "holdout",
            "configuration": name, "score": score,
        }
        for name in ("baseline", "candidate")
    ]
    gate = evaluation.report(rows)["promotion_gates"]["candidate"]
    assert not gate["passed"]
    assert any("10 held-out" in reason for reason in gate["reasons"])


def test_runner_preserves_source_and_does_not_send_labels(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "metadata.json").write_text('{"original": true}')
    (source / "audit.json").write_text('{"original": true}')
    snapshots = {path.name: path.read_bytes() for path in source.iterdir()}
    calls = []
    def run(cfg):
        calls.append(cfg)
        cfg.output_dir.mkdir(parents=True)
        (cfg.output_dir / "audit.json").write_text(json.dumps({
            "overall_verdict": "fail", "findings": [finding()],
        }))
        return 0
    monkeypatch.setattr(auditor, "run", run)
    result = evaluation.run(
        manifest([case(execution_dir=str(source))]), tmp_path / "evaluation",
        split="holdout", repeats=2,
    )
    assert len(calls) == 4
    assert len({id(cfg.usage_tracker) for cfg in calls}) == 4
    assert all(not hasattr(cfg, "expected_findings") for cfg in calls)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == snapshots
    assert result["configurations"]["candidate"]["overall"]["runs"] == 2


def test_runner_stops_on_guardrail_without_trying_another_model(tmp_path, monkeypatch):
    calls = []
    def run(cfg):
        calls.append(cfg)
        raise model_client.ModelInterventionError("blocked")
    monkeypatch.setattr(auditor, "run", run)
    with pytest.raises(model_client.ModelInterventionError):
        evaluation.run(
            manifest([case(execution_dir=str(tmp_path / "source"))]),
            tmp_path / "evaluation", split="holdout",
        )
    assert len(calls) == 1
    rows = json.loads((tmp_path / "evaluation" / "results.json").read_text())
    assert rows[0]["score"]["incomplete"] == 1


def test_runner_rejects_in_place_output(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        evaluation.run(
            manifest([case(execution_dir=str(tmp_path))]),
            tmp_path / "inside", split="holdout",
        )


def test_finding_change_is_visible_in_repeatability_metrics():
    score = evaluation.score(case(), {"overall_verdict": "fail", "findings": [finding()]})
    rows = [
        {"case_id": "same", "finding_signature": value, "score": deepcopy(score)}
        for value in ("one", "two", "one")
    ]
    assert evaluation.aggregate(rows)["unstable_cases"] == 1


def passing_rows():
    score = evaluation.score(case(), {"overall_verdict": "fail", "findings": [finding()]})
    return [{
        "case_id": f"case{number}", "profile": "mobile", "split": "holdout",
        "configuration": name, "repeat": repeat, "score": deepcopy(score),
        "provenance": {
            "metadata_sha256": f"metadata{number}", "evidence_sha256": f"evidence{number}",
            "context_sha256": f"context{number}",
        },
    } for number in range(10) for name in ("baseline", "candidate") for repeat in range(1, 4)]


def test_complete_comparison_passes_gate():
    assert evaluation.report(passing_rows())["promotion_gates"]["candidate"]["passed"]


@pytest.mark.parametrize("failure", ["missing_repeat", "missing_provenance", "changed_source", "failed_baseline"])
def test_incomplete_or_uncontrolled_comparison_cannot_pass(failure):
    rows = passing_rows()
    if failure == "missing_repeat":
        rows.pop(0)
    elif failure == "missing_provenance":
        rows[0]["provenance"] = None
    elif failure == "changed_source":
        rows[0]["provenance"]["evidence_sha256"] = "changed"
    else:
        rows[0]["score"]["incomplete"] = 1
    assert not evaluation.report(rows)["promotion_gates"]["candidate"]["passed"]


def test_cost_aggregation_keeps_unpriced_usage_visible():
    score = evaluation.score(case(), {"overall_verdict": "pass", "findings": []})
    result = evaluation.aggregate([{
        "case_id": "a", "score": score,
        "cost": {
            "calls": 2, "unpriced_calls": 1, "estimated_cost_usd": 0.5,
            "usage": [{"input_tokens": 100, "output_tokens": 5}],
        },
    }, {"case_id": "b", "score": score, "cost": None}])
    assert result["estimated_cost_usd"] == 0.5
    assert result["model_calls"] == 2
    assert result["unpriced_calls"] == 1
    assert result["usage_unavailable_runs"] == 1
    assert result["tokens"]["input_tokens"] == 100
