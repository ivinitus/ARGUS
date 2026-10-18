"""ground_findings: deterministic page/step anchor validation."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from argus.pipeline import auditor
from argus.pipeline.auditor import _max_step_index, ground_findings

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


class FakeModelClient:
    """Scripted stand-in for public model client."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []

    def invoke_model(self, model_id, body):
        self.calls.append({"model_id": model_id, "body": body})
        assert self._i < len(self._responses), "ran out of scripted responses"
        text = self._responses[self._i]
        self._i += 1
        payload = {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        }
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _audit_fixture(exec_dir: Path) -> None:
    """Minimal execution dir: metadata with one step + one page image."""
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    (shots / "page_001.png").write_bytes(_TINY_PNG)
    (exec_dir / "metadata.json").write_text(json.dumps(
        {"test_results": [{"steps": [
            {"index": 1, "description": "Open homepage",
             "expected_result": "Page loads"},
        ]}]}
    ))


def _audit_json(verdict: str, findings: list[dict]) -> str:
    return json.dumps({
        "overall_verdict": verdict,
        "summary": "summary text",
        "findings": findings,
    })


def _finding(severity="high", page=None, step_index=None, confidence=None):
    f = {"severity": severity, "description": "x"}
    if page is not None:
        f["page"] = page
    if step_index is not None:
        f["step_index"] = step_index
    if confidence is not None:
        f["confidence"] = confidence
    return f



def test_out_of_range_page_nulled_and_confidence_capped():
    audit = {"findings": [_finding(page=41, confidence="high")]}
    out = ground_findings(audit, n_pages=30)
    f = out["findings"][0]
    assert f["page"] is None
    assert f["confidence"] == "low"
    assert f["severity"] == "high"
    assert out["_grounding_repairs"] == [
        {"finding_index": 0, "field": "page", "claimed": 41, "max": 30}
    ]


def test_in_range_and_null_anchors_untouched():
    audit = {"findings": [
        _finding(page=30, confidence="high"),
        _finding(page=1),
        _finding(),
    ]}
    out = ground_findings(audit, n_pages=30, max_step_index=5)
    assert out["findings"][0]["page"] == 30
    assert out["findings"][0]["confidence"] == "high"
    assert out["findings"][1]["page"] == 1
    assert "_grounding_repairs" not in out


def test_zero_and_negative_pages_are_out_of_range():
    audit = {"findings": [_finding(page=0), _finding(page=-3)]}
    out = ground_findings(audit, n_pages=10)
    assert all(f["page"] is None for f in out["findings"])
    assert len(out["_grounding_repairs"]) == 2


def test_out_of_range_step_index_nulled():
    audit = {"findings": [_finding(step_index=9, confidence="medium")]}
    out = ground_findings(audit, max_step_index=7)
    f = out["findings"][0]
    assert f["step_index"] is None
    assert f["confidence"] == "low"
    assert out["_grounding_repairs"][0]["field"] == "step_index"


def test_none_bounds_are_noops():
    audit = {"findings": [_finding(page=999, step_index=999)]}
    out = ground_findings(audit)
    assert out["findings"][0]["page"] == 999
    assert out["findings"][0]["step_index"] == 999
    assert "_grounding_repairs" not in out


def test_low_confidence_not_raised():
    audit = {"findings": [_finding(page=99, confidence="low")]}
    out = ground_findings(audit, n_pages=5)
    assert out["findings"][0]["confidence"] == "low"


def test_evidence_by_step_pages_filtered():
    audit = {
        "findings": [],
        "evidence_by_step": [
            {"step_index": 1, "pages": [1, 2, 77], "status": "supported"},
            {"step_index": 2, "pages": [3], "status": "supported"},
        ],
    }
    out = ground_findings(audit, n_pages=10)
    assert out["evidence_by_step"][0]["pages"] == [1, 2]
    assert out["evidence_by_step"][1]["pages"] == [3]
    assert out["_grounding_repairs"][0]["evidence_index"] == 0


def test_max_step_index_respects_non_contiguous_indices():
    md = {"test_results": [{"steps": [
        {"index": 1}, {"index": 4}, {"index": 8},
    ]}]}
    assert _max_step_index(md) == 8
    assert _max_step_index({"test_results": [{"steps": []}]}) is None
    assert _max_step_index({}) is None



def test_hallucinated_page_cannot_corroborate_across_runs(
    tmp_path: Path, monkeypatch,
):
    exec_dir = tmp_path / "TESTKEY-GROUND-CONSENSUS"
    _audit_fixture(exec_dir)

    high = {"severity": "high", "page": 41, "description": "phantom error"}
    r1 = _audit_json("fail", [dict(high)])
    r2 = _audit_json("fail", [dict(high)])
    r3 = _audit_json("fail", [dict(high)])
    fake = FakeModelClient([r1, r2, r3])
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "concerns"
    assert out["review_status"] == "required"
    assert all(f["assessment_status"] == "needs_review" for f in out["findings"])
    assert out["_consensus"][0]["kept"] is False
    assert out["_consensus"][0]["votes"] == 1


def test_valid_page_anchor_still_corroborates(tmp_path: Path, monkeypatch):
    """Control: an in-range page anchor still confirms across runs."""
    exec_dir = tmp_path / "TESTKEY-GROUND-CONTROL"
    _audit_fixture(exec_dir)

    high = {"severity": "high", "page": 1, "description": "real error"}
    r1 = _audit_json("fail", [dict(high)])
    r2 = _audit_json("fail", [dict(high)])
    r3 = _audit_json("fail", [dict(high)])
    fake = FakeModelClient([r1, r2, r3])
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "fail"
    assert len(out["findings"]) == 1
    assert out["_consensus"][0]["votes"] == 3


def test_run_records_grounding_repairs_in_audit_json(
    tmp_path: Path, monkeypatch,
):
    exec_dir = tmp_path / "TESTKEY-GROUND-TRACE"
    _audit_fixture(exec_dir)

    resp = _audit_json("concerns", [
        {"severity": "medium", "page": 7, "step_index": 1,
         "description": "cites a page beyond the single-image evidence"},
    ])
    fake = FakeModelClient([resp])
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["findings"][0]["page"] is None
    assert out["_grounding_repairs"][0]["claimed"] == 7
