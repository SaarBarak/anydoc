# RTL-fix anydoc

This branch (`rtl-fix`) is our tracking fork of [`firecrawl/anydoc`](https://github.com/firecrawl/anydoc).
It is upstream, unmodified, **plus** a single `[patch.crates-io]` block in
[`Cargo.toml`](Cargo.toml) that redirects `pdf-inspector` to our fork.

Nothing else is changed, apart from one snapshot: upstream anydoc still pins
`pdf-inspector = "1.14.2"`, which predates upstream's *own* RTL fix (1.16.0),
so `tests/snapshots/snapshots__pdf__text.pdf.snap` still records the Persian
sample in visual order. Our pin is 1.17.0, which reads it logically. The
override goes away when upstream anydoc bumps past 1.16.0.

That minimalism is deliberate: the smaller the diff, the cheaper every
upstream merge.

## Why the patch exists

anydoc consumes pdf-inspector as a **Rust crate**, statically linked — there is
exactly one call site, [`src/formats/pdf.rs`](src/formats/pdf.rs). So a
`pip install pdf-inspector` alongside anydoc changes nothing: the PDF code that
actually runs is compiled into `anydoc._anydoc`. The only way to get our RTL fix
into anydoc is to rebuild anydoc from source with a Cargo-level override.

What we maintain:

```
anydoc  ──links──>  pdf-inspector
(this repo)         (our fork: RTL fix)
```

Upstream's `firecrawl/firecrawl` links anydoc the same way, from
`apps/api/native`. We do not fork it — that is recorded only so the shape of
the problem is obvious if we ever need the fix to reach that layer too.

## Version scheme

Upstream crate versions are left alone. Each RTL-fix build is identified by a
**git tag** of the form:

```
v<upstream-version>-rtl-fix.<n>
```

| Tag                 | Meaning                                            |
| ------------------- | -------------------------------------------------- |
| `v0.1.8-rtl-fix.1`  | upstream 0.1.8 + our first RTL-fix patch set       |
| `v0.1.8-rtl-fix.2`  | same upstream base, second RTL-fix revision        |
| `v1.17.0-rtl-fix.1` | rebranched onto upstream 1.17.0, counter resets    |

Both forks use this same scheme, so a glance at the tags tells you which
upstream release each is sitting on and whether they agree.

**Superseded from `v1.17.0-fork.1` on.** `-rtl-fix.N` named this fork's
first patch; it stopped fitting once each fork carried patches that are not
RTL fixes (the Python-bindings feature split on the pdf-inspector side, the
Azure OCR dispatch and page-health routing on the anydoc side). Both forks'
`FORK.md` now use `v<upstream-version>-fork.<n>` instead — same idea, tag
names the fork's role rather than whichever patch came first. Existing
`-rtl-fix.N` tags below are unchanged; they are immutable history.

Current alignment:

| Repo                      | Upstream base | Fork tag           | Pinned SHA |
| ------------------------- | -------------- | ------------------- | ---------- |
| `SaarBarak/pdf-inspector` | 1.17.0          | `v1.17.0-fork.1`    | `cfc86792` |
| `SaarBarak/anydoc`        | 0.2.4           | `v1.17.0-fork.1`    | this branch |

The two repos share the fork tag name but sit on different upstream
versions — the tag names the pdf-inspector base, because that is what the
patch pins.

### What each revision carries

| Tag                 | Change |
| ------------------- | ------ |
| `v0.1.8-rtl-fix.1`  | RTL logical-order extraction, mirrored brackets, tagged-table column order |
| `v0.1.8-rtl-fix.2`  | Recovers CIDs a `/ToUnicode` CMap never names, via the embedded font's cmap |
| `v1.17.0-rtl-fix.1` | Adopts upstream's own RTL fix; retires our `src/rtl.rs`; keeps the CID fix and adds a UAX #9 number-separator fix |
| `v1.17.0-rtl-fix.2` | Adds an orthographic override for documents upstream's visual/logical verdict gets wrong |
| `v1.17.0-fork.1`    | No RTL content. Lets the Python bindings build without the native OCR path (`python = ["pyo3"]`, 99 crates instead of 262) — the anydoc fork's Python `ocr` extra now pays that cost instead of pulling in ONNX/PDFium/TLS on every install |

### Why `.1` was retired

Upstream shipped its own RTL visual-order fix in 1.16.0
([firecrawl#440](https://github.com/firecrawl/pdf-inspector/pull/440),
[firecrawl#441](https://github.com/firecrawl/pdf-inspector/pull/441)) — a
per-page geometric verdict on whether the producer stores visual or logical
order, which ours lacked entirely. Scored on a 21-document Hebrew corpus,
upstream matched or beat our 1044-line `src/rtl.rs` on reading order (0
word-initial final forms against our 2) while extracting more text, so the
module was dropped rather than merged. Keeping it would also have corrupted
logical-order producers such as OCR text layers, which upstream now serves.

Three gaps remained, and they are the fork's entire diff (3 files):

- **`fix(cid)`** — the `.2` fix above, unchanged and still absent upstream.
  Producers that omit one glyph from the ToUnicode CMap make that letter
  disappear from every extractor's output; nun (U+05E0) was the observed
  casualty. Upstream emits 124 `U+FFFD` on our reference NDA.
- **`fix(rtl)`** — upstream absorbs any punctuation adjacent to a digit into
  the forward-ordered run, which pins it to the side the renderer chose. UAX
  #9 draws the line by flanking: W4/W5 fold a separator into a number only
  when digits sit on **both** sides, otherwise it is a neutral resolving to
  the paragraph direction under N1/N2. Without the fix, 52 of 78 Hebrew
  ordered-list markers come out `.1` instead of `1.`.
- **`fix(rtl)` orthographic override** — upstream's verdict reads run
  *emission* direction, but emission direction and intra-run character order
  are independent: a producer can walk runs right-to-left (voting "already
  logical") and still store each run visually. Two of our five Hebrew test
  files are that class and came out fully mirrored — 649 and 347 word-initial
  final forms. Since a word-initial final form (`ך ם ן ף ץ`) is impossible in
  Hebrew, comparing initial against terminal placement decides the question
  outright, and a correct logical-order producer scores zero initial so the
  override cannot fire on it.

All three are clean upstream PR candidates; landing them retires the fork.

The 21-document corpus is byte-identical with and without the override — it
is inert wherever the geometric verdict was already right. Residual: 4
word-initial final forms out of 68 682 tokens on a 208-page report, in a
masthead whose page carries too little Hebrew to sample.

### Do not put `-rtl-fix` in the crate version

`Cargo.toml` requires `pdf-inspector = "1.14.2"`, i.e. `^1.14.2`. A `[patch]`
is only accepted if the replacement is **semver-compatible** with that
requirement. Two ways to break it:

- Fork crate version below the requirement → `error: failed to select a
  version for the requirement 'pdf-inspector = "^1.14.2"' ... candidate
  versions found which didn't match`
- Fork crate version `1.17.0-rtl-fix.1` → a semver **prerelease**, which sorts
  *below* `1.17.0` and also fails to match.

So the fork's `package.version` stays exactly upstream's — `1.17.0` today. The
RTL-fix revision lives in the git tag and nowhere else.

This is what broke when upstream anydoc moved its requirement from `0.1.8` to
`1.14.2`: the old `v0.1.8-rtl-fix.2` fork no longer satisfied it. Rebranching
the fork onto upstream 1.17.0 fixed it, which is why the pdf-inspector fork
must be updated *before* this repo.

## Installing

### From a release wheel — no Rust needed

Each RTL-fix tag has a GitHub release carrying prebuilt wheels. Installs in
about a second. Pushing the tag is what builds them, so take the exact asset
names from the release page rather than guessing — the version in the filename
tracks anydoc's own `package.version` (`0.2.4` today), not the RTL-fix tag:

- [releases/tag/v1.17.0-rtl-fix.2](https://github.com/SaarBarak/anydoc/releases/tag/v1.17.0-rtl-fix.2)

```bash
pip install https://github.com/SaarBarak/anydoc/releases/download/<tag>/<asset>.whl
```

```toml
[tool.uv.sources]
firecrawl-anydoc = { url = "https://github.com/SaarBarak/anydoc/releases/download/<tag>/<asset>.whl" }
```

Wheels are `abi3-py310`, so one file per platform covers CPython 3.10 through
3.14.

The `v0.1.8-rtl-fix.*` releases carry `firecrawl_anydoc-0.1.8-*` wheels built
against the retired `src/rtl.rs`; do not mix them with a `1.17.0` pin.

`v0.1.8-rtl-fix.1` carries a single hand-built wheel (Linux x86_64,
glibc ≥ 2.35). From `v0.1.8-rtl-fix.2` on,
[`.github/workflows/rtl-fix-wheels.yml`](.github/workflows/rtl-fix-wheels.yml)
builds these six targets on tag push and attaches them automatically:

| platform | targets |
| --- | --- |
| Linux glibc (manylinux2014, glibc ≥ 2.17) | x86_64, aarch64 |
| Linux musl (musllinux_1_2) | x86_64, aarch64 |
| macOS | arm64 |
| Windows | x86_64 |

Intel macOS is absent on purpose. GitHub retired the `macos-13` runners, and on
`v0.1.8-rtl-fix.1` that job waited the full 24-hour limit, was cancelled, and
skipped the `publish` job with it — so a run that built 7 of 8 targets attached
nothing. `publish` now runs even when a target drops out, and uploads whatever
did build. Add the target back with a current Intel label if you need it.

### Enabling the workflow (one time)

GitHub keeps Actions dormant on forks until a human opts in, and there is no
API for it. Once, in this repo's **Actions** tab, click *"I understand my
workflows, go ahead and enable them"*.

Then disable the inherited upstream workflows — they target `blacksmith-*`
runners this fork cannot schedule, so their jobs would queue forever:

```bash
gh workflow disable Release --repo SaarBarak/anydoc
gh workflow disable CI      --repo SaarBarak/anydoc
gh workflow disable pages   --repo SaarBarak/anydoc
```

Upstream's `release.yml` also triggers on `v*`, which matches our tags. It
cannot actually publish — its version gate rejects an `-rtl-fix` tag, and the
fork holds no PyPI or npm credentials — but disabling it keeps the tab clean.

`workflow_dispatch` (rebuilding wheels for an existing tag) additionally needs
this workflow present on the repo's **default branch**; GitHub only registers
dispatchable workflows from there. Either point the default branch at
`rtl-fix`, or just cut a new tag — the `push` trigger works from any
branch, as long as the tagged commit contains the workflow file.

### From source — any platform

```bash
pip install "firecrawl-anydoc @ git+https://github.com/SaarBarak/anydoc.git@rtl-fix#subdirectory=python"
```

```toml
[tool.uv.sources]
firecrawl-anydoc = { git = "https://github.com/SaarBarak/anydoc.git", branch = "rtl-fix", subdirectory = "python" }
```

Needs a Rust toolchain (≥1.88); maturin compiles anydoc and pdf-inspector from
source, about a minute cold. Swap `@rtl-fix` for `@v1.17.0-rtl-fix.2` to
pin a build exactly.

## Verifying

```bash
python scripts/verify_rtl_fix.py path/to/hebrew.pdf
```

Exits non-zero if the patch silently fell out of the build. It checks two
things: that `pdf-inspector` resolves to our git fork in `Cargo.lock`, and that
Hebrew text actually comes out in logical order.

## Adding a new fork patch

Use `-fork.N`, not `-rtl-fix.N` — see "Superseded from `v1.17.0-fork.1` on"
above.

1. Commit the fix on the fork's branch (e.g. `SaarBarak/pdf-inspector`).
2. Confirm `package.version` still satisfies the requirement in this
   repo's `Cargo.toml` (`^1.14.2` today).
3. Tag and push: `git tag -a v1.17.0-fork.2 -m "..." && git push origin v1.17.0-fork.2`
4. Update the `tag = ` line in this repo's `[patch.crates-io]`.
5. `cargo update -p pdf-inspector` to refresh the pinned SHA in `Cargo.lock`.
6. Re-run the verification, commit `Cargo.toml` + `Cargo.lock`, tag this repo
   `v1.17.0-fork.2` as well.

Keep the two tags in lockstep — same tag name in both repos means they were
built and validated together.

## Syncing with upstream

```bash
git fetch upstream
git merge upstream/main          # or rebase; merge keeps the patch diff obvious
```

If upstream bumps its `pdf-inspector` requirement (say to `0.2.x`), the patch
will fail loudly at resolve time rather than silently reverting. Rebase the
pdf-inspector fork onto that upstream release, keep its crate version equal to
the new upstream version, re-tag as `v<new>-rtl-fix.1`, and update the pin here.
