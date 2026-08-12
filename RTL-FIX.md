# RTL-fix anydoc

This branch (`rtl-fix`) is our tracking fork of [`firecrawl/anydoc`](https://github.com/firecrawl/anydoc).
It is upstream, unmodified, **plus** a single `[patch.crates-io]` block in
[`Cargo.toml`](Cargo.toml) that redirects `pdf-inspector` to our fork.

Nothing else is changed. That is deliberate: the smaller the diff, the cheaper
every upstream merge.

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

| Tag                | Meaning                                            |
| ------------------ | -------------------------------------------------- |
| `v0.1.8-rtl-fix.1` | upstream 0.1.8 + our first RTL-fix patch set       |
| `v0.1.8-rtl-fix.2` | same upstream base, second RTL-fix revision        |
| `v0.1.9-rtl-fix.1` | rebased onto upstream 0.1.9, counter resets         |

Both forks use this same scheme, so a glance at the tags tells you which
upstream release each is sitting on and whether they agree.

Current alignment:

| Repo                     | Upstream base | RTL-fix tag       | Pinned SHA |
| ------------------------ | ------------- | ------------------ | ---------- |
| `SaarBarak/pdf-inspector`| 0.1.8         | `v0.1.8-rtl-fix.2` | `8f4df7df` |
| `SaarBarak/anydoc`       | 0.1.8         | `v0.1.8-rtl-fix.2` | this branch |

### What each revision carries

| Tag                | Change |
| ------------------ | ------ |
| `v0.1.8-rtl-fix.1` | RTL logical-order extraction, mirrored brackets, tagged-table column order |
| `v0.1.8-rtl-fix.2` | Recovers CIDs a `/ToUnicode` CMap never names, via the embedded font's cmap |

The `.2` fix is not RTL-specific, but it surfaced through Hebrew: producers that
omit one glyph from the ToUnicode CMap make that letter disappear from every
extractor's output. Nun (U+05E0) was the observed casualty — see the
[pdf-inspector release notes](https://github.com/SaarBarak/pdf-inspector/releases/tag/v0.1.8-rtl-fix.2).

### Do not put `-rtl-fix` in the crate version

`Cargo.toml` line 31 requires `pdf-inspector = "0.1.8"`, i.e. `^0.1.8`. A
`[patch]` is only accepted if the replacement is **semver-compatible** with
that requirement. Two ways to break it:

- Fork crate version `0.1.7` → `error: failed to select a version for the
  requirement 'pdf-inspector = "^0.1.8"' ... candidate versions found which
  didn't match: 0.1.7`
- Fork crate version `0.1.8-rtl-fix.1` → a semver **prerelease**, which sorts
  *below* `0.1.8` and also fails to match.

So the fork's `package.version` stays exactly `0.1.8`. The RTL-fix revision
lives in the git tag and nowhere else.

## Installing

### From a release wheel — no Rust needed

Each RTL-fix tag has a GitHub release carrying prebuilt wheels. Installs in
about a second. Asset names embed the platform tag, so copy the URL for your
platform from the
[release page](https://github.com/SaarBarak/anydoc/releases/tag/v0.1.8-rtl-fix.2):

```bash
pip install https://github.com/SaarBarak/anydoc/releases/download/v0.1.8-rtl-fix.2/<asset>.whl
```

```toml
[tool.uv.sources]
firecrawl-anydoc = { url = "https://github.com/SaarBarak/anydoc/releases/download/v0.1.8-rtl-fix.2/<asset>.whl" }
```

Wheels are `abi3-py310`, so one file per platform covers CPython 3.10 through
3.14.

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
source, about a minute cold. Swap `@rtl-fix` for `@v0.1.8-rtl-fix.2` to
pin a build exactly.

## Verifying

```bash
python scripts/verify_rtl_fix.py path/to/hebrew.pdf
```

Exits non-zero if the patch silently fell out of the build. It checks two
things: that `pdf-inspector` resolves to our git fork in `Cargo.lock`, and that
Hebrew text actually comes out in logical order.

## Adding a new RTL-fix patch

1. Commit the fix on the fork's branch (e.g. `SaarBarak/pdf-inspector`).
2. Confirm `package.version` is still within `^0.1.8`.
3. Tag and push: `git tag -a v0.1.8-rtl-fix.2 -m "..." && git push origin v0.1.8-rtl-fix.2`
4. Update the `tag = ` line in this repo's `[patch.crates-io]`.
5. `cargo update -p pdf-inspector` to refresh the pinned SHA in `Cargo.lock`.
6. Re-run the verification, commit `Cargo.toml` + `Cargo.lock`, tag this repo
   `v0.1.8-rtl-fix.2` as well.

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
