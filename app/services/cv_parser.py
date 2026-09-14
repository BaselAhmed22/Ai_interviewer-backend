import logging
from pathlib import Path
from zipfile import BadZipFile

from docx.opc.exceptions import OpcError
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from docx import Document

from app.core.config import BASE_DIR

logger = logging.getLogger(__name__)


class CorruptFileError(Exception):
    """Raised when a CV file exists and is readable from disk, but its
    content is not a valid/complete PDF or DOCX (truncated upload, wrong
    magic bytes, etc). Callers should treat this as terminal — re-running
    the same parse on the same bytes will never succeed, so it must not
    be retried the way a transient error (DB hiccup, etc) would be."""


def resolve_cv_path(file_path: str | Path) -> Path:
    """No-op for new uploads (already absolute, see candidate.py); resolves
    a legacy relative path against BASE_DIR instead of the caller's cwd."""
    path = Path(file_path)
    return path if path.is_absolute() else BASE_DIR / path


def extract_text_from_file(file_path: str) -> str:
    """Extract text from files such as PDF and DOCX."""
    path = resolve_cv_path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found at: {path}")

    ext = path.suffix.lower()
    text = ""

    if ext == ".pdf":
        try:
            reader = PdfReader(str(path))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        except PyPdfError as exc:
            # Covers every pypdf failure mode — all non-retryable; only a
            # fresh re-upload can fix it.
            logger.warning("Corrupt/unreadable PDF at %s: %s", path, exc)
            raise CorruptFileError(
                f"The PDF file is corrupted or incomplete and could not be read: {exc}"
            ) from exc
    elif ext in (".docx", ".doc"):
        try:
            doc = Document(str(path))
        except (OpcError, BadZipFile) as exc:
            # .docx is a zip package — a truncated upload fails here the
            # same non-retryable way a corrupt PDF does.
            logger.warning("Corrupt/unreadable DOCX at %s: %s", path, exc)
            raise CorruptFileError(
                f"The DOCX file is corrupted or incomplete and could not be read: {exc}"
            ) from exc
        for paragraph in doc.paragraphs:
            if paragraph.text:
                text += paragraph.text + "\n"
    else:
        raise ValueError(f"Unsupported file format: {ext}")

    return text.strip()
