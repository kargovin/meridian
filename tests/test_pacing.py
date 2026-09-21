"""The per-publisher pacer (FR-I3). No database: a ``Source`` is enough on its own."""

import itertools
import threading
import time

from meridian_contract import RightsLevel

from meridian.db.models import Source
from meridian.ingest.pacing import Pacer, round_robin


def _source(source_id: int, rate: int) -> Source:
    return Source(
        source_id=source_id,
        name=f"P{source_id}",
        home_url=f"https://p{source_id}.example",
        rights_level=RightsLevel.BODY_TEXT,
        jurisdiction="GB",
        rate_limit_per_min=rate,
    )


class Clock:
    """A clock that only moves when slept on — the pacer's whole effect made visible."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_requests_to_one_publisher_are_spaced_by_its_rate() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    five_per_min = _source(1, 5)

    assert pacer.acquire(five_per_min) == 0.0
    assert pacer.acquire(five_per_min) == 12.0
    assert pacer.acquire(five_per_min) == 12.0
    assert clock.slept == [12.0, 12.0]


def test_time_already_spent_counts_toward_the_gap() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    source = _source(1, 5)
    pacer.acquire(source)
    clock.now += 9.0  # a fetch that took nine seconds
    assert pacer.acquire(source) == 3.0


def test_crawl_delay_raises_the_gap_and_never_lowers_it() -> None:
    """AC3. The longer of the two promises is the one kept."""
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    source = _source(1, 5)  # 12 s
    pacer.acquire(source)
    assert pacer.acquire(source, floor=30.0) == 30.0
    assert pacer.acquire(source, floor=1.0) == 12.0


def test_publishers_do_not_wait_on_each_other() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    a, b = _source(1, 5), _source(2, 5)
    pacer.acquire(a)
    assert pacer.acquire(b) == 0.0


def test_wait_for_reports_without_reserving() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    source = _source(1, 5)
    assert pacer.wait_for(source) == 0.0
    pacer.acquire(source)
    assert pacer.wait_for(source) == 12.0
    assert pacer.wait_for(source, floor=30.0) == 30.0
    # Asking twice reserved nothing: the real acquire still waits the same 12 s.
    assert pacer.acquire(source) == 12.0
    assert clock.slept == [12.0]


def test_try_acquire_takes_a_slot_within_the_bound() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    source = _source(1, 5)
    pacer.acquire(source)
    assert pacer.try_acquire(source, max_wait=12.0) == 12.0
    assert clock.slept == [12.0]
    # It reserved: the next request waits the full gap again.
    assert pacer.wait_for(source) == 12.0


def test_try_acquire_past_the_bound_reserves_nothing_and_sleeps_nothing() -> None:
    clock = Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    source = _source(1, 5)
    pacer.acquire(source)
    assert pacer.try_acquire(source, max_wait=11.9) is None
    assert clock.slept == []
    # Nothing was reserved: the slot is still 12 s away, not 24.
    assert pacer.wait_for(source) == 12.0
    assert pacer.try_acquire(source, max_wait=30.0, floor=20.0) == 20.0


def test_round_robin_alternates_keys_and_keeps_each_keys_order() -> None:
    items = [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("c", 1)]
    assert round_robin(items, key=lambda item: item[0]) == [
        ("a", 1),
        ("b", 1),
        ("c", 1),
        ("a", 2),
        ("b", 2),
        ("a", 3),
    ]
    assert round_robin([], key=lambda item: item) == []


def test_two_jobs_sharing_one_pacer_keep_one_budget_between_them() -> None:
    """AC3. Discovery and acquire on one scheduler both reach a publisher; the gap holds
    across them, not merely within each.

    Real threads and a real (short) gap: the reservation is taken under the lock, so two
    threads asking at once get consecutive slots rather than the same one.
    """
    stamps: list[float] = []
    lock = threading.Lock()
    pacer = Pacer()  # real clock, real sleep
    source = _source(1, 600)  # 0.1 s gap

    def job() -> None:
        for _ in range(3):
            pacer.acquire(source)
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=job) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    stamps.sort()
    assert len(stamps) == 6
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    # Tolerance for scheduling jitter; the property is that no two requests were near-simultaneous.
    assert all(gap >= 0.08 for gap in gaps), gaps
