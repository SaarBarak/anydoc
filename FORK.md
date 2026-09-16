# anydoc fork — SaarBarak/anydoc

## What this fork is for

Redirects anydoc's PDF backend to a patched `pdf-inspector` build (see
`SaarBarak/pdf-inspector`'s own `FORK.md`) that fixes Hebrew/RTL text
extraction. **This fork's own Rust/Python source is unmodified from
upstream** — every commit here is about wiring to the patched dependency
and shipping installable wheels for it, not changing anydoc's own behavior.

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

Bump the `[patch.crates-io]` pin in `Cargo.toml` on `develop` to the new
tag. This repo's own test suite doesn't cover Hebrew — the real regression
gate is `tests/test_document_parser.py` in `SysAgentsHarness`. Tag the
result `vX.Y.Z-rtl-fix.N` once that passes.

## If we ever need a newer anydoc upstream base

Move `upstream-base` to the new commit, rebase `develop` onto it. None of
our commits touch anydoc's own extraction logic — only `Cargo.toml`, CI,
and docs — so this should be close to conflict-free regardless of how far
upstream has moved.
