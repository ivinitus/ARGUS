"""Bounded Appium/ADB XML parsing and deterministic locator normalization."""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from argus.core import atomic_io

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 80
MAX_NODES = 10_000
MAX_ATTRIBUTES_PER_NODE = 40
MAX_ATTRIBUTES_TOTAL = 50_000
MAX_VALUE_CHARS = 4_096
MAX_PROMPT_CHARS = 24_000

_ANDROID_BOUNDS = re.compile(
    r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$"
)
_IOS_ELEMENT = re.compile(r"^XCUIElementType[A-Za-z0-9_]+$")
_XPATH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_FORBIDDEN_MARKERS = (
    b"<!doctype",
    b"<!entity",
    b"<?xml-stylesheet",
)
_XINCLUDE_NS = "http://www.w3.org/2001/XInclude"
_SCHEMA_INSTANCE_NS = "http://www.w3.org/2001/XMLSchema-instance"


class XmlEvidenceError(ValueError):
    """Raised when XML evidence cannot be safely and completely analyzed."""


def xpath_literal(value: str) -> str:
    """Return an XPath 1.0 string literal for arbitrary text."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts: list[str] = []
    chunks = value.split("'")
    for index, chunk in enumerate(chunks):
        if chunk:
            parts.append(f"'{chunk}'")
        if index != len(chunks) - 1:
            parts.append('"\'"')
    return f"concat({', '.join(parts)})"


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _read_utf8_xml(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise XmlEvidenceError(f"cannot stat XML evidence: {exc}") from exc
    if size <= 0:
        raise XmlEvidenceError("XML evidence is empty")
    if size > MAX_XML_BYTES:
        raise XmlEvidenceError(
            f"XML evidence exceeds {MAX_XML_BYTES} byte limit"
        )
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise XmlEvidenceError("XML evidence must use UTF-8, not UTF-16/32")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise XmlEvidenceError("XML evidence is not valid UTF-8") from exc
    declaration = re.match(r"\s*<\?xml\b([^?]*)\?>", text, re.IGNORECASE)
    if declaration:
        encoding = re.search(
            r"\bencoding\s*=\s*['\"]([^'\"]+)['\"]",
            declaration.group(1),
            re.IGNORECASE,
        )
        if encoding and encoding.group(1).lower().replace("_", "-") not in {
            "utf-8",
            "utf8",
        }:
            raise XmlEvidenceError("XML evidence declares a non-UTF-8 encoding")
    return text.encode("utf-8")


def _parse_bounded(path: Path) -> ET.Element:
    raw = _read_utf8_xml(path)
    lowered = raw.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise XmlEvidenceError("DTD, entity, and stylesheet declarations are forbidden")

    depth = 0
    max_depth = 0
    node_count = 0
    attribute_count = 0
    try:
        parser = ET.iterparse(io.BytesIO(raw), events=("start", "end"))
        root: ET.Element | None = None
        for event, element in parser:
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                max_depth = max(max_depth, depth)
                node_count += 1
                attribute_count += len(element.attrib)
                if max_depth > MAX_DEPTH:
                    raise XmlEvidenceError(
                        f"XML hierarchy exceeds depth limit {MAX_DEPTH}"
                    )
                if node_count > MAX_NODES:
                    raise XmlEvidenceError(
                        f"XML hierarchy exceeds node limit {MAX_NODES}"
                    )
                if len(element.attrib) > MAX_ATTRIBUTES_PER_NODE:
                    raise XmlEvidenceError(
                        "XML node exceeds per-node attribute limit "
                        f"{MAX_ATTRIBUTES_PER_NODE}"
                    )
                if attribute_count > MAX_ATTRIBUTES_TOTAL:
                    raise XmlEvidenceError(
                        f"XML hierarchy exceeds attribute limit {MAX_ATTRIBUTES_TOTAL}"
                    )
                for name, value in element.attrib.items():
                    if len(name) > MAX_VALUE_CHARS or len(value) > MAX_VALUE_CHARS:
                        raise XmlEvidenceError(
                            f"XML attribute exceeds {MAX_VALUE_CHARS} character limit"
                        )
                    local = _local_name(name)
                    if local in {"schemaLocation", "noNamespaceSchemaLocation"}:
                        raise XmlEvidenceError("XML schema references are forbidden")
                if (
                    element.tag == f"{{{_XINCLUDE_NS}}}include"
                    or element.tag == f"{{{_XINCLUDE_NS}}}fallback"
                ):
                    raise XmlEvidenceError("XInclude is forbidden")
            else:
                if element.text and len(element.text) > MAX_VALUE_CHARS:
                    raise XmlEvidenceError(
                        f"XML text exceeds {MAX_VALUE_CHARS} character limit"
                    )
                if element.tail and len(element.tail) > MAX_VALUE_CHARS:
                    raise XmlEvidenceError(
                        f"XML tail text exceeds {MAX_VALUE_CHARS} character limit"
                    )
                depth -= 1
    except XmlEvidenceError:
        raise
    except ET.ParseError as exc:
        raise XmlEvidenceError(f"malformed XML evidence: {exc}") from exc
    if root is None or depth != 0:
        raise XmlEvidenceError("malformed XML hierarchy")
    return root


def _platform(root: ET.Element) -> str:
    root_name = _local_name(root.tag)
    if root_name == "hierarchy":
        return "android"
    if root_name == "AppiumAUT" or _IOS_ELEMENT.fullmatch(root_name):
        return "ios"
    raise XmlEvidenceError(f"unsupported XML root {root_name!r}")


def _elements(root: ET.Element, platform: str) -> list[ET.Element]:
    if platform == "android":
        elements = list(root.iter("node"))
        if any(_local_name(child.tag) != "node" for child in root.iter() if child is not root):
            raise XmlEvidenceError("Android hierarchy contains an unsupported element")
    else:
        elements = [
            element
            for element in root.iter()
            if _IOS_ELEMENT.fullmatch(_local_name(element.tag))
        ]
        unsupported = [
            _local_name(element.tag)
            for element in root.iter()
            if element is not root and not _IOS_ELEMENT.fullmatch(_local_name(element.tag))
        ]
        if unsupported:
            raise XmlEvidenceError(
                f"iOS hierarchy contains unsupported element {unsupported[0]!r}"
            )
    if not elements:
        raise XmlEvidenceError(f"{platform} XML hierarchy contains no UI nodes")
    return elements


def _element_type(element: ET.Element, platform: str) -> str:
    if platform == "android":
        return element.attrib.get("class") or "node"
    return _local_name(element.tag)


def _bounds(element: ET.Element, platform: str) -> dict[str, int] | None:
    if platform == "android":
        match = _ANDROID_BOUNDS.fullmatch(element.attrib.get("bounds", ""))
        if not match:
            return None
        x1, y1, x2, y2 = (int(value) for value in match.groups())
        if x2 < x1 or y2 < y1:
            raise XmlEvidenceError("Android node has invalid bounds")
        return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}
    values = [element.attrib.get(key) for key in ("x", "y", "width", "height")]
    if not all(value is not None for value in values):
        return None
    try:
        x, y, width, height = (int(float(str(value))) for value in values)
    except ValueError as exc:
        raise XmlEvidenceError("iOS node has invalid bounds") from exc
    if width < 0 or height < 0:
        raise XmlEvidenceError("iOS node has invalid bounds")
    return {"x": x, "y": y, "width": width, "height": height}


def _structural_paths(
    root: ET.Element,
    platform: str,
) -> dict[int, str]:
    paths: dict[int, str] = {}

    def visit(element: ET.Element, parent_path: str) -> None:
        children = [
            child
            for child in list(element)
            if platform != "android" or _local_name(child.tag) == "node"
        ]
        type_counts = Counter(_element_type(child, platform) for child in children)
        seen: Counter[str] = Counter()
        for child in children:
            element_type = _element_type(child, platform)
            seen[element_type] += 1
            if platform == "android":
                segment = "node"
                if element_type != "node":
                    segment += f"[@class={xpath_literal(element_type)}]"
            elif _XPATH_NAME.fullmatch(element_type):
                segment = element_type
            else:
                segment = f"*[@class={xpath_literal(element_type)}]"
            if type_counts[element_type] > 1:
                segment += f"[{seen[element_type]}]"
            child_path = f"{parent_path}/{segment}"
            paths[id(child)] = child_path
            visit(child, child_path)

    root_name = _local_name(root.tag)
    if platform == "android":
        root_path = f"/{root_name}"
    else:
        root_type = _element_type(root, platform)
        root_path = f"/{root_name}"
        if _IOS_ELEMENT.fullmatch(root_type):
            paths[id(root)] = root_path
    visit(root, root_path)
    return paths


def _preferred_xpath(
    element: ET.Element,
    platform: str,
    unique: dict[str, Counter[str]],
    structural: str,
) -> str:
    attrs = element.attrib
    element_type = _element_type(element, platform)
    if platform == "android":
        candidates = (
            ("resource-id", attrs.get("resource-id")),
            ("content-desc", attrs.get("content-desc")),
            ("text", attrs.get("text")),
        )
    else:
        candidates = (
            ("name", attrs.get("name")),
            ("label", attrs.get("label")),
            ("value", attrs.get("value")),
        )
    for attr, value in candidates:
        if not value or unique[attr][value] != 1:
            continue
        if attr in {"resource-id", "content-desc", "name"}:
            return f"//*[@{attr}={xpath_literal(value)}]"
        selector = element_type if _XPATH_NAME.fullmatch(element_type) else "*"
        return f"//{selector}[@{attr}={xpath_literal(value)}]"
    return structural


def normalize_xml(
    path: Path,
    *,
    step_index: int | None = None,
    screenshot: str | None = None,
) -> dict[str, Any]:
    """Parse one XML file and return normalized, report-safe locator evidence."""
    root = _parse_bounded(path)
    platform = _platform(root)
    elements = _elements(root, platform)
    unique: dict[str, Counter[str]] = {
        attr: Counter(
            element.attrib[attr]
            for element in elements
            if element.attrib.get(attr)
        )
        for attr in (
            "resource-id",
            "content-desc",
            "text",
            "name",
            "label",
            "value",
        )
    }
    structural = _structural_paths(root, platform)
    nodes: list[dict[str, Any]] = []
    for element in elements:
        attrs = element.attrib
        element_type = _element_type(element, platform)
        fallback = structural.get(id(element))
        if not fallback:
            fallback = (
                f"/{element_type}"
                if _XPATH_NAME.fullmatch(element_type)
                else f"/*[@class={xpath_literal(element_type)}]"
            )
        accessibility_id = (
            attrs.get("content-desc")
            if platform == "android"
            else attrs.get("name")
        )
        nodes.append({
            "platform": platform,
            "element_type": element_type,
            "resource_id": attrs.get("resource-id") or None,
            "accessibility_id": accessibility_id or None,
            "text": attrs.get("text") or None,
            "label": attrs.get("label") or None,
            "value": attrs.get("value") or None,
            "bounds": _bounds(element, platform),
            "step_index": step_index,
            "screenshot": screenshot,
            "xpath": _preferred_xpath(
                element,
                platform,
                unique,
                fallback,
            ),
        })
    return {
        "source": path.name,
        "platform": platform,
        "step_index": step_index,
        "screenshot": screenshot,
        "nodes": nodes,
    }


def validate_xml_file(path: Path) -> None:
    """Validate an XML file fully without retaining normalized output."""
    normalize_xml(path)


def pair_screenshot(
    *,
    xml_name: str,
    step_index: int | None,
    screenshots: list[dict[str, Any]],
) -> str | None:
    """Pair XML to a screenshot by step first, then original filename stem."""
    candidates = [
        row
        for row in screenshots
        if row.get("step_index") == step_index and isinstance(row.get("path"), str)
    ]
    xml_stem = Path(xml_name).stem.lower()
    stem_matches = [
        row
        for row in candidates
        if xml_stem
        and (
            xml_stem in Path(str(row.get("original_name") or "")).stem.lower()
            or xml_stem in Path(row["path"]).stem.lower()
        )
    ]
    if len(stem_matches) == 1:
        return stem_matches[0]["path"]
    if len(candidates) == 1:
        return candidates[0]["path"]
    return None


def write_normalized_evidence(
    execution_dir: Path,
    xml_records: list[dict[str, Any]],
    screenshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize downloaded XML files and persist a bounded evidence document."""
    files: list[dict[str, Any]] = []
    for record in xml_records:
        path = Path(record["path"])
        screenshot = pair_screenshot(
            xml_name=str(record.get("name") or path.name),
            step_index=record.get("step_index"),
            screenshots=screenshots,
        )
        normalized = normalize_xml(
            path,
            step_index=record.get("step_index"),
            screenshot=screenshot,
        )
        normalized["source"] = path.relative_to(execution_dir).as_posix()
        files.append(normalized)
    payload = {
        "schema": 1,
        "files": files,
        "nodes": [node for file in files for node in file["nodes"]],
    }
    atomic_io.atomic_write_text(
        execution_dir / "xml_evidence.json",
        json.dumps(payload, indent=2, sort_keys=True),
    )
    return payload


def prompt_context(execution_dir: Path) -> str:
    """Return bounded normalized hierarchy text for guarded model input."""
    path = execution_dir / "xml_evidence.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise XmlEvidenceError(f"normalized XML evidence is unreadable: {exc}") from exc
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise XmlEvidenceError("normalized XML evidence has invalid nodes")
    text = json.dumps(
        {"schema": payload.get("schema"), "nodes": nodes},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "...[bounded]"
    return text
