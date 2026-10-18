from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.pipeline import xml_evidence


ANDROID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">
    <node class="android.widget.Button" resource-id="com.example:id/submit"
          content-desc="Submit" text="Continue" bounds="[10,20][110,70]"/>
    <node class="android.widget.Button" text="Repeated" bounds="[10,80][110,130]"/>
    <node class="android.widget.Button" text="Repeated" bounds="[10,140][110,190]"/>
  </node>
</hierarchy>
"""

IOS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication"
 name="Example Storefront" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0"
   width="390" height="844">
    <XCUIElementTypeButton name="continue-button" label="Continue"
     value="ready" x="20" y="100" width="120" height="44"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
"""


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_android_normalization_prefers_unique_identifier(tmp_path: Path):
    result = xml_evidence.normalize_xml(_write(tmp_path / "view.xml", ANDROID_XML))

    assert result["platform"] == "android"
    [identified] = [
        node for node in result["nodes"]
        if node["resource_id"] == "com.example:id/submit"
    ]
    assert identified["xpath"] == "//*[@resource-id='com.example:id/submit']"
    assert identified["accessibility_id"] == "Submit"
    assert identified["bounds"] == {
        "x": 10,
        "y": 20,
        "width": 100,
        "height": 50,
    }

    repeated = [node for node in result["nodes"] if node["text"] == "Repeated"]
    assert len(repeated) == 2
    assert repeated[0]["xpath"] != repeated[1]["xpath"]
    assert repeated[0]["xpath"].endswith(
        "node[@class='android.widget.Button'][2]"
    )
    assert repeated[1]["xpath"].endswith(
        "node[@class='android.widget.Button'][3]"
    )


def test_ios_normalization_prefers_accessibility_name(tmp_path: Path):
    result = xml_evidence.normalize_xml(_write(tmp_path / "ios.xml", IOS_XML))

    assert result["platform"] == "ios"
    [button] = [
        node for node in result["nodes"]
        if node["element_type"] == "XCUIElementTypeButton"
    ]
    assert button["accessibility_id"] == "continue-button"
    assert button["xpath"] == "//*[@name='continue-button']"
    assert button["label"] == "Continue"
    assert button["bounds"]["height"] == 44


def test_ios_appium_wrapper_is_preserved_in_structural_xpath(tmp_path: Path):
    payload = b"""<AppiumAUT>
      <XCUIElementTypeApplication>
        <XCUIElementTypeWindow>
          <XCUIElementTypeButton/>
          <XCUIElementTypeButton/>
        </XCUIElementTypeWindow>
      </XCUIElementTypeApplication>
    </AppiumAUT>"""

    result = xml_evidence.normalize_xml(
        _write(tmp_path / "appium-ios.xml", payload)
    )

    buttons = [
        node for node in result["nodes"]
        if node["element_type"] == "XCUIElementTypeButton"
    ]
    assert [node["xpath"] for node in buttons] == [
        "/AppiumAUT/XCUIElementTypeApplication/XCUIElementTypeWindow/"
        "XCUIElementTypeButton[1]",
        "/AppiumAUT/XCUIElementTypeApplication/XCUIElementTypeWindow/"
        "XCUIElementTypeButton[2]",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("simple", "'simple'"),
        ("has'quote", '"has\'quote"'),
        ('has"quote', """'has"quote'"""),
        ('both\'and"', """concat('both', "'", 'and"')"""),
    ],
)
def test_xpath_literal_escaping(value: str, expected: str):
    assert xml_evidence.xpath_literal(value) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE hierarchy [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<hierarchy><node text=\"&xxe;\"/></hierarchy>",
        b'<!DOCTYPE lolz [<!ENTITY a "123"><!ENTITY b "&a;&a;">]>'
        b"<hierarchy><node text=\"&b;\"/></hierarchy>",
        b'<hierarchy xmlns:xi="http://www.w3.org/2001/XInclude">'
        b'<xi:include href="file:///etc/passwd"/></hierarchy>',
        b'<hierarchy xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        b'xsi:noNamespaceSchemaLocation="https://example.test/schema.xsd">'
        b"<node/></hierarchy>",
        b'<hierarchy schemaLocation="https://example.test/schema.xsd">'
        b"<node/></hierarchy>",
        b"<unsupported><node/></unsupported>",
        b"<hierarchy><button/></hierarchy>",
        b"<hierarchy><node></hierarchy>",
    ],
)
def test_rejects_unsafe_or_unsupported_xml(tmp_path: Path, payload: bytes):
    with pytest.raises(xml_evidence.XmlEvidenceError):
        xml_evidence.normalize_xml(_write(tmp_path / "unsafe.xml", payload))


