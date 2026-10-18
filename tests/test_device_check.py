from __future__ import annotations

import io
import json
from pathlib import Path

from argus.domain import execution_policy
from argus.pipeline import device_check


def test_haiku_55_device_request_uses_supported_sampling_and_thinking(monkeypatch):
    from PIL import Image

    requests = []

    def invoke(_client, **kwargs):
        requests.append(kwargs)
        return {"body": io.BytesIO(json.dumps({
            "content": [{"type": "text", "text": json.dumps({
                "execution_mode": "desktop_browser",
                "signals": ["desktop_taskbar"],
                "confidence": "high",
            })}],
        }).encode())}

    monkeypatch.setattr(device_check, "_get_client", lambda *args: object())
    monkeypatch.setattr(device_check.model_client, "invoke_model", invoke)
    result = device_check._classify_one_image(
        Image.new("RGB", (8, 8)), device_check.DEFAULT_MODEL_ID, "https://api.anthropic.com/v1/messages", None,
    )
    assert result["execution_mode"] == "desktop_browser"
    assert requests[0]["model_id"] == "claude-haiku-5-5"
    body = json.loads(requests[0]["body"])
    assert "temperature" not in body
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 256


def _metadata(
    *,
    run: str = "",
    device: str | None = None,
    surface: str | None = None,
    mode: str | None = None,
    step: str = "",
) -> dict:
    result = {
        "testrun_name": run,
        "expected_device": device,
        "steps": [{"index": 1, "description": step}],
    }
    if surface:
        result["expected_surface"] = surface
    if mode:
        result["expected_execution_mode"] = mode
    return {"test_results": [result]}


def _detected(
    mode: str,
    *,
    physical: str = "unknown",
    emulated: str = "unknown",
    confidence: str = "high",
    signals: tuple[str, ...] = (),
) -> dict:
    return {
        "execution_mode": mode,
        "physical_os_family": physical,
        "emulated_profile_family": emulated,
        "confidence": confidence,
        "signals": list(signals),
        "images_checked": 2,
        "observations": [],
    }


def _evaluate(monkeypatch, metadata: dict, detected: dict):
    monkeypatch.setattr(
        device_check,
        "detect_device_family",
        lambda *args, **kwargs: detected,
    )
    return device_check.evaluate_device_compliance(
        metadata,
        Path("/tmp/execution"),
    )


def test_execution_policy_resolves_run_text_modes():
    assert execution_policy.expected_execution_mode(
        _metadata(run="E2E US Desktop Chrome"),
        "",
    ) == execution_policy.DESKTOP_BROWSER
    assert execution_policy.expected_execution_mode(
        _metadata(run="E2E US Mobile Chrome"),
        "",
    ) == execution_policy.MOBILE_EMULATION_ALLOWED
    assert execution_policy.expected_execution_mode(
        _metadata(run="Webview E2E US Android"),
        "",
    ) == execution_policy.PHYSICAL_DEVICE
    assert execution_policy.expected_execution_mode(
        _metadata(run="Arya Mobile - Playback - iOS"),
        "",
    ) == execution_policy.PHYSICAL_DEVICE


def test_chrome_inspect_alone_does_not_require_physical_device():
    metadata = _metadata(step="Open chrome://inspect and capture evidence")
    assert execution_policy.expected_execution_mode(
        metadata,
        "",
    ) == execution_policy.UNKNOWN


def test_mobile_run_is_not_overridden_by_desktop_cloud_player_step():
    metadata = _metadata(
        run="E2E US Mobile Chrome",
        step="Open the Cloud Player in a desktop browser",
    )
    assert execution_policy.expected_execution_mode(
        metadata,
        "",
    ) == execution_policy.MOBILE_EMULATION_ALLOWED


def test_explicit_surface_takes_precedence_over_incidental_step_text():
    metadata = _metadata(
        surface="desktop",
        step="Verify the related Mobile app behavior",
    )
    assert execution_policy.expected_execution_mode(
        metadata,
        "",
    ) == execution_policy.DESKTOP_BROWSER


def test_incidental_webview_step_does_not_require_physical_device():
    metadata = _metadata(
        run="E2E US Mobile Chrome",
        step="Compare the Webview behavior documented by another suite",
    )
    assert execution_policy.expected_execution_mode(
        metadata,
        "",
    ) == execution_policy.MOBILE_EMULATION_ALLOWED


def test_detection_prefers_concrete_high_confidence_observation(
    tmp_path,
    monkeypatch,
):
    image_paths = [
        tmp_path / "screenshots" / "page_001.png",
        tmp_path / "screenshots" / "page_002.png",
    ]
    monkeypatch.setattr(
        device_check.env_check,
        "_iter_audit_images",
        lambda execution_dir: image_paths,
    )
    monkeypatch.setattr(
        device_check.Image,
        "open",
        lambda path: object(),
    )
    observations = iter([
        _detected("unknown"),
        _detected("physical_mobile", physical="android"),
    ])
    monkeypatch.setattr(
        device_check,
        "_classify_one_image",
        lambda *args, **kwargs: next(observations),
    )

    result = device_check.detect_device_family(tmp_path)

    assert result["execution_mode"] == "physical_mobile"
    assert result["physical_os_family"] == "android"
    assert result["family"] == "android"
    assert result["observations"][0]["image"] == "screenshots/page_001.png"


def test_mobile_system_chrome_is_physical_but_desktop_taskbar_is_not():
    assert device_check._has_physical_evidence({
        "mobile_status_bar",
        "mobile_navigation_bar",
    })
    assert not device_check._has_physical_evidence({
        "desktop_taskbar",
        "devtools_panel",
    })
    assert device_check._has_physical_evidence({
        "chrome_inspect",
        "remote_target",
    })


