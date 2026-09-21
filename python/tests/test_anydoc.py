"""Smoke test: the bindings load and every entry point round-trips a fixture."""

import ast
import io
import json
import os
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anydoc

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
OUTLINE = FIXTURES / "docx" / "handmade-outline.docx"
RICH = FIXTURES / "docx" / "handmade-rich.docx"
CSV = FIXTURES / "csv" / "sheet.csv"
ENCRYPTED = FIXTURES / "malformed" / "encrypted--errors.odt"
ZIPBOMB = FIXTURES / "abuse" / "zipbomb--errors.docx"
MIXED = FIXTURES / "pdf" / "handmade-mixed.pdf"

HOSTED_MARKDOWN = "# Read by the hosted parser\n"

try:
    import azure.ai.documentintelligence  # noqa: F401

    _AZURE_EXTRA_INSTALLED = True
except ImportError:
    _AZURE_EXTRA_INSTALLED = False


@contextmanager
def hosted_stub(status, body):
    """A stand-in for api.firecrawl.dev that answers every request with one
    reply and records each hit as (path, whether a PDF came with it). The
    block runs keyless, whatever the environment."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers.get("content-length", 0)))
            hits.append((self.path, b"%PDF-" in payload))
            reply = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = {name: os.environ.pop(name, None) for name in ("FIRECRAWL_API_URL", "FIRECRAWL_API_KEY")}
    os.environ["FIRECRAWL_API_URL"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield hits
    finally:
        server.shutdown()
        server.server_close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def azure_env(endpoint="https://example.cognitiveservices.azure.com/", key="fake-key"):
    """Sets (or, if None, clears) the two Azure env vars, restoring whatever
    was there after."""
    names = ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_KEY")
    saved = {name: os.environ.pop(name, None) for name in names}
    if endpoint is not None:
        os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"] = endpoint
    if key is not None:
        os.environ["AZURE_DOCUMENT_INTELLIGENCE_KEY"] = key
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def azure_stub(analyze):
    """A stand-in for `DocumentIntelligenceClient`, patched at its origin
    module so `_analyze_page`'s lazy import picks it up. `analyze(page_bytes)
    -> str` runs once per Azure call with the single-page PDF bytes
    `_analyze_page` actually built and sent (real `_single_page_pdf`
    extraction is not mocked, so it's exercised for real); its return value
    becomes that call's `result.content`, or if it raises, that call fails.
    Yields the list of each call's (start, end) wall-clock span, for
    checking real concurrency rather than just "it was fast"."""
    import azure.ai.documentintelligence as adi

    spans = []
    lock = threading.Lock()

    class _FakePoller:
        def __init__(self, content):
            self._content = content

        def result(self):
            return SimpleNamespace(content=self._content)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def begin_analyze_document(self, model, request, **kwargs):
            start = time.monotonic()
            content = analyze(request.bytes_source)
            with lock:
                spans.append((start, time.monotonic()))
            return _FakePoller(content)

    with patch.object(adi, "DocumentIntelligenceClient", _FakeClient):
        yield spans


def _blank_pdf(num_pages: int) -> bytes:
    """A real, valid multi-page PDF with no content -- enough for
    `_single_page_pdf` to slice and `pdf_inspector` to read, without needing
    a committed fixture file. Content is irrelevant to the orchestration/
    concurrency tests that use this; the Azure call itself is always mocked."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _max_overlap(spans: list[tuple[float, float]]) -> int:
    """Max number of (start, end) intervals active at the same instant."""
    events = sorted((t, delta) for start, end in spans for t, delta in ((start, 1), (end, -1)))
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


class AnydocTest(unittest.TestCase):
    def test_to_markdown_detects_the_format_from_the_file_content(self):
        markdown = anydoc.to_markdown(OUTLINE)
        self.assertRegex(markdown, r"(?m)^# ")

    def test_to_markdown_bytes_converts_in_memory(self):
        markdown = anydoc.to_markdown_bytes(RICH.read_bytes(), "docx")
        self.assertIn("| Quarter | Widgets |", markdown)

    def test_to_markdown_bytes_detects_the_format_when_none_is_named(self):
        markdown = anydoc.to_markdown_bytes(RICH.read_bytes())
        self.assertIn("| Quarter | Widgets |", markdown)
        # CSV carries no signature, so it has to be named.
        with self.assertRaisesRegex(anydoc.ConvertError, "unrecognized file content"):
            anydoc.to_markdown_bytes(CSV.read_bytes())
        self.assertIn("| --- |", anydoc.to_markdown_bytes(CSV.read_bytes(), "csv"))

    def test_to_document_exposes_the_document_model(self):
        document = anydoc.to_document(OUTLINE.read_bytes(), "docx")
        heading = next(block for block in document.blocks if block.kind == "heading")
        self.assertTrue(1 <= heading.level <= 6)
        self.assertIsInstance(heading.content[0].text, str)
        self.assertEqual(heading.content[0].kind, "text")
        self.assertIsInstance(heading.content[0].style.bold, bool)

    def test_to_document_carries_embedded_assets_as_bytes(self):
        document = anydoc.to_document(RICH.read_bytes(), "docx")
        image = next(asset for asset in document.assets if asset.media_type == "image/png")
        self.assertIsInstance(image.data, bytes)
        self.assertGreater(len(image.data), 0)
        self.assertEqual(image.id, document.assets.index(image))

    def test_format_detection_reads_content_extension_and_path(self):
        self.assertEqual(anydoc.format_from_bytes(RICH.read_bytes()), "docx")
        # CSV carries no signature: only the extension names it.
        self.assertIsNone(anydoc.format_from_bytes(CSV.read_bytes()))
        self.assertEqual(anydoc.format_from_extension(".pptm"), "pptx")
        self.assertEqual(anydoc.format_from_extension("xls"), "xlsx")
        self.assertEqual(anydoc.format_from_path("report.odt"), "odt")
        self.assertIsNone(anydoc.format_from_path("report.unknown"))

    def test_conversion_errors_raise_the_subclass_that_names_the_failure(self):
        with self.assertRaises(anydoc.MalformedError) as caught:
            anydoc.to_markdown_bytes(b"not a document", "docx")
        # The base class still catches every one of them.
        self.assertIsInstance(caught.exception, anydoc.ConvertError)
        # Nothing about these bytes is a package part.
        self.assertIsNone(caught.exception.part)

        with self.assertRaises(anydoc.UnsupportedError):
            anydoc.to_markdown_bytes(CSV.read_bytes())

        with self.assertRaises(anydoc.EncryptedError):
            anydoc.to_markdown_bytes(ENCRYPTED.read_bytes(), "odt")

        # A scanned page is reported, not dropped from the output.
        with self.assertRaises(anydoc.NeedsOcrError) as caught:
            anydoc.to_markdown(MIXED)
        self.assertEqual((caught.exception.pages, caught.exception.page_count), ([2], 2))

        with self.assertRaises(anydoc.ResourceLimitError) as caught:
            anydoc.to_markdown_bytes(ZIPBOMB.read_bytes(), "docx")
        self.assertEqual(caught.exception.limit, "max_entry_bytes")

        # A readable package carrying none of the parts a docx is made of.
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
        with self.assertRaises(anydoc.MissingPartError) as caught:
            anydoc.to_markdown_bytes(package.getvalue(), "docx")
        self.assertEqual(caught.exception.part, "word/document.xml")

    def test_ocr_hosted_sends_a_pdf_with_scanned_pages_to_firecrawl_parse_and_nothing_else(self):
        reply = {"success": True, "data": {"markdown": HOSTED_MARKDOWN}}
        with hosted_stub(200, reply) as hits:
            self.assertEqual(anydoc.to_markdown(MIXED, ocr="hosted"), HOSTED_MARKDOWN)
            self.assertEqual(hits, [("/v2/parse", True)])
            self.assertRegex(anydoc.to_markdown(OUTLINE, ocr="hosted"), r"(?m)^# ")
            self.assertEqual(hits, [("/v2/parse", True)])

    def test_the_keyless_limit_says_to_set_an_api_key(self):
        with hosted_stub(429, {"success": False, "error": "Rate limit exceeded"}):
            with self.assertRaisesRegex(anydoc.HostedError, "set FIRECRAWL_API_KEY"):
                anydoc.to_markdown_bytes(MIXED.read_bytes(), ocr="hosted")

    def test_unreadable_files_and_bad_arguments_raise_the_python_exception(self):
        with self.assertRaises(FileNotFoundError):
            anydoc.to_markdown("no-such-file.docx")
        with self.assertRaisesRegex(ValueError, "unknown format"):
            anydoc.to_markdown_bytes(b"", "wat")

    def test_the_stubs_cover_the_module(self):
        stub = Path(anydoc.__file__).with_name("_anydoc.pyi")
        stubbed = {
            node.name
            for node in ast.parse(stub.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        exported = {name for name in dir(anydoc._anydoc) if not name.startswith("_")}
        self.assertEqual(stubbed, exported)
        # __init__.py re-exports the whole module, plus what it adds itself.
        self.assertEqual(set(anydoc.__all__), exported | {"Format", "HostedError", "AzureError", "Ocr"})


@unittest.skipUnless(_AZURE_EXTRA_INSTALLED, "azure extra not installed")
class AzureOcrTest(unittest.TestCase):
    """Layer 2a/2b from the Azure OCR test plan: our own dispatch logic,
    mocked at the `DocumentIntelligenceClient` boundary. Never calls the
    real Azure service -- Layer 2c (real Azure behavior) is a separate,
    deliberately deferred question, not covered here."""

    def test_no_azure_config_raises_the_original_needs_ocr_error_unchanged(self):
        with azure_env(endpoint=None, key=None):
            with self.assertRaises(anydoc.NeedsOcrError) as caught:
                anydoc.to_markdown(MIXED)
            self.assertEqual(caught.exception.pages, [2])

    def test_partial_config_raises_azure_error_immediately(self):
        with azure_env(endpoint="https://example.cognitiveservices.azure.com/", key=None):
            with self.assertRaisesRegex(anydoc.AzureError, "both.*set; only one is"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())
        with azure_env(endpoint=None, key="fake-key"):
            with self.assertRaisesRegex(anydoc.AzureError, "both.*set; only one is"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())

    def test_ocr_hosted_wins_even_when_azure_is_configured(self):
        reply = {"success": True, "data": {"markdown": HOSTED_MARKDOWN}}
        with azure_env(), hosted_stub(200, reply) as hits, patch("anydoc._parse_azure") as mock_azure:
            result = anydoc.to_markdown(MIXED, ocr="hosted")
            self.assertEqual(result, HOSTED_MARKDOWN)
            mock_azure.assert_not_called()
            self.assertEqual(hits, [("/v2/parse", True)])

    def test_azure_replaces_only_the_flagged_page(self):
        with azure_env(), azure_stub(lambda page_bytes: "AZURE OCR TEXT\n"):
            result = anydoc.to_markdown_bytes(MIXED.read_bytes())
        self.assertIn("Text on the first page", result)  # untouched native page
        self.assertIn("AZURE OCR TEXT", result)  # the flagged page, replaced

    def test_extra_not_installed_raises_a_clean_azure_error(self):
        with azure_env(), patch.dict("sys.modules", {"azure.ai.documentintelligence": None}):
            with self.assertRaisesRegex(anydoc.AzureError, r"pip install firecrawl-anydoc\[azure\]"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())

    def test_page_number_outside_the_document_raises_azure_error_not_indexerror(self):
        fake_error = SimpleNamespace(pages=[99], page_count=2)
        with azure_env(), azure_stub(lambda page_bytes: "irrelevant"):
            with self.assertRaises(anydoc.AzureError) as caught:
                anydoc._parse_azure(MIXED.read_bytes(), fake_error)
            self.assertNotIsInstance(caught.exception, IndexError)

    def test_empty_pages_list_returns_the_document_unchanged(self):
        fake_error = SimpleNamespace(pages=[], page_count=2)

        def fail_if_called(page_bytes):
            self.fail("should not call Azure for an empty page list")

        with azure_env(), azure_stub(fail_if_called):
            result = anydoc._parse_azure(MIXED.read_bytes(), fake_error)
        self.assertIn("Text on the first page", result)

    def test_pages_dispatch_in_parallel_and_none_are_dropped(self):
        n = 10  # > max_workers (8), so this also covers the queueing case
        data = _blank_pdf(n)
        fake_error = SimpleNamespace(pages=list(range(1, n + 1)), page_count=n)

        def slow_analyze(page_bytes):
            time.sleep(0.2)
            return "ok"

        with azure_env():
            t0 = time.monotonic()
            with azure_stub(slow_analyze) as spans:
                anydoc._parse_azure(data, fake_error)
            elapsed = time.monotonic() - t0

        self.assertEqual(len(spans), n)  # every page dispatched exactly once
        # Serial would take n * 0.2s = 2.0s; 8-way parallel should finish in
        # two batches, ~0.4s. Generous bound against CI jitter.
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s -- looks serial, not parallel")
        self.assertGreater(_max_overlap(spans), 1, "no real overlap between calls")

    def test_one_page_failing_fails_the_whole_call(self):
        data = _blank_pdf(3)
        fake_error = SimpleNamespace(pages=[1, 2, 3], page_count=3)
        calls = {"n": 0}
        lock = threading.Lock()

        def flaky_analyze(page_bytes):
            with lock:
                calls["n"] += 1
                this_call = calls["n"]
            if this_call == 2:
                raise RuntimeError("simulated Azure failure")
            return "ok"

        with azure_env(), azure_stub(flaky_analyze):
            with self.assertRaisesRegex(anydoc.AzureError, "simulated Azure failure"):
                anydoc._parse_azure(data, fake_error)

    def test_a_still_oversized_single_page_raises_a_clean_azure_error(self):
        """_single_page_pdf fixes the whole-document size limit (confirmed
        live against a real 5.1MB document -- see _single_page_pdf's
        docstring), but a single page can in principle still be too large
        on its own after extraction. A real oversized fixture is
        disproportionate for a unit test, so this simulates Azure's actual
        observed rejection shape for that case (HttpResponseError,
        InvalidContentLength -- the exact error hit live earlier against the
        unfixed whole-document case) and confirms it still surfaces as a
        clean AzureError, not a raw azure.core exception leaking through."""
        from azure.core.exceptions import HttpResponseError

        def oversized_rejection(page_bytes):
            raise HttpResponseError("(InvalidRequest) Invalid request. InvalidContentLength: The input image is too large.")

        fake_error = SimpleNamespace(pages=[2], page_count=2)
        with azure_env(), azure_stub(oversized_rejection):
            with self.assertRaises(anydoc.AzureError) as caught:
                anydoc._parse_azure(MIXED.read_bytes(), fake_error)
            self.assertNotIsInstance(caught.exception, HttpResponseError)
            self.assertIn("InvalidContentLength", str(caught.exception))

    def test_verify_single_page_accepts_one_page_rejects_more(self):
        """Unit test of ocr_clients.base.verify_single_page in isolation,
        against real PDF bytes -- not mocked, since pypdf's own page count
        is exactly what's being trusted here."""
        from anydoc.ocr_clients.base import verify_single_page

        verify_single_page(_blank_pdf(1))  # does not raise

        with self.assertRaisesRegex(ValueError, "expected exactly one page, got 2"):
            verify_single_page(_blank_pdf(2))

    def test_a_regressed_single_page_pdf_fails_the_call_instead_of_silently_corrupting(self):
        """The actual point of verify_single_page: if _single_page_pdf (or
        any future replacement) ever regressed to producing more than one
        page, dispatch must fail loudly, not silently merge a multi-page
        Azure result into a single array slot. Proven by making
        _single_page_pdf actually misbehave (not simulated at a distance),
        confirming both that it fails, and that it fails as AzureError."""
        fake_error = SimpleNamespace(pages=[2], page_count=2)

        with azure_env(), azure_stub(lambda page_bytes: "should never be reached"):
            with patch("anydoc._single_page_pdf", return_value=_blank_pdf(2)):
                with self.assertRaisesRegex(anydoc.AzureError, "expected exactly one page, got 2"):
                    anydoc._parse_azure(MIXED.read_bytes(), fake_error)


if __name__ == "__main__":
    unittest.main()
