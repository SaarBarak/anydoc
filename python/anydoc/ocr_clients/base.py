"""The shape every OCR engine client implements, and the one error type
they all raise for "can't get a working client" -- config missing, bad
credentials, or the engine's own optional dependency isn't installed.
Zero third-party dependencies: importable regardless of which (if any)
engine's extra is installed."""

from typing import Protocol


class OcrClient(Protocol):
    def ocr_page(self, page_pdf_bytes: bytes) -> str:
        """OCR one single-page PDF's bytes, returning its Markdown text."""
        ...


class ClientConfigError(Exception):
    """A client's required configuration (env vars, credentials, or its own
    optional dependency) is missing or incomplete. Engine-agnostic --
    anydoc/__init__.py catches this and re-raises as its own public
    `ConvertError` subclass, so callers never see this type directly."""
