"""VLM (vision-language model) OCR client -- any vendor exposing an
OpenAI-compatible Chat Completions endpoint that accepts an image. Reached
over plain HTTP via the standard library rather than the `openai` package,
matching `anydoc/__init__.py`'s own `_parse_hosted` precedent: one JSON
POST doesn't need an SDK. Everything HTTP- and prompt-specific lives here --
anydoc's orchestration knows nothing about it, only the `OcrClient`
protocol.

Deliberately engine-agnostic in naming (`VLM_OCR_*`, not e.g. `GAIA_*`) --
which vision model answers is a deployment detail set by whatever
`VLM_OCR_BASE_URL`/`_MODEL` point at, same as `azure_di` not knowing which
Azure subscription it's talking to.

All third-party imports are lazy, inside methods, not at module level: this
module must stay importable (for `is_requested`, which only needs
`os.environ`) even when the `vlm` extra (the image-rendering half --
`pypdfium2` and Pillow) isn't installed."""

import base64
import json
import os
import urllib.error
import urllib.request
from io import BytesIO

from anydoc.ocr_clients.base import ClientConfigError

BASE_URL_ENV = "VLM_OCR_BASE_URL"
API_KEY_ENV = "VLM_OCR_API_KEY"
MODEL_ENV = "VLM_OCR_MODEL"

_TIMEOUT_SECONDS = 300

_PROMPT = (
    "Transcribe every word of text visible on this page exactly as it appears, "
    "in reading order, as GitHub-Flavored Markdown. Preserve headings, lists, "
    "and tables where the layout shows them. Output only the transcription -- "
    "no preamble, no commentary, no code fence."
)


def is_requested() -> bool:
    """Any of the three vars present counts -- same shape as azure_di's
    check across its two vars, so a half-set deployment gets a clean
    `ClientConfigError` from `client()` instead of silently falling through
    to whatever engine is configured next (or to `NeedsOcrError`)."""
    return bool(
        os.environ.get(BASE_URL_ENV) or os.environ.get(API_KEY_ENV) or os.environ.get(MODEL_ENV)
    )


def client() -> "VlmClient":
    """This module's half of the engine contract -- see `ocr_clients`."""
    return VlmClient.from_env()


class VlmClient:
    """Construct via `from_env`, not directly -- that's where config
    validation happens."""

    def __init__(self, base_url: str, model: str, api_key: "str | None"):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    @classmethod
    def from_env(cls) -> "VlmClient":
        base_url = os.environ.get(BASE_URL_ENV)
        model = os.environ.get(MODEL_ENV)
        if not base_url or not model:
            raise ClientConfigError(
                f"VLM OCR needs both {BASE_URL_ENV} and {MODEL_ENV} set; only one is"
            )
        return cls(base_url=base_url, model=model, api_key=os.environ.get(API_KEY_ENV) or None)

    def ocr_page(self, page_pdf_bytes: bytes) -> str:
        from anydoc.ocr_clients._render import render_page_to_image

        image = render_page_to_image(page_pdf_bytes)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        data_url = f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"

        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            }
        ).encode()

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(
            f"{self._base_url}/chat/completions", data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"VLM OCR request failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"VLM OCR request failed: {exc.reason}") from exc

        return body["choices"][0]["message"]["content"]
