# anydoc fork — SaarBarak/anydoc

## What this fork is for

Two things now, and they have different costs to maintain.

1. **Wiring.** Redirects anydoc's PDF backend to a patched `pdf-inspector`
   build (see `SaarBarak/pdf-inspector`'s own `FORK.md`) that fixes
   Hebrew/RTL text extraction. Cargo's `[patch.crates-io]` and the `ocr`
   extra's pin in `python/pyproject.toml` both name that fork's tag, and
   both must move together — see "The pin is in two places" below.
2. **Feature work.** Azure Document Intelligence OCR dispatch: when a PDF's
   scanned pages would otherwise raise `NeedsOcrError`, they are recovered
   page by page and merged back, and the rest of the document never leaves
   the machine. This is anydoc's own Python source, changed here and not
   upstream.

**This fork used to say its own source was unmodified from upstream, and
that rebasing was therefore close to conflict-free. That stopped being true
when the OCR dispatch landed.** The claim is recorded here as it was, rather
than quietly dropped, because the promise it made — cheap upstream
rebases — is the thing that changed, and whoever does the next one should
know before starting. Rebasing now means merging real changes to
`anydoc/__init__.py`, `anydoc/ocr_clients/`, and `python/pyproject.toml`,
not just re-applying a `Cargo.toml` block.

That was a deliberate trade, not drift: upstream has no OCR escalation and
no plans stated for one, and the pipeline consuming this fork needs Hebrew
scanned pages read rather than rejected. Revisit it if upstream ever grows
its own OCR path — at that point this feature work should move there and
the fork can shrink back to wiring.

## Frozen base

`upstream-base` tag → `firecrawl/anydoc` @ `261fc25` (2026-08-27, "Update
README.md"). Upstream has not moved since (0 commits ahead as of
2026-09-16) — this fork is currently fully caught up with its own upstream,
unlike `pdf-inspector`'s (see that repo's `FORK.md`).

## What's on top of the base, in order

1. `d1bae3e` — build: route pdf-inspector through our fork for the RTL
   logical-order fix. **The one commit that actually matters behaviorally**:
   adds the `[patch.crates-io]` redirect in `Cargo.toml` pointing the
   `pdf-inspector` dependency at our patched fork instead of crates.io.
2. `8bd6950`, `847cc18`, `0c73154`, `1c7dd56` — docs describing this fork's
   own install/build story (no-Rust wheel install path, the wheel matrix,
   naming the built asset).
3. `c98757d` — ci: cross-build wheels for every tag on this fork, so a
   consumer doesn't need a Rust toolchain to install it.