def test_generic_devtools_is_not_proof_of_emulation():
    assert device_check._resolve_observed_mode(
        "devtools_emulation",
        {"devtools_panel"},
        "high",
    ) == ("unknown", "low")


def test_physical_label_without_device_evidence_is_unknown():
    assert device_check._resolve_observed_mode(
        "physical_mobile",
        set(),
        "high",
    ) == ("unknown", "low")


def test_responsive_toolbar_is_proven_emulation():
    assert device_check._resolve_observed_mode(
        "unknown",
        {"devtools_panel", "responsive_device_toolbar", "emulator_profile"},
        "high",
    ) == ("devtools_emulation", "high")


def test_remote_physical_evidence_overrides_devtools_label():
    assert device_check._resolve_observed_mode(
        "devtools_emulation",
        {
            "devtools_panel",
            "chrome_inspect",
            "remote_target",
            "attached_device_identifier",
            "screen_mirroring",
            "android_system_ui",
        },
        "high",
    ) == ("physical_mobile", "high")


def test_detection_prefers_physical_over_incidental_desktop(
    tmp_path,
    monkeypatch,
):
    image_paths = [
        tmp_path / "screenshots" / "page_001.png",
        tmp_path / "screenshots" / "page_002.png",
    ]
    monkeypatch.setattr(
        device_check.env_check,
        "_iter_audit_images",
        lambda execution_dir: image_paths,
    )
    monkeypatch.setattr(device_check.Image, "open", lambda path: object())
    observations = iter([
        _detected(
            "physical_mobile",
            physical="android",
            signals=(
                "remote_target",
                "attached_device_identifier",
                "screen_mirroring",
                "mobile_status_bar",
            ),
        ),
        _detected(
            "desktop_browser",
            signals=("desktop_browser_content", "devtools_panel"),
        ),
    ])
    monkeypatch.setattr(
        device_check,
        "_classify_one_image",
        lambda *args, **kwargs: next(observations),
    )

    result = device_check.detect_device_family(tmp_path)

    assert result["execution_mode"] == "physical_mobile"
    assert result["physical_os_family"] == "android"
    assert "screen_mirroring" in result["signals"]
    assert "devtools_panel" in result["signals"]


def test_detection_mixed_physical_and_explicit_emulation_is_ambiguous(
    tmp_path,
    monkeypatch,
):
    image_paths = [
        tmp_path / "screenshots" / "page_001.png",
        tmp_path / "screenshots" / "page_002.png",
    ]
    monkeypatch.setattr(
        device_check.env_check,
        "_iter_audit_images",
        lambda execution_dir: image_paths,
    )
    monkeypatch.setattr(device_check.Image, "open", lambda path: object())
    observations = iter([
        _detected(
            "physical_mobile",
            physical="android",
            signals=("screen_mirroring", "android_system_ui"),
        ),
        _detected(
            "devtools_emulation",
            emulated="android",
            signals=("responsive_device_toolbar", "emulator_profile"),
        ),
    ])
    monkeypatch.setattr(
        device_check,
        "_classify_one_image",
        lambda *args, **kwargs: next(observations),
    )

    result = device_check.detect_device_family(tmp_path)

    assert result["execution_mode"] == "unknown"
    assert result["confidence"] == "low"
    assert "screen_mirroring" in result["signals"]
    assert "responsive_device_toolbar" in result["signals"]


def test_ordinary_mobile_allows_matching_devtools_emulation(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="E2E US Mobile Chrome", device="Android"),
        _detected("devtools_emulation", emulated="android"),
    )
    assert result["status"] == "compliant"
    assert result["findings"] == []


def test_ordinary_mobile_rejects_proven_wrong_os_family(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="E2E US Mobile Chrome", device="Android"),
        _detected("devtools_emulation", emulated="ios"),
    )
    assert result["status"] == "mismatch"
    assert result["findings"][0]["rule"] == "R19"
    assert result["findings"][0]["advisory"] is False


def test_physical_webview_rejects_devtools_emulation(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="Webview E2E US Android", device="Android"),
        _detected("devtools_emulation", emulated="android"),
    )
    assert result["status"] == "mismatch"
    assert result["findings"][0]["severity"] == "high"


def test_inconclusive_device_evidence_is_advisory(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="E2E US Mobile Chrome", device="Android"),
        _detected("unknown", confidence="low"),
    )
    assert result["status"] == "unverified"
    assert result["findings"] == [{
        "severity": "low",
        "page": None,
        "step_index": None,
        "description": result["findings"][0]["description"],
        "source": "device_check",
        "rule": "R19-amb",
        "category": "insufficient_evidence",
        "action": "add_evidence",
        "confidence": "low",
        "advisory": True,
    }]


def test_unknown_expected_mode_is_advisory(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="Unclassified regression"),
        _detected("desktop_browser"),
    )
    assert result["status"] == "unverified"
    assert result["findings"][0]["rule"] == "R19-amb"


def test_matching_physical_device_is_compliant(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="Listen in App iOS", device="iPhone"),
        _detected("physical_mobile", physical="ios"),
    )
    assert result["status"] == "compliant"
    assert result["findings"] == []


def test_matching_remote_physical_device_is_compliant(monkeypatch):
    result = _evaluate(
        monkeypatch,
        _metadata(run="Webview E2E US Android", device="Android"),
        _detected(
            "physical_mobile",
            physical="android",
            signals=(
                "chrome_inspect",
                "remote_target",
                "attached_device_identifier",
                "screen_mirroring",
                "mobile_status_bar",
            ),
        ),
    )
    assert result["status"] == "compliant"
    assert result["findings"] == []
    assert "mobile_status_bar" in result["signals"]
