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


def verify_single_page(page_pdf_bytes: bytes) -> None:
    """Every `OcrClient.ocr_page()` trusts that it's given exactly one PDF
    page (see `OcrClient.ocr_page`'s docstring) -- enforced here once,
    centrally, in the one place every engine's dispatch already passes
    through, instead of duplicated inside each engine's own implementation.
    Re-parses the bytes rather than trusting whatever produced them, so it
    also catches a bug in that production code, not just hypothetical
    misuse by a future caller.

    Raises `ValueError` on any count other than 1 -- deliberately not
    `ClientConfigError` (this isn't a "can't get a working client" problem)
    -- callers already wrap dispatch failures broadly and don't need a new
    exception type to catch this specifically."""
    from io import BytesIO

    from pypdf import PdfReader

    count = len(PdfReader(BytesIO(page_pdf_bytes)).pages)
    if count != 1:
        raise ValueError(f"expected exactly one page, got {count}")