4. `23bc3d2` — refactor: rename "in-house" to "rtl-fix" everywhere (this
   fork's own internal naming; unrelated to upstream anydoc).
5. `1e8d65c` — build: pin `pdf-inspector` to `v0.1.8-rtl-fix.2` (superseded
   by the merge commit below).
6. `6191de9` — test: refresh a stale Persian-script test snapshot.
7. `480d783` — Merge upstream/main (v0.2.4) and repoint the fork at
   `pdf-inspector` 1.17.0 — most recent sync + pin bump.

8. Azure Document Intelligence OCR dispatch (`feat/azure-ocr-dispatch`) —
   the first commits here to change anydoc's own behavior. Adds
   `anydoc/ocr_clients/`, the `ocr="reject"` escalation in
   `anydoc/__init__.py`, and the `ocr`/`azure` extras. See "What this fork is for".

## The pin is in two places

`pdf-inspector` is consumed twice, and both consumers resolve independently:

| Consumer | Declared in | Resolves from |
|---|---|---|
| Rust core, links the crate | `Cargo.toml` `[patch.crates-io]` | the fork tag |
| the OCR dispatch, imports the package | `python/pyproject.toml`, `ocr` extra | the fork tag |

They were not always both redirected. The Python side carried a plain
`pdf-inspector>=1.17.0`, which resolves from PyPI — upstream, without the
RTL fix — so one install held two versions of one library and Hebrew came
back character-reversed from whichever path went through Python, in the
fork that exists to prevent exactly that.

**Move both together, always.** A bump that touches one is the bug.

**`ForkPinTest` in `python/tests/test_anydoc.py` enforces that**, so this is a
rule the build checks rather than one a reader has to remember. Two tests:

- the tag in `[patch.crates-io]` and the tag in the `ocr` extra must match.
  Static, needs nothing installed, and fails at review time on a half-finished
  bump.
- the *installed* `pdf-inspector` must come from the fork URL and from that
  same tag, read out of its `direct_url.json`. A package resolved from PyPI has
  no such file, which is exactly the original failure; a stale venv on an older
  fork tag is the other one this catches.

Removing the second consumer entirely was prototyped and rejected — see
"Rejected: reading through our own bindings" below.

## Rejected: reading through our own bindings

`proto/anydoc-pdf-bindings` (closed PR #3) removes the second consumer instead
of keeping it in step: two PyO3 functions expose the reads the Python layer
needs, against the crate `[patch.crates-io]` already redirects. One resolution,
nothing to keep aligned, and no `git+` direct reference in `pyproject.toml`
(PyPI rejects those in dependencies).

It works — it compiles, and its output is byte-identical to the Python package
on a Hebrew fixture. **It is rejected on maintenance cost, not correctness.**
`python/src/lib.rs` is upstream's PyO3 bridge, a file upstream touches whenever
it adds or changes any API, which makes it the most expensive place in the tree
to carry a fork patch — and in a language the rest of this fork's work
deliberately avoids. It also adds `pdf-inspector` to `python/Cargo.toml`,
another upstream-owned file, changing the build for anyone installing the fork.

The constraint it breaks is recorded: *"Python layer only, no Rust changes, no
changes to `pdf-inspector`"* — `SysAgentsHarness`'s OCR architecture decisions
doc, the page-health routing PRD, and this repo's PR #1 as D1.

The branch is kept, not deleted. It is the right answer if that constraint is
ever lifted, or if tracking upstream stops mattering.

## Branch

**`develop`** is the one long-lived branch — it accumulates every
patch/pin-bump this fork carries, and releases are tagged directly off it
(there is no separate release branch; `main` never receives these commits
— see below). Renamed from `rtl-fix` (item 4 above renamed it from
"in-house" to "rtl-fix"; that name described the *first* patch, not what
the branch actually is now that it carries unrelated changes too — hence
`develop`).

**Working on it, including in parallel:** never commit directly to
`develop`. Cut a short-lived branch off it per patch/feature, merge back
via PR when it's done, then re-tag. That's what makes concurrent work by
more than one person safe on a single branch — `develop`'s tip is always
either fully done or not yet touched, never half-finished.

**`main` stays a plain, untouched mirror of upstream** — not a merge
target for `develop`. It has no fork commits and isn't meant to gain any;
it's just an honest "here's vanilla upstream" landing page.

**When to introduce a second branch (a `main-fork` line):** only if either
(a) someone needs to cut a release while another patch is genuinely
mid-flight and can't be merged or set aside, as a recurring situation, or
(b) two deployments need to diverge onto different, separately-maintained
patch sets. Neither applies today — don't create one preemptively.

## If `pdf-inspector` cuts a new patch tag

Bump **both** pins on `develop` to the new tag — `[patch.crates-io]` in
`Cargo.toml` *and* the `ocr` extra in `python/pyproject.toml`. Bumping
only the first is the bug described in "The pin is in two places": the Rust
core moves to the new fork build while the Python side keeps resolving
upstream from PyPI, and Hebrew comes back reversed from the Azure path.

`ForkPinTest` fails if you bump one and not the other, and again if your venv
is still on the old tag after both are bumped. Reinstall the extra before
believing a Hebrew result.

This repo's own test suite doesn't cover Hebrew — the real regression gate
is `tests/test_document_parser.py` in `SysAgentsHarness`. Tag the result
`vX.Y.Z-rtl-fix.N` once that passes.

## If we ever need a newer anydoc upstream base

Move `upstream-base` to the new commit, rebase `develop` onto it.

**Expect real conflicts now.** This used to be close to free, because no
commit here touched anydoc's own code — only `Cargo.toml`, CI and docs. The
OCR dispatch changed that: `anydoc/__init__.py` and `python/pyproject.toml`
both carry fork edits, and upstream owns both files. `anydoc/ocr_clients/`
is ours alone and should not conflict.

Two checks the rebase is not done without:

- `python/tests/test_anydoc.py`, which covers the OCR dispatch's own logic
  against a mocked Azure client;
- `SysAgentsHarness`'s `tests/test_document_parser.py`, the Hebrew gate —
  this repo's suite does not cover Hebrew, and a rebase that silently drops
  either pin is exactly the failure it catches.
