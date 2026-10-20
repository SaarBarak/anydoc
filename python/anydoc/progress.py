"""Progress reporting for page-level OCR."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OcrProgress:
    """One progress event from `_parse_ocr`. `pages_total` counts the pages
    routed to OCR, not the pages in the document, so a 3-page OCR job in a
    95-page PDF reports 3, not 95. `eta_s` is an estimate, and `None` until
    the first page has completed."""

    stage: Literal["ocr"]
    engine: str
    pages_done: int
    pages_total: int
    elapsed_s: float
    eta_s: "float | None"


class EtaTracker:
    """Builds `OcrProgress` events from per-page wall times. Not thread-safe
    on purpose: `_parse_ocr` only touches it from the calling thread.

    `eta_s = remaining * mean_page_seconds / min(workers, remaining)`: the
    remaining pages run `workers` at a time, and fewer than that on the tail.
    The clock is injectable so tests can drive it without sleeping."""

    def __init__(
        self,
        engine: str,
        pages_total: int,
        workers: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._total = pages_total
        self._workers = workers
        self._clock = clock
        self._started = clock()
        self._page_seconds: "list[float]" = []

    def page_done(self, seconds: float) -> None:
        self._page_seconds.append(seconds)

    def progress(self) -> OcrProgress:
        done = len(self._page_seconds)
        remaining = self._total - done
        eta = None
        if done:
            mean = sum(self._page_seconds) / done
            eta = remaining * mean / min(self._workers, remaining) if remaining else 0.0
        return OcrProgress("ocr", self._engine, done, self._total, self._clock() - self._started, eta)
