"""R19: deterministic device-family mismatch detection."""
from __future__ import annotations

import base64
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any


from argus.core import claude
from argus.core.costs import UsageTracker
from argus.domain import execution_policy
from argus.pipeline import model_client, env_check

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

log = logging.getLogger("argus.device_check")

DEFAULT_MODEL_ID = "claude-haiku-5-5"


class DeviceCheckUnavailable(RuntimeError):
    """The device classifier could not run (model or transport failure)."""
DEFAULT_MODEL_API_URL = model_client.DEFAULT_MODEL_API_URL

DEVICE_CHECK_VERSION = 5

_MAX_DEVICE_SAMPLES = 4

SYSTEM_PROMPT = """You classify observable execution-mode evidence in a QA screenshot.

Report facts only. Do not decide whether the test used the correct device.
Return ONE JSON object and nothing else:

{
  "execution_mode": "<devtools_emulation | physical_mobile | desktop_browser | unknown>",
  "physical_os_family": "<android | ios | unknown>",
  "emulated_profile_family": "<android | ios | unknown>",
  "device_string": "<exact visible physical model or emulator api_key_env, or null>",
  "signals": ["<zero or more of: devtools_panel, responsive_device_toolbar, emulator_profile, chrome_inspect, remote_target, attached_device_identifier, screen_mirroring, mobile_status_bar, mobile_navigation_bar, android_system_ui, ios_system_ui, device_settings, desktop_taskbar, desktop_browser_content>"],
  "confidence": "<high | low>"
}

Rules:
- A generic DevTools panel is NOT proof of emulation. Use devtools_emulation
  only when a responsive-device toolbar, synthetic viewport controls, or a
  named emulated api_key_env such as Samsung Galaxy S8+ or iPhone 12 is visible.
- chrome://inspect with a Remote Target, an attached-device identifier/serial,
  or a mirrored phone is remote debugging of a device, not responsive
  DevTools emulation. Classify it as physical_mobile when physical evidence is
  visible, even if a DevTools window is also open.
- A mobile status bar may show battery level, Wi-Fi/cellular state, clock/date,
  notifications, and other phone indicators. Emit mobile_status_bar only when
  that bar is inside an identifiable phone/mirrored-device viewport. Windows
  and macOS taskbars may show similar battery, network, and clock indicators;
  emit desktop_taskbar for those and NEVER use them as physical-mobile proof.
- physical_mobile requires contextual evidence such as screen mirroring,
  attached-device identity, device Settings, a mobile status/navigation bar,
  or unambiguous Android/iOS system UI. App UI alone is insufficient.
- A desktop image viewer displaying a phone screenshot is physical_mobile
  only when the inner screenshot has unambiguous OS evidence.
- Use unknown and low confidence when evidence is ambiguous. Never guess.
"""


_PHYSICAL_SIGNALS = {
    "attached_device_identifier",
    "screen_mirroring",
    "mobile_status_bar",
    "mobile_navigation_bar",
    "android_system_ui",
    "ios_system_ui",
    "device_settings",
}
_EMULATION_SIGNALS = {
    "responsive_device_toolbar",
    "emulator_profile",
}


def _normalise_signals(raw_signals: Any) -> list[str]:
    return sorted({
        str(signal).strip().lower()
        for signal in (raw_signals or [])
        if isinstance(signal, str) and signal.strip()
    })


def _has_physical_evidence(signals: set[str]) -> bool:
    return bool(
        signals & _PHYSICAL_SIGNALS
        or {"chrome_inspect", "remote_target"} <= signals
    )


def _has_emulation_evidence(signals: set[str]) -> bool:
    return bool(signals & _EMULATION_SIGNALS)


def _resolve_observed_mode(
    mode: str,
    signals: set[str],
    confidence: str,
) -> tuple[str, str]:
    if _has_physical_evidence(signals):
        return "physical_mobile", confidence
    if _has_emulation_evidence(signals):
        return "devtools_emulation", confidence
    if (
        mode == "devtools_emulation"
        or mode == "physical_mobile"
        or (mode == "desktop_browser" and "devtools_panel" in signals)
    ):
        return "unknown", "low"
    return mode, confidence


def _get_client(model_api_url: str, api_key_env: str | None):
    return model_client.create_client(
        model_api_url=model_api_url, api_key_env=api_key_env, timeout=60,
    )


