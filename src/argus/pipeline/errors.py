"""Exceptions shared by the extractor and the attachment quarantine.

They live here so ``attachment_quarantine`` can let them pass through its
fail-closed wrapper without importing ``extractor`` (which imports it).
"""
from __future__ import annotations


class AuthError(Exception):
    """Raised when Zephyr auth clearly fails (401/403)."""


class AttachmentTooLargeError(ValueError):
    """Raised when an attachment exceeds its pre-parse download limit."""
