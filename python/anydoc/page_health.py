"""Per-page extraction-health signals.

A PDF page can lose text in ways that leave no error behind. This module
measures four independent signals per page so a caller can tell a page that
extracted correctly from one that merely extracted *quietly*.

Everything here is pure Python over primitives the `azure` extra already
installs (`pdf_inspector`, `pypdf`): no Rust change, no new model, no
rendering, no network.

The four signals, and what each is for:

A. `cmap_corruption_ratio` -- characters that decoded to something unmappable
   (Private Use Area, U+FFFD, `(cid:N)`). A broken or missing `ToUnicode`
   CMap. Note that the *characters* are still in the file, so this is
   repairable in principle; by the time we see it, pdf-inspector's own CID
   recovery has already run and failed.

B. `outlined_text` -- the page draws far more Bezier curves than its text
   operators can account for. Glyphs converted to filled paths at export time
   carry no font, no character code and no text operator: they render
   perfectly and extract as nothing, and no parser can ever recover them.
   Table rules and cell borders are straight lines and rectangles and produce
   no curve at all, which is what keeps this from firing on ruled tables.
   **This is the only signal that justifies an OCR call.**

C. `markdown_wiped` -- `extract_pages_markdown_bytes()` blanked a page whose
   text the positions API can still see. That is our toolchain over-reacting,
   not damage to the document (upstream firecrawl/pdf-inspector#252, #342).
   The text is still in the file, but it routes to OCR anyway: reading the
   page again is cheaper than reassembling it from coordinates, and far more
   reliable. See "why nothing is reconstructed here" below.

D. `text_present` -- not a detector. Whether the rawest extraction path sees
   any characters at all, measured as `native_chars`. It is the input A and C
   are computed from, and nothing else.

Why nothing is reconstructed here: an earlier version rebuilt a page's text
by sorting positioned items into approximate reading order, and used it to
repair C for free. PDF stores text as positioned fragments with no spaces
between them, so that join fused neighbours -- on a real tender it welded two
table headers into `PROPOSALMINIMUM`, a token in no document. Inventing a
token is no better than dropping one, so a page that is not cleanly readable
now goes to OCR whole and the engine's output is taken as the page. This
module decides *which* pages; it never produces their text.

Not detected here: a scanner application's own bad OCR layer, which yields
well-formed but semantically wrong text. Every signal above stays silent on
it. Catching it requires OCR'ing the page and comparing, which costs what it
saves.
"""

import re
from dataclasses import dataclass, field

__all__ = ["PageHealth", "cmap_corruption_ratio", "scan_pdf_health"]

_PUA = re.compile(r"[-]")
_CID_TOKEN = re.compile(r"\(cid:\d+\)")
_REPLACEMENT = "�"
# The positions API reports embedded images as pseudo-text. Left in, they make
# an empty page look like it has content -- the cover page of a real tender
# scored 74 "characters" that were four of these plus fourteen spaces.
_IMAGE_PLACEHOLDER = re.compile(r"\[Image:[^\]]*\]")

# Counted on the raw content stream. This is a first-order operator census,
# not a content-stream parse: bytes inside a string literal can be miscounted.
# The margin it has to resolve is wide (a real outlined page measured 5,254
# curves against 54 text operators, a clean one 0 against 83), so the
# imprecision does not reach the decision. A stricter implementation should
# parse properly.
_CURVE_OP = re.compile(rb"(?<![A-Za-z0-9])c(?![A-Za-z0-9])")
_TEXT_SHOW_OP = re.compile(rb"(?<![A-Za-z0-9])(Tj|TJ|'|\")(?![A-Za-z0-9])")

# Calibrated against one 95-page tender, where clean pages topped out at 499
# curves and damaged ones started at 512. They will not transfer unchanged --
# recalibrate against the real corpus before relying on them.
CMAP_CORRUPTION_MAX = 0.03
CURVE_FLOOR = 500
CURVES_PER_TEXT_OP = 10.0

REASON_CMAP = "cmap_corruption"
REASON_OUTLINED = "outlined_text"
REASON_WIPED = "markdown_wiped"
REASON_NO_TEXT = "no_extractable_text"

