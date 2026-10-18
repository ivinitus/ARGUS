"""Fail-closed local type, size, and content validation for Jira attachments.

This validation does not provide an antivirus verdict. Unsupported, malformed,
or oversized attachments are rejected before downstream evidence processing.
"""
from __future__ import annotations

import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from argus.pipeline.errors import AttachmentTooLargeError, AuthError

# Download and decoded-content limits apply before evidence is released.
_PDF_MAX_BYTES = 64 * 1024 * 1024
# High-resolution device and browser captures regularly exceed 5 MiB. Keep
# downloads bounded while allowing legitimate evidence through validation;
# decoded images remain independently capped by _IMAGE_MAX_PIXELS.
_IMAGE_MAX_BYTES = 32 * 1024 * 1024
# Canonicalization can grow a clean compressed image. Preserve bounded
# headroom above the accepted input size; decoded pixels are capped separately.
_NORMALIZED_IMAGE_MAX_BYTES = 48 * 1024 * 1024
_IMAGE_MAX_PIXELS = 40_000_000
_XML_MAX_BYTES = 2 * 1024 * 1024
_VIDEO_MAX_BYTES = 128 * 1024 * 1024
_GENERIC_BINARY_MIMES = frozenset({"application/octet-stream"})
# Downloader failures the caller must see unwrapped: an expired PAT has to
# surface as auth (rc=2), a transport error as retry-later, and an oversize
# attachment as "skip this evidence" rather than as a quarantine verdict.
_PASSTHROUGH_ERRORS = (AuthError, AttachmentTooLargeError, requests.RequestException)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()[\]-]{0,254}$")
_DANGEROUS_INNER_SUFFIXES = frozenset({
    ".bat",
    ".cmd",
    ".com",
    ".exe",
    ".html",
    ".htm",
    ".jar",
    ".js",
    ".msi",
    ".ps1",
    ".py",
    ".sh",
    ".svg",
    ".vbs",
})

_TYPES: dict[str, tuple[frozenset[str], int]] = {
    ".pdf": (frozenset({"application/pdf"}), _PDF_MAX_BYTES),
    ".png": (frozenset({"image/png"}), _IMAGE_MAX_BYTES),
    ".jpg": (frozenset({"image/jpeg"}), _IMAGE_MAX_BYTES),
    ".jpeg": (frozenset({"image/jpeg"}), _IMAGE_MAX_BYTES),
    ".gif": (frozenset({"image/gif"}), _IMAGE_MAX_BYTES),
    ".webp": (frozenset({"image/webp"}), _IMAGE_MAX_BYTES),
    ".xml": (frozenset({"application/xml", "text/xml"}), _XML_MAX_BYTES),
    ".mp4": (frozenset({"video/mp4"}), _VIDEO_MAX_BYTES),
    ".webm": (frozenset({"video/webm"}), _VIDEO_MAX_BYTES),
}

class QuarantineError(RuntimeError):
    """Raised when an attachment fails local safety validation."""


@dataclass(frozen=True)
class ReleasedAttachment:
    """An attachment released after local validation."""

    path: Path


def configure(settings: Any) -> None:
    """Keep the shared pipeline configuration interface; no remote services needed."""


def _validate_magic(path: Path, suffix: str) -> None:
    with path.open("rb") as handle:
        header = handle.read(16)
    if suffix == ".xml":
        from argus.pipeline import xml_evidence

        try:
            xml_evidence.validate_xml_file(path)
        except xml_evidence.XmlEvidenceError as exc:
            raise QuarantineError(str(exc)) from exc
        return
    if suffix == ".mp4":
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise QuarantineError(
                "attachment content does not match .mp4 magic bytes"
            )
        return
    if suffix == ".webm":
        if not header.startswith(b"\x1a\x45\xdf\xa3"):
            raise QuarantineError(
                "attachment content does not match .webm magic bytes"
            )
        return
    valid = {
        ".pdf": header.startswith(b"%PDF-"),
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".gif": header.startswith((b"GIF87a", b"GIF89a")),
        ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
    }[suffix]
    if not valid:
        raise QuarantineError(f"attachment content does not match {suffix} magic bytes")


