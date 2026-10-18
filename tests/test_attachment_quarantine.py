from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from argus.config.models import ArgusConfig
from argus.pipeline import attachment_quarantine, extractor


@pytest.fixture(autouse=True)
def reset_quarantine():
    attachment_quarantine.configure(ArgusConfig())
    yield
    attachment_quarantine.configure(ArgusConfig())




def _png() -> bytes:
    payload = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(payload, format="PNG")
    return payload.getvalue()


def _jpeg() -> bytes:
    payload = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(payload, format="JPEG")
    return payload.getvalue()


def _pdf() -> bytes:
    return b"%PDF-1.7\n%ARGUS\n"


def _xml() -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<hierarchy><node class="android.widget.Button" '
        b'resource-id="com.example:id/continue" bounds="[0,0][10,10]"/>'
        b"</hierarchy>"
    )


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomARGUS"


def _downloader(payload: bytes, content_type: str = "image/png"):
    paths: list[Path] = []

    def download(
        _session,
        _base_url,
        _attachment_id,
        destination,
        *,
        max_bytes=None,
    ):
        assert max_bytes is None or len(payload) <= max_bytes
        paths.append(destination)
        destination.write_bytes(payload)
        return content_type

    return download, paths


def test_local_mode_uses_direct_download(tmp_path):
    downloader, _ = _downloader(_png())
    destination = tmp_path / "image.png"
    attachment_quarantine.download_and_release(
        session=object(),
        base_url="https://jira.example",
        attachment_id=1,
        attachment_name="image.png",
        execution_key="QA-E1",
        destination=destination,
        downloader=downloader,
    )
    assert destination.read_bytes() == _png()




















@pytest.mark.parametrize(
    "name",
    [
        "../hierarchy.xml",
        "nested/hierarchy.xml",
        r"nested\hierarchy.xml",
        "hierarchy..xml",
        "hierarchy\u202e.xml",
        "Customer hierarchy: final (日本語) [1].xml",
    ],
)
def test_display_filename_is_never_used_as_destination_path(tmp_path, name):
    downloader, _ = _downloader(_xml(), "application/xml")
    destination = tmp_path / "attachment_28.xml"

    attachment_quarantine.download_and_release(
        session=object(),
        base_url="https://jira.example",
        attachment_id=28,
        attachment_name=name,
        execution_key="QA-E28",
        destination=destination,
        downloader=downloader,
    )

    assert destination.read_bytes() == _xml()
    assert [path.name for path in tmp_path.iterdir()] == ["attachment_28.xml"]


def test_dangerous_double_extension_is_rejected(tmp_path):
    downloader = mock.MagicMock()

    with pytest.raises(
        attachment_quarantine.QuarantineError,
        match="dangerous double extension",
    ):
        attachment_quarantine.download_and_release(
            session=object(),
            base_url="https://jira.example",
            attachment_id=28,
            attachment_name="payload.exe.xml",
            execution_key="QA-E28",
            destination=tmp_path / "attachment_28.xml",
            downloader=downloader,
        )

    downloader.assert_not_called()


def test_user_controlled_destination_filename_is_rejected(tmp_path):
    downloader = mock.MagicMock()

    with pytest.raises(
        attachment_quarantine.QuarantineError,
        match="destination filename is not safe",
    ):
        attachment_quarantine.download_and_release(
            session=object(),
            base_url="https://jira.example",
            attachment_id=28,
            attachment_name="evidence.xml",
            execution_key="QA-E28",
            destination=tmp_path / "顧客 evidence.xml",
            downloader=downloader,
        )

    downloader.assert_not_called()






def test_jpeg_content_with_png_filename_is_normalized(tmp_path):
    downloader, _ = _downloader(_jpeg(), "image/jpeg")
    destination = tmp_path / "attachment_3.png"

    attachment_quarantine.download_and_release(
        session=object(),
        base_url="https://jira.example",
        attachment_id=3,
        attachment_name="image.png",
        execution_key="QA-E3",
        destination=destination,
        downloader=downloader,
    )

    assert destination.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(destination) as image:
        assert image.format == "PNG"






