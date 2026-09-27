"""Tesseract OCR client -- a local, offline engine: no cloud credentials, no
per-page network cost, and no data ever leaving the machine, at the expense
of accuracy against a cloud engine on messy scans. Every page is passed
through Tesseract's own OSD (orientation and script detection) before the
real OCR call, and rotated to match -- see `TesseractClient._detect_rotation`
for why this isn't optional. Everything Tesseract- and image-rendering-
specific lives here -- anydoc/__init__.py's orchestration knows nothing
about either, only the `OcrClient` protocol.

All third-party imports are lazy, inside methods, not at module level: this
module must stay importable (for `is_requested`, which only needs
`os.environ`) even when the `tesseract` extra isn't installed -- only
constructing or using a client should ever require it."""

import os
import re

from anydoc.ocr_clients.base import ClientConfigError

ENABLED_ENV = "TESSERACT_OCR_ENABLED"
LANG_ENV = "TESSERACT_OCR_LANG"
CMD_ENV = "TESSERACT_CMD"

_DEFAULT_LANG = "eng"

# Tesseract's own OSD (orientation and script detection) output includes a
# line like "Rotate: 90" -- degrees to rotate the image clockwise to correct
# its reading orientation. This is a real, measured failure mode, not a
# hypothetical: a live 95-page tender had 43 of 53 flagged pages rendered
# sideways (content-level rotation inside the page's own drawing commands,
# not a PDF /Rotate flag pdfium would already apply), which a single
# uncorrected OCR pass reads as near-total garbage.
_OSD_ROTATE = re.compile(r"Rotate: (\d+)")


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
        angle = self._detect_rotation(image)
        if angle:
            image = image.rotate(-angle, expand=True)
        return pytesseract.image_to_string(image, lang=self._lang)

    @staticmethod
    def _detect_rotation(image) -> int:
        """Degrees to rotate `image` clockwise before OCRing it, or 0.
        Unconditional, not opt-in: the extra OSD pass costs roughly as much
        again as the real OCR call (measured: +61% total time across 53
        pages), which is trivial for a local, free engine next to what a
        wrong orientation actually costs -- unreadable text with no error
        raised to say so. OSD can refuse outright on a near-blank or
        low-text page (`TesseractError`); that means "nothing to orient
        from", not a page worth failing the whole call over, so it is
        treated the same as "no rotation detected"."""
        import pytesseract

        try:
            osd = pytesseract.image_to_osd(image)
        except pytesseract.TesseractError:
            return 0
        match = _OSD_ROTATE.search(osd)
        return int(match.group(1)) if match else 0