def test_rejects_depth_node_attribute_and_value_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(xml_evidence, "MAX_DEPTH", 3)
    deep = b"<hierarchy><node><node><node/></node></node></hierarchy>"
    with pytest.raises(xml_evidence.XmlEvidenceError, match="depth"):
        xml_evidence.normalize_xml(_write(tmp_path / "deep.xml", deep))

    monkeypatch.setattr(xml_evidence, "MAX_DEPTH", 80)
    monkeypatch.setattr(xml_evidence, "MAX_NODES", 2)
    with pytest.raises(xml_evidence.XmlEvidenceError, match="node limit"):
        xml_evidence.normalize_xml(_write(tmp_path / "nodes.xml", ANDROID_XML))

    monkeypatch.setattr(xml_evidence, "MAX_NODES", 10_000)
    monkeypatch.setattr(xml_evidence, "MAX_ATTRIBUTES_PER_NODE", 1)
    with pytest.raises(xml_evidence.XmlEvidenceError, match="attribute"):
        xml_evidence.normalize_xml(_write(tmp_path / "attrs.xml", ANDROID_XML))

    monkeypatch.setattr(xml_evidence, "MAX_ATTRIBUTES_PER_NODE", 40)
    monkeypatch.setattr(xml_evidence, "MAX_VALUE_CHARS", 3)
    with pytest.raises(xml_evidence.XmlEvidenceError, match="character"):
        xml_evidence.normalize_xml(_write(tmp_path / "value.xml", ANDROID_XML))


def test_rejects_size_and_non_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(xml_evidence, "MAX_XML_BYTES", 20)
    with pytest.raises(xml_evidence.XmlEvidenceError, match="byte limit"):
        xml_evidence.normalize_xml(_write(tmp_path / "large.xml", ANDROID_XML))

    monkeypatch.setattr(xml_evidence, "MAX_XML_BYTES", 2 * 1024 * 1024)
    with pytest.raises(xml_evidence.XmlEvidenceError, match="UTF-8"):
        xml_evidence.normalize_xml(
            _write(tmp_path / "utf16.xml", "<hierarchy/>".encode("utf-16"))
        )


def test_pairing_prefers_step_and_filename_stem():
    screenshots = [
        {
            "step_index": 3,
            "original_name": "checkout.png",
            "path": "screenshots/step_attachments/step_003_1_checkout.png",
        },
        {
            "step_index": 3,
            "original_name": "other.png",
            "path": "screenshots/step_attachments/step_003_2_other.png",
        },
    ]
    assert xml_evidence.pair_screenshot(
        xml_name="checkout.xml",
        step_index=3,
        screenshots=screenshots,
    ) == "screenshots/step_attachments/step_003_1_checkout.png"
    assert xml_evidence.pair_screenshot(
        xml_name="checkout.xml",
        step_index=4,
        screenshots=screenshots,
    ) is None


def test_persists_flat_normalized_nodes(tmp_path: Path):
    xml_path = tmp_path / "xml_evidence" / "step_003_1_checkout.xml"
    xml_path.parent.mkdir()
    xml_path.write_bytes(ANDROID_XML)
    screenshot = "screenshots/step_attachments/step_003_2_checkout.png"
    payload = xml_evidence.write_normalized_evidence(
        tmp_path,
        [{
            "path": str(xml_path),
            "name": "checkout.xml",
            "step_index": 3,
        }],
        [{
            "path": screenshot,
            "original_name": "checkout.png",
            "step_index": 3,
        }],
    )

    stored = json.loads((tmp_path / "xml_evidence.json").read_text())
    assert stored == payload
    assert payload["files"][0]["screenshot"] == screenshot
    assert all(node["step_index"] == 3 for node in payload["nodes"])
    assert all(node["screenshot"] == screenshot for node in payload["nodes"])