def test_image_normalization_rejects_decoded_pixel_overflow(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(_png())
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (attachment_quarantine._IMAGE_MAX_PIXELS + 1, 1)

    with (
        mock.patch("PIL.Image.open", return_value=image),
        pytest.raises(
            attachment_quarantine.QuarantineError,
            match="decoded image pixel limit",
        ),
    ):
        attachment_quarantine._normalize_image_format(
            source,
            ".png",
            5 * 1024 * 1024,
        )


def test_jpeg_normalization_preserves_screenshot_detail(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(_jpeg())
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (4, 4)
    image.mode = "RGB"

    with mock.patch("PIL.Image.open", return_value=image):
        attachment_quarantine._normalize_image_format(
            source,
            ".jpg",
            5 * 1024 * 1024,
        )

    image.save.assert_called_once()
    assert image.save.call_args.kwargs == {
        "format": "JPEG",
        "quality": 95,
        "subsampling": 0,
        "optimize": True,
    }


def test_image_normalization_allows_bounded_reencode_headroom(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(_jpeg())
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (4, 4)
    image.mode = "RGB"
    expanded_size = attachment_quarantine._IMAGE_MAX_BYTES + 1

    def save_expanded(path, **_kwargs):
        path.write_bytes(b"x" * expanded_size)

    image.save.side_effect = save_expanded
    with mock.patch("PIL.Image.open", return_value=image):
        attachment_quarantine._normalize_image_format(
            source,
            ".jpg",
            attachment_quarantine._IMAGE_MAX_BYTES,
        )

    assert source.stat().st_size == expanded_size
    assert expanded_size < attachment_quarantine._NORMALIZED_IMAGE_MAX_BYTES


def test_allowed_image_mime_mismatch_uses_verified_content(tmp_path):
    downloader, _ = _downloader(_png(), "image/jpeg")
    destination = tmp_path / "attachment_3.png"

    attachment_quarantine.download_and_release(
        session=object(),
        base_url="https://jira.example",
        attachment_id=3,
        attachment_name="image.png",
        execution_key="QA-E3",
        destination=destination,
        downloader=downloader,
    )

    assert destination.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(destination) as image:
        assert image.format == "PNG"






def test_download_attachment_enforces_streaming_byte_limit(tmp_path):
    response = mock.MagicMock()
    response.status_code = 200
    response.headers = {"Content-Type": "image/png"}
    response.iter_content.return_value = [b"1234", b"5678"]
    response.__enter__.return_value = response
    session = mock.MagicMock()
    session.get.return_value = response
    destination = tmp_path / "large.png"

    with pytest.raises(extractor.AttachmentTooLargeError):
        extractor.download_attachment(
            session,
            "https://jira.example",
            9,
            destination,
            max_bytes=5,
        )
    assert not destination.exists()












@pytest.mark.parametrize("name,mime,payload", [
    ("capture.webm", "video/webm", b"\x1a\x45\xdf\xa3valid-webm"),
    ("capture.mp4", "video/mp4", _mp4()),
    ("capture.pdf", "application/octet-stream", _pdf()),
    ("hierarchy.xml", "application/xml", _xml()),
])
def test_validated_binary_evidence_is_preserved(tmp_path, name, mime, payload):
    download, temporary_paths = _downloader(payload, mime)
    destination = tmp_path / name
    released = attachment_quarantine.download_and_release(
        session=object(), base_url="https://jira.example.com", attachment_id=1,
        attachment_name=name, execution_key="QA-E1", destination=destination,
        downloader=download,
    )
    assert released.path == destination
    assert destination.read_bytes() == payload
    assert all(not path.exists() for path in temporary_paths)


@pytest.mark.parametrize("name,mime,payload", [
    ("image.exe", "application/octet-stream", b"MZ"),
    ("image.png", "image/png", b"not-an-image"),
    ("image.png", "text/html", _png()),
    ("capture.webm", "video/webm", b"not-a-video"),
    ("capture.mp4", "video/mp4", b"not-a-video"),
    ("hierarchy.xml", "application/xml", b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><x>&e;</x>"),
])
def test_invalid_content_is_never_released(tmp_path, name, mime, payload):
    download, temporary_paths = _downloader(payload, mime)
    destination = tmp_path / name
    with pytest.raises(attachment_quarantine.QuarantineError):
        attachment_quarantine.download_and_release(
            session=object(), base_url="https://jira.example.com", attachment_id=1,
            attachment_name=name, execution_key="QA-E1", destination=destination,
            downloader=download,
        )
    assert not destination.exists()
    assert all(not path.exists() for path in temporary_paths)


def test_byte_limit_is_enforced_even_for_custom_downloader(tmp_path, monkeypatch):
    monkeypatch.setitem(attachment_quarantine._TYPES, ".pdf", (frozenset({"application/pdf"}), 8))
    def download(_session, _base, _id, path, **kwargs):
        path.write_bytes(b"%PDF-1.7\n" + b"x" * 20)
        return "application/pdf"
    destination = tmp_path / "evidence.pdf"
    with pytest.raises(extractor.AttachmentTooLargeError):
        attachment_quarantine.download_and_release(
            session=object(), base_url="https://jira.example.com", attachment_id=1,
            attachment_name="evidence.pdf", execution_key="QA-E1", destination=destination,
            downloader=download,
        )
    assert not destination.exists()


def test_batch_returns_only_validated_attachments(tmp_path):
    download, _ = _downloader(_png())
    batch = attachment_quarantine.QuarantineBatch(session=object(), base_url="https://jira.example.com", execution_key="QA-E1", downloader=download)
    ticket = batch.add(attachment_id=1, attachment_name="image.png", destination=tmp_path / "image.png")
    batch.release()
    assert ticket.result().path.read_bytes() == _png()
