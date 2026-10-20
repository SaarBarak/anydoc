"""Progress reporting for page-level OCR."""

import heapq
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
    """Builds `OcrProgress` events from per-page wall times. Workers call
    `page_started`; everything else is only touched from the calling thread.

    The ETA replays the rest of the run as list scheduling. Each in-flight
    page is expected to finish `mean` after it started (never sooner than a
    tenth of a mean from now, so an overdue page does not zero the estimate),
    and each queued page goes to whichever worker frees up first and takes one
    `mean`. `eta_s` is when the last worker finishes. Unlike
    `remaining * mean / workers`, it credits the part of every in-flight page
    that has already run, which is what keeps the tail honest.

    The clock is injectable so tests can drive it without sleeping."""

    _OVERDUE_FLOOR = 0.1  # fraction of a mean page

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
        self._in_flight: "dict[int, float]" = {}  # page -> start; written by workers

    def page_started(self, page: int) -> None:
        self._in_flight[page] = self._clock()

    def page_done(self, page: int, seconds: float) -> None:
        self._in_flight.pop(page, None)
        self._page_seconds.append(seconds)

    def progress(self) -> OcrProgress:
        now = self._clock()
        done = len(self._page_seconds)
        remaining = self._total - done
        eta = None
        if done:
            eta = self._eta(now, remaining) if remaining else 0.0
        return OcrProgress("ocr", self._engine, done, self._total, now - self._started, eta)

    def _eta(self, now: float, remaining: int) -> float:
        mean = sum(self._page_seconds) / len(self._page_seconds)
        starts = tuple(self._in_flight.values())[: self._workers]
        free = [max(start + mean, now + self._OVERDUE_FLOOR * mean) for start in starts]
        free += [now] * (self._workers - len(free))
        heapq.heapify(free)
        for _ in range(remaining - len(starts)):
            heapq.heappush(free, heapq.heappop(free) + mean)
        return max(0.0, max(free) - now)
