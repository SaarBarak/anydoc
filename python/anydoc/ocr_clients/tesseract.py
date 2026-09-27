"""Tesseract OCR client -- a local, offline engine: no cloud credentials, no
per-page network cost, and no data ever leaving the machine, at the expense
of accuracy against a cloud engine on messy scans. Everything Tesseract- and
image-rendering-specific lives here -- anydoc/__init__.py's orchestration
knows nothing about either, only the `OcrClient` protocol.

All third-party imports are lazy, inside methods, not at module level: this
module must stay importable (for `is_requested`, which only needs
`os.environ`) even when the `tesseract` extra isn't installed -- only
constructing or using a client should ever require it."""

import os

from anydoc.ocr_clients.base import ClientConfigError

ENABLED_ENV = "TESSERACT_OCR_ENABLED"
LANG_ENV = "TESSERACT_OCR_LANG"
CMD_ENV = "TESSERACT_CMD"

_DEFAULT_LANG = "eng"


def is_requested() -> bool:
    """Unlike azure_di or vlm, Tesseract needs no connection details to run
    at all -- an always-on local binary would activate for anyone who
    happens to have it installed, an implicit behavior change nobody asked
    for. So this engine's "configured" signal is one explicit opt-in flag,
    not the presence of credentials it has none of."""
    return bool(os.environ.get(ENABLED_ENV))


def client() -> "TesseractClient":
    """This module's half of the engine contract -- see `ocr_clients`."""
    return TesseractClient.from_env()


class TesseractClient:
    """Construct via `from_env`, not directly -- that's where the binary and
    optional-import checks happen."""

    def __init__(self, lang: str):
        self._lang = lang

    @classmethod
    def from_env(cls) -> "TesseractClient":
        try:
            import pytesseract
        except ImportError as exc:
            raise ClientConfigError(
                "Tesseract OCR requires the 'tesseract' extra: "
                "pip install firecrawl-anydoc[tesseract]"
            ) from exc

        cmd = os.environ.get(CMD_ENV)
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd

        try:
            pytesseract.get_tesseract_version()
        except EnvironmentError as exc:
            raise ClientConfigError(
                f"Tesseract OCR needs the 'tesseract' binary on PATH (or {CMD_ENV} "
                f"pointing at it): {exc}"
            ) from exc

        return cls(lang=os.environ.get(LANG_ENV) or _DEFAULT_LANG)

    def ocr_page(self, page_pdf_bytes: bytes) -> str:
        import pytesseract

        from anydoc.ocr_clients._render import render_page_to_image

        image = render_page_to_image(page_pdf_bytes)
        return pytesseract.image_to_string(image, lang=self._lang)