# Every reason routes to OCR. `markdown_wiped` was once excluded, on the
# grounds that its text is still in the file and could be rebuilt locally at
# no cost. Rebuilding it proved worse than re-reading it -- see "why nothing
# is reconstructed here" -- so a wipe now spends an OCR call like the rest.
# The set is kept rather than collapsed into `bool(reasons)`: it is where a
# future reason that should *not* route gets expressed.
_OCR_REASONS = frozenset({REASON_CMAP, REASON_OUTLINED, REASON_NO_TEXT, REASON_WIPED})


@dataclass
class PageHealth:
    """One page's signals. `page` is 1-indexed, matching `NeedsOcrError`."""

    page: int
    cmap_corruption_ratio: float
    curve_ops: int
    text_show_ops: int
    native_chars: int
    markdown_chars: int
    has_ink: bool
    reasons: "list[str]" = field(default_factory=list)

    @property
    def needs_ocr(self) -> bool:
        """Whether this page has to go to an OCR engine."""
        return any(reason in _OCR_REASONS for reason in self.reasons)

    @property
    def markdown_wiped(self) -> bool:
        """Good text was discarded by our own toolchain. The page still goes
        to OCR -- what is cheap to detect is not cheap to rebuild."""
        return REASON_WIPED in self.reasons


def cmap_corruption_ratio(text: str) -> float:
    """Share of `text` that decoded to something unmappable.

    Detects a broken `ToUnicode` CMap. It cannot detect outlined text, which
    produces no characters at all and therefore always scores 0.0 -- the two
    failures need separate signals."""
    if not text:
        return 0.0
    bad = (
        len(_PUA.findall(text))
        + text.count(_REPLACEMENT)
        + sum(len(match.group()) for match in _CID_TOKEN.finditer(text))
    )
    return bad / len(text)


def _real_text(items) -> str:
    """Drop image placeholders; keep only what a reader would call text."""
    joined = "".join(item.text for item in items if "image" not in str(item.item_type).lower())
    return _IMAGE_PLACEHOLDER.sub("", joined)


def scan_pdf_health(data: bytes) -> "list[PageHealth]":
    """Measure every page of `data`. Never raises for a readable PDF."""
    import pdf_inspector
    from io import BytesIO
    from collections import defaultdict
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    markdown_pages = pdf_inspector.extract_pages_markdown_bytes(data).pages

    by_page = defaultdict(list)
    for item in pdf_inspector.extract_text_with_positions_bytes(data):
        by_page[item.page].append(item)

    results = []
    for index, page in enumerate(reader.pages):
        number = index + 1
        items = by_page.get(number, [])
        real = _real_text(items).strip()

        try:
            stream = page.get_contents().get_data()
        except Exception:
            stream = b""
        curves = len(_CURVE_OP.findall(stream))
        shows = len(_TEXT_SHOW_OP.findall(stream))

        markdown = (markdown_pages[index].markdown or "") if index < len(markdown_pages) else ""
        markdown_chars = len(markdown.strip())

        has_images = bool((page.get("/Resources", {}) or {}).get("/XObject"))
        has_ink = bool(curves or shows or has_images)

        ratio = cmap_corruption_ratio(real)
        reasons = []
        if ratio > CMAP_CORRUPTION_MAX:
            reasons.append(REASON_CMAP)
        if curves >= CURVE_FLOOR and curves > shows * CURVES_PER_TEXT_OP:
            reasons.append(REASON_OUTLINED)
        # A page that draws something but yields no characters has lost
        # whatever it draws, whatever the reason. This catches the cover page
        # whose title was outlined below the curve floor, which every other
        # signal here missed.
        if not real and has_ink:
            reasons.append(REASON_NO_TEXT)
        if not markdown_chars and real:
            reasons.append(REASON_WIPED)

        results.append(
            PageHealth(
                page=number,
                cmap_corruption_ratio=ratio,
                curve_ops=curves,
                text_show_ops=shows,
                native_chars=len(real),
                markdown_chars=markdown_chars,
                has_ink=has_ink,
                reasons=reasons,
            )
        )
    return results
