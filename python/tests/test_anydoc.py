"""Smoke test: the bindings load and every entry point round-trips a fixture."""

import ast
import io
import json
import os
import re
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

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
OUTLINE = FIXTURES / "docx" / "handmade-outline.docx"
RICH = FIXTURES / "docx" / "handmade-rich.docx"
CSV = FIXTURES / "csv" / "sheet.csv"
ENCRYPTED = FIXTURES / "malformed" / "encrypted--errors.odt"
ZIPBOMB = FIXTURES / "abuse" / "zipbomb--errors.docx"
MIXED = FIXTURES / "pdf" / "handmade-mixed.pdf"
OUTLINED = FIXTURES / "pdf" / "handmade-outlined.pdf"

HOSTED_MARKDOWN = "# Read by the hosted parser\n"

try:
    import azure.ai.documentintelligence  # noqa: F401

    _AZURE_EXTRA_INSTALLED = True
except ImportError:
    _AZURE_EXTRA_INSTALLED = False

try:
    import pdf_inspector  # noqa: F401

    _PDF_INSPECTOR_INSTALLED = True
except ImportError:
    _PDF_INSPECTOR_INSTALLED = False

# page_health needs the extra's PDF libraries but not the Azure SDK, so it
# gets its own guard rather than riding on _AZURE_EXTRA_INSTALLED. It needs
# pypdf on top of what the pin check above already probed.
try:
    import pypdf  # noqa: F401

    _PDF_LIBS_INSTALLED = _PDF_INSPECTOR_INSTALLED
except ImportError:
    _PDF_LIBS_INSTALLED = False


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


# Synthetic page geometry. A page's *width* encodes its 1-indexed page
# number -- page N is `_PAGE_WIDTH_BASE + N` points wide -- which is what lets
# a stub identify which page it was handed without depending on any text
# extraction. The height is fixed and carries no meaning.
_PAGE_WIDTH_BASE = 200
_PAGE_HEIGHT = 200


def _blank_pdf(num_pages: int) -> bytes:
    """A real, valid multi-page PDF with no content -- enough for
    `_single_page_pdf` to slice and `pdf_inspector` to read, without needing
    a committed fixture file. Content is irrelevant to the orchestration/
    concurrency tests that use this; the Azure call itself is always mocked."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=_PAGE_WIDTH_BASE, height=_PAGE_HEIGHT)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _numbered_pdf(num_pages: int) -> bytes:
    """A real multi-page PDF whose pages are telling apart by size: page N is
    (200 + N) points wide. That is what lets a stub know which page it was
    handed, without depending on any text extraction."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for number in range(1, num_pages + 1):
        writer.add_blank_page(width=_PAGE_WIDTH_BASE + number, height=_PAGE_HEIGHT)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _page_number_of(page_pdf_bytes: bytes) -> int:
    """The page number `_numbered_pdf` encoded in this single page's width."""
    from pypdf import PdfReader

    width = int(PdfReader(io.BytesIO(page_pdf_bytes)).pages[0].mediabox.width)
    return width - _PAGE_WIDTH_BASE


def _max_overlap(spans: list[tuple[float, float]]) -> int:
    """Max number of (start, end) intervals active at the same instant."""
    events = sorted((t, delta) for start, end in spans for t, delta in ((start, 1), (end, -1)))
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


