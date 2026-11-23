"""Strict upload validation: MIME allowlist, magic-byte inspection, 10 MB cap, UUID storage keys.

Security guarantees (mandatory-secure-web-skills):
  - Rejects path traversal in user filenames (os.path.basename + character allowlist)
  - Verifies actual file header magic bytes (not trusting Content-Type or extension alone)
  - Generates unpredictable UUID storage keys outside any web root
  - Enforces hard payload size limit (default 10 MB)

TODO(security): For production customer deployments, attach a Cloud Storage malware scanning
and Content Disarm & Reconstruction (CDR) pipeline before long-term archival of user uploads.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass

from app.core.models import PACK_SLUG_RE

ALLOWED_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".pdf": ("application/pdf",),
    ".png": ("image/png",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".txt": ("text/plain", "application/octet-stream"),
    ".md": ("text/markdown", "text/plain", "application/octet-stream"),
    ".csv": ("text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"),
    ".yaml": ("application/x-yaml", "text/yaml", "text/plain", "application/octet-stream"),
    ".yml": ("application/x-yaml", "text/yaml", "text/plain", "application/octet-stream"),
}

CANONICAL_MIME_BY_EXT: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".yaml": "application/x-yaml; charset=utf-8",
    ".yml": "application/x-yaml; charset=utf-8",
}

_SAFE_DISPLAY_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails extension, MIME, magic-byte, or size validation."""


@dataclass(frozen=True)
class ValidatedUpload:
    """Immutable, sanitized upload descriptor safe to pass downstream."""

    pack_id: str
    display_name: str
    extension: str
    canonical_mime: str
    gcs_object_key: str
    size_bytes: int
    sha256: str
    content: bytes


def _sanitize_display_name(raw_filename: str) -> tuple[str, str]:
    """Strip directory traversal (POSIX & Windows) and validate allowlisted extension."""
    if not raw_filename or not raw_filename.strip():
        raise UploadValidationError("Uploaded filename must not be empty")
    if "\x00" in raw_filename:
        raise UploadValidationError("Filename contains illegal NUL byte")

    # Normalize Windows backslashes before os.path.basename so "..\\foo" never leaks path components
    normalized = raw_filename.replace("\\", "/")
    base = os.path.basename(normalized).strip()
    if not base or base in (".", "..") or ".." in base:
        raise UploadValidationError("Invalid filename (path traversal rejected)")

    cleaned = _SAFE_DISPLAY_NAME_RE.sub("_", base)[:140]
    _, ext = os.path.splitext(cleaned)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(
            f"Unsupported file extension {ext!r}. Allowed: {sorted(ALLOWED_EXTENSIONS.keys())}"
        )
    return cleaned, ext


def _verify_magic_bytes(ext: str, content: bytes) -> None:
    """Confirm binary header magic bytes or clean UTF-8 encoding match the declared extension."""
    if ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise UploadValidationError("File content failed PDF magic-byte check (%PDF-)")
        return
    if ext == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise UploadValidationError("File content failed PNG magic-byte check")
        return
    if ext in (".jpg", ".jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            raise UploadValidationError("File content failed JPEG magic-byte check")
        return
    # Text formats (.txt, .md, .csv, .yaml, .yml): reject NUL bytes & binary payloads
    if b"\x00" in content:
        raise UploadValidationError("Text upload contains illegal binary NUL bytes")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadValidationError("Text upload must be valid UTF-8") from exc
    if not decoded.strip():
        raise UploadValidationError("Text upload must not be empty or whitespace-only")


def validate_upload(
    *,
    pack_id: str,
    filename: str,
    content_type: str | None,
    content: bytes,
    max_upload_bytes: int = 10_485_760,
) -> ValidatedUpload:
    """Validate untrusted uploaded bytes and assign an opaque UUID GCS object key."""
    if not PACK_SLUG_RE.match(pack_id):
        raise UploadValidationError(f"Invalid pack_id: {pack_id!r}")
    if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
        raise UploadValidationError("Uploaded file is empty")
    raw_bytes = bytes(content)
    if len(raw_bytes) > max_upload_bytes:
        raise UploadValidationError(
            f"Upload size {len(raw_bytes)} bytes exceeds limit of {max_upload_bytes} bytes"
        )

    display_name, ext = _sanitize_display_name(filename)

    declared_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if declared_mime and declared_mime not in ALLOWED_EXTENSIONS[ext]:
        raise UploadValidationError(
            f"MIME type {declared_mime!r} does not match extension {ext!r}"
        )

    _verify_magic_bytes(ext, raw_bytes)

    digest = hashlib.sha256(raw_bytes).hexdigest()
    object_key = f"uploads/{pack_id}/{uuid.uuid4().hex}{ext}"

    return ValidatedUpload(
        pack_id=pack_id,
        display_name=display_name,
        extension=ext,
        canonical_mime=CANONICAL_MIME_BY_EXT[ext],
        gcs_object_key=object_key,
        size_bytes=len(raw_bytes),
        sha256=digest,
        content=raw_bytes,
    )
