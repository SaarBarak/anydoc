"""OCR engine clients: one module per engine (`azure_di.py`, and in future
`tesseract.py` etc.).

Each engine module exposes two things, which is the whole contract:

- `is_requested()` -- does this engine look configured at all? Reads the
  environment and nothing else, so it is safe to call when the engine's own
  optional dependencies are not installed.
- `client()` -- construct it, raising `base.ClientConfigError` if the
  configuration is incomplete or its dependency is missing. Returns something
  satisfying `base.OcrClient`.

`requested()` below picks between them. It is the only thing that knows more
than one engine exists: `anydoc/__init__.py` orchestrates against the
protocol and names no engine, and an engine module knows nothing about its
neighbours.

Importing this package pulls in every engine module, which is deliberate and
costs nothing -- an engine module imports only the standard library at module
level and defers its own third-party imports into `client()` and the client's
methods. So this package stays importable with no extra installed; only
constructing or using a client needs one."""

from anydoc.ocr_clients import azure_di

__all__ = ["requested"]

# Precedence order. The first engine that looks configured wins, so a
# deployment with two sets of credentials present gets a defined answer
# rather than whichever import happened first.
_ENGINES = (azure_di,)


def requested():
    """The first engine module that looks configured, or `None`.

    Deliberately does not construct the client: a half-configured engine must
    still raise (a missing key is a mistake, not a reason to fall back
    silently), and that error belongs at the one boundary that translates it
    for callers, not here."""
    for engine in _ENGINES:
        if engine.is_requested():
            return engine
    return None