class ForkPinTest(unittest.TestCase):
    """`pdf-inspector` is consumed twice and the two consumers resolve
    independently: the Rust core links the crate, redirected by
    `Cargo.toml`'s `[patch.crates-io]`, and the OCR dispatch imports the
    Python package declared in `pyproject.toml`'s `ocr` extra.

    When they disagreed, nothing raised. The crate carried the RTL fix, the
    Python package came from PyPI without it, and Hebrew came back
    character-reversed from whichever path went through Python -- in the fork
    that exists to prevent exactly that. FORK.md answers it with a rule,
    "move both together, always"; these tests are that rule enforced, so a
    bump that touches one pin fails the build instead of a document."""

    CRATE_PIN = re.compile(
        r"""pdf-inspector\s*=\s*\{[^}]*?tag\s*=\s*["']([^"']+)["']""", re.S
    )
    EXTRA_PIN = re.compile(r"""pdf-inspector\s*@\s*git\+(\S+?)@([^"'\s]+)""")

    def test_the_crate_and_the_python_package_name_the_same_fork_tag(self):
        """Static, so it runs everywhere and needs nothing installed. This is
        the one that catches a half-finished bump at review time."""
        crate = self.CRATE_PIN.search((REPO / "Cargo.toml").read_text())
        self.assertIsNotNone(crate, "no [patch.crates-io] tag found in Cargo.toml")
        extra = self.EXTRA_PIN.search((REPO / "python" / "pyproject.toml").read_text())
        self.assertIsNotNone(extra, "no pdf-inspector pin found in the ocr extra")
        url, extra_tag = extra.groups()
        self.assertEqual(
            extra_tag,
            crate.group(1),
            "the ocr extra and [patch.crates-io] name different fork tags; one was "
            "bumped without the other, which is how Hebrew came back reversed",
        )
        self.assertIn("SaarBarak/pdf-inspector", url)

    @unittest.skipUnless(_PDF_INSPECTOR_INSTALLED, "pdf-inspector not installed")
    def test_the_installed_package_came_from_that_fork_tag(self):
        """The environment, not the declaration. A venv can be stale: a
        Hebrew check once "failed" against an install still holding PyPI
        1.20.0, long after both pins were correct."""
        from importlib.metadata import distribution

        crate_tag = self.CRATE_PIN.search((REPO / "Cargo.toml").read_text()).group(1)
        raw = distribution("pdf-inspector").read_text("direct_url.json")
        self.assertIsNotNone(
            raw,
            "pdf-inspector has no direct_url.json, so it came from PyPI -- that is "
            "upstream, without the RTL fix. Reinstall the ocr extra.",
        )
        info = json.loads(raw)
        self.assertIn("SaarBarak/pdf-inspector", info["url"])
        self.assertEqual(
            info.get("vcs_info", {}).get("requested_revision"),
            crate_tag,
            "the installed pdf-inspector is a different fork tag from the crate",
        )


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
        self.assertEqual(set(anydoc.__all__), exported | {"Format", "HostedError", "OcrError", "Ocr"})


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
            with self.assertRaisesRegex(anydoc.OcrError, "both.*set; only one is"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())
        with azure_env(endpoint=None, key="fake-key"):
            with self.assertRaisesRegex(anydoc.OcrError, "both.*set; only one is"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())

    def test_ocr_hosted_wins_even_when_azure_is_configured(self):
        reply = {"success": True, "data": {"markdown": HOSTED_MARKDOWN}}
        with azure_env(), hosted_stub(200, reply) as hits, patch("anydoc._parse_ocr") as mock_azure:
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
            with self.assertRaisesRegex(anydoc.OcrError, r"pip install firecrawl-anydoc\[azure\]"):
                anydoc.to_markdown_bytes(MIXED.read_bytes())

    def test_page_number_outside_the_document_raises_azure_error_not_indexerror(self):
        with azure_env(), azure_stub(lambda page_bytes: "irrelevant"):
            with self.assertRaises(anydoc.OcrError) as caught:
                anydoc._parse_ocr(MIXED.read_bytes(), [99])
            self.assertNotIsInstance(caught.exception, IndexError)

    def test_empty_pages_list_returns_the_document_unchanged(self):

        def fail_if_called(page_bytes):
            self.fail("should not call Azure for an empty page list")

        with azure_env(), azure_stub(fail_if_called):
            result = anydoc._parse_ocr(MIXED.read_bytes(), [])
        self.assertIn("Text on the first page", result)

    # Two more pages than the pool has workers, so the run needs a second
    # batch and the queueing path is covered too. Derived from the real
    # constant: raising `_MAX_WORKERS` must not quietly stop testing queueing.
    CONCURRENCY_EXTRA_PAGES = 2
    PER_CALL_SECONDS = 0.2

    def test_pages_dispatch_in_parallel_and_none_are_dropped(self):
        page_count = anydoc._MAX_WORKERS + self.CONCURRENCY_EXTRA_PAGES
        pages = list(range(1, page_count + 1))
        # Two batches of work, plus generous slack for a loaded machine. Serial
        # would take `page_count` calls end to end, which is far above this.
        batches = -(-page_count // anydoc._MAX_WORKERS)
        serial_seconds = page_count * self.PER_CALL_SECONDS
        parallel_ceiling = batches * self.PER_CALL_SECONDS * 2.5
        self.assertLess(parallel_ceiling, serial_seconds, "the bound cannot tell the two apart")

        def slow_analyze(page_bytes):
            time.sleep(self.PER_CALL_SECONDS)
            return "ok"

        with azure_env():
            t0 = time.monotonic()
            with azure_stub(slow_analyze) as spans:
                anydoc._parse_ocr(_blank_pdf(page_count), pages)
            elapsed = time.monotonic() - t0

        self.assertEqual(len(spans), page_count)  # every page dispatched exactly once
        self.assertLess(
            elapsed, parallel_ceiling, f"took {elapsed:.2f}s -- looks serial, not parallel"
        )
        self.assertGreater(_max_overlap(spans), 1, "no real overlap between calls")

    # Enough pages to interleave without making the test slow. Page 1 waits
    # `PAGES * STEP` and page N waits `STEP`, so completion order is the exact
    # reverse of dispatch order -- the worst case for a merge that trusted it.
    ORDERING_PAGES = 5
    ORDERING_DELAY_STEP_SECONDS = 0.05

    def test_results_land_by_page_number_even_when_they_finish_backwards(self):
        """Pages are OCR'd one per call but eight at a time, so they finish in
        whatever order the service returns them. That must not be able to
        reorder the document.

        Two things make it safe, and this pins both: `pool.map` yields results
        in submission order regardless of completion order, and the merge
        writes each result to `merged[page_num - 1]` rather than appending.
        Here the stub finishes page 5 first and page 1 last -- the exact
        inversion -- and the document must still read 1, 2, 3, 4, 5."""
        pages = list(range(1, self.ORDERING_PAGES + 1))
        finished = []

        def slow_for_early_pages(page_bytes):
            page_num = _page_number_of(page_bytes)
            waits = self.ORDERING_PAGES + 1 - page_num  # page 1 waits longest
            time.sleep(self.ORDERING_DELAY_STEP_SECONDS * waits)
            finished.append(page_num)
            return f"OCR-{page_num}\n"

        with azure_env(), azure_stub(slow_for_early_pages):
            result = anydoc._parse_ocr(_numbered_pdf(self.ORDERING_PAGES), pages)

        self.assertEqual(finished, sorted(pages, reverse=True), "the stub did not finish backwards")
        positions = [result.index(f"OCR-{n}") for n in pages]
        self.assertEqual(positions, sorted(positions), f"pages came out scrambled: {result!r}")

    def test_one_page_failing_fails_the_whole_call(self):
        data = _blank_pdf(3)
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
            with self.assertRaisesRegex(anydoc.OcrError, "simulated Azure failure"):
                anydoc._parse_ocr(data, [1, 2, 3])

    def test_a_still_oversized_single_page_raises_a_clean_azure_error(self):
        """_single_page_pdf fixes the whole-document size limit (confirmed
        live against a real 5.1MB document -- see _single_page_pdf's
        docstring), but a single page can in principle still be too large
        on its own after extraction. A real oversized fixture is
        disproportionate for a unit test, so this simulates Azure's actual
        observed rejection shape for that case (HttpResponseError,
        InvalidContentLength -- the exact error hit live earlier against the
        unfixed whole-document case) and confirms it still surfaces as a
        clean OcrError, not a raw azure.core exception leaking through."""
        from azure.core.exceptions import HttpResponseError

        def oversized_rejection(page_bytes):
            raise HttpResponseError("(InvalidRequest) Invalid request. InvalidContentLength: The input image is too large.")

        with azure_env(), azure_stub(oversized_rejection):
            with self.assertRaises(anydoc.OcrError) as caught:
                anydoc._parse_ocr(MIXED.read_bytes(), [2])
            self.assertNotIsInstance(caught.exception, HttpResponseError)
            self.assertIn("InvalidContentLength", str(caught.exception))

    def _fake_pages(self, *specs):
        """Stands in for `extract_pages_markdown_bytes(...).pages`. Each spec
        is (markdown, ocr_reason); pages are numbered 0-indexed in order, as
        pdf-inspector numbers them."""
        pages = [
            SimpleNamespace(page=index, markdown=markdown, needs_ocr=not markdown.strip(), ocr_reason=reason)
            for index, (markdown, reason) in enumerate(specs)
        ]
        return SimpleNamespace(pages=pages)

    def test_an_unflagged_wiped_page_fails_the_call_instead_of_merging_empty(self):
        """`extract_pages_markdown_bytes` blanks a page whose text it
        distrusts. Until page health routes those pages to OCR they are not
        in `error.pages`, so merging would silently drop real content -- the
        one outcome this path must never produce. It must fail loudly, and
        it must fail before paying Azure for the other pages."""
        import pdf_inspector

        pages = self._fake_pages(("", None), ("native text\n", None), ("", "vector_text"))

        with azure_env(), azure_stub(lambda page_bytes: self.fail("should not reach Azure")):
            with patch.object(pdf_inspector, "extract_pages_markdown_bytes", return_value=pages):
                with self.assertRaisesRegex(anydoc.OcrError, r"pages \[3\].*vector_text"):
                    anydoc._parse_ocr(_blank_pdf(3), [1])

    def test_a_genuinely_blank_page_is_not_mistaken_for_a_wiped_one(self):
        """A blank page reports `needs_ocr` too, but states no reason. The
        guard above must not reject a document for containing one."""
        import pdf_inspector

        pages = self._fake_pages(("", None), ("native text\n", None), ("", None))

        with azure_env(), azure_stub(lambda page_bytes: "OCR OF PAGE ONE\n"):
            with patch.object(pdf_inspector, "extract_pages_markdown_bytes", return_value=pages):
                result = anydoc._parse_ocr(_blank_pdf(3), [1])
        self.assertIn("OCR OF PAGE ONE", result)
        self.assertIn("native text", result)

    def test_engine_selection_is_by_environment_and_names_no_engine_when_unset(self):
        """`requested()` is the only thing that knows more than one engine
        exists. With nothing configured it must return None so `to_markdown`
        re-raises the original `NeedsOcrError` -- the unchanged-behaviour
        promise for every caller who never asked for OCR."""
        from anydoc.ocr_clients import requested

        with azure_env(endpoint=None, key=None):
            self.assertIsNone(requested())
        with azure_env():
            self.assertIs(requested(), __import__("anydoc.ocr_clients.azure_di", fromlist=["x"]))

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
        confirming both that it fails, and that it fails as OcrError."""

        with azure_env(), azure_stub(lambda page_bytes: "should never be reached"):
            with patch("anydoc._single_page_pdf", return_value=_blank_pdf(2)):
                with self.assertRaisesRegex(anydoc.OcrError, "expected exactly one page, got 2"):
                    anydoc._parse_ocr(MIXED.read_bytes(), [2])


@unittest.skipUnless(_PDF_LIBS_INSTALLED, "pdf-inspector/pypdf not installed")
@unittest.skipUnless(_AZURE_EXTRA_INSTALLED, "azure extra not installed")
class RoutingTest(unittest.TestCase):
    """Page health decides which pages go to OCR; `_parse_ocr` merges whatever
    it is handed. These cover the seam between them."""

    def test_a_conversion_that_did_not_raise_is_still_scanned_and_routed(self):
        """The case the whole feature exists for, and the easiest to leave
        untested: the core's check fires only when a page yields nothing at
        all, so a page whose glyphs became outlines renders perfectly,
        extracts as nothing, and never raises. The real 95-page tender does
        exactly this -- `pages_needing_ocr: []`, converts "successfully", 14
        pages short.

        No committed fixture reproduces it. The handmade PDFs all trip
        `NeedsOcrError` (`handmade-outlined` raises for pages 2-3), because
        none of them reproduces the *detection* short-circuit that makes the
        tender succeed: `PdfType::TextBased` hard-codes `pages_needing_ocr`
        empty without running a per-page check. So the clean conversion is
        simulated here, and the real document was verified by hand -- 53
        pages routed, page 25 recovered. A fixture that reproduces it would
        be worth having."""
        import anydoc.page_health as page_health

        data = FIXTURES / "pdf" / "text.pdf"
        health = [SimpleNamespace(page=1, needs_ocr=True)]
        with azure_env():
            with patch("anydoc._to_markdown_bytes", return_value="native\n"):
                with patch.object(page_health, "scan_pdf_health", return_value=health):
                    with azure_stub(lambda page_bytes: "OCR TEXT\n") as spans:
                        result = anydoc.to_markdown_bytes(data.read_bytes())
        self.assertEqual(len(spans), 1, "a clean conversion was not routed")
        self.assertIn("OCR TEXT", result)

    def test_nothing_is_scanned_when_no_engine_is_configured(self):
        """The zero-cost promise: a caller who never asked for OCR pays for no
        scan and gets byte-identical output."""
        import anydoc.page_health as page_health

        data = (FIXTURES / "pdf" / "text.pdf").read_bytes()
        with azure_env(endpoint=None, key=None):
            with patch.object(page_health, "scan_pdf_health") as scan:
                before = anydoc.to_markdown_bytes(data)
                scan.assert_not_called()
            after = anydoc.to_markdown_bytes(data)
        self.assertEqual(before, after)

    def test_a_non_pdf_is_never_scanned(self):
        """`NeedsOcr` is raised in one place in the Rust core, and page health
        reads PDF content streams. Neither applies to a docx."""
        import anydoc.page_health as page_health

        with azure_env():
            with patch.object(page_health, "scan_pdf_health") as scan:
                anydoc.to_markdown_bytes(RICH.read_bytes())
                scan.assert_not_called()

    def test_a_failing_scan_does_not_break_a_working_conversion(self):
        """Fails open, deliberately. A scan is an opinion about a document
        that already converted; a bug in forming that opinion must not take
        the conversion down with it."""
        import anydoc.page_health as page_health

        data = (FIXTURES / "pdf" / "text.pdf").read_bytes()
        with azure_env():
            with patch.object(page_health, "scan_pdf_health", side_effect=RuntimeError("scan bug")):
                with azure_stub(lambda page_bytes: self.fail("should not reach Azure")):
                    result = anydoc.to_markdown_bytes(data)
        self.assertEqual(result, anydoc.to_markdown_bytes(data))

    def test_the_two_detectors_are_unioned_not_replaced(self):
        """The core catches a page with no text at all; page health catches a
        page that renders perfectly and extracts as nothing. A document with
        both must send both, or whichever detector ran second silently wins.
        `handmade-mixed` raises for its scanned page 2; health is made to
        flag page 1 as well."""
        import anydoc.page_health as page_health

        health = [SimpleNamespace(page=1, needs_ocr=True), SimpleNamespace(page=2, needs_ocr=False)]
        with azure_env():
            with patch.object(page_health, "scan_pdf_health", return_value=health):
                with azure_stub(lambda page_bytes: "OCR\n") as spans:
                    anydoc.to_markdown_bytes(MIXED.read_bytes())
        self.assertEqual(len(spans), 2, "expected the core's page 2 and health's page 1")

    def test_hosted_still_wins_and_skips_the_scan_entirely(self):
        import anydoc.page_health as page_health

        reply = {"success": True, "data": {"markdown": HOSTED_MARKDOWN}}
        with azure_env(), hosted_stub(200, reply):
            with patch.object(page_health, "scan_pdf_health") as scan:
                result = anydoc.to_markdown(MIXED, ocr="hosted")
                scan.assert_not_called()
        self.assertEqual(result, HOSTED_MARKDOWN)


class PageHealthTest(unittest.TestCase):
    """Per-page extraction-health signals.

    `handmade-outlined.pdf` is three pages built for this: a clean text page,
    a page whose body is drawn as filled Bezier curves (text converted to
    outlines, unrecoverable without OCR), and a heavily ruled table page that
    loses nothing. The ruled page is the control -- a curve-based signal that
    also fires on table borders is useless on the documents we actually
    ingest, which are wall-to-wall ruled tables."""

    @classmethod
    def setUpClass(cls):
        from anydoc.page_health import scan_pdf_health

        cls.pages = scan_pdf_health(OUTLINED.read_bytes())

    def test_the_ratio_counts_only_characters_that_failed_to_map(self):
        from anydoc.page_health import cmap_corruption_ratio

        self.assertEqual(cmap_corruption_ratio(""), 0.0)
        self.assertEqual(cmap_corruption_ratio("ordinary prose"), 0.0)
        # Hebrew is not corruption; a signal that flags it would route every
        # RTL document to OCR.
        self.assertEqual(cmap_corruption_ratio("עיריית תל אביב"), 0.0)
        self.assertGreater(cmap_corruption_ratio("abd"), 0.0)
        self.assertGreater(cmap_corruption_ratio("ab�d"), 0.0)
        # A (cid:N) token is ten characters of junk in the output, not one,
        # so it is counted by length.
        self.assertGreater(cmap_corruption_ratio("(cid:45)xy"), 0.5)

    def test_a_clean_page_raises_nothing(self):
        clean = self.pages[0]
        self.assertEqual(clean.curve_ops, 0)
        self.assertEqual(clean.reasons, [])
        self.assertFalse(clean.needs_ocr)

    def test_outlined_text_is_flagged_and_routes_to_ocr(self):
        outlined = self.pages[1]
        self.assertGreaterEqual(outlined.curve_ops, 500)
        self.assertIn("outlined_text", outlined.reasons)
        self.assertTrue(outlined.needs_ocr)

    def test_a_ruled_table_page_is_not_mistaken_for_outlined_text(self):
        """The control, and the reason B is usable on a corpus of ruled
        tables at all: table rules are straight lines and rectangles and emit
        no curve operator, so the same quantity of vector ink drawn as borders
        must not look like glyphs converted to paths.

        This page does route to OCR -- pdf-inspector wipes it, so C fires --
        but that is a different signal reaching a different conclusion for a
        stated reason. What must never happen is B firing here."""
        ruled = self.pages[2]
        self.assertEqual(ruled.curve_ops, 0)
        self.assertNotIn("outlined_text", ruled.reasons)
        self.assertEqual(ruled.reasons, ["markdown_wiped"])

    def test_a_wiped_page_routes_to_ocr_rather_than_being_rebuilt(self):
        """pdf-inspector blanks a page's Markdown whenever its own per-page
        check fires, discarding good body text with it (upstream #252/#342).
        The characters are still reachable through the positions API, so this
        page could in principle be rebuilt locally -- and was, until that join
        proved to fuse neighbouring fragments into tokens that exist in no
        document. A wipe now routes to OCR like any other reason. The page
        must still report that its text *is* present, because that is what
        distinguishes a wipe from a genuinely empty page."""
        wiped = [page for page in self.pages if page.markdown_wiped]
        self.assertTrue(wiped, "fixture no longer reproduces the upstream wipe")
        for page in wiped:
            self.assertEqual(page.markdown_chars, 0)
            self.assertGreater(page.native_chars, 0, "a wipe means the text is still there")
            self.assertTrue(page.needs_ocr, "a wipe must route to OCR, not be rebuilt")

    def test_image_placeholders_do_not_count_as_extracted_text(self):
        """The positions API reports images as `[Image: ...]` pseudo-text.
        Counting it makes an empty page look like it has content: a real
        tender's cover page scored 74 such characters while carrying no
        readable text at all, and its outlined title was lost silently."""
        from anydoc.page_health import _real_text

        items = [
            SimpleNamespace(text="[Image: Im1]", item_type="image"),
            SimpleNamespace(text="   ", item_type="text"),
        ]
        self.assertEqual(_real_text(items).strip(), "")

    def test_every_page_reports_its_signals_whether_or_not_it_is_flagged(self):
        for page, number in zip(self.pages, (1, 2, 3)):
            self.assertEqual(page.page, number)  # 1-indexed, matching NeedsOcrError
            self.assertIsInstance(page.curve_ops, int)
            self.assertIsInstance(page.cmap_corruption_ratio, float)


if __name__ == "__main__":
    unittest.main()
