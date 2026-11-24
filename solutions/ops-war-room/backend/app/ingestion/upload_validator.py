"""Strict upload validation for BYO operational log files (size, extension, traversal, binary)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ALLOWED_LOG_EXTENSIONS = frozenset({".jsonl", ".json", ".csv", ".log", ".txt"})
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails security, type, or size validation."""


@dataclass(frozen=True)
class ValidatedUpload:
    safe_filename: str
    extension: str
    size_bytes: int
    text_content: str


def validate_log_upload(filename: str, raw_bytes: bytes) -> ValidatedUpload:
    """Validate uploaded log bundle safely; strips traversal paths and rejects binary payloads."""
    base_name = Path(filename or "").name.strip()
    if not base_name or base_name in (".", ".."):
        raise UploadValidationError("Filename is empty or invalid.")

    ext = Path(base_name).suffix.lower()
    if ext not in ALLOWED_LOG_EXTENSIONS:
        raise UploadValidationError(
            f"Unsupported file extension {ext!r}. Allowed: {sorted(ALLOWED_LOG_EXTENSIONS)}"
        )

    size = len(raw_bytes)
    if size == 0:
        raise UploadValidationError("Uploaded file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise UploadValidationError(
            f"Uploaded file exceeds maximum allowed size ({MAX_UPLOAD_BYTES // (1024 * 1024)} MiB)."
        )
    if b"\x00" in raw_bytes[:4096]:
        raise UploadValidationError("Binary content detected; only UTF-8 text/JSON/CSV logs are allowed.")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadValidationError("Uploaded file must be valid UTF-8 text.") from exc

    return ValidatedUpload(
        safe_filename=base_name,
        extension=ext,
        size_bytes=size,
        text_content=text,
    )
