# In-house anydoc

This branch (`inhouse/main`) is our tracking fork of [`firecrawl/anydoc`](https://github.com/firecrawl/anydoc).
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

Upstream crate versions are left alone. Each in-house build is identified by a
**git tag** of the form:

```
v<upstream-version>-inhouse.<n>
```

| Tag                | Meaning                                            |
| ------------------ | -------------------------------------------------- |
| `v0.1.8-inhouse.1` | upstream 0.1.8 + our first in-house patch set       |
| `v0.1.8-inhouse.2` | same upstream base, second in-house revision        |
| `v0.1.9-inhouse.1` | rebased onto upstream 0.1.9, counter resets         |

Every in-house repo uses this same scheme, so a glance at the tags tells you
which upstream release each fork is sitting on and whether they agree.

Current alignment:

| Repo                     | Upstream base | In-house tag       | Pinned SHA |
| ------------------------ | ------------- | ------------------ | ---------- |
| `SaarBarak/pdf-inspector`| 0.1.8         | `v0.1.8-inhouse.1` | `bbda40fe` |
| `SaarBarak/anydoc`       | 0.1.8         | `v0.1.8-inhouse.1` | this branch |

### Do not put `-inhouse` in the crate version

`Cargo.toml` line 31 requires `pdf-inspector = "0.1.8"`, i.e. `^0.1.8`. A
`[patch]` is only accepted if the replacement is **semver-compatible** with
that requirement. Two ways to break it:

- Fork crate version `0.1.7` → `error: failed to select a version for the
  requirement 'pdf-inspector = "^0.1.8"' ... candidate versions found which
  didn't match: 0.1.7`
- Fork crate version `0.1.8-inhouse.1` → a semver **prerelease**, which sorts
  *below* `0.1.8` and also fails to match.

So the fork's `package.version` stays exactly `0.1.8`. The in-house revision
lives in the git tag and nowhere else.

## Installing

### From a release wheel — no Rust needed

Each in-house tag has a GitHub release carrying a prebuilt wheel. Installs in
about a second:

```bash
pip install https://github.com/SaarBarak/anydoc/releases/download/v0.1.8-inhouse.1/firecrawl_anydoc-0.1.8-cp310-abi3-manylinux_2_35_x86_64.whl
```

```toml
[tool.uv.sources]
firecrawl-anydoc = { url = "https://github.com/SaarBarak/anydoc/releases/download/v0.1.8-inhouse.1/firecrawl_anydoc-0.1.8-cp310-abi3-manylinux_2_35_x86_64.whl" }
```

Wheels are `abi3-py310`, so one file per platform covers CPython 3.10 through
3.14.

`v0.1.8-inhouse.1` carries a single hand-built wheel (Linux x86_64,
glibc ≥ 2.35). From `v0.1.8-inhouse.2` on,
[`.github/workflows/inhouse-wheels.yml`](.github/workflows/inhouse-wheels.yml)
builds all seven targets on tag push and attaches them automatically:

| platform | targets |
| --- | --- |
| Linux glibc (manylinux2014, glibc ≥ 2.17) | x86_64, aarch64 |
| Linux musl (musllinux_1_2) | x86_64, aarch64 |
| macOS | x86_64, arm64 |
| Windows | x86_64 |

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
cannot actually publish — its version gate rejects an `-inhouse` tag, and the
fork holds no PyPI or npm credentials — but disabling it keeps the tab clean.

`workflow_dispatch` (rebuilding wheels for an existing tag) additionally needs
this workflow present on the repo's **default branch**; GitHub only registers
dispatchable workflows from there. Either point the default branch at
`inhouse/main`, or just cut a new tag — the `push` trigger works from any
branch, as long as the tagged commit contains the workflow file.

### From source — any platform

```bash
pip install "firecrawl-anydoc @ git+https://github.com/SaarBarak/anydoc.git@inhouse/main#subdirectory=python"
```

```toml
[tool.uv.sources]
firecrawl-anydoc = { git = "https://github.com/SaarBarak/anydoc.git", branch = "inhouse/main", subdirectory = "python" }
```

Needs a Rust toolchain (≥1.88); maturin compiles anydoc and pdf-inspector from
source, about a minute cold. Swap `@inhouse/main` for `@v0.1.8-inhouse.1` to
pin a build exactly.

## Verifying

```bash
python scripts/verify_inhouse.py path/to/hebrew.pdf
```

Exits non-zero if the patch silently fell out of the build. It checks two
things: that `pdf-inspector` resolves to our git fork in `Cargo.lock`, and that
Hebrew text actually comes out in logical order.

## Adding a new in-house patch

1. Commit the fix on the fork's branch (e.g. `SaarBarak/pdf-inspector`).
2. Confirm `package.version` is still within `^0.1.8`.
3. Tag and push: `git tag -a v0.1.8-inhouse.2 -m "..." && git push origin v0.1.8-inhouse.2`
4. Update the `tag = ` line in this repo's `[patch.crates-io]`.
5. `cargo update -p pdf-inspector` to refresh the pinned SHA in `Cargo.lock`.
6. Re-run the verification, commit `Cargo.toml` + `Cargo.lock`, tag this repo
   `v0.1.8-inhouse.2` as well.

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
the new upstream version, re-tag as `v<new>-inhouse.1`, and update the pin here.
