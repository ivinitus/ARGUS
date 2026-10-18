import json
from unittest import mock

import pytest

from argus.pipeline import auditor, model_client, video_evaluation as evaluation


def execution(tmp_path):
    root = tmp_path / "execution"
    root.mkdir()
    (root / "metadata.json").write_text(json.dumps({"test_results": []}))
    (root / "video.mp4").write_bytes(b"video")
    (root / "video_evidence.json").write_text(json.dumps({
        "videos": [{"path": "video.mp4", "name": "video.mp4", "format": "mp4"}],
    }))
    (root / "audit.json").write_text("original audit")
    return root


def test_comparison_leaves_originals_and_configuration_unchanged(tmp_path, monkeypatch):
    root = execution(tmp_path)
    invoke = mock.Mock(return_value={
        "overall_verdict": "pass", "summary": "Supported", "findings": [],
    })
    monkeypatch.setattr(evaluation.video_evidence, "audit_videos", invoke)
    cfg = auditor.AuditConfig(execution_dir=root)
    results = evaluation.evaluate(cfg, ["model-one", "model-two"], tmp_path / "comparison")
    assert len(results) == 2
    assert (root / "audit.json").read_text() == "original audit"
    assert json.loads((root / "video_evidence.json").read_text())["videos"][0]["path"] == "video.mp4"
    assert cfg.video_model_id == auditor.AuditConfig(execution_dir=root).video_model_id
    first, second = invoke.call_args_list
    assert first.args[0].video_model_id == "model-one"
    assert first.args[0].video_model_id != cfg.video_model_id
    assert first.args[0].usage_tracker is not second.args[0].usage_tracker
    assert "Compliant" in (tmp_path / "comparison" / "comparison.md").read_text()


def test_guardrail_stop_is_saved_and_no_other_model_is_tried(tmp_path, monkeypatch):
    root = execution(tmp_path)
    invoke = mock.Mock(side_effect=model_client.ModelInterventionError("blocked"))
    monkeypatch.setattr(evaluation.video_evidence, "audit_videos", invoke)
    with pytest.raises(model_client.ModelInterventionError):
        evaluation.evaluate(
            auditor.AuditConfig(execution_dir=root),
            ["model-one", "model-two"], tmp_path / "comparison",
        )
    assert invoke.call_count == 1
    result = json.loads((tmp_path / "comparison" / "model-1.json").read_text())
    assert result["status"] == "guardrail_stopped"


def test_evaluation_rejects_in_place_or_existing_destinations(tmp_path, monkeypatch):
    root = execution(tmp_path)
    cfg = auditor.AuditConfig(execution_dir=root)
    invoke = mock.Mock()
    monkeypatch.setattr(evaluation.video_evidence, "audit_videos", invoke)
    with pytest.raises(ValueError, match="outside"):
        evaluation.evaluate(cfg, ["model-one"], root / "comparison")
    with pytest.raises(FileExistsError):
        evaluation.evaluate(cfg, ["model-one"], tmp_path)
    invoke.assert_not_called()
