"""Azure Document Intelligence OCR client. Everything Azure-SDK-specific
lives here -- anydoc/__init__.py's orchestration (page slicing, dispatch,
reindexing) knows nothing about Azure's actual API shape, only the
`OcrClient` protocol.

All Azure SDK imports are lazy, inside methods, not at module level: this
module must stay importable (for `is_requested`, which only needs
`os.environ`) even when the `azure` extra isn't installed -- only
constructing or using a client should ever require it."""

import os

from anydoc.ocr_clients.base import ClientConfigError

ENDPOINT_ENV = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
KEY_ENV = "AZURE_DOCUMENT_INTELLIGENCE_KEY"


def is_requested() -> bool:
    """Whether Azure looks configured at all -- checked by anydoc's
    dispatch logic before trying to construct a client, so a caller with
    neither variable set gets the original `NeedsOcrError` (unchanged
    behavior) instead of an Azure-specific error about a service they
    never asked for."""
    return bool(os.environ.get(ENDPOINT_ENV) or os.environ.get(KEY_ENV))


class AzureDiClient:
    """One Azure `prebuilt-layout` call per page. Construct via `from_env`,
    not directly -- that's where config validation and the optional-import
    check happen."""

    def __init__(self, client):
        self._client = client

    @classmethod
    def from_env(cls) -> "AzureDiClient":
        endpoint = os.environ.get(ENDPOINT_ENV)
        key = os.environ.get(KEY_ENV)
        if not endpoint or not key:
            raise ClientConfigError(
                f"Azure Document Intelligence needs both {ENDPOINT_ENV} and {KEY_ENV} set; only one is"
            )
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:
            raise ClientConfigError(
                "Azure Document Intelligence OCR requires the 'azure' extra: "
                "pip install firecrawl-anydoc[azure]"
            ) from exc
        return cls(DocumentIntelligenceClient(endpoint, AzureKeyCredential(key)))

    def ocr_page(self, page_pdf_bytes: bytes) -> str:
        # output_content_format=MARKDOWN returns one joined string per job
        # with no documented per-page span boundary -- the caller is
        # responsible for only ever passing a single page's bytes here,
        # never a multi-page document, or a combined result can't be
        # safely split back apart afterward.
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat

        poller = self._client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(bytes_source=page_pdf_bytes),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        return poller.result().content
