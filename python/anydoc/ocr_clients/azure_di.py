"""Azure Document Intelligence OCR client. Everything Azure-SDK-specific
lives here -- anydoc/__init__.py's orchestration (page slicing, dispatch,
reindexing) knows nothing about Azure's actual API shape, only the
`OcrClient` protocol.

All Azure SDK imports are lazy, inside methods, not at module level: this
module must stay importable (for `is_requested`, which only needs
`os.environ`) even when the `azure` extra isn't installed -- only
constructing or using a client should ever require it."""

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from anydoc.ocr_clients.base import ClientConfigError

ENDPOINT_ENV = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
KEY_ENV = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
POLL_SECONDS_ENV = "ANYDOC_OCR_POLL_SECONDS"
CALLS_PER_MINUTE_ENV = "ANYDOC_OCR_CALLS_PER_MINUTE"
PAGE_TIMEOUT_SECONDS_ENV = "ANYDOC_OCR_PAGE_TIMEOUT_SECONDS"
TIER_ENV = "ANYDOC_AZURE_TIER"

_log = logging.getLogger(__name__)


class Profile(NamedTuple):
    """Defaults for the two rate settings. An explicit env var always wins."""

    poll_seconds: float
    calls_per_minute: float


# The tier cannot be read from the key, only from the management API. So
# `auto` starts on PAID and drops to FREE when the service's own 429 names F0.
# A page takes seconds, so on the free tier two or three checks are enough.
FREE_PROFILE = Profile(poll_seconds=4.0, calls_per_minute=20.0)  # F0's limit
PAID_PROFILE = Profile(poll_seconds=1.0, calls_per_minute=300.0)  # S0 allows far more
TIERS = ("auto", "free", "paid")
DEFAULT_TIER = "auto"
FREE_TIER_NAME = re.compile(r"\bF0\b")  # as the 429 text names it: "FormRecognizer F0 pricing tier"
FREE_TIER_WARNING = (
    "Azure Document Intelligence is on the free tier (%s): it allows about 20 calls a minute, "
    "so OCR will be slow (roughly 7 to 10 pages a minute). A paid tier removes the limit."
)
DEFAULT_PAGE_TIMEOUT_SECONDS = 120.0
# How the 429 answer is handled: how often the same request is retried, how
# long to wait when the service names no time, and the cap on a named time.
MAX_RATE_LIMIT_RETRIES = 4
DEFAULT_RETRY_AFTER_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 30.0
RATE_LIMITED_STATUS = 429
SECONDS_PER_MINUTE = 60.0
_BUDGET_KWARG = "anydoc_page_budget"  # request option carrying a page's `_PageBudget`


def is_requested() -> bool:
    """Whether Azure looks configured at all -- checked by anydoc's
    dispatch logic before trying to construct a client, so a caller with
    neither variable set gets the original `NeedsOcrError` (unchanged
    behavior) instead of an Azure-specific error about a service they
    never asked for."""
    return bool(os.environ.get(ENDPOINT_ENV) or os.environ.get(KEY_ENV))


def client() -> "AzureDiClient":
    """This module's half of the engine contract -- see `ocr_clients`."""
    return AzureDiClient.from_env()