def _image_to_base64_jpeg(img: "Image.Image", quality: int = 85) -> str:
    buf = BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _normalise_expected_device(expected: str | None) -> str | None:
    if not expected:
        return None
    e = expected.strip().lower()
    if "ios" in e or "iphone" in e or "ipad" in e:
        return "ios"
    if "android" in e or "samsung" in e or "pixel" in e:
        return "android"
    if "desktop" in e or "web" in e:
        return "desktop"
    return None


def _classify_one_image(img: "Image.Image", model_id: str, model_api_url: str,
                        api_key_env: str | None,
                        usage_tracker: UsageTracker | None = None,
                        ) -> dict[str, Any] | None:
    """Ask Haiku for the device family of one screenshot. None on error."""
    if Image is None:  # pragma: no cover
        return None
    try:
        b64 = _image_to_base64_jpeg(img)
    except Exception as e:  # pragma: no cover
        log.warning("device_check: image encode failed: %s: %s",
                    type(e).__name__, e)
        return None
    body = {

        "max_tokens": 256,
        "temperature": 0.0,
        "system": [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "Identify the device."},
            ],
        }],
    }
    try:
        client = _get_client(model_api_url, api_key_env)
        resp = model_client.invoke_model(
            client,
            model_id=model_id,
            body=json.dumps(claude.prepare_request(body, model_id=model_id)),
            content_type="application/json",
            accept="application/json",
            allow_untagged_multimodal=True,
        )
        raw = json.loads(resp["body"].read())
        if usage_tracker is not None:
            usage_tracker.record_response(
                raw, model_id=model_id, operation="device_check")
    except Exception as e:
        # A model outage is not evidence about the device. Raising lets
        # env_check_merge mark the environment module incomplete instead of
        # emitting an R19 advisory that blames the screenshots.
        log.warning("device_check: invoke failed: %s: %s",
                    type(e).__name__, e)
        raise DeviceCheckUnavailable(f"{type(e).__name__}: {e}") from e
    text = "".join(b.get("text", "") for b in (raw.get("content") or [])
                   if b.get("type") == "text").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(text[first:last + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(parsed, dict):
        return None
    mode = str(parsed.get("execution_mode") or "unknown").strip().lower()
    if mode not in {
        "devtools_emulation", "physical_mobile", "desktop_browser", "unknown",
    }:
        mode = "unknown"
    physical = str(
        parsed.get("physical_os_family") or "unknown"
    ).strip().lower()
    profile_family = str(
        parsed.get("emulated_profile_family") or "unknown"
    ).strip().lower()
    if physical not in {"android", "ios", "unknown"}:
        physical = "unknown"
    if profile_family not in {"android", "ios", "unknown"}:
        profile_family = "unknown"
    signals = _normalise_signals(parsed.get("signals"))
    signal_set = set(signals)
    confidence = str(
        parsed.get("confidence") or "low"
    ).strip().lower()
    if confidence not in {"high", "low"}:
        confidence = "low"

    # Trust observable signals over a coarse mode label. Remote inspection of
    # a phone commonly shows DevTools and a mirrored device in one screenshot.
    mode, confidence = _resolve_observed_mode(
        mode, signal_set, confidence)

    legacy_family = (
        physical if mode == "physical_mobile"
        else "desktop" if mode in {"devtools_emulation", "desktop_browser"}
        else "unknown"
    )
    return {
        "device_string": parsed.get("device_string"),
        "family": legacy_family,
        "execution_mode": mode,
        "physical_os_family": physical,
        "emulated_profile_family": profile_family,
        "signals": signals,
        "confidence": confidence,
    }


def detect_device_family(
    execution_dir: Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    model_api_url: str = DEFAULT_MODEL_API_URL,
    api_key_env: str | None = None,
    max_samples: int = _MAX_DEVICE_SAMPLES,
    usage_tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    """Detect the device family from an execution's screenshots."""
    out: dict[str, Any] = {
        "family": "unknown", "device_string": None,
        "execution_mode": "unknown", "physical_os_family": "unknown",
        "emulated_profile_family": "unknown", "signals": [],
        "confidence": "low", "images_checked": 0, "observations": [],
    }
    try:
        all_images = list(env_check._iter_audit_images(execution_dir))
    except Exception as e:
        log.debug("device_check image enumeration failed for %s: %s: %s",
                  execution_dir, type(e).__name__, e)
        return out
    if not all_images:
        return out
    step = max(1, len(all_images) // max_samples)
    sampled = all_images[::step][:max_samples]
    checked = 0
    observations: list[dict[str, Any]] = []
    for path in sampled:
        if Image is None:
            break
        try:
            img = Image.open(path)
        except (OSError, ValueError) as e:
            log.debug("device_check could not open %s: %s: %s",
                      path, type(e).__name__, e)
            continue
        result = _classify_one_image(
            img, model_id, model_api_url, api_key_env, usage_tracker)
        checked += 1
        if not result:
            continue
        observations.append({
            "image": str(path.relative_to(execution_dir)),
            **result,
        })

    high = [o for o in observations if o.get("confidence") == "high"]
    physical = [
        o for o in high if o.get("execution_mode") == "physical_mobile"
    ]
    emulated = [
        o for o in high if o.get("execution_mode") == "devtools_emulation"
    ]
    concrete = [
        o for o in high if o.get("execution_mode") != "unknown"
    ]
    combined_signals = sorted({
        str(signal)
        for observation in observations
        for signal in (observation.get("signals") or [])
    })

    # Physical evidence wins over incidental desktop/DevTools observations.
    # Explicit physical and explicit emulation evidence in the same execution
    # remains ambiguous rather than producing a false mismatch.
    selected: dict[str, Any] | None = None
    if physical and emulated:
        out["signals"] = combined_signals
    elif physical:
        selected = physical[0]
    elif emulated:
        selected = emulated[0]
    elif concrete:
        modes = {o.get("execution_mode") for o in concrete}
        if len(modes) == 1:
            selected = concrete[0]

    if selected is not None:
        out.update({
            key: selected.get(key)
            for key in (
                "family", "device_string", "execution_mode",
                "physical_os_family", "emulated_profile_family",
                "signals", "confidence",
            )
        })
        relevant = (
            physical
            if out["execution_mode"] == "physical_mobile"
            else emulated
            if out["execution_mode"] == "devtools_emulation"
            else concrete
        )
        for key in ("physical_os_family", "emulated_profile_family"):
            families = {
                observation.get(key) for observation in relevant
                if observation.get(key) != "unknown"
            }
            out[key] = next(iter(families)) if len(families) == 1 else "unknown"
        out["signals"] = combined_signals
        mode = out["execution_mode"]
        out["family"] = (
            out["physical_os_family"]
            if mode == "physical_mobile"
            else "desktop"
            if mode in {"devtools_emulation", "desktop_browser"}
            else "unknown"
        )
    elif observations and not high:
        out.update({
            key: observations[0].get(key)
            for key in (
                "family", "device_string", "execution_mode",
                "physical_os_family", "emulated_profile_family",
                "signals", "confidence",
            )
        })
        out["signals"] = combined_signals
    out["images_checked"] = checked
    out["observations"] = observations
    return out


def _device_finding(
    *,
    severity: str,
    rule: str,
    description: str,
    advisory: bool,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "page": None,
        "step_index": None,
        "description": description,
        "source": "device_check",
        "rule": rule,
        "category": (
            "insufficient_evidence" if advisory else "tester_error"
        ),
        "action": "add_evidence" if advisory else "re_run",
        "confidence": "low" if advisory else "high",
        "advisory": advisory,
    }


def evaluate_device_compliance(
    metadata: dict[str, Any],
    execution_dir: Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    model_api_url: str = DEFAULT_MODEL_API_URL,
    api_key_env: str | None = None,
    usage_tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    """Evaluate observed device facts against the declared execution mode."""
    tr = (metadata.get("test_results") or [{}])
    tr = tr[0] if tr else {}
    expected_mode = execution_policy.expected_execution_mode(
        metadata, execution_dir)
    expected_family = _normalise_expected_device(tr.get("expected_device"))
    detected = detect_device_family(
        execution_dir,
        model_id=model_id,
        model_api_url=model_api_url,
        api_key_env=api_key_env,
        usage_tracker=usage_tracker,
    )
    observed_mode = detected.get("execution_mode") or "unknown"
    confidence = detected.get("confidence") or "low"
    physical_family = detected.get("physical_os_family") or "unknown"
    profile_family = detected.get("emulated_profile_family") or "unknown"
    observed_family = (
        physical_family
        if observed_mode == "physical_mobile"
        else profile_family
        if observed_mode == "devtools_emulation"
        else "desktop"
        if observed_mode == "desktop_browser"
        else "unknown"
    )

    result: dict[str, Any] = {
        "version": DEVICE_CHECK_VERSION,
        "expected_mode": expected_mode,
        "expected_family": expected_family or "unknown",
        "status": "compliant",
        "reason": "",
        "observed_mode": observed_mode,
        "observed_family": observed_family,
        "confidence": confidence,
        "images_checked": detected.get("images_checked", 0),
        "signals": detected.get("signals") or [],
        "observations": detected.get("observations") or [],
        "findings": [],
    }

    def unverified(reason: str) -> dict[str, Any]:
        result["status"] = "unverified"
        result["reason"] = reason
        result["findings"] = [_device_finding(
            severity="low",
            rule="R19-amb",
            advisory=True,
            description=(
                f"Device/environment evidence is inconclusive: {reason} "
                "This is an evidence advisory only and does not make the "
                "execution non-compliant."
            ),
        )]
        return result

    def mismatch(reason: str) -> dict[str, Any]:
        result["status"] = "mismatch"
        result["reason"] = reason
        result["findings"] = [_device_finding(
            severity="high",
            rule="R19",
            advisory=False,
            description=(
                f"Device execution mismatch: {reason} Re-run using the "
                "execution mode and OS family assigned by the run."
            ),
        )]
        return result

    if expected_mode == execution_policy.UNKNOWN:
        return unverified(
            "the run metadata does not declare Desktop, Mobile, Webview, "
            "or another supported execution mode."
        )
    if confidence != "high" or observed_mode == "unknown":
        return unverified(
            f"the expected mode is {expected_mode}, but screenshots do not "
            "provide consistent high-confidence mode evidence."
        )

    if expected_mode == execution_policy.MOBILE_EMULATION_ALLOWED:
        if observed_mode == "desktop_browser":
            return unverified(
                "the run permits DevTools mobile emulation, but no emulated "
                "Android/iOS api_key_env is clearly visible."
            )
        if observed_mode not in {"devtools_emulation", "physical_mobile"}:
            return unverified(f"the observed mode is {observed_mode}.")
        if (
            expected_family in {"android", "ios"}
            and observed_family in {"android", "ios"}
            and observed_family != expected_family
        ):
            return mismatch(
                f"the run is assigned to {expected_family}, while the proven "
                f"{observed_mode} api_key_env/device is {observed_family}."
            )
        if (
            expected_family in {"android", "ios"}
            and observed_family == "unknown"
        ):
            return unverified(
                f"the run is assigned to {expected_family}, but the "
                f"{observed_mode} OS family cannot be verified."
            )
        result["reason"] = (
            f"{observed_mode} is permitted for an ordinary Mobile run"
            + (
                f" and the observed family is {observed_family}."
                if observed_family != "unknown" else "."
            )
        )
        return result

    if expected_mode == execution_policy.PHYSICAL_DEVICE:
        if observed_mode in {"devtools_emulation", "desktop_browser"}:
            return mismatch(
                f"this {expected_mode} run was proven to use {observed_mode}."
            )
        if observed_mode != "physical_mobile":
            return unverified(
                "a physical device is required, but physical-device use is "
                "not proven."
            )
        if (
            expected_family in {"android", "ios"}
            and physical_family in {"android", "ios"}
            and physical_family != expected_family
        ):
            return mismatch(
                f"the run is assigned to {expected_family}, while the proven "
                f"physical device is {physical_family}."
            )
        if (
            expected_family in {"android", "ios"}
            and physical_family == "unknown"
        ):
            return unverified(
                "a physical device is proven, but its OS family cannot be "
                f"verified against assigned {expected_family}."
            )
        result["reason"] = (
            "physical-device execution is proven"
            + (
                f" on {physical_family}."
                if physical_family != "unknown" else "."
            )
        )
        return result

    if expected_mode == execution_policy.DESKTOP_BROWSER:
        if observed_mode == "physical_mobile":
            return mismatch(
                "this Desktop run was proven to use a physical mobile device."
            )
        if observed_mode == "devtools_emulation":
            return mismatch(
                "this Desktop run was proven to use a mobile DevTools api_key_env."
            )
        if observed_mode != "desktop_browser":
            return unverified(
                "desktop-browser execution cannot be verified."
            )
        result["reason"] = "desktop-browser execution is proven."
        return result

    return unverified("the execution mode could not be evaluated.")


def check_device_mismatch(
    metadata: dict[str, Any],
    execution_dir: Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    model_api_url: str = DEFAULT_MODEL_API_URL,
    api_key_env: str | None = None,
    usage_tracker: UsageTracker | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning R19/R19-amb findings only."""
    return evaluate_device_compliance(
        metadata,
        execution_dir,
        model_id=model_id,
        model_api_url=model_api_url,
        api_key_env=api_key_env,
        usage_tracker=usage_tracker,
    )["findings"]
