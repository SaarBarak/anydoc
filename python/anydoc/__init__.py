"""Convert documents to GitHub-Flavored Markdown."""

import json
import os
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from typing import Literal

from anydoc._anydoc import (
    Asset,
    Block,
    Cell,
    CellSlot,
    ConvertError,
    Document,
    EncryptedError,
    ImageSource,
    Inline,
    LinkTarget,
    List,
    ListItem,
    MalformedError,
    MissingPartError,
    NeedsOcrError,
    Note,
    ResourceLimitError,
    Style,
    Table,
    UnsupportedError,
    format_from_bytes,
    format_from_extension,
    format_from_path,
    pdf_pages_markdown,
    pdf_text_positions,
    to_document,
)
from anydoc._anydoc import to_markdown as _to_markdown
from anydoc._anydoc import to_markdown_bytes as _to_markdown_bytes
from anydoc.ocr_clients import requested as _ocr_requested
from anydoc.ocr_clients.base import ClientConfigError, verify_single_page

Format = Literal[
    "doc", "docx", "odt", "pdf", "ppt", "pptx", "rtf", "epub", "xlsx", "ods", "odp", "csv"
]
"""Input format, named after the extension that identifies it. Container
variants that share a parser (`.docm`, `.xlsm`, `.ppsx`, ...) map onto these
via `format_from_bytes` or `format_from_extension`."""

Ocr = Literal["reject", "hosted"]
"""What happens to a PDF whose pages need OCR. `reject` (the default) raises
`NeedsOcrError` naming the pages -- unless an OCR engine is configured, in
which case only those pages are recovered through it and the rest of the
document is untouched. `hosted` sends the whole document to Firecrawl Parse
instead, keyless unless a key is given, and wins regardless of which engines
are configured. Documents anydoc converts itself never leave the machine
unless one of these applies.

Engines are selected from the environment rather than by this argument, so
existing callers keep working unchanged; `anydoc.ocr_clients` lists them and
sets their precedence. Azure Document Intelligence
(`AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` and `_KEY`) is the one shipped today
and needs the `azure` extra: `pip install firecrawl-anydoc[azure]`."""


class HostedError(ConvertError):
    """`ocr="hosted"` could not get the document through Firecrawl Parse."""


class OcrError(ConvertError):
    """An OCR engine could not recover the pages that need OCR, is
    misconfigured, or the document lost pages that nothing recovered.

    Engine-agnostic on purpose: which engine ran is a deployment detail, and
    the failures worth catching -- no usable text came back -- are the same
    whichever one did. `str(error)` names the engine where it is relevant."""


def to_markdown(
    path: "str | os.PathLike[str]",
    *,
    ocr: Ocr = "reject",
    api_key: "str | None" = None,
    api_url: "str | None" = None,
) -> str:
    """Convert a document file to Markdown. The format is detected from the
    file content; the extension is the fallback for signature-less formats
    (CSV) and unrecognizable containers.

    For `ocr="hosted"`, `api_key` falls back to `FIRECRAWL_API_KEY`, then
    keyless; `api_url` to `FIRECRAWL_API_URL`, then
    `https://api.firecrawl.dev`."""
    try:
        return _to_markdown(path)
    except NeedsOcrError as error:
        if ocr == "hosted":
            path = Path(path)
            return _parse_hosted(path.read_bytes(), path.name, api_key, api_url)
        engine = _ocr_requested()
        if engine is not None:
            return _parse_ocr(Path(path).read_bytes(), error, engine)
        raise


def to_markdown_bytes(
    data: "bytes | bytearray",
    format: "Format | None" = None,
    *,
    ocr: Ocr = "reject",
    api_key: "str | None" = None,
    api_url: "str | None" = None,
) -> str:
    """Convert an in-memory document to Markdown. Without a format, it is
    detected from the content, which signature-less formats (CSV) have to
    name explicitly. `ocr`, `api_key` and `api_url` are as for
    `to_markdown`."""
    try:
        return _to_markdown_bytes(data, format)
    except NeedsOcrError as error:
        if ocr == "hosted":
            return _parse_hosted(bytes(data), "document.pdf", api_key, api_url)
        engine = _ocr_requested()
        if engine is not None:
            return _parse_ocr(bytes(data), error, engine)
        raise


_API_URL = "https://api.firecrawl.dev"
_TIMEOUT_SECONDS = 300


