#!/usr/bin/env python3
"""Verify the in-house build: the patch is wired, and the RTL fix is live.

    python scripts/verify_inhouse.py path/to/hebrew.pdf [more.pdf ...]

Two independent checks, because they fail in different ways:

1. Cargo.lock resolves `pdf-inspector` to our git fork. Catches a patch that
   was dropped by a merge, or a `cargo update` that fell back to crates.io.
2. Hebrew text converts in logical order. Catches the case where the patch is
   wired but the installed `anydoc._anydoc` is a stale build of it.

Exits non-zero on any failure, so it drops straight into CI.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXPECTED_FORK = "SaarBarak/pdf-inspector"

FINALS = set("ךםןףץ")
HEBREW_RUN = re.compile(r"[֐-׿]+")

# Frequent Hebrew words whose reversal is not itself a common word, so a
# reversed-heavy count is strong evidence of visual order.
COMMON = ["של", "את", "על", "זה", "היא", "הוא", "אשר", "כמו", "יותר", "אבל",
          "גם", "כל", "אני", "אין", "יש", "מה", "כי", "רק", "עם", "לפי"]


def check_lockfile() -> bool:
    """Confirm the crates.io patch actually took effect."""
    lock = (REPO / "Cargo.lock").read_text(encoding="utf-8")
    block = re.search(
        r'\[\[package\]\]\nname = "pdf-inspector"\nversion = "([^"]+)"\n'
        r'source = "([^"]+)"',
        lock,
    )
    if not block:
        print("FAIL  Cargo.lock has no resolved `pdf-inspector` entry")
        return False
    version, source = block.group(1), block.group(2)
    print(f"      pdf-inspector {version}")
    print(f"      source: {source}")
    if EXPECTED_FORK not in source:
        print(f"FAIL  expected the fork {EXPECTED_FORK}; the patch is not applied")
        return False
    if not source.startswith("git+"):
        print("FAIL  resolved from a registry, not our git fork")
        return False
    print("PASS  patch is wired\n")
    return True


def score(text: str) -> dict[str, int]:
    """Measure reading order without needing a dictionary.

    Hebrew final forms (ך ם ן ף ץ) are legal only as a word's last letter. If
    extraction leaked visual order every run is reversed, so those letters
    surface word-initially instead.
    """
    tokens = [t for t in HEBREW_RUN.findall(text) if len(t) > 1]
    return {
        "hebrew_tokens": len(tokens),
        "final_initial": sum(1 for t in tokens if t[0] in FINALS),
        "final_final": sum(1 for t in tokens if t[-1] in FINALS),
        "words_forward": sum(text.count(w) for w in COMMON),
        "words_reversed": sum(text.count(w[::-1]) for w in COMMON),
    }


def check_pdf(path: str) -> bool:
    import anydoc

    s = score(anydoc.to_markdown(path))
    print(f"      {path}")
    if s["hebrew_tokens"] == 0:
        print("SKIP  no Hebrew text found\n")
        return True
    for key in ("hebrew_tokens", "final_initial", "final_final",
                "words_forward", "words_reversed"):
        print(f"        {key:16} = {s[key]}")
    ok = s["final_final"] > s["final_initial"] and s["words_forward"] > s["words_reversed"]
    print(f"{'PASS' if ok else 'FAIL'}  {'logical' if ok else 'VISUAL'} order\n")
    return ok


def main(argv: list[str]) -> int:
    print("== Cargo.lock ==")
    ok = check_lockfile()
    # With no PDFs given, run as a lockfile-only gate. CI uses this: it does
    # not ship a Hebrew corpus, but it still must fail if the patch is gone.
    if len(argv) > 1:
        print("== conversion ==")
        for pdf in argv[1:]:
            ok &= check_pdf(pdf)
    else:
        print("(no PDFs given — skipping the conversion check)")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