def _detected_image_suffix(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _validate_image_magic(path: Path, suffix: str) -> str:
    """Return the verified image format, allowing only PNG/JPEG label swaps."""
    detected = _detected_image_suffix(path)
    expected = ".jpg" if suffix == ".jpeg" else suffix
    if detected is None:
        raise QuarantineError("attachment content is not an allowed image")
    if detected != expected and {detected, expected} - {".png", ".jpg"}:
        raise QuarantineError(
            f"attachment content does not match {suffix} image format"
        )
    return detected


def _normalize_image_format(path: Path, suffix: str, max_bytes: int) -> None:
    """Validate every clean image and rewrite PNG/JPEG to a canonical form."""
    _validate_image_magic(path, suffix)
    expected = ".jpg" if suffix == ".jpeg" else suffix

    from PIL import Image, UnidentifiedImageError

    fd, normalized_name = tempfile.mkstemp(
        prefix="argus-normalized-",
        suffix=suffix,
    )
    os.close(fd)
    normalized_path = Path(normalized_name)
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
                    if (
                        width <= 0
                        or height <= 0
                        or width * height > _IMAGE_MAX_PIXELS
                    ):
                        raise QuarantineError(
                            "attachment exceeds decoded image pixel limit"
                        )
                    image.load()
                    if expected not in {".png", ".jpg"}:
                        return
                    output: Image.Image = image
                    if expected == ".jpg" and image.mode not in {"RGB", "L"}:
                        output = image.convert("RGB")
                    if expected == ".jpg":
                        output.save(
                            normalized_path,
                            format="JPEG",
                            quality=95,
                            subsampling=0,
                            optimize=True,
                        )
                    else:
                        output.save(normalized_path, format="PNG")
        except (
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise QuarantineError(
                "attachment content is not a valid PNG or JPEG image"
            ) from exc
        normalized_limit = max(max_bytes, _NORMALIZED_IMAGE_MAX_BYTES)
        if normalized_path.stat().st_size > normalized_limit:
            raise QuarantineError("normalized attachment exceeds image size limit")
        normalized_path.replace(path)
    finally:
        normalized_path.unlink(missing_ok=True)


def _normalized_mime(value: str) -> str:
    return value.partition(";")[0].strip().lower()


def _validate_filename(value: str) -> str:
    """Validate type hints in an opaque display name that is never a path."""
    if not isinstance(value, str) or not value.strip():
        raise QuarantineError("attachment filename is missing")
    leaf = re.split(r"[/\\]", value.rstrip())[-1]
    suffixes = [
        f".{match.lower()}"
        for match in re.findall(r"\.([A-Za-z0-9]+)", leaf)
    ]
    if any(suffix in _DANGEROUS_INNER_SUFFIXES for suffix in suffixes[:-1]):
        raise QuarantineError(
            "attachment filename contains a dangerous double extension"
        )
    return suffixes[-1] if suffixes else ""


def _validate_destination(destination: Path, suffix: str) -> None:
    """Require a generated leaf name instead of a user-controlled filename."""
    name = destination.name
    if (
        not name
        or not name.isascii()
        or _SAFE_FILENAME.fullmatch(name) is None
        or destination.suffix.lower() != suffix
    ):
        raise QuarantineError("attachment destination filename is not safe")


@dataclass
class QuarantineTicket:
    """Handle for one attachment queued in a `QuarantineBatch`.

    `destination` is the final path (it may differ from the requested one
    when a mislabelled image keeps its real extension). `released` is set
    after local validation in `QuarantineBatch.add()`.
    """

    attachment_id: int
    destination: Path
    released: ReleasedAttachment | None = None

    def result(self) -> ReleasedAttachment:
        if self.released is None:
            raise QuarantineError(
                f"attachment {self.attachment_id} was not released from quarantine"
            )
        return self.released


class QuarantineBatch:
    """Validate downloaded evidence before releasing it into the output tree."""

    def __init__(
        self,
        *,
        session: Any,
        base_url: str,
        execution_key: str,
        downloader: Callable[..., str],
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._execution_key = execution_key
        self._downloader = downloader
    def add(
        self,
        *,
        attachment_id: int,
        attachment_name: str,
        destination: Path,
    ) -> QuarantineTicket:
        """Download, validate and release one attachment locally."""
        suffix = _validate_filename(attachment_name)
        type_config = _TYPES.get(suffix)
        if type_config is None:
            raise QuarantineError(
                f"attachment type {suffix or '(none)'} is not allowed"
            )
        _validate_destination(destination, suffix)
        allowed_mimes, max_bytes = type_config
        fd, download_name = tempfile.mkstemp(
            prefix="argus-quarantine-",
            suffix=suffix,
        )
        os.close(fd)
        download_path = Path(download_name)
        try:
            try:
                content_type = self._downloader(
                    self._session,
                    self._base_url,
                    attachment_id,
                    download_path,
                    max_bytes=max_bytes,
                )
            except _PASSTHROUGH_ERRORS:
                raise
            except Exception as exc:
                raise QuarantineError(
                    f"attachment {attachment_id} failed quarantine download: {exc}"
                ) from exc

            if download_path.stat().st_size > max_bytes:
                raise AttachmentTooLargeError(
                    f"attachment {attachment_id} exceeds the {max_bytes}-byte limit"
                )
            normalized_mime = _normalized_mime(content_type)
            accepted_mimes = (
                _IMAGE_MIMES if suffix in _IMAGE_SUFFIXES else allowed_mimes
            )
            if (
                normalized_mime not in accepted_mimes
                and normalized_mime not in _GENERIC_BINARY_MIMES
            ):
                expected = ", ".join(sorted(accepted_mimes))
                raise QuarantineError(
                    f"attachment MIME {normalized_mime or '(missing)'} "
                    f"does not match {expected}"
                )
            detected_image_suffix: str | None = None
            if suffix in _IMAGE_SUFFIXES:
                detected_image_suffix = _detected_image_suffix(download_path)
                if detected_image_suffix is None:
                    raise QuarantineError(
                        "attachment content is not an allowed image"
                    )
                expected_suffix = ".jpg" if suffix == ".jpeg" else suffix
                if (
                    detected_image_suffix != expected_suffix
                    and {detected_image_suffix, expected_suffix} - {".png", ".jpg"}
                ):
                    # Zephyr labels every inline comment image ".png"; a
                    # pasted GIF or WebP is still allowed evidence, so keep
                    # it under its real extension (which downstream media
                    # typing relies on) instead of rejecting the execution.
                    suffix = detected_image_suffix
                    destination = destination.with_suffix(detected_image_suffix)
                    _validate_destination(destination, suffix)
            else:
                _validate_magic(download_path, suffix)
            ticket = QuarantineTicket(
                attachment_id=attachment_id,
                destination=destination,
            )
            if detected_image_suffix is not None:
                _normalize_image_format(download_path, suffix, max_bytes)
            destination.parent.mkdir(parents=True, exist_ok=True)
            download_path.replace(destination)
            ticket.released = ReleasedAttachment(path=destination)
            return ticket
        except QuarantineError:
            raise
        except _PASSTHROUGH_ERRORS:
            raise
        except Exception as exc:
            raise QuarantineError(
                f"attachment validation failed for {attachment_id}: {exc}"
            ) from exc
        finally:
            download_path.unlink(missing_ok=True)

    def release(self) -> None:
        """Finish the batch; each add already validated and released its result."""


def download_and_release(
    *,
    session: Any,
    base_url: str,
    attachment_id: int,
    attachment_name: str,
    execution_key: str,
    destination: Path,
    downloader: Callable[..., str],
) -> ReleasedAttachment:
    """Download and release one attachment only after local validation."""
    batch = QuarantineBatch(
        session=session,
        base_url=base_url,
        execution_key=execution_key,
        downloader=downloader,
    )
    ticket = batch.add(
        attachment_id=attachment_id,
        attachment_name=attachment_name,
        destination=destination,
    )
    batch.release()
    return ticket.result()
