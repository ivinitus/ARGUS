"""Discover external evidence references in locally released session PDFs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from argus.domain.desktop_policy import drive_urls


def with_pdf_links(metadata: dict[str, Any], execution_dir: Path) -> dict[str, Any]:
    """Read annotations/text only; external URLs are never downloaded."""
    refs = list(metadata.get("external_evidence_links") or [])
    for path in sorted(execution_dir.glob("*.pdf")):
        try:
            with fitz.open(path) as document:
                for page in document:
                    texts = [
                        page.get_text(),
                        *(str(link.get("uri") or "") for link in page.get_links()),
                    ]
                    for url in dict.fromkeys(url for text in texts for url in drive_urls(text)):
                        ref = {
                            "url": url, "step_index": None, "source": "pdf",
                            "attachment": path.name, "attachment_page": page.number + 1,
                        }
                        if ref not in refs:
                            refs.append(ref)
        except (fitz.EmptyFileError, fitz.FileDataError):
            # The extractor handles invalid PDFs as unavailable evidence.
            continue
    return {**metadata, "external_evidence_links": refs} if refs else metadata