# The whole document goes, not only the pages that need OCR: Parse has no
# page selection.
def _parse_hosted(data: bytes, filename: str, api_key: "str | None", api_url: "str | None") -> str:
    if api_key is None:
        api_key = os.environ.get("FIRECRAWL_API_KEY")
    api_url = api_url or os.environ.get("FIRECRAWL_API_URL") or _API_URL
    url = api_url.rstrip("/") + "/v2/parse"
    options = {"parsers": [{"type": "pdf", "mode": "auto"}], "origin": f"anydoc@{_version()}"}
    boundary = uuid.uuid4().hex
    request = urllib.request.Request(
        url,
        data=_multipart(boundary, json.dumps(options), filename, data),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            status, reply = response.status, _json(response.read())
    except urllib.error.HTTPError as error:
        status, reply = error.code, _json(error.read())
    except OSError as error:
        raise HostedError(f"Firecrawl Parse: {error}") from error
    if status != 200 or not reply.get("success"):
        detail = reply.get("error") or f"HTTP {status}"
        raise HostedError(_describe(status, detail, bool(api_key)))
    data = reply.get("data")
    markdown = data.get("markdown") if isinstance(data, dict) else None
    if not isinstance(markdown, str) or not markdown:
        raise HostedError("Firecrawl Parse returned no Markdown")
    return markdown if markdown.endswith("\n") else markdown + "\n"


_MAX_WORKERS = 8


# `data` is guaranteed to be PDF bytes here, not just assumed: NeedsOcr is
# raised in exactly one place in the whole Rust core, src/formats/pdf.rs --
# no other format parser has any such logic, so this function can only
# ever be reached via a PDF.
#
# Only the pages NeedsOcrError named are sent, not the whole document. One
# job per page, never a page range -- see `OcrClient.ocr_page` for why.
#
# Engine-agnostic: everything here is page slicing, dispatch, reindexing and
# merging, and the only engine-shaped operations are constructing the client
# and calling `ocr_page`, both behind `ocr_clients.base.OcrClient`. Adding an
# engine means writing a client module, never touching this function.
def _parse_ocr(data: bytes, error: NeedsOcrError, engine=None) -> str:
    engine = engine if engine is not None else _ocr_requested()
    try:
        client = engine.client()
    except ClientConfigError as exc:
        raise OcrError(str(exc)) from exc

    try:
        from pypdf import PdfReader  # noqa: F401  -- probed, used in _dispatch
    except ImportError as exc:
        raise OcrError(
            "OCR needs the page slicer from the 'ocr' extra: "
            "pip install firecrawl-anydoc[ocr]"
        ) from exc

    # Unrestricted read of every page's native text, in document order --
    # the pages needing OCR (error.pages) come only from anydoc's own
    # restricted check, never re-derived from this array.
    #
    # Read through anydoc's own bindings, not a separately installed
    # `pdf-inspector` Python package. Both would call the same library, but
    # they would resolve it independently: the crate this binary links is
    # redirected to the RTL-fixed fork, a PyPI install is not, and one install
    # carried both versions until the pins were brought into line. There is no
    # second resolution to keep in line now.
    #
    # A page this returns empty is a page whose text `pdf-inspector` distrusted
    # and discarded wholesale (upstream firecrawl/pdf-inspector#252, #342). It
    # is left empty here on purpose. Rebuilding it locally from the positioned
    # -text API was tried and removed: reading order approximated from
    # coordinates loses the structure that makes tender content legible, and
    # the decision to recover such a page belongs to detection, which owns
    # what lands in `error.pages`, not to this merge step. Route the page to
    # OCR instead of reconstructing it.
    pages = pdf_pages_markdown(data)
    merged = [page["markdown"] for page in pages]

    # Refuse to return a document already known to be incomplete, and refuse
    # before spending anything on OCR for the pages that would have succeeded.
    #
    # This is not detection deciding what to OCR -- it routes nothing and
    # flags nothing. It is the merge step declining to present a partial
    # document as a whole one, which is the trade `ocr="reject"` makes
    # everywhere else: for compliance content a loud failure beats a quiet
    # gap. Once page health feeds these pages into `error.pages` they are
    # OCR'd like any other and this can never fire.
    #
    # Gated on a stated `ocr_reason`, not on emptiness: a genuinely blank
    # page also reports `needs_ocr`, and failing on those would reject valid
    # documents. A wiped page whose reason is unstated -- an outlined title
    # below the curve floor, say -- still passes here; separating that from
    # a blank page needs ink detection, which belongs with the other signals.
    flagged = set(error.pages)
    dropped = {
        page["page"] + 1: page["ocr_reason"]
        for page in pages
        if page["page"] + 1 not in flagged
        and not page["markdown"].strip()
        and page["ocr_reason"]
    }
    if dropped:
        raise OcrError(
            f"pages {sorted(dropped)} lost their text to the extractor's own "
            f"suppression and were not flagged for OCR; returning the document "
            f"would drop them silently. Reasons: {dropped}"
        )

    def _dispatch(page_num: int) -> str:
        page_bytes = _single_page_pdf(data, page_num)
        verify_single_page(page_bytes)
        return client.ocr_page(page_bytes)

    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            results = list(pool.map(_dispatch, error.pages))
    except Exception as exc:
        raise OcrError(f"{engine.__name__.rsplit('.', 1)[-1]}: {exc}") from exc

    for page_num, markdown in zip(error.pages, results):
        merged[page_num - 1] = markdown  # error.pages is 1-indexed, merged is 0-indexed

    joined = "\n\n".join(merged)
    return joined if joined.endswith("\n") else joined + "\n"


def _single_page_pdf(data: bytes, page_num: int) -> bytes:
    """Slices out page_num (1-indexed) as its own single-page PDF. Azure
    size-limits the whole uploaded payload before `pages=` is ever applied --
    confirmed live: a 5.1MB, 138-page document was rejected outright asking
    for one page. Sending only that page's own bytes keeps every request
    small regardless of source size. Module-level (not nested in
    `_parse_ocr`) so tests can call or patch it directly.

    Known limitations, not yet addressed:
    - Fixes the whole-document case only. A single page can in principle
      still be too large on its own (e.g. one very high-resolution scan) --
      that surfaces as a generic OcrError via _parse_ocr's exception
      wrapper (see test_a_still_oversized_single_page_raises_a_clean_azure_error),
      not a distinct, more actionable one.
    - Azure's documented size ceiling (checked against current Microsoft
      docs): 4MB per request on the free F0 tier, 500MB on paid S0 -- the
      5.1MB failure this fix was verified against lines up almost exactly
      with F0's ceiling, suggesting (not confirmed) the resource used in
      testing is F0. F0 also caps at 500 pages/month total across the whole
      resource and only processes the first 2 pages of any multi-page
      upload -- for real production volume, that monthly cap is a bigger
      constraint than file size ever was, and worth a real answer from
      whoever owns the Azure subscription before this ships, independent of
      anything in this code. This fix's one-page-per-request shape sidesteps
      F0's 2-page restriction as a side effect, not a deliberate design goal.
    - Re-parses the entire source document from scratch once per flagged
      page, each in its own thread -- cost scales with page count times
      document size. Kept this way deliberately (an independent parse per
      thread avoids sharing a PdfReader across threads, which isn't
      documented as thread-safe), but it's an unverified tradeoff, not a
      measured one. Untested under real stress (many flagged pages in a
      very large document)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(data))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_num - 1])
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _multipart(boundary: str, options: str, filename: str, data: bytes) -> bytes:
    filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="options"\r\n\r\n',
            options.encode(),
            f"\r\n--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )


def _json(body: bytes) -> dict:
    try:
        reply = json.loads(body)
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def _describe(status: int, detail: str, keyed: bool) -> str:
    if status == 401:
        return f"Firecrawl Parse rejected the API key: {detail}"
    if status == 402:
        return f"Firecrawl Parse is out of credits: {detail}"
    if status == 429 and keyed:
        return f"Firecrawl Parse rate limit reached: {detail}"
    if status == 429:
        return f"Firecrawl Parse keyless limit reached, set FIRECRAWL_API_KEY: {detail}"
    return f"Firecrawl Parse: {detail}"


def _version() -> str:
    try:
        return version("firecrawl-anydoc")
    except PackageNotFoundError:
        return "unknown"


__all__ = [
    "Asset",
    "OcrError",
    "Block",
    "Cell",
    "CellSlot",
    "ConvertError",
    "Document",
    "EncryptedError",
    "Format",
    "HostedError",
    "ImageSource",
    "Inline",
    "LinkTarget",
    "List",
    "ListItem",
    "MalformedError",
    "MissingPartError",
    "NeedsOcrError",
    "Note",
    "pdf_pages_markdown",
    "pdf_text_positions",
    "Ocr",
    "ResourceLimitError",
    "Style",
    "Table",
    "UnsupportedError",
    "format_from_bytes",
    "format_from_extension",
    "format_from_path",
    "to_document",
    "to_markdown",
    "to_markdown_bytes",
]
