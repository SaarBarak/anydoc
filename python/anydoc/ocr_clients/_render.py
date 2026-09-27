"""Renders a single-page PDF to an image -- shared by whichever engines need
one. `azure_di` hands Azure the page's own PDF bytes directly and never
imports this; `tesseract` and `vlm` both need pixels first, so the
rendering step lives here once instead of twice.

Lazy import, same reason as every engine module: this must stay importable
even when neither `tesseract` nor `vlm` extra is installed -- only calling
`render_page_to_image` should ever require `pypdfium2`."""


def render_page_to_image(page_pdf_bytes: bytes, *, scale: float = 2.0) -> "PIL.Image.Image":
    """`page_pdf_bytes` is trusted to already be exactly one page (callers
    go through `verify_single_page` first, same as every `OcrClient`).
    `scale` multiplies PDFium's 72-DPI default page size -- 2.0 (~144 DPI)
    is enough for both Tesseract and a VLM to read body text reliably
    without uploading a needlessly large image."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(page_pdf_bytes)
    try:
        return pdf[0].render(scale=scale).to_pil()
    finally:
        pdf.close()
