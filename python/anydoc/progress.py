"""Progress reporting for page-level OCR."""

import heapq
import threading
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
    `page_started`; the calling thread does the rest. A lock guards the shared
    state, and the ETA is computed from a copy taken under it, so correctness
    does not lean on the GIL.

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
        self._lock = threading.Lock()
        self._done = 0
        self._seconds = 0.0  # summed wall time of completed pages
        self._in_flight: "dict[int, float]" = {}  # page -> start

    def page_started(self, page: int) -> None:
        start = self._clock()
        with self._lock:
            self._in_flight[page] = start

    def page_done(self, page: int, seconds: float) -> None:
        with self._lock:
            self._in_flight.pop(page, None)
            self._done += 1
            self._seconds += seconds

    def progress(self) -> OcrProgress:
        now = self._clock()
        with self._lock:
            done, seconds = self._done, self._seconds
            starts = list(self._in_flight.values())
        remaining = self._total - done
        eta = None
        if done:
            eta = self._eta(now, remaining, seconds / done, starts) if remaining else 0.0
        return OcrProgress("ocr", self._engine, done, self._total, now - self._started, eta)

    def _eta(self, now: float, remaining: int, mean: float, starts: "list[float]") -> float:
        starts = starts[: self._workers]
        free = [max(start + mean, now + self._OVERDUE_FLOOR * mean) for start in starts]
        free += [now] * (self._workers - len(free))
        # Queued pages, one mean each, go to the earliest-free worker. Free
        # times span at most one mean (an in-flight page started no later than
        # now), so every full round of `workers` queued pages adds exactly one
        # page to every worker: skip those rounds arithmetically and replay
        # only the remainder. Bounded by `workers` whatever `pages_total` says.
        rounds, rest = divmod(max(0, remaining - len(starts)), self._workers)
        free = [t + rounds * mean for t in free]
        heapq.heapify(free)
        for _ in range(rest):
            heapq.heappush(free, heapq.heappop(free) + mean)
        return max(0.0, max(free) - now)
