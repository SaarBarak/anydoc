"""Convert documents to GitHub-Flavored Markdown."""

import json
import os
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
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
    to_document,
)
from anydoc._anydoc import to_markdown as _to_markdown
from anydoc._anydoc import to_markdown_bytes as _to_markdown_bytes

Format = Literal[
    "doc", "docx", "odt", "pdf", "ppt", "pptx", "rtf", "epub", "xlsx", "ods", "odp", "csv"
]
"""Input format, named after the extension that identifies it. Container
variants that share a parser (`.docm`, `.xlsm`, `.ppsx`, ...) map onto these
via `format_from_bytes` or `format_from_extension`."""

Ocr = Literal["reject", "hosted"]
"""What happens to a PDF whose pages need OCR. `reject` (the default) raises
`NeedsOcrError` naming the pages -- unless Azure Document Intelligence is
configured (`AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` and `_KEY` both set), in
which case only those pages are recovered via Azure instead, and the rest of
the document is untouched. `hosted` sends the whole document to Firecrawl
Parse instead, keyless unless a key is given, regardless of Azure
configuration. Documents anydoc converts itself never leave the machine
unless one of these applies.

Azure requires the `azure` extra: `pip install firecrawl-anydoc[azure]`."""


class HostedError(ConvertError):
    """`ocr="hosted"` could not get the document through Firecrawl Parse."""


class AzureError(ConvertError):
    """Azure Document Intelligence could not recover the pages that need
    OCR, or `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`/`_KEY` are misconfigured."""


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
        if _azure_requested():
            return _parse_azure(Path(path).read_bytes(), error)
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
        if _azure_requested():
            return _parse_azure(bytes(data), error)
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


_AZURE_ENDPOINT_ENV = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
_AZURE_KEY_ENV = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
_AZURE_MAX_WORKERS = 8


def _azure_requested() -> bool:
    """Whether Azure looks configured at all -- checked before `_parse_azure`
    so a caller with neither variable set gets the original `NeedsOcrError`
    (unchanged behavior) instead of an Azure-specific error about a service
    they never asked for."""
    return bool(os.environ.get(_AZURE_ENDPOINT_ENV) or os.environ.get(_AZURE_KEY_ENV))


# Only the pages NeedsOcrError named go to Azure, not the whole document:
# unlike Parse, prebuilt-layout takes a page selection. One job per page,
# never a page range -- output_content_format=MARKDOWN returns one joined
# string per job with no documented per-page span boundary, so a multi-page
# result can't be safely split back apart afterward.
def _parse_azure(data: bytes, error: NeedsOcrError) -> str:
    endpoint = os.environ.get(_AZURE_ENDPOINT_ENV)
    key = os.environ.get(_AZURE_KEY_ENV)
    if not endpoint or not key:
        raise AzureError(
            f"Azure Document Intelligence needs both {_AZURE_ENDPOINT_ENV} "
            f"and {_AZURE_KEY_ENV} set; only one is"
        )
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import (
            AnalyzeDocumentRequest,
            DocumentContentFormat,
        )
        from azure.core.credentials import AzureKeyCredential
        import pdf_inspector
    except ImportError as exc:
        raise AzureError(
            "Azure Document Intelligence OCR requires the 'azure' extra: "
            "pip install firecrawl-anydoc[azure]"
        ) from exc

    client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(key))
    # Unrestricted read of every page's native text, in document order --
    # the pages needing OCR (error.pages) come only from anydoc's own
    # restricted check, never re-derived from this array.
    pages = pdf_inspector.extract_pages_markdown_bytes(data).pages
    merged = [page.markdown for page in pages]

    def _ocr_page(page_num: int) -> str:
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(bytes_source=data),
            pages=str(page_num),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        return poller.result().content

    try:
        with ThreadPoolExecutor(max_workers=_AZURE_MAX_WORKERS) as pool:
            results = list(pool.map(_ocr_page, error.pages))
    except Exception as exc:
        raise AzureError(f"Azure Document Intelligence: {exc}") from exc

    for page_num, markdown in zip(error.pages, results):
        merged[page_num - 1] = markdown  # error.pages is 1-indexed, merged is 0-indexed

    joined = "\n\n".join(merged)
    return joined if joined.endswith("\n") else joined + "\n"


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
    "AzureError",
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