def _setting(name: str, default: "float | None") -> "float | None":
    """A positive number from the environment. Empty is treated as unset, as
    for `ANYDOC_OCR_MAX_WORKERS`: a Compose `${NAME:-}` default sets the
    variable to an empty string rather than leaving it out."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise ClientConfigError(f"{name} must be a positive number, got {raw!r}")
    return value


class _PageTimeout(Exception):
    """Not an `AzureError` on purpose: the SDK's retry policy retries those,
    and a page past its deadline must stop, not retry."""


class _RateLimited(Exception):
    """The service kept answering 429 after every allowed retry."""


@dataclass
class _PageBudget:
    """One page's wall-clock allowance, shared by its submit, every poll and
    every retry. Handed to the pipeline as a request option."""

    deadline: float
    timeout_s: float
    rate_limited: bool = False

    def timeout_error(self) -> _PageTimeout:
        cause = "; the service kept answering 429 (rate limit)" if self.rate_limited else ""
        return _PageTimeout(f"page did not finish within {self.timeout_s:g} s{cause}")


class _CallLimiter:
    """Spaces every request, from every worker, `60 / calls_per_minute` apart:
    each caller reserves the next free slot under the lock and sleeps until
    it outside the lock. A fixed spacing, not a bucket, so a burst can never
    exceed the per-minute limit. Clock and sleep are injectable for tests."""

    def __init__(
        self, calls_per_minute: float, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self._interval = SECONDS_PER_MINUTE / calls_per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free = 0.0

    def set_calls_per_minute(self, calls_per_minute: float) -> None:
        """Takes effect for every slot reserved from now on."""
        with self._lock:
            self._interval = SECONDS_PER_MINUTE / calls_per_minute

    def acquire(self, budget: _PageBudget) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_free)
            if slot >= budget.deadline:
                raise budget.timeout_error()  # the slot is past the deadline: do not reserve it
            self._next_free = slot + self._interval
        if slot > now:
            self._sleep(slot - now)


class _RatePolicy:
    """The one policy that sits in the pipeline's retry loop, so it sees every
    request the SDK sends (the submit, every poll, every library retry).
    Before each: enforce the page deadline and take a limiter slot. On 429:
    wait what the service names (capped), tell the notifier, resend, a bounded
    number of times. On any other answer: drop `Retry-After`, so the SDK polls
    at `polling_interval` -- it otherwise prefers the service's header (1 s)."""

    def __init__(
        self, limiter: _CallLimiter, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self._limiter = limiter
        self._clock = clock
        self._sleep = sleep
        self.notify: Callable[[float], None] = lambda seconds: None
        self.free_tier_named: Callable[[], None] = lambda: None  # a 429 said "F0"
        self.next: Any = None  # set by the pipeline

    def send(self, request: Any) -> Any:
        budget = request.context.get(_BUDGET_KWARG) or request.context.options.pop(_BUDGET_KWARG)
        request.context[_BUDGET_KWARG] = budget  # later attempts and polls find it here
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            self._limiter.acquire(budget)
            response = self.next.send(request)
            http = response.http_response
            if http.status_code != RATE_LIMITED_STATUS:
                http.headers.pop("Retry-After", None)
                return response
            budget.rate_limited = True
            http.read()  # no-op for an already read body; the checks below need the text
            if FREE_TIER_NAME.search(http.text()):
                self.free_tier_named()
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise _RateLimited(
                    f"the service kept answering 429 (rate limit) after {attempt} retries: {http.text()[:300]}"
                )
            wait = _retry_after(http.headers.get("Retry-After"))
            if self._clock() + wait >= budget.deadline:
                raise budget.timeout_error()
            self.notify(wait)
            self._sleep(wait)
        raise AssertionError("unreachable")  # the loop always returns or raises


def _retry_after(header: "str | None") -> float:
    try:
        seconds = float(header) if header else DEFAULT_RETRY_AFTER_SECONDS
    except ValueError:
        seconds = DEFAULT_RETRY_AFTER_SECONDS  # an HTTP date: not worth parsing for a few seconds
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class AzureDiClient:
    """One Azure `prebuilt-layout` call per page. Construct via `from_env`,
    not directly -- that's where config validation and the optional-import
    check happen."""

    def __init__(
        self,
        client,
        *,
        policy: "_RatePolicy | None" = None,
        limiter: "_CallLimiter | None" = None,
        tier: str = DEFAULT_TIER,
        poll_seconds: "float | None" = None,
        calls_per_minute: "float | None" = None,
        page_timeout_s: float = DEFAULT_PAGE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._policy = policy
        self._limiter = limiter
        self._tier = tier
        self._poll_override = poll_seconds  # explicit settings: they beat any profile
        self._calls_override = calls_per_minute
        self._poll_seconds = poll_seconds or (FREE_PROFILE if tier == "free" else PAID_PROFILE).poll_seconds
        self._page_timeout_s = page_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        if policy:
            policy.free_tier_named = self._free_tier_named

    def _free_tier_named(self) -> None:
        """Once: the service's own 429 named the free tier, so move the shared
        limiter and the poll interval to the FREE profile for good. Pages
        already waiting go on under the new rate."""
        with self._lock:
            if self._tier != "auto":
                return
            self._tier = "free"
        _log.warning(FREE_TIER_WARNING, "detected from the service's own 429 answer")
        self._apply_profile(FREE_PROFILE)

    def _apply_profile(self, profile: Profile) -> None:
        if self._poll_override is None:
            self._poll_seconds = profile.poll_seconds
        if self._limiter and self._calls_override is None:
            self._limiter.set_calls_per_minute(profile.calls_per_minute)

    def set_rate_limit_notifier(self, notify: Callable[[float], None]) -> None:
        """Optional hook (engines that never rate-limit need not have it):
        `notify(seconds)` is called, from whichever thread is waiting, each
        time the run sleeps because the service said to. It must be thread
        safe and quick; `_parse_ocr` hands it a queue's `put`."""
        if self._policy:
            self._policy.notify = notify

    @classmethod
    def from_env(
        cls,
        *,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "AzureDiClient":
        endpoint = os.environ.get(ENDPOINT_ENV)
        key = os.environ.get(KEY_ENV)
        if not endpoint or not key:
            raise ClientConfigError(
                f"Azure Document Intelligence needs both {ENDPOINT_ENV} and {KEY_ENV} set; only one is"
            )
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:
            raise ClientConfigError(
                "Azure Document Intelligence OCR requires the 'azure' extra: "
                "pip install firecrawl-anydoc[azure]"
            ) from exc
        tier = (os.environ.get(TIER_ENV) or DEFAULT_TIER).strip().lower()
        if tier not in TIERS:
            raise ClientConfigError(f"{TIER_ENV} must be one of {', '.join(TIERS)}, got {tier!r}")
        profile = FREE_PROFILE if tier == "free" else PAID_PROFILE  # `auto` starts paid
        poll_seconds = _setting(POLL_SECONDS_ENV, None)
        calls_per_minute = _setting(CALLS_PER_MINUTE_ENV, None)
        page_timeout_s = _setting(PAGE_TIMEOUT_SECONDS_ENV, DEFAULT_PAGE_TIMEOUT_SECONDS)
        limiter = _CallLimiter(calls_per_minute or profile.calls_per_minute, clock, sleep)
        if tier == "free":
            _log.warning(FREE_TIER_WARNING, "set by " + TIER_ENV)
        policy = _RatePolicy(limiter, clock, sleep)
        # The generated client ignores `per_retry_policies`. `custom_hook_policy`
        # is the one documented slot that sits inside the retry loop, so the
        # policy goes there (we use no request/response hooks).
        extra: "dict[str, Any]" = {"transport": transport} if transport is not None else {}
        sdk = DocumentIntelligenceClient(endpoint, AzureKeyCredential(key), custom_hook_policy=policy, **extra)
        return cls(
            sdk,
            policy=policy,
            limiter=limiter,
            tier=tier,
            poll_seconds=poll_seconds,
            calls_per_minute=calls_per_minute,
            page_timeout_s=page_timeout_s,
            clock=clock,
        )

    def ocr_page(self, page_pdf_bytes: bytes) -> str:
        # output_content_format=MARKDOWN returns one joined string per job
        # with no documented per-page span boundary -- the caller is
        # responsible for only ever passing a single page's bytes here,
        # never a multi-page document, or a combined result can't be
        # safely split back apart afterward.
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat

        poller = self._client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(bytes_source=page_pdf_bytes),
            output_content_format=DocumentContentFormat.MARKDOWN,
            polling_interval=self._poll_seconds,
            **{_BUDGET_KWARG: _PageBudget(self._clock() + self._page_timeout_s, self._page_timeout_s)},
        )
        return poller.result().content
