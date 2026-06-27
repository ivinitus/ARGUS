"""Integration test for auditor.run() with a mocked LLM Runtime client.

Covers the full audit pipeline:
  metadata.json + JPEG pages  →  auditor.run()  →  audit.json + audit.md

boto3 is patched so we never hit the network. The fake LLM Runtime response
matches the Claude Messages API shape that auditor.extract_audit_json() parses.
"""
from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path

import pytest

import auditor
import config as argus_config


def _make_tiny_png(path: Path) -> None:
    """Write a 1x1 valid PNG. Cheaper than pulling in Pillow for a test fixture."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw = b"\x00\xff\x00\x00"  # filter byte + one red pixel
    idat = zlib.compress(raw)
    path.write_bytes(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class FakeLLMRuntimeClient:
    """Stand-in for boto3.client('llm_runtime-runtime').

    Accepts either a single response string (returned on every call) or a
    list of strings (returned in order; one per invoke_model call). This lets
    tests exercise the repair-retry path and consensus path where the same
    pipeline calls invoke_model multiple times.
    """

    def __init__(self, response_text: str | list[str]):
        if isinstance(response_text, str):
            self._responses = [response_text]
            self._cycle = True  # single response = return it every call
        else:
            self._responses = list(response_text)
            self._cycle = False
        self._i = 0
        self.calls: list[dict] = []

    def invoke_model(self, modelId, body, contentType, accept):  # noqa: N803 - boto3 API
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        if self._cycle:
            text = self._responses[0]
        else:
            if self._i >= len(self._responses):
                raise AssertionError(
                    f"FakeLLMRuntimeClient ran out of scripted responses "
                    f"(requested call {self._i + 1}, had {len(self._responses)})"
                )
            text = self._responses[self._i]
            self._i += 1
        payload = {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        }
        return {"body": io.BytesIO(json.dumps(payload).encode())}


class _HTTPResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_invoke_model_supports_anthropic_http_provider(
        tmp_path: Path, monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers, json, timeout):
        calls.append({
            "url": url, "headers": headers, "json": json, "timeout": timeout,
        })
        return _HTTPResponse({
            "content": [{"type": "text", "text": '{"overall_verdict":"pass"}'}]
        })

    monkeypatch.setenv("ARGUS_TEST_ANTHROPIC_KEY", "test-key")
    monkeypatch.setattr(auditor.requests, "post", fake_post)

    raw = auditor.invoke_model(
        auditor.AuditConfig(
            execution_dir=tmp_path,
            model_provider="anthropic",
            model_id="claude-test",
            api_key_env="ARGUS_TEST_ANTHROPIC_KEY",
        ),
        [{"type": "text", "text": "audit this"}],
    )

    assert raw["content"][0]["text"] == '{"overall_verdict":"pass"}'
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "test-key"
    assert calls[0]["json"]["model"] == "claude-test"
    assert calls[0]["json"]["messages"][0]["content"][0]["text"] == "audit this"


def test_invoke_model_supports_openai_http_provider(tmp_path: Path, monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers, json, timeout):
        calls.append({
            "url": url, "headers": headers, "json": json, "timeout": timeout,
        })
        return _HTTPResponse({
            "choices": [{
                "message": {"content": '{"overall_verdict":"concerns"}'}
            }]
        })

    monkeypatch.setenv("ARGUS_TEST_OPENAI_KEY", "test-key")
    monkeypatch.setattr(auditor.requests, "post", fake_post)

    raw = auditor.invoke_model(
        auditor.AuditConfig(
            execution_dir=tmp_path,
            model_provider="openai",
            model_id="gpt-test",
            api_key_env="ARGUS_TEST_OPENAI_KEY",
        ),
        [
            {"type": "text", "text": "audit this"},
            {
                "type": "image",
                "source": {
                    "media_type": "image/png",
                    "data": "abc123",
                },
            },
        ],
    )

    assert raw["content"][0]["text"] == '{"overall_verdict":"concerns"}'
    assert calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls[0]["headers"]["authorization"] == "Bearer test-key"
    user_content = calls[0]["json"]["messages"][1]["content"]
    assert user_content[0] == {"type": "text", "text": "audit this"}
    assert user_content[1]["image_url"]["url"] == (
        "data:image/png;base64,abc123"
    )


def test_invoke_model_supports_google_http_provider(tmp_path: Path, monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers, json, timeout):
        calls.append({
            "url": url, "headers": headers, "json": json, "timeout": timeout,
        })
        return _HTTPResponse({
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"overall_verdict":"pass"}'}],
                },
            }],
        })

    monkeypatch.setenv("ARGUS_TEST_GOOGLE_KEY", "test-key")
    monkeypatch.setattr(auditor.requests, "post", fake_post)

    raw = auditor.invoke_model(
        auditor.AuditConfig(
            execution_dir=tmp_path,
            model_provider="google",
            model_id="gemini-test",
            api_key_env="ARGUS_TEST_GOOGLE_KEY",
        ),
        [
            {"type": "text", "text": "audit this"},
            {
                "type": "image",
                "source": {
                    "media_type": "image/jpeg",
                    "data": "abc123",
                },
            },
        ],
    )

    assert raw["content"][0]["text"] == '{"overall_verdict":"pass"}'
    assert calls[0]["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-test:generateContent?key=test-key"
    )
    parts = calls[0]["json"]["contents"][0]["parts"]
    assert parts[0] == {"text": "audit this"}
    assert parts[1]["inline_data"] == {
        "mime_type": "image/jpeg",
        "data": "abc123",
    }


def test_audit_end_to_end(tmp_path: Path, monkeypatch):
    # Layout: tmp/metadata.json + tmp/screenshots/sess1/page_001.png
    exec_dir = tmp_path / "TESTKEY-E1"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")

    metadata = {
        "test_results": [{
            "id": 999,
            "executed_by": "USER00001",
            "executed_by_name": "Jane Tester",
            "execution_date": "2026-04-26T12:00:00Z",
            "steps": [
                {"index": 1, "description": "Open homepage",
                 "expected_result": "Page loads"},
            ],
        }]
    }
    (exec_dir / "metadata.json").write_text(json.dumps(metadata))

    # Clean, well-formed audit response.
    fake_audit = {
        "overall_verdict": "pass",
        "summary": "Execution looks clean; no concerns.",
        "findings": [],
    }
    fake_client = FakeLLMRuntimeClient(json.dumps(fake_audit))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake_client)

    cfg = auditor.AuditConfig(execution_dir=exec_dir, temperature=0.0)
    rc = auditor.run(cfg)

    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "pass"
    assert out["findings"] == []

    md = (exec_dir / "audit.md").read_text()
    # Resolved display name should win over raw userKey in the report.
    assert "Jane Tester" in md
    assert "USER00001" not in md
    assert "Auditor verdict:** pass" in md

    # temperature plumbing reached the LLM Runtime call.
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["body"]["temperature"] == 0.0


def test_audit_handles_prose_wrapped_json(tmp_path: Path, monkeypatch):
    """The fallback brace-extractor should salvage JSON embedded in prose."""
    exec_dir = tmp_path / "TESTKEY-E2"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    (exec_dir / "metadata.json").write_text(json.dumps({"test_results": [{"steps": []}]}))

    noisy = (
        "Sure, here's the audit:\n"
        '{"overall_verdict": "concerns", "summary": "x", "findings": []}\n'
        "Let me know if you need more."
    )
    monkeypatch.setattr(auditor.boto3, "client",
                        lambda *a, **kw: FakeLLMRuntimeClient(noisy))

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert json.loads((exec_dir / "audit.json").read_text())["overall_verdict"] == "concerns"


# ---------------------------------------------------------------------------
# Schema validation (#3)
# ---------------------------------------------------------------------------


def test_validate_audit_accepts_clean_shape():
    audit = {
        "overall_verdict": "pass",
        "summary": "Looks fine.",
        "findings": [],
    }
    assert auditor.validate_audit(audit) == []


def test_validate_audit_accepts_findings_with_nulls():
    # page and step_index may be null per the SYSTEM_PROMPT shape.
    audit = {
        "overall_verdict": "concerns",
        "summary": "A thing.",
        "findings": [
            {"severity": "medium", "page": None, "step_index": None,
             "description": "something unclear"},
            {"severity": "high", "page": 5, "step_index": 2,
             "description": "real issue"},
        ],
    }
    assert auditor.validate_audit(audit) == []


def test_validate_audit_accepts_v6_triage_fields():
    audit = {
        "overall_verdict": "concerns",
        "summary": "Evidence is partial.",
        "findings": [{
            "severity": "medium",
            "page": 4,
            "step_index": 2,
            "category": "insufficient_evidence",
            "confidence": "medium",
            "action": "add_evidence",
            "description": "Step 2's required verification is partial.",
        }],
        "evidence_by_step": [{
            "step_index": 2,
            "pages": [4, 5],
            "status": "partial",
            "confidence": "medium",
            "missing_reason": "Confirmation email is not shown.",
        }],
    }
    assert auditor.validate_audit(audit) == []


def test_validate_audit_rejects_bad_verdict():
    audit = {"overall_verdict": "ok", "summary": "x", "findings": []}
    errs = auditor.validate_audit(audit)
    assert len(errs) == 1
    assert "overall_verdict" in errs[0]
    assert "'ok'" in errs[0]


def test_validate_audit_rejects_missing_summary():
    audit = {"overall_verdict": "pass", "findings": []}
    errs = auditor.validate_audit(audit)
    assert any("summary" in e for e in errs)


def test_validate_audit_rejects_empty_summary():
    audit = {"overall_verdict": "pass", "summary": "   ", "findings": []}
    errs = auditor.validate_audit(audit)
    assert any("summary" in e for e in errs)


def test_validate_audit_rejects_non_list_findings():
    audit = {"overall_verdict": "pass", "summary": "x", "findings": "none"}
    errs = auditor.validate_audit(audit)
    assert len(errs) == 1
    assert "findings" in errs[0]


def test_validate_audit_rejects_bad_severity():
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{"severity": "critical", "description": "boom"}],
    }
    errs = auditor.validate_audit(audit)
    assert any("severity" in e and "'critical'" in e for e in errs)


def test_validate_audit_rejects_non_integer_page():
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{"severity": "high", "page": "five",
                      "step_index": 2, "description": "boom"}],
    }
    errs = auditor.validate_audit(audit)
    assert any("page" in e for e in errs)


def test_validate_audit_rejects_bad_v6_triage_fields():
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{
            "severity": "high",
            "description": "x",
            "category": "misc",
            "confidence": "certain",
            "action": "panic",
        }],
        "evidence_by_step": [{
            "step_index": "2",
            "pages": ["4"],
            "status": "unknown",
            "confidence": "certain",
        }],
    }
    errs = auditor.validate_audit(audit)
    assert any("category" in e for e in errs)
    assert any("action" in e for e in errs)
    assert any("evidence_by_step" in e for e in errs)


def test_validate_audit_accumulates_multiple_errors():
    # Bad verdict + missing summary + bad severity in a finding.
    audit = {
        "overall_verdict": "maybe",
        "findings": [{"description": "x"}],  # missing severity
    }
    errs = auditor.validate_audit(audit)
    assert len(errs) >= 3


def test_validate_audit_skips_parse_fallback():
    # extract_audit_json's parse-failure fallback carries _raw — we should
    # NOT double-report validation errors on top of the existing concerns
    # summary it already wrote.
    audit = {
        "overall_verdict": "concerns",
        "summary": "Model returned non-JSON...",
        "findings": [],
        "_raw": "oops",
    }
    assert auditor.validate_audit(audit) == []


def _audit_fixture(exec_dir: Path) -> None:
    """Shared fixture: minimal metadata + one page for integration tests."""
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )


def test_invoke_and_parse_repairs_invalid_output(tmp_path: Path, monkeypatch):
    """First response has wrong verdict key; repair retry returns valid JSON."""
    exec_dir = tmp_path / "TESTKEY-REPAIR"
    _audit_fixture(exec_dir)

    # Round 1: invalid shape — 'verdict' instead of 'overall_verdict'.
    bad = json.dumps({"verdict": "pass", "summary": "x", "findings": []})
    # Round 2: corrected.
    good = json.dumps({"overall_verdict": "pass", "summary": "ok now",
                       "findings": []})
    fake = FakeLLMRuntimeClient([bad, good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    # Both calls were made (original + one repair retry).
    assert len(fake.calls) == 2
    # Repair-retry prompt should mention what was wrong.
    repair_body = fake.calls[1]["body"]
    repair_text = json.dumps(repair_body)
    assert "overall_verdict" in repair_text
    # Final output is the corrected response.
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "pass"
    assert out["summary"] == "ok now"


def test_invoke_and_parse_falls_back_when_repair_fails(tmp_path: Path, monkeypatch):
    """Both attempts invalid — pipeline still succeeds, produces concerns."""
    exec_dir = tmp_path / "TESTKEY-FALLBACK"
    _audit_fixture(exec_dir)

    bad1 = json.dumps({"verdict": "pass", "summary": "x", "findings": []})
    bad2 = json.dumps({"overall_verdict": "maybe", "summary": "", "findings": []})
    fake = FakeLLMRuntimeClient([bad1, bad2])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 2  # original + one repair retry, no third try
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "concerns"
    assert "schema validation" in out["summary"].lower()
    # Diagnostics preserved for inspection.
    assert "_invalid_original" in out
    assert "_invalid_repair" in out


def test_invoke_and_parse_no_retry_when_valid(tmp_path: Path, monkeypatch):
    """Happy path: valid response => exactly one invoke, no repair."""
    exec_dir = tmp_path / "TESTKEY-VALID"
    _audit_fixture(exec_dir)

    good = json.dumps({"overall_verdict": "pass", "summary": "ok", "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 1  # no repair retry needed


def test_invoke_and_parse_preserves_parse_fallback(tmp_path: Path, monkeypatch):
    """Un-parseable output => existing extract_audit_json fallback wins, NO
    repair retry (we only repair parseable-but-invalid output)."""
    exec_dir = tmp_path / "TESTKEY-UNPARSEABLE"
    _audit_fixture(exec_dir)

    garbage = "this is not json at all {{"
    fake = FakeLLMRuntimeClient([garbage])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 1  # no repair attempt on un-parseable output
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "concerns"
    assert "non-JSON" in out["summary"]


# ---------------------------------------------------------------------------
# Consensus for high-severity findings (#2)
# ---------------------------------------------------------------------------


def _audit_json(verdict: str, findings: list[dict]) -> str:
    """Build a valid audit-shaped JSON response string for the fake client."""
    return json.dumps({
        "overall_verdict": verdict,
        "summary": "summary text",
        "findings": findings,
    })


def test_consensus_skipped_when_no_high_findings(tmp_path: Path, monkeypatch):
    """Run 1 has only low/medium findings => no extra consensus runs."""
    exec_dir = tmp_path / "TESTKEY-NOHIGH"
    _audit_fixture(exec_dir)

    resp = _audit_json("concerns", [
        {"severity": "low", "page": 1, "step_index": 1,
         "description": "minor thing"},
        {"severity": "medium", "page": 2, "step_index": 2,
         "description": "medium thing"},
    ])
    fake = FakeLLMRuntimeClient([resp])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    # No high finding => no extra runs. Single call total.
    assert len(fake.calls) == 1
    out = json.loads((exec_dir / "audit.json").read_text())
    assert len(out["findings"]) == 2
    # No _consensus key when consensus wasn't triggered.
    assert "_consensus" not in out


def test_consensus_confirms_high_finding(tmp_path: Path, monkeypatch):
    """High finding appears in all 3 runs => kept, verdict stays 'fail'."""
    exec_dir = tmp_path / "TESTKEY-HIGH-CONFIRMED"
    _audit_fixture(exec_dir)

    high = {"severity": "high", "page": 5, "step_index": 3,
            "description": "500 error"}
    # Three runs all report the same high finding (description can drift,
    # matching is by step_index + severity).
    r1 = _audit_json("fail", [high])
    r2 = _audit_json("fail", [dict(high, description="server error 500")])
    r3 = _audit_json("fail", [dict(high, description="backend returned 500")])
    fake = FakeLLMRuntimeClient([r1, r2, r3])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 3  # 1 original + 2 consensus
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "fail"
    assert len(out["findings"]) == 1
    assert out["findings"][0]["severity"] == "high"
    # Consensus trace present and shows 3/3 votes.
    assert out["_consensus"][0]["votes"] == 3
    assert out["_consensus"][0]["kept"] is True


def test_consensus_rejects_unreproducible_high(tmp_path: Path, monkeypatch):
    """High finding only in run 1 => rejected, verdict downgraded."""
    exec_dir = tmp_path / "TESTKEY-HIGH-REJECTED"
    _audit_fixture(exec_dir)

    high = {"severity": "high", "page": 5, "step_index": 3,
            "description": "flaky claim"}
    r1 = _audit_json("fail", [high])
    # Runs 2 and 3 don't see it.
    r2 = _audit_json("pass", [])
    r3 = _audit_json("pass", [])
    fake = FakeLLMRuntimeClient([r1, r2, r3])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 3
    out = json.loads((exec_dir / "audit.json").read_text())
    # No surviving findings, no non-high findings => verdict drops to pass.
    assert out["overall_verdict"] == "pass"
    assert out["findings"] == []
    # Consensus trace shows the rejection with vote count.
    assert out["_consensus"][0]["votes"] == 1
    assert out["_consensus"][0]["kept"] is False
    # Summary notes that a finding was dropped.
    assert "rejected" in out["summary"].lower()


def test_consensus_mixed_confirmed_and_rejected(tmp_path: Path, monkeypatch):
    """Two high findings; one confirmed (appears in run 2), one rejected."""
    exec_dir = tmp_path / "TESTKEY-HIGH-MIXED"
    _audit_fixture(exec_dir)

    confirmed = {"severity": "high", "page": 5, "step_index": 3,
                 "description": "500 error"}
    rejected = {"severity": "high", "page": 8, "step_index": 7,
                "description": "broken image"}
    low = {"severity": "low", "page": 1, "step_index": 1,
           "description": "minor nit"}

    r1 = _audit_json("fail", [confirmed, rejected, low])
    r2 = _audit_json("fail", [dict(confirmed)])      # only confirmed reappears
    r3 = _audit_json("pass", [])                      # nothing reappears
    fake = FakeLLMRuntimeClient([r1, r2, r3])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 3
    out = json.loads((exec_dir / "audit.json").read_text())
    # 1 surviving high (confirmed) + low from run 1. rejected is gone.
    assert len(out["findings"]) == 2
    severities = sorted(f["severity"] for f in out["findings"])
    assert severities == ["high", "low"]
    # Remaining high => verdict is fail.
    assert out["overall_verdict"] == "fail"


def test_consensus_preserves_low_medium_without_extra_calls_when_no_high(
    tmp_path: Path, monkeypatch
):
    """Sanity: low/medium findings from run 1 pass through untouched; the
    second and third runs are not invoked when run 1 has zero high."""
    exec_dir = tmp_path / "TESTKEY-LOWMED-ONLY"
    _audit_fixture(exec_dir)

    r1 = _audit_json("concerns", [
        {"severity": "medium", "page": 3, "step_index": 2,
         "description": "text looks wrong"},
        {"severity": "low", "page": 10, "step_index": 5,
         "description": "minor layout"},
    ])
    # Script only one response. If consensus tried to call more, the fake
    # client raises (we set cycle=False via the list form).
    fake = FakeLLMRuntimeClient([r1])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 1
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "concerns"
    assert len(out["findings"]) == 2


def test_consensus_disabled_via_config(tmp_path: Path, monkeypatch):
    """consensus_enabled=False short-circuits even with a high finding."""
    exec_dir = tmp_path / "TESTKEY-CONSENSUS-OFF"
    _audit_fixture(exec_dir)

    r1 = _audit_json("fail", [
        {"severity": "high", "page": 1, "step_index": 1, "description": "x"},
    ])
    fake = FakeLLMRuntimeClient([r1])  # only one response scripted
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    cfg = auditor.AuditConfig(execution_dir=exec_dir, consensus_enabled=False)
    rc = auditor.run(cfg)
    assert rc == 0
    assert len(fake.calls) == 1  # no extra calls despite high finding
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "fail"
    assert len(out["findings"]) == 1
    assert "_consensus" not in out


def test_consensus_tolerates_failed_confirmation_run(tmp_path: Path, monkeypatch):
    """If a confirmation run returns invalid JSON, it's skipped (not counted
    as a no-vote). One good run + one bad run + the original = high keeps if
    the good run also saw it.

    Note: un-parseable output goes straight to the _raw fallback WITHOUT a
    repair retry (invoke_and_parse only repairs parseable-but-schema-invalid
    output). So run 2's bad response consumes exactly 1 call, not 2.
    """
    exec_dir = tmp_path / "TESTKEY-CONSENSUS-PARTIAL"
    _audit_fixture(exec_dir)

    high = {"severity": "high", "page": 5, "step_index": 3,
            "description": "real issue"}
    r1 = _audit_json("fail", [high])
    # Run 2 is un-parseable — goes to _raw fallback, no repair attempted.
    r2 = "not json at all"
    # Run 3 confirms the finding.
    r3 = _audit_json("fail", [dict(high)])
    fake = FakeLLMRuntimeClient([r1, r2, r3])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    # 1 (run 1) + 1 (run 2, un-parseable, no repair) + 1 (run 3) = 3 total calls.
    assert len(fake.calls) == 3
    out = json.loads((exec_dir / "audit.json").read_text())
    # Run 3 confirms => finding kept with 2 votes (run 1 + run 3).
    assert out["overall_verdict"] == "fail"
    assert len(out["findings"]) == 1
    assert out["_consensus"][0]["votes"] == 2
    # Honest denominator: run 2 was skipped (invalid output), so the
    # effective run count is 2, NOT the nominal CONSENSUS_RUNS (3). A
    # skipped run didn't vote either way and must not be treated as an
    # implicit "no."
    assert out["_consensus"][0]["runs"] == 2
    assert out["_consensus"][0]["kept"] is True


def test_finding_key_clusters_same_step_same_severity():
    f1 = {"severity": "high", "page": 5, "step_index": 3, "description": "a"}
    f2 = {"severity": "high", "page": 99, "step_index": 3, "description": "b"}  # diff desc + page, same step
    assert auditor._finding_key(f1) == auditor._finding_key(f2)


def test_finding_key_distinguishes_step_and_severity():
    a = {"severity": "high", "step_index": 3}
    b = {"severity": "medium", "step_index": 3}  # diff severity
    c = {"severity": "high", "step_index": 4}    # diff step
    assert auditor._finding_key(a) != auditor._finding_key(b)
    assert auditor._finding_key(a) != auditor._finding_key(c)


def test_finding_key_falls_back_to_page_when_step_missing():
    a = {"severity": "high", "step_index": None, "page": 7}
    b = {"severity": "high", "step_index": None, "page": 7}
    assert auditor._finding_key(a) == auditor._finding_key(b)


def test_finding_key_anonymous_finding_matches_only_itself():
    a = {"severity": "high"}
    b = {"severity": "high"}
    # No step, no page => can't cluster across runs.
    assert auditor._finding_key(a) != auditor._finding_key(b)


# ---------------------------------------------------------------------------
# Chunked audits for large executions (#4)
# ---------------------------------------------------------------------------


def test_plan_chunks_no_chunking_needed():
    # Fewer pages than chunk_size: one chunk covering all.
    assert auditor.plan_chunks(30, chunk_size=50, chunk_overlap=5) == [(0, 30)]


def test_plan_chunks_exactly_chunk_size():
    # Page count == chunk_size: still one chunk.
    assert auditor.plan_chunks(50, chunk_size=50, chunk_overlap=5) == [(0, 50)]


def test_plan_chunks_with_overlap():
    # 120 pages, size=50, overlap=5 => step 45.
    # Chunks: (0,50), (45,95), (90,120).
    assert auditor.plan_chunks(120, chunk_size=50, chunk_overlap=5) == [
        (0, 50), (45, 95), (90, 120),
    ]


def test_plan_chunks_covers_every_page():
    # Sanity: every page index in [0, n) must appear in at least one chunk.
    n = 173
    chunks = auditor.plan_chunks(n, chunk_size=50, chunk_overlap=5)
    covered = set()
    for start, end in chunks:
        covered.update(range(start, end))
    assert covered == set(range(n))


def test_plan_chunks_zero_overlap():
    # overlap=0 => non-overlapping chunks, step = chunk_size.
    assert auditor.plan_chunks(100, chunk_size=50, chunk_overlap=0) == [
        (0, 50), (50, 100),
    ]


def test_plan_chunks_empty_input():
    assert auditor.plan_chunks(0, chunk_size=50, chunk_overlap=5) == []


def test_plan_chunks_rejects_bad_params():
    import pytest
    with pytest.raises(ValueError):
        auditor.plan_chunks(100, chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError):
        auditor.plan_chunks(100, chunk_size=50, chunk_overlap=50)  # overlap >= size
    with pytest.raises(ValueError):
        auditor.plan_chunks(100, chunk_size=50, chunk_overlap=-1)


def test_merge_rewrites_page_numbers_to_global():
    chunk_audits = [
        {
            "overall_verdict": "concerns",
            "summary": "chunk 1",
            "findings": [
                {"severity": "medium", "page": 3, "step_index": 1,
                 "description": "issue A"},
            ],
        },
        {
            "overall_verdict": "fail",
            "summary": "chunk 2",
            "findings": [
                {"severity": "high", "page": 10, "step_index": 5,
                 "description": "issue B"},  # chunk-local page 10
            ],
        },
    ]
    # Chunk 1 is pages 1-50 (0-indexed 0-50), chunk 2 is pages 51-100 (50-100).
    ranges = [(0, 50), (50, 100)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)

    assert merged["overall_verdict"] == "fail"  # worst-case
    # Chunk 1 page 3 => global 3 (0 + 3).
    # Chunk 2 page 10 => global 60 (50 + 10).
    pages = sorted(f["page"] for f in merged["findings"])
    assert pages == [3, 60]


def test_merge_dedupes_overlap():
    # The same finding in the overlap zone of two chunks: should appear once.
    shared = {"severity": "high", "page": 2, "step_index": 3,
              "description": "A"}
    chunk_audits = [
        {"overall_verdict": "fail", "summary": "c1", "findings": [shared]},
        {"overall_verdict": "fail", "summary": "c2",
         "findings": [{"severity": "high", "page": 2, "step_index": 3,
                       "description": "A rephrased"}]},
    ]
    # Chunk 1 covers pages 0-45 (local page 2 = global 2).
    # Chunk 2 covers pages 45-90 (local page 2 = global 47).
    # Note: same step_index means they'll dedupe by _finding_key regardless
    # of resulting global page. That's intentional — same step_index + same
    # severity = same logical issue.
    ranges = [(0, 45), (45, 90)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    assert len(merged["findings"]) == 1


def test_merge_keeps_distinct_findings():
    chunk_audits = [
        {"overall_verdict": "fail", "summary": "c1",
         "findings": [{"severity": "high", "page": 3, "step_index": 2,
                       "description": "A"}]},
        {"overall_verdict": "fail", "summary": "c2",
         "findings": [{"severity": "high", "page": 8, "step_index": 9,
                       "description": "B"}]},  # different step
    ]
    ranges = [(0, 50), (45, 90)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    assert len(merged["findings"]) == 2


def test_merge_combines_verdicts_worst_case():
    def make(verdict: str) -> dict:
        return {"overall_verdict": verdict, "summary": verdict, "findings": []}

    # any fail => fail
    assert auditor.merge_chunk_audits(
        [make("pass"), make("fail"), make("concerns")],
        [(0, 10), (10, 20), (20, 30)],
    )["overall_verdict"] == "fail"
    # concerns > pass
    assert auditor.merge_chunk_audits(
        [make("pass"), make("concerns")],
        [(0, 10), (10, 20)],
    )["overall_verdict"] == "concerns"
    # all pass => pass
    assert auditor.merge_chunk_audits(
        [make("pass"), make("pass")],
        [(0, 10), (10, 20)],
    )["overall_verdict"] == "pass"


def test_merge_preserves_fallback_diagnostics():
    """A chunk that fell back to the un-parseable path should surface its
    _raw in the merged audit.json so operators see what went wrong."""
    chunk_audits = [
        {"overall_verdict": "concerns", "summary": "parse failed",
         "findings": [], "_raw": "model was angry"},
        {"overall_verdict": "pass", "summary": "clean", "findings": []},
    ]
    ranges = [(0, 50), (50, 100)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    assert "_chunk_diagnostics" in merged
    assert "chunk_1" in merged["_chunk_diagnostics"]
    assert merged["_chunk_diagnostics"]["chunk_1"]["_raw"] == "model was angry"


def test_merge_clamps_page_overflow():
    """If a model returns a chunk-local page exceeding the chunk length
    (rare but possible), clamp to the chunk end rather than emit an
    out-of-bounds global page."""
    chunk_audits = [
        {"overall_verdict": "fail", "summary": "c1",
         "findings": [{"severity": "high", "page": 999, "step_index": 1,
                       "description": "malformed"}]},
    ]
    ranges = [(0, 50)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    # Clamped to chunk end (50).
    assert merged["findings"][0]["page"] == 50


def test_merge_summary_includes_chunk_markers():
    chunk_audits = [
        {"overall_verdict": "pass", "summary": "first half fine", "findings": []},
        {"overall_verdict": "concerns", "summary": "second half odd", "findings": []},
    ]
    ranges = [(0, 50), (45, 90)]
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    assert "Chunk 1/2" in merged["summary"]
    assert "Chunk 2/2" in merged["summary"]
    assert "first half fine" in merged["summary"]
    assert "second half odd" in merged["summary"]


def test_merge_rewrites_evidence_by_step_pages_to_global():
    merged = auditor.merge_chunk_audits(
        [
            {
                "overall_verdict": "pass",
                "summary": "c1",
                "findings": [],
                "evidence_by_step": [{
                    "step_index": 2,
                    "pages": [1, 3],
                    "status": "supported",
                    "confidence": "high",
                    "missing_reason": None,
                }],
            },
            {
                "overall_verdict": "concerns",
                "summary": "c2",
                "findings": [],
                "evidence_by_step": [{
                    "step_index": 2,
                    "pages": [2],
                    "status": "partial",
                    "confidence": "medium",
                    "missing_reason": "Email page missing.",
                }],
            },
        ],
        [(0, 50), (45, 95)],
    )
    assert merged["evidence_by_step"] == [{
        "step_index": 2,
        "confidence": "high",
        "status": "supported",
        "missing_reason": "Email page missing.",
        "pages": [1, 3, 47],
    }]


def test_chunk_threshold_short_circuits_for_small_executions(
    tmp_path: Path, monkeypatch
):
    """Under-threshold runs must NOT invoke chunking. This protects the
    default single-call path and its performance for the common case."""
    exec_dir = tmp_path / "TESTKEY-SMALL"
    _audit_fixture(exec_dir)  # creates 1 page only

    good = _audit_json("pass", [])
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    # Threshold = 0 would force chunking; default 70 means 1 page stays single.
    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    # Single-shot => no chunk diagnostics key.
    assert "_chunk_diagnostics" not in out
    # No chunk markers in summary.
    assert "Chunk 1/" not in out.get("summary", "")


def test_chunked_audit_end_to_end(tmp_path: Path, monkeypatch):
    """Full integration: 120-page execution triggers 3-chunk audit, findings
    merged with global page numbers."""
    exec_dir = tmp_path / "TESTKEY-LARGE"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    # Seed 120 page images so len(pages) >= default chunk_threshold=70.
    for i in range(1, 121):
        _make_tiny_png(shots / f"page_{i:03d}.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": [
            {"index": 1, "description": "step 1", "expected_result": "ok"},
        ]}]})
    )

    # Chunks: (0,50), (45,95), (90,120) with defaults size=50, overlap=5.
    # Script one response per chunk. Each chunk reports a finding at
    # chunk-local page 10 with a different step_index so they don't dedupe.
    r1 = _audit_json("concerns", [
        {"severity": "medium", "page": 10, "step_index": 1,
         "description": "c1 thing"},
    ])
    r2 = _audit_json("concerns", [
        {"severity": "medium", "page": 10, "step_index": 2,
         "description": "c2 thing"},
    ])
    r3 = _audit_json("concerns", [
        {"severity": "medium", "page": 10, "step_index": 3,
         "description": "c3 thing"},
    ])
    # One extra text-only synthesis call happens after merge — provide a
    # plain-text response for it.
    synth = "Consolidated summary — execution had some medium concerns."
    fake = FakeLLMRuntimeClient([r1, r2, r3, synth])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 4  # one call per chunk + one synthesis call

    out = json.loads((exec_dir / "audit.json").read_text())
    # All three findings survive (distinct step_index => distinct _finding_key).
    assert len(out["findings"]) == 3
    # Global page rewrites:
    #   chunk 1 local 10 -> global 10 (start 0 + 10)
    #   chunk 2 local 10 -> global 55 (start 45 + 10)
    #   chunk 3 local 10 -> global 100 (start 90 + 10)
    pages = sorted(f["page"] for f in out["findings"])
    assert pages == [10, 55, 100]
    # Verdict is worst-case across chunks; all three were "concerns" so result is "concerns".
    assert out["overall_verdict"] == "concerns"
    # Synthesis succeeded: summary is the consolidated text (no chunk markers).
    assert out["summary"] == synth
    # Concatenated chunk summary preserved for debug mode.
    assert "_chunk_summary" in out
    assert "Chunk 1/3" in out["_chunk_summary"]


def test_chunked_audit_high_severity_triggers_consensus_per_chunk(
    tmp_path: Path, monkeypatch
):
    """Within-chunk consensus should still fire when a chunk reports highs.
    Other chunks don't pay the consensus cost if they have no highs."""
    exec_dir = tmp_path / "TESTKEY-LARGE-HIGH"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    # 100 pages => 3 chunks with defaults (size=50, overlap=5):
    #   (0,50), (45,95), (90,100).
    for i in range(1, 101):
        _make_tiny_png(shots / f"page_{i:03d}.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    # Chunk 1: high finding, will trigger consensus (2 more calls, both agree)
    high = {"severity": "high", "page": 10, "step_index": 1,
            "description": "500 error"}
    c1 = _audit_json("fail", [high])
    c1_verify1 = _audit_json("fail", [dict(high)])
    c1_verify2 = _audit_json("fail", [dict(high)])
    # Chunks 2 and 3: no highs, no consensus.
    c2 = _audit_json("pass", [])
    c3 = _audit_json("pass", [])
    # Post-merge synthesis call (text-only).
    synth = "Execution had one high-severity backend error."

    fake = FakeLLMRuntimeClient([c1, c1_verify1, c1_verify2, c2, c3, synth])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    # Pin to serial dispatch so the scripted responses line up with the
    # specific chunks the test expects (chunk 1 = fail+high triggering
    # consensus; chunks 2/3 = pass, no consensus). With parallel
    # dispatch chunks race for responses and the ordering would become
    # non-deterministic. The test's purpose is consensus semantics, not
    # parallelism — so pinning is the right move.
    rc = auditor.run(auditor.AuditConfig(
        execution_dir=exec_dir, chunk_max_parallel=1))
    assert rc == 0
    # 3 consensus calls on chunk 1 + 1 on chunk 2 + 1 on chunk 3 + 1 synthesis = 6 total.
    assert len(fake.calls) == 6

    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "fail"
    assert len(out["findings"]) == 1
    assert out["findings"][0]["page"] == 10  # chunk 1 start=0, local 10 => global 10
    # Chunk 1 diagnostics should include _consensus trace.
    assert "_chunk_diagnostics" in out
    assert "_consensus" in out["_chunk_diagnostics"]["chunk_1"]


# ---------------------------------------------------------------------------
# Stakeholder-friendly audit.md (debug flag, page-ref rewrites, synthesis)
# ---------------------------------------------------------------------------


def test_rewrite_page_refs_simple_singular():
    # "page N" => "page (start+N)".
    assert auditor._rewrite_page_refs("see page 5", 45, 95) == "see page 50"


def test_rewrite_page_refs_plural_range_hyphen():
    assert auditor._rewrite_page_refs("pages 3-7", 45, 95) == "pages 48-52"


def test_rewrite_page_refs_plural_range_en_dash():
    # Models sometimes use en-dash or em-dash.
    assert auditor._rewrite_page_refs("pages 3–7", 45, 95) == "pages 48–52"


def test_rewrite_page_refs_plural_list():
    assert auditor._rewrite_page_refs("pages 1, 2, 3", 45, 95) == "pages 46, 47, 48"


def test_rewrite_page_refs_and():
    assert auditor._rewrite_page_refs("pages 2 and 4", 45, 95) == "pages 47 and 49"


def test_rewrite_page_refs_case_insensitive():
    assert auditor._rewrite_page_refs("Page 5", 45, 95) == "Page 50"
    assert auditor._rewrite_page_refs("PAGES 3-7", 45, 95) == "PAGES 48-52"


def test_rewrite_page_refs_clamps_overflow():
    # local page 100 in a chunk of ~50 overflows; clamp to chunk_end.
    assert auditor._rewrite_page_refs("page 100", 45, 95) == "page 95"


def test_rewrite_page_refs_leaves_non_page_numbers_alone():
    # "step 5" should not be rewritten — we only touch "page(s) N".
    assert (
        auditor._rewrite_page_refs("step 5 failed on page 3", 45, 95)
        == "step 5 failed on page 48"
    )


def test_rewrite_page_refs_ignores_bare_numbers():
    # A bare "5" with no page prefix is untouched.
    assert auditor._rewrite_page_refs("5 minutes elapsed", 45, 95) == "5 minutes elapsed"


def test_rewrite_page_refs_handles_none_and_empty():
    assert auditor._rewrite_page_refs("", 0, 10) == ""
    assert auditor._rewrite_page_refs(None, 0, 10) is None


def test_rewrite_applied_via_merge():
    """End-to-end: merge_chunk_audits rewrites description text too."""
    chunk_audits = [
        {"overall_verdict": "concerns", "summary": "c1", "findings": [
            {"severity": "medium", "page": 3, "step_index": 1,
             "description": "see page 3 and pages 5-7 for details"},
        ]},
    ]
    ranges = [(45, 95)]  # chunk starts at page 46 global
    merged = auditor.merge_chunk_audits(chunk_audits, ranges)
    desc = merged["findings"][0]["description"]
    # Description now references global pages.
    assert "page 48" in desc
    assert "pages 50-52" in desc
    # Top-level page rewritten too (independently).
    assert merged["findings"][0]["page"] == 48


def test_synthesis_fallback_keeps_concatenated_summary(tmp_path: Path, monkeypatch):
    """If the synthesis LLM Runtime call fails, the concatenated per-chunk
    summary must remain as `summary` (graceful degradation)."""
    exec_dir = tmp_path / "TESTKEY-SYNTH-FAIL"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    for i in range(1, 121):  # 120 pages => 3 chunks
        _make_tiny_png(shots / f"page_{i:03d}.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    r1 = _audit_json("pass", [])
    r2 = _audit_json("pass", [])
    r3 = _audit_json("pass", [])
    # Only 3 scripted responses — the 4th synthesis call raises when the
    # fake runs out. audit_chunked catches it and logs a warning.
    fake = FakeLLMRuntimeClient([r1, r2, r3])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    # Summary stays as the per-chunk concatenation (has chunk markers).
    assert "Chunk 1/3" in out["summary"]
    # No _chunk_summary key — we only set it when synthesis succeeded.
    assert "_chunk_summary" not in out


def _make_audit(**overrides) -> dict:
    base = {
        "overall_verdict": "pass",
        "summary": "clean",
        "findings": [],
    }
    base.update(overrides)
    return base


def _make_meta() -> dict:
    return {
        "test_results": [{
            "id": 1,
            "executed_by_name": "Alice",
            "execution_date": "2026-04-28",
        }]
    }


def test_render_pass_with_no_findings_hides_findings_section():
    audit = _make_audit(overall_verdict="pass", findings=[])
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    # No "## Findings" header at all.
    assert "## Findings" not in md
    # But the verdict and counts are still there.
    assert "Auditor verdict:** pass" in md
    assert "0 findings" in md


def test_render_concerns_with_no_findings_shows_placeholder():
    """If the verdict isn't 'pass' but findings is empty (parse fallback
    scenario), the section should appear with a clarifying note."""
    audit = _make_audit(
        overall_verdict="concerns",
        summary="Model fell back; no confident findings.",
        findings=[],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    assert "## Findings" in md
    assert "_No confident findings._" in md


def test_render_groups_findings_by_severity():
    audit = _make_audit(
        overall_verdict="fail",
        findings=[
            {"severity": "low", "page": 1, "step_index": 1, "description": "L1"},
            {"severity": "high", "page": 2, "step_index": 2, "description": "H1"},
            {"severity": "medium", "page": 3, "step_index": 3, "description": "M1"},
            {"severity": "high", "page": 4, "step_index": 4, "description": "H2"},
        ],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    # Order: H1, H2, M1, L1.
    h1 = md.index("H1")
    h2 = md.index("H2")
    m1 = md.index("M1")
    l1 = md.index("L1")
    assert h1 < h2 < m1 < l1


def test_render_findings_summary_counts():
    audit = _make_audit(
        overall_verdict="fail",
        findings=[
            {"severity": "high", "description": "a"},
            {"severity": "medium", "description": "b"},
            {"severity": "medium", "description": "c"},
            {"severity": "low", "description": "d"},
        ],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    assert "4 findings" in md
    assert "1 high" in md
    assert "2 medium" in md
    assert "1 low" in md


def test_render_finding_locator_omits_null_parts():
    audit = _make_audit(
        overall_verdict="concerns",
        findings=[
            # No step_index, only page.
            {"severity": "medium", "page": 5, "step_index": None,
             "description": "only page"},
            # No page, only step_index.
            {"severity": "medium", "page": None, "step_index": 3,
             "description": "only step"},
            # Neither — no locator segment at all.
            {"severity": "low", "page": None, "step_index": None,
             "description": "no loc"},
        ],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    # Contains the clean variants, NOT "page None" or "step None".
    assert "page 5: only page" in md
    assert "step 3: only step" in md
    assert "[low]** no loc" in md  # no locator segment
    assert "None" not in md


def test_render_finding_triage_fields_and_evidence_coverage():
    audit = _make_audit(
        overall_verdict="concerns",
        findings=[{
            "severity": "medium",
            "page": 4,
            "step_index": 2,
            "category": "insufficient_evidence",
            "confidence": "medium",
            "action": "add_evidence",
            "description": "Email confirmation is not shown.",
        }],
        evidence_by_step=[{
            "step_index": 2,
            "pages": [4],
            "status": "partial",
            "confidence": "medium",
            "missing_reason": "No inbox screenshot.",
        }],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    assert "insufficient evidence" in md
    assert "action: add evidence" in md
    assert "## Evidence Coverage" in md
    assert "Step 2" in md
    assert "pages: 4" in md
    assert "No inbox screenshot." in md


def test_render_debug_false_hides_chunk_breakdown():
    audit = _make_audit(
        overall_verdict="pass",
        summary="Synthesized single-paragraph summary.",
        _chunk_summary="[Chunk 1/2]: ...\n[Chunk 2/2]: ...",
        _chunk_diagnostics={"chunk_1": {"_consensus": [{"votes": 3}]}},
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=False)
    assert "Diagnostics" not in md
    assert "Chunk 1" not in md
    assert "_consensus" not in md


def test_render_debug_true_shows_chunk_breakdown():
    audit = _make_audit(
        overall_verdict="pass",
        summary="Synthesized single-paragraph summary.",
        _chunk_summary="[Chunk 1/2 (pages 1-50)]: ok\n\n[Chunk 2/2 (pages 46-100)]: ok",
        _chunk_diagnostics={
            "chunk_1": {"_consensus": [{"votes": 3, "kept": True}]},
        },
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=True)
    assert "Diagnostics — per-chunk summary" in md
    assert "Chunk 1/2" in md
    assert "Diagnostics — per-chunk metadata" in md
    assert "chunk_1" in md
    # JSON-fenced content for the metadata block.
    assert "```json" in md


def test_render_debug_true_exposes_consensus_trace():
    audit = _make_audit(
        overall_verdict="fail",
        _consensus=[{"step_index": 3, "votes": 2, "kept": True}],
        findings=[{"severity": "high", "page": 1, "step_index": 3, "description": "x"}],
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=True)
    assert "Diagnostics — consensus trace" in md
    # Hidden when debug=False.
    md_clean = auditor.render_markdown(audit, _make_meta(), debug=False)
    assert "Diagnostics" not in md_clean


def test_render_debug_true_exposes_invalid_schema_diagnostics():
    audit = _make_audit(
        overall_verdict="concerns",
        summary="Schema validation failed twice.",
        _invalid_original={"verdict": "pass"},
        _invalid_repair={"overall_verdict": "maybe"},
    )
    md = auditor.render_markdown(audit, _make_meta(), debug=True)
    assert "_invalid_original" in md
    assert "_invalid_repair" in md


# --- Config-side debug_output knob ---


def test_config_debug_output_defaults_to_false():
    cfg = argus_config.ARGUSConfig()
    assert cfg.auditor.debug_output is False


def test_config_debug_output_reads_from_file(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text('[auditor]\ndebug_output = true\n')
    cfg = argus_config.load(p)
    assert cfg.auditor.debug_output is True


# ---------------------------------------------------------------------------
# Phase 2: auditor prompt includes tester status/comment/traceLinks
# ---------------------------------------------------------------------------


def test_build_steps_block_unchanged_when_no_tester_fields():
    """Sanity: when tester fields are absent (pre-Phase-1 metadata shape),
    the prompt still contains description + expected result and no empty
    'Tester ...' rows."""
    meta = {"test_results": [{
        "steps": [
            {"index": 1, "description": "Open page", "expected_result": "Loads"},
            {"index": 2, "description": "Click button", "expected_result": "Modal"},
        ],
    }]}
    out = auditor.build_steps_block(meta)
    assert "Step 1" in out
    assert "Description: Open page" in out
    assert "Expected result: Loads" in out
    # No empty rows added.
    assert "Tester status:" not in out
    assert "Tester comment:" not in out
    assert "Linked issues:" not in out
    # Top-level block omitted entirely when nothing to show.
    assert "Tester overall result" not in out


def test_build_steps_block_includes_top_level_tester_context():
    meta = {"test_results": [{
        "status": "Fail",
        "comment": "Step 9 did not reach future-cancel state.",
        "trace_links": ["QA-BUG-42"],
        "steps": [
            {"index": 1, "description": "d", "expected_result": "e"},
        ],
    }]}
    out = auditor.build_steps_block(meta)
    assert "Tester overall result" in out
    assert "Status: Fail" in out
    assert "Step 9 did not reach future-cancel state." in out
    assert "QA-BUG-42" in out


def test_build_steps_block_includes_per_step_tester_fields_when_present():
    meta = {"test_results": [{
        "steps": [
            {
                "index": 1, "description": "d1", "expected_result": "e1",
                "status": "Pass",  # status present
            },
            {
                "index": 2, "description": "d2", "expected_result": "e2",
                "status": "Fail",
                "comment": "server returned 500 on submit",
                "trace_links": ["QA-BUG-7", "QA-BUG-8"],
            },
        ],
    }]}
    out = auditor.build_steps_block(meta)
    # Step 1 only has status -> one tester row.
    assert "Tester status: Pass" in out
    # Step 2 has all three.
    assert "Tester status: Fail" in out
    assert "Tester comment: server returned 500 on submit" in out
    assert "Linked issues: QA-BUG-7, QA-BUG-8" in out


def test_build_steps_block_omits_empty_tester_rows_per_step():
    """Steps that have description + expected result only — no tester fields
    — must NOT emit empty rows. This keeps the prompt tight."""
    meta = {"test_results": [{
        "steps": [
            # One step with everything, one with nothing.
            {"index": 1, "description": "d1", "expected_result": "e1",
             "status": "Pass", "comment": "ok",
             "trace_links": ["QA-BUG-1"]},
            {"index": 2, "description": "d2", "expected_result": "e2"},
        ],
    }]}
    out = auditor.build_steps_block(meta)
    # Count occurrences of each tester row — must be exactly 1.
    assert out.count("Tester status:") == 1
    assert out.count("Tester comment:") == 1
    assert out.count("Linked issues:") == 1


def test_build_steps_block_ignores_empty_string_comment():
    """Comment == '' (from stripping an image-only HTML) must NOT emit a
    'Tester comment:' row with blank value."""
    meta = {"test_results": [{
        "steps": [
            {"index": 1, "description": "d", "expected_result": "e",
             "status": "Pass", "comment": "", "trace_links": []},
        ],
    }]}
    out = auditor.build_steps_block(meta)
    assert "Tester status: Pass" in out
    assert "Tester comment:" not in out
    assert "Linked issues:" not in out


def test_system_prompt_mentions_tester_status_as_context():
    """Regression guard: the prompt must contain the Option A language
    stating tester fields are CONTEXT, so the model doesn't defer blindly
    when tester Pass contradicts the screenshots."""
    assert "CONTEXT" in auditor.SYSTEM_PROMPT
    # Make sure the specific guidance about contradicting Pass is in there.
    assert "Pass" in auditor.SYSTEM_PROMPT
    assert "screenshots" in auditor.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Phase 4: workflow rules merged into audit pipeline
# ---------------------------------------------------------------------------


def test_merge_rule_findings_noop_when_no_rule_findings():
    audit = {"overall_verdict": "pass", "summary": "ok", "findings": []}
    merged = auditor.merge_rule_findings(audit, [])
    # Identity return when nothing to merge.
    assert merged is audit


def test_merge_rule_findings_escalates_verdict_high_to_fail():
    audit = {"overall_verdict": "pass", "summary": "looks clean", "findings": []}
    rule = [{"severity": "high", "rule": "R2", "source": "rule",
             "description": "fail step + pass overall",
             "step_index": 5, "page": None}]
    merged = auditor.merge_rule_findings(audit, rule)
    assert merged["overall_verdict"] == "fail"
    assert len(merged["findings"]) == 1


def test_merge_rule_findings_escalates_verdict_medium_to_concerns():
    audit = {"overall_verdict": "pass", "summary": "clean", "findings": []}
    rule = [{"severity": "medium", "rule": "R1", "source": "rule",
             "description": "in progress step", "step_index": 3, "page": None}]
    merged = auditor.merge_rule_findings(audit, rule)
    assert merged["overall_verdict"] == "concerns"


def test_merge_rule_findings_never_demotes_verdict():
    # Audit already fail — medium rule should NOT downgrade to concerns.
    audit = {"overall_verdict": "fail", "summary": "x",
             "findings": [{"severity": "high", "description": "real",
                           "step_index": 1, "page": 2}]}
    rule = [{"severity": "medium", "rule": "R1", "source": "rule",
             "description": "in progress step", "step_index": 3, "page": None}]
    merged = auditor.merge_rule_findings(audit, rule)
    assert merged["overall_verdict"] == "fail"


def test_merge_rule_findings_appends_summary_note():
    audit = {"overall_verdict": "pass", "summary": "original summary", "findings": []}
    rule = [{"severity": "medium", "rule": "R1", "source": "rule",
             "description": "x", "step_index": 1, "page": None}]
    merged = auditor.merge_rule_findings(audit, rule)
    assert "original summary" in merged["summary"]
    assert "workflow consistency" in merged["summary"].lower()


def test_merge_rule_findings_preserves_existing_findings():
    audit = {
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{"severity": "medium", "description": "screenshot-based",
                      "step_index": 2, "page": 5}],
    }
    rule = [{"severity": "high", "rule": "R2", "source": "rule",
             "description": "rule-based", "step_index": 7, "page": None}]
    merged = auditor.merge_rule_findings(audit, rule)
    assert len(merged["findings"]) == 2
    # Screenshot finding first, rule finding appended.
    assert merged["findings"][0]["description"] == "screenshot-based"
    assert merged["findings"][1]["source"] == "rule"


def test_enrich_audit_adds_triage_fields_and_demotes_unpaged_visual_high():
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{
            "severity": "high",
            "page": None,
            "step_index": 2,
            "description": "The screenshot shows a broken UI button.",
        }],
        "evidence_by_step": [{
            "step_index": 2,
            "pages": [3, 3, "bad"],
            "status": "supported",
        }],
    }
    out = auditor.enrich_audit(audit, {})
    finding = out["findings"][0]
    assert finding["severity"] == "medium"
    assert finding["_severity_adjusted_from"] == "high"
    assert finding["confidence"] == "low"
    assert finding["action"] == "review_manually"
    assert finding["category"] == "product_issue"
    assert out["evidence_by_step"] == [{
        "step_index": 2,
        "pages": [3],
        "confidence": "medium",
        "status": "supported",
        "missing_reason": None,
    }]


def test_enrich_audit_classifies_named_missing_verification_as_evidence_gap():
    audit = {
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "page": 6,
            "step_index": 8,
            "description": (
                "Step 8 expected result requires verifying the audiobook "
                "is added to the Library and the discounted price is "
                "displayed in purchase history. The TYP page is evidenced, "
                "but no library screenshot and no purchase history "
                "screenshot are present; the purchase history check is not "
                "otherwise evidenced."
            ),
        }],
    }
    out = auditor.enrich_audit(audit, {})
    finding = out["findings"][0]
    assert finding["category"] == "insufficient_evidence"
    assert finding["action"] == "add_evidence"


def test_run_merges_rule_findings_into_final_output(tmp_path: Path, monkeypatch):
    """End-to-end: metadata that trips R2 (Pass overall + Fail step) produces
    a final audit with verdict escalated to 'fail' and the rule finding
    appearing in audit.json."""
    exec_dir = tmp_path / "TESTKEY-RULES"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    # Metadata with the inconsistency the rule catches.
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "status_id": 39,
            "status": "Pass",
            "steps": [
                {"index": 1, "status_id": 39, "status": "Pass",
                 "description": "d1", "expected_result": "e1"},
                {"index": 2, "status_id": 40, "status": "Fail",
                 "description": "d2", "expected_result": "e2"},
            ],
        }]
    }))

    # Model side: "pass" with no findings (screenshots look clean).
    good = json.dumps({"overall_verdict": "pass", "summary": "clean", "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    # Exactly one LLM Runtime call — rules are free.
    assert len(fake.calls) == 1

    out = json.loads((exec_dir / "audit.json").read_text())
    # Verdict escalated by rule.
    assert out["overall_verdict"] == "fail"
    # Rule findings appended: R2 (Pass overall + Fail step) AND R11
    # (Fail step has no trace_links / no defect ref in comment) —
    # both fire correctly on this fixture, see R11 docstring.
    rule_findings = [f for f in out["findings"] if f.get("source") == "rule"]
    rule_ids = sorted(f["rule"] for f in rule_findings)
    assert rule_ids == ["R11", "R2"]
    sev = {f["rule"]: f["severity"] for f in rule_findings}
    assert sev == {"R2": "high", "R11": "medium"}
    # Summary note added.
    assert "workflow consistency" in out["summary"].lower()


def test_run_no_rule_findings_leaves_audit_untouched(tmp_path: Path, monkeypatch):
    """Metadata without any rule violations => audit output unchanged by the
    rule merge path (Phase 4 must be a no-op on clean executions)."""
    exec_dir = tmp_path / "TESTKEY-CLEAN"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "status": "Pass",
            "steps": [
                {"index": 1, "status": "Pass",
                 "description": "d", "expected_result": "e"},
            ],
        }]
    }))

    good = json.dumps({"overall_verdict": "pass", "summary": "clean", "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["overall_verdict"] == "pass"
    # No rule findings, no summary note.
    assert out["findings"] == []
    assert "workflow consistency" not in out["summary"].lower()


def test_run_merges_multiple_rule_findings(tmp_path: Path, monkeypatch):
    """Metadata that trips both R1 and R5 produces both findings in output."""
    exec_dir = tmp_path / "TESTKEY-MULTI-RULES"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "status": "Pass",
            "steps": [
                {"index": 1, "status": "In Progress",
                 "description": "d", "expected_result": "e"},
                {"index": 2, "status": "Pass",
                 "description": "d", "expected_result": "e",
                 "comment": "Button was broken on first click"},
            ],
        }]
    }))

    good = json.dumps({"overall_verdict": "pass", "summary": "clean", "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    out = json.loads((exec_dir / "audit.json").read_text())
    # Both rules fired (R1 + R5) → 2 findings, both medium severity.
    rule_findings = [f for f in out["findings"] if f.get("source") == "rule"]
    assert {f["rule"] for f in rule_findings} == {"R1", "R5"}
    # All medium => verdict is concerns (not fail).
    assert out["overall_verdict"] == "concerns"


# ---------------------------------------------------------------------------
# Phase 5: render shows tester status alongside auditor verdict
# ---------------------------------------------------------------------------


def _meta_with_status(status_name: str | None, status_id: int | None = 39) -> dict:
    return {
        "test_results": [{
            "id": 1,
            "status_id": status_id,
            "status": status_name,
            "executed_by_name": "Alice",
            "execution_date": "2026-04-28",
        }]
    }


def test_render_shows_resolved_tester_status():
    audit = {"overall_verdict": "concerns", "summary": "ok", "findings": []}
    md = auditor.render_markdown(audit, _meta_with_status("Pass"), debug=False)
    assert "Auditor verdict:** concerns" in md
    assert "Tester status:** Pass" in md


def test_render_falls_back_to_status_id_when_name_not_resolved():
    # Status map didn't resolve the id — show the raw id so the reader
    # has something to go on rather than a blank field.
    audit = {"overall_verdict": "pass", "summary": "ok", "findings": []}
    md = auditor.render_markdown(audit, _meta_with_status(None, status_id=39),
                                 debug=False)
    assert "Tester status:** (status_id=39)" in md


def test_render_tester_status_question_mark_when_no_data():
    audit = {"overall_verdict": "pass", "summary": "ok", "findings": []}
    md = auditor.render_markdown(audit,
                                 _meta_with_status(None, status_id=None),
                                 debug=False)
    assert "Tester status:** ?" in md


def test_render_auditor_verdict_vs_tester_status_side_by_side():
    """When verdict and tester status diverge, both appear in the header so
    stakeholders see the mismatch at a glance."""
    audit = {"overall_verdict": "fail", "summary": "auditor disagrees",
             "findings": [{"severity": "high", "page": 5, "step_index": 3,
                           "description": "tester missed a 500 error"}]}
    md = auditor.render_markdown(audit, _meta_with_status("Pass"), debug=False)
    assert "Auditor verdict:** fail" in md
    assert "Tester status:** Pass" in md


# ---------------------------------------------------------------------------
# Step/comment image attachments flowing through to the prompt
# ---------------------------------------------------------------------------


def _seed_step_attachment(exec_dir: Path, filename: str) -> Path:
    """Create a tiny PNG at screenshots/step_attachments/<filename>."""
    step_dir = exec_dir / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True, exist_ok=True)
    path = step_dir / filename
    _make_tiny_png(path)
    return path


def test_load_step_attachment_images_empty_when_no_dir(tmp_path: Path):
    # Nothing extracted yet — loader returns [], not an error.
    exec_dir = tmp_path / "EXEC"
    exec_dir.mkdir()
    (exec_dir / "screenshots").mkdir()
    assert auditor.load_step_attachment_images(exec_dir) == []


def test_load_step_attachment_images_parses_step_index(tmp_path: Path):
    exec_dir = tmp_path / "EXEC"
    # Create one per-step and one top-level attachment.
    p1 = _seed_step_attachment(exec_dir, "step_003_42_shot.png")
    p2 = _seed_step_attachment(exec_dir, "top_99_overview.png")

    out = auditor.load_step_attachment_images(exec_dir)
    assert len(out) == 2
    # Step-attributed first.
    assert out[0] == (p1, 3)
    assert out[1] == (p2, None)


def test_load_step_attachment_images_sorts_by_step_index(tmp_path: Path):
    exec_dir = tmp_path / "EXEC"
    _seed_step_attachment(exec_dir, "step_010_11_late.png")
    _seed_step_attachment(exec_dir, "step_002_10_early.png")
    _seed_step_attachment(exec_dir, "top_99_last.png")

    out = auditor.load_step_attachment_images(exec_dir)
    # Step 2 comes before step 10, top-level last.
    indices = [si for _, si in out]
    assert indices == [2, 10, None]


def test_load_page_images_skips_step_attachments_subdir(tmp_path: Path):
    """Regression guard: PDF-page loader must not pick up step attachments."""
    exec_dir = tmp_path / "EXEC"
    # A legitimate PDF-split subdir.
    pdf_dir = exec_dir / "screenshots" / "pdf_session"
    pdf_dir.mkdir(parents=True)
    _make_tiny_png(pdf_dir / "page_001.png")
    # A step_attachments subdir — must be ignored by load_page_images.
    _seed_step_attachment(exec_dir, "step_001_1_extra.png")

    pages = auditor.load_page_images(exec_dir, max_pages=None)
    assert len(pages) == 1
    assert pages[0].name == "page_001.png"


def test_build_message_content_omits_attachment_block_when_none():
    # Backward-compat: existing calls without step_attachments must produce
    # a prompt with no "Tester-attached reference images" divider.
    content = auditor.build_message_content("step text", [])
    text_blocks = [c for c in content if c.get("type") == "text"]
    assert not any("Tester-attached reference images" in b.get("text", "")
                   for b in text_blocks)


def test_build_message_content_includes_attachment_block_with_labels(
    tmp_path: Path,
):
    # One step-attributed image, one top-level.
    p1 = tmp_path / "step_003_42_shot.png"
    _make_tiny_png(p1)
    p2 = tmp_path / "top_99_overview.png"
    _make_tiny_png(p2)
    step_attachments = [(p1, 3), (p2, None)]

    content = auditor.build_message_content(
        "step text", [], step_attachments=step_attachments,
    )
    text_blocks = [c.get("text", "") for c in content if c.get("type") == "text"]
    # Divider present.
    assert any("Tester-attached reference images" in t for t in text_blocks)
    # Step attribution label for the per-step image.
    assert any(t.startswith("Attachment for Step 3") for t in text_blocks)
    # Top-level label for the other.
    assert any(t.startswith("Top-level attachment") for t in text_blocks)
    # Two image blocks present.
    image_blocks = [c for c in content if c.get("type") == "image"]
    assert len(image_blocks) == 2


def test_build_message_content_mentions_attachment_count_in_intro(tmp_path: Path):
    p1 = tmp_path / "step_001_1_x.png"
    _make_tiny_png(p1)
    content = auditor.build_message_content(
        "step text", [], step_attachments=[(p1, 1)],
    )
    # The intro text mentions the number of additional attachments so the
    # model knows they're coming.
    intro = content[0].get("text", "")
    assert "1 additional" in intro
    assert "reference image" in intro


def test_build_message_content_attachment_uses_correct_media_type(tmp_path: Path):
    """Make sure PNG/JPG/WebP all get correct media_type in the payload."""
    p_png = tmp_path / "step_001_1_a.png"
    _make_tiny_png(p_png)
    p_jpg = tmp_path / "step_001_2_b.jpg"
    _make_tiny_png(p_jpg)  # filename.jpg, content is PNG — test only checks media_type routing

    content = auditor.build_message_content(
        "x", [], step_attachments=[(p_png, 1), (p_jpg, 1)],
    )
    image_blocks = [c for c in content if c.get("type") == "image"]
    media_types = [b["source"]["media_type"] for b in image_blocks]
    assert media_types == ["image/png", "image/jpeg"]


def test_run_uses_step_attachments_when_present(tmp_path: Path, monkeypatch):
    """End-to-end single-call path: run() should load step_attachments and
    the LLM Runtime call should contain the image + divider text."""
    exec_dir = tmp_path / "EXEC-STEP-ATT"
    # One PDF page + one step attachment.
    pdf_dir = exec_dir / "screenshots" / "pdf_session"
    pdf_dir.mkdir(parents=True)
    _make_tiny_png(pdf_dir / "page_001.png")
    _seed_step_attachment(exec_dir, "step_003_42_shot.png")

    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    good = json.dumps({"overall_verdict": "pass", "summary": "ok", "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    assert len(fake.calls) == 1
    # Inspect the body — content array should include the attachment divider
    # text and 2 image blocks (1 PDF page + 1 step attachment).
    content = fake.calls[0]["body"]["messages"][0]["content"]
    text_blocks = [c.get("text", "") for c in content if c.get("type") == "text"]
    assert any("Tester-attached reference images" in t for t in text_blocks)
    assert any("Attachment for Step 3" in t for t in text_blocks)
    image_blocks = [c for c in content if c.get("type") == "image"]
    assert len(image_blocks) == 2


# ---------------------------------------------------------------------------
# Phase 2: error persistence (_last_error.json)
# ---------------------------------------------------------------------------


def test_last_error_written_on_missing_metadata(tmp_path: Path):
    """metadata.json missing => rc=1 AND _last_error.json captures the
    failure reason so `_failed_keys.txt` retries are self-describing."""
    exec_dir = tmp_path / "TESTKEY-NO-METADATA"
    exec_dir.mkdir()

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 1
    err_path = exec_dir / "_last_error.json"
    assert err_path.exists(), "failed audit must persist _last_error.json"
    err = json.loads(err_path.read_text())
    assert err["rc"] == 1
    assert "metadata.json not found" in err["failure_reason"]
    # AuditConfig snapshot survives so reader knows model/region used.
    assert err["model_id"] == auditor.DEFAULT_MODEL_ID
    assert err["region"] == auditor.DEFAULT_REGION
    # Timestamp is ISO-8601 with timezone info (we set UTC).
    assert err["timestamp"].endswith("+00:00") or err["timestamp"].endswith("Z")


def test_last_error_written_on_no_page_images(tmp_path: Path):
    """Metadata present but no screenshots or step attachments => rc=1
    AND an error file describing the reason."""
    exec_dir = tmp_path / "TESTKEY-NO-PAGES"
    exec_dir.mkdir()
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 1
    err = json.loads((exec_dir / "_last_error.json").read_text())
    assert err["rc"] == 1
    assert "no page images" in err["failure_reason"]


def test_last_error_written_on_llm_runtime_exception(tmp_path: Path, monkeypatch):
    """LLM Runtime throwing an exception inside the audit flow => _last_error.json
    captures the exception class, message, and traceback, then the
    exception propagates so batch.run_batch marks the key failed."""
    exec_dir = tmp_path / "TESTKEY-BEDROCK-BOOM"
    _audit_fixture(exec_dir)

    class _SimulatedThrottle(Exception):
        pass

    # boto3.client() itself constructs fine; invoke_model raises.
    class _ExplodingClient:
        def __init__(self):
            self.calls = 0

        def invoke_model(self, **kwargs):  # noqa: D401
            self.calls += 1
            raise _SimulatedThrottle("Rate exceeded")

    bad = _ExplodingClient()
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: bad)

    with pytest.raises(_SimulatedThrottle):
        auditor.run(auditor.AuditConfig(execution_dir=exec_dir,
                                        consensus_enabled=False))
    err_path = exec_dir / "_last_error.json"
    assert err_path.exists()
    err = json.loads(err_path.read_text())
    assert err["exception_class"] == "_SimulatedThrottle"
    assert "Rate exceeded" in err["exception_message"]
    assert "Traceback" in err["traceback"]
    # Config snapshot still written so operator can confirm which model
    # + region hit the throttle.
    assert err["model_id"] == auditor.DEFAULT_MODEL_ID


def test_last_error_cleared_on_successful_audit(tmp_path: Path, monkeypatch):
    """A stale _last_error.json from a previous failure must be removed
    when a subsequent audit succeeds — otherwise the execution dir keeps
    reporting resolved failures."""
    exec_dir = tmp_path / "TESTKEY-RETRY-OK"
    _audit_fixture(exec_dir)

    # Seed a stale error file as if a previous run had failed.
    stale = {"exception_class": "OldError", "exception_message": "previous run"}
    (exec_dir / "_last_error.json").write_text(json.dumps(stale))

    good = json.dumps({"overall_verdict": "pass", "summary": "ok",
                       "findings": []})
    fake = FakeLLMRuntimeClient([good])
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0
    # _last_error.json must be removed on success.
    assert not (exec_dir / "_last_error.json").exists()


def test_last_error_overwritten_on_repeat_failure(tmp_path: Path):
    """A second failure of the same key must replace, not append to, the
    error file — the 'last' in the name is an invariant readers rely on."""
    exec_dir = tmp_path / "TESTKEY-REPEAT-FAIL"
    exec_dir.mkdir()

    # Round 1: no metadata => rc=1.
    rc1 = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc1 == 1
    err1 = json.loads((exec_dir / "_last_error.json").read_text())
    ts1 = err1["timestamp"]

    # Wait at least 1s so timestamp (second-precision) can differ, then
    # fail again.
    import time
    time.sleep(1.1)
    rc2 = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc2 == 1
    err2 = json.loads((exec_dir / "_last_error.json").read_text())
    # Content matches same failure shape, but timestamp changed — proof
    # it's a fresh write, not an append.
    assert err2["timestamp"] != ts1
    # Single JSON object, not an array.
    assert isinstance(err2, dict)


# ---------------------------------------------------------------------------
# Phase 3: variant_name provenance stamping
# ---------------------------------------------------------------------------


def test_audit_json_carries_schema_version(tmp_path: Path, monkeypatch):
    """schema_version is always stamped so downstream readers can
    discriminate rule-set / shape generations."""
    exec_dir = tmp_path / "TESTKEY-SCHEMA"
    _audit_fixture(exec_dir)
    fake = FakeLLMRuntimeClient(json.dumps({
        "overall_verdict": "pass", "summary": "ok", "findings": [],
    }))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["schema_version"] == auditor.AUDIT_SCHEMA_VERSION
    # Exact value pinned so a future bump forces a deliberate test
    # update — prevents silent schema drift.
    assert auditor.AUDIT_SCHEMA_VERSION == 6


def test_audit_json_stamps_default_variant_name_v5(tmp_path: Path, monkeypatch):
    """Production callers that don't set variant_name get 'v5' — the
    canonical default promoted 2026-05-23 after the V5.2 wide-replay
    validation. Guarantees audit.json never claims unknown provenance
    for a production-path run, and that new audits are correctly
    tagged for report.py's variant-aware scan.

    Promotion history of this assertion:
      pre-2026-05-09: "v1"
      2026-05-09:     "v4"
      2026-05-23:     "v5"
    A future promotion (v6 etc) should update both
    AuditConfig.variant_name default AND this assertion in the same
    commit.
    """
    exec_dir = tmp_path / "TESTKEY-VARIANT-DEFAULT"
    _audit_fixture(exec_dir)
    fake = FakeLLMRuntimeClient(json.dumps({
        "overall_verdict": "pass", "summary": "ok", "findings": [],
    }))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    # No variant_name argument => defaults to "v5".
    auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["variant_name"] == "v5"


def test_audit_json_stamps_explicit_variant_name(tmp_path: Path, monkeypatch):
    """An explicit variant_name override flows through to audit.json —
    this is the contract replay.replay_audit relies on."""
    exec_dir = tmp_path / "TESTKEY-VARIANT-EXPLICIT"
    _audit_fixture(exec_dir)
    fake = FakeLLMRuntimeClient(json.dumps({
        "overall_verdict": "pass", "summary": "ok", "findings": [],
    }))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    auditor.run(auditor.AuditConfig(
        execution_dir=exec_dir,
        variant_name="v-experiment-xyz",
    ))
    out = json.loads((exec_dir / "audit.json").read_text())
    assert out["variant_name"] == "v-experiment-xyz"



# ---------------------------------------------------------------------------
# Parallel chunk dispatch (chunk_max_parallel)
# ---------------------------------------------------------------------------

def test_audit_chunked_runs_chunks_in_parallel(tmp_path: Path, monkeypatch):
    """When chunk_max_parallel > 1, chunks dispatch concurrently so at least
    2 invoke_model calls overlap at some point.

    Timing-independent: the assertion is "max simultaneous in-flight chunks
    was >=2", which is the actual definition of parallel dispatch. Avoids
    absolute wall-time assertions which are brittle under OCR/env_check
    overhead and CI scheduling variance.
    """
    import threading
    import time

    exec_dir = tmp_path / "TESTKEY-PARALLEL"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    # 120 pages => 3 chunks with defaults (size=50, overlap=5).
    for i in range(1, 121):
        _make_tiny_png(shots / f"page_{i:03d}.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    SLEEP_S = 0.4  # long enough that concurrent threads reliably overlap
    in_flight = [0]
    max_in_flight = [0]
    lock = threading.Lock()
    chunk_body = _audit_json("pass", [])

    class ParallelismProbeClient:
        def invoke_model(self, **kw):
            with lock:
                in_flight[0] += 1
                max_in_flight[0] = max(max_in_flight[0], in_flight[0])
            try:
                time.sleep(SLEEP_S)
                import io as _io
                payload = {
                    "content": [{"type": "text", "text": chunk_body}],
                    "stop_reason": "end_turn",
                }
                return {"body": _io.BytesIO(json.dumps(payload).encode())}
            finally:
                with lock:
                    in_flight[0] -= 1

    monkeypatch.setattr(auditor.boto3, "client",
                        lambda *a, **kw: ParallelismProbeClient())

    rc = auditor.run(auditor.AuditConfig(
        execution_dir=exec_dir, chunk_max_parallel=4))
    assert rc == 0
    # >=2 chunks ran simultaneously at some point. Proves parallel dispatch.
    # Synthesis is the last call and runs alone, so this is chunk-overlap
    # specifically, not synthesis overlapping chunks.
    assert max_in_flight[0] >= 2, (
        f"chunks did not overlap — max_in_flight={max_in_flight[0]}, "
        "parallel dispatch regressed to serial"
    )


def test_audit_chunked_respects_chunk_max_parallel_1(
    tmp_path: Path, monkeypatch
):
    """chunk_max_parallel=1 reproduces the pre-parallel serial path
    exactly — no chunks overlap, same call ordering as before."""
    import threading
    import time

    exec_dir = tmp_path / "TESTKEY-SERIAL"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    for i in range(1, 121):
        _make_tiny_png(shots / f"page_{i:03d}.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )

    in_flight = [0]
    max_in_flight = [0]
    lock = threading.Lock()
    chunk_body = _audit_json("pass", [])

    class SerialProbeClient:
        def invoke_model(self, **kw):
            with lock:
                in_flight[0] += 1
                max_in_flight[0] = max(max_in_flight[0], in_flight[0])
            try:
                time.sleep(0.05)
                import io as _io
                payload = {
                    "content": [{"type": "text", "text": chunk_body}],
                    "stop_reason": "end_turn",
                }
                return {"body": _io.BytesIO(json.dumps(payload).encode())}
            finally:
                with lock:
                    in_flight[0] -= 1

    monkeypatch.setattr(auditor.boto3, "client",
                        lambda *a, **kw: SerialProbeClient())

    rc = auditor.run(auditor.AuditConfig(
        execution_dir=exec_dir, chunk_max_parallel=1))

    assert rc == 0
    # Serial path: at most 1 call in flight at any time.
    assert max_in_flight[0] == 1, (
        f"chunk_max_parallel=1 overlapped calls — max_in_flight="
        f"{max_in_flight[0]}, should be 1"
    )


# --- _finalize_verdict (P1.1) -----------------------------------------------


def test_finalize_verdict_escalates_concerns_to_fail_on_high_finding():
    """Bug fix: model emitted high finding but kept verdict at concerns
    (E179041 case from May 15 corpus). _finalize_verdict must force fail."""
    audit = {
        "overall_verdict": "concerns",
        "findings": [{"severity": "high", "description": "x"}],
    }
    out = auditor._finalize_verdict(audit)
    assert out["overall_verdict"] == "fail"
    assert out["_verdict_escalated_from"] == "concerns"


def test_finalize_verdict_escalates_pass_to_concerns_on_any_finding():
    audit = {
        "overall_verdict": "pass",
        "findings": [{"severity": "low", "description": "y"}],
    }
    out = auditor._finalize_verdict(audit)
    assert out["overall_verdict"] == "concerns"


def test_finalize_verdict_never_demotes():
    # An empty findings list with verdict=fail should stay fail (caller
    # may have set it deliberately for a parse-fallback summary).
    audit = {"overall_verdict": "fail", "findings": []}
    out = auditor._finalize_verdict(audit)
    assert out["overall_verdict"] == "fail"
    assert "_verdict_escalated_from" not in out


def test_finalize_verdict_idempotent_when_already_fail():
    audit = {
        "overall_verdict": "fail",
        "findings": [{"severity": "high", "description": "x"}],
    }
    out = auditor._finalize_verdict(audit)
    assert out["overall_verdict"] == "fail"
    assert "_verdict_escalated_from" not in out


# --- _drop_deprecated_blocked_findings (P1.4) -------------------------------


def test_drop_deprecated_blocked_strips_no_buglink_finding_on_pass_blocked():
    metadata = {
        "test_results": [{
            "status": "Pass",
            "steps": [
                {"index": 1, "status": "Pass"},
                {"index": 2, "status": "Blocked"},
            ],
        }]
    }
    audit = {
        "overall_verdict": "concerns",
        "findings": [
            {"severity": "low", "step_index": 2,
             "description": "Step 2 is blocked but no bug link is attached"},
        ],
    }
    out = auditor._drop_deprecated_blocked_findings(audit, metadata)
    assert out["findings"] == []
    assert out["_dropped_blocked_buglink_fp"] == 1


def test_drop_deprecated_blocked_preserves_rule_findings():
    # Rule-sourced findings (source="rule") must NEVER be dropped — they're
    # deterministic and bypass the FP filter.
    metadata = {
        "test_results": [{
            "status": "Pass",
            "steps": [{"index": 2, "status": "Blocked"}],
        }]
    }
    audit = {
        "overall_verdict": "concerns",
        "findings": [{
            "severity": "low", "step_index": 2,
            "description": "Step 2 is blocked but no bug link",
            "source": "rule", "rule": "R10",
        }],
    }
    out = auditor._drop_deprecated_blocked_findings(audit, metadata)
    assert len(out["findings"]) == 1
    assert "_dropped_blocked_buglink_fp" not in out


def test_drop_deprecated_blocked_preserves_high_findings():
    # High-severity findings on Blocked steps describe real failures
    # (e.g. expected entity missing). Must not be dropped.
    metadata = {
        "test_results": [{
            "status": "Pass",
            "steps": [{"index": 2, "status": "Blocked"}],
        }]
    }
    audit = {
        "overall_verdict": "concerns",
        "findings": [{
            "severity": "high", "step_index": 2,
            "description": "Step 2 is blocked step with no bug link",
        }],
    }
    out = auditor._drop_deprecated_blocked_findings(audit, metadata)
    assert len(out["findings"]) == 1


def test_drop_deprecated_blocked_skips_when_overall_not_pass():
    metadata = {
        "test_results": [{
            "status": "Fail",
            "steps": [{"index": 2, "status": "Blocked"}],
        }]
    }
    audit = {
        "overall_verdict": "fail",
        "findings": [{
            "severity": "low", "step_index": 2,
            "description": "Step 2 blocked but no bug link",
        }],
    }
    out = auditor._drop_deprecated_blocked_findings(audit, metadata)
    assert len(out["findings"]) == 1


# --- _is_webview_path -------------------------------------------------------


def test_is_webview_path_matches_webview_only():
    """V5.3 tightening: _is_webview_path must MATCH explicit webview
    folders ("Webview_E2E_*", "Listen_in_App", etc.) but NOT match
    mobile-browser folders. Pre-V5.3 it matched "mobile" too, which
    over-fired R9-amb on every mobile-browser test (mobile browsers
    open exampleapp.com directly — URL bar visible, chrome://inspect
    inapplicable). See SYSTEM_PROMPT_V5 changelog comment for the
    full rationale and the 12-audit FP class this fix addresses."""
    # MUST match — explicit webview folders
    assert auditor._is_webview_path("/output/foo/Webview_E2E_-_DE/x")
    assert auditor._is_webview_path("output/A/Webview_E2E_-_AU/foo/x")
    assert auditor._is_webview_path("/output/Listen_in_App_-_US/x")
    assert auditor._is_webview_path("/output/in-app_test/x")
    # MUST NOT match — mobile-browser folders are not webview tests
    assert not auditor._is_webview_path("/output/Mobile_Test_-_AU/x")
    assert not auditor._is_webview_path("output/A/B/Mobile_-_iphone/x")
    assert not auditor._is_webview_path(
        "/output/Arya_Regression_-_US_Mobile_-_Playback_-_Android/x")
    assert not auditor._is_webview_path(
        "/output/E2E_Flow_-_GMA_-_US_Mobile_-_Firefox_-_Samsung/x")
    # MUST NOT match — desktop-only folders
    assert not auditor._is_webview_path("/output/Desktop_Flow_FR/x")
    assert not auditor._is_webview_path("output/Additional_Non_Automated/x")
