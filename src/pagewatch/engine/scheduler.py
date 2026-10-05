"""AutoWatch scheduler: due-time heap, per-host ready queues, bounded worker pools.

Because ``next_due_at`` lives in SQLite, a restart resumes exactly where it stopped. This
class only holds the in-memory view: a min-heap of due times, and ready queues grouped by
host so one slow or rate-limited host never blocks checks for others.

Dispatch rules: at most one in-flight check per bookmark; the global static/browser pools;
the per-host gate (concurrency, spacing, ``Retry-After`` back-off). Manual "Check now" and
hotsites jump the queue.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pagewatch.engine.clock import Clock
from pagewatch.engine.hostgate import HostGate, host_of
from pagewatch.engine.logs import get_logger
from pagewatch.models import CheckMethod, Settings, Trigger

log = get_logger("pagewatch.scheduler")

MAX_IDLE_WAIT_S = 30.0  # re-read the wall clock at least this often (clock changes, sleep)
CRASH_RETRY_S = 300.0
RANK_MANUAL, RANK_HOTSITE, RANK_NORMAL = -2, -1, 0


@dataclass(slots=True)
class RunResult:
    """What a finished check tells the scheduler."""

    next_due: datetime | None
    next_trigger: Trigger = Trigger.SCHEDULE
    browser: bool | None = None  # the bookmark now needs the browser pool (auto-detection)


RunCallback = Callable[[int, Trigger, bool], Awaitable[RunResult | None]]


@dataclass(slots=True)
class _Entry:
    id: int
    host: str
    priority: int
    browser: bool
    enabled: bool = True
    due: float | None = None  # epoch seconds
    seq: int = 0  # invalidates stale heap items
    next_trigger: Trigger = Trigger.SCHEDULE
    override_due: float | None = None  # set by upsert() while a check is running


@dataclass(slots=True)
class _Ready:
    id: int
    trigger: Trigger
    force: bool
    seq: int
    key: tuple[int, float, int] = field(default=(0, 0.0, 0))

    def __lt__(self, other: _Ready) -> bool:
        return self.key < other.key


def uses_browser(method: str) -> bool:
    return method in (CheckMethod.BROWSER.value, CheckMethod.SCREENSHOT.value)


class Scheduler:
    def __init__(
        self,
        clock: Clock,
        settings: Callable[[], Settings],
        gate: HostGate,
        run: RunCallback,
        *,
        on_autowatch_change: Callable[[bool, datetime | None], None] | None = None,
    ) -> None:
        self._clock = clock
        self._settings = settings
        self._gate = gate
        self._run = run
        self._on_autowatch_change = on_autowatch_change
        self._entries: dict[int, _Entry] = {}
        self._heap: list[tuple[float, int, int]] = []  # (due, seq, id)
        self._ready: dict[str, list[_Ready]] = {}
        self._queued: dict[int, int] = {}  # id -> seq of its live ready item
        self._inflight: dict[int, asyncio.Task[None]] = {}
        self._tick = itertools.count(1)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._paused = False
        self._paused_until: float | None = None
        self.online = True
        self._static_active = 0
        self._browser_active = 0
        self._dispatch_from = 0.0  # monotonic: scheduled work waits for the start-up delay

    # -- introspection ------------------------------------------------------------------

    @property
    def queue_length(self) -> int:
        return len(self._queued)

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def paused_until(self) -> datetime | None:
        if self._paused_until is None:
            return None
        return datetime.fromtimestamp(self._paused_until, tz=self._clock.now().tzinfo)

    @property
    def backlog_warning(self) -> bool:
        return self.queue_length > self._settings().backlog_warning

    @property
    def scheduled(self) -> int:
        return sum(1 for e in self._entries.values() if e.due is not None)

    def due_at(self, bookmark_id: int) -> datetime | None:
        e = self._entries.get(bookmark_id)
        if e is None or e.due is None:
            return None
        return datetime.fromtimestamp(e.due, tz=self._clock.now().tzinfo)

    def is_in_flight(self, bookmark_id: int) -> bool:
        return bookmark_id in self._inflight

    def is_queued(self, bookmark_id: int) -> bool:
        return bookmark_id in self._queued

    # -- membership ---------------------------------------------------------------------

    def upsert(
        self,
        bookmark_id: int,
        *,
        url: str,
        priority: int,
        check_method: str,
        enabled: bool,
        due: datetime | None,
        trigger: Trigger = Trigger.SCHEDULE,
    ) -> None:
        e = self._entries.get(bookmark_id)
        if e is None:
            e = self._entries[bookmark_id] = _Entry(bookmark_id, "", 0, False)
        e.host = host_of(url)
        e.priority = priority
        e.browser = uses_browser(check_method)
        e.enabled = enabled
        e.next_trigger = trigger
        e.seq = next(self._tick)
        e.due = due.timestamp() if (due is not None and enabled) else None
        if bookmark_id in self._inflight:
            e.override_due = e.due  # applied when the running check finishes
            e.due = None
        elif e.due is not None and bookmark_id not in self._queued:
            heapq.heappush(self._heap, (e.due, e.seq, bookmark_id))
        if not enabled:
            self._queued.pop(bookmark_id, None)
        self._wake.set()

    def remove(self, bookmark_id: int) -> None:
        self._entries.pop(bookmark_id, None)
        self._queued.pop(bookmark_id, None)
        self._wake.set()

    def load(self, rows: list[tuple[int, str, int, str, bool, datetime | None]]) -> None:
        """Populate from the database: ``(id, url, priority, check_method, enabled, due)``."""
        for bid, url, priority, method, enabled, due in rows:
            self.upsert(
                bid, url=url, priority=priority, check_method=method, enabled=enabled, due=due
            )

    # -- control ------------------------------------------------------------------------

    def pause(self, until: datetime | None = None) -> None:
        self._paused = True
        self._paused_until = until.timestamp() if until else None
        self._wake.set()

    def resume(self) -> None:
        self._paused = False
        self._paused_until = None
        self._wake.set()

    def set_online(self, online: bool) -> None:
        if online != self.online:
            self.online = online
            self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def check_now(self, ids: list[int], *, force: bool = False) -> int:
        """Queue immediate checks that jump the queue. Returns how many were queued; a
        bookmark that is already running is skipped (one in-flight check per bookmark)."""
        now = self._clock.now().timestamp()
        n = 0
        for bid in ids:
            e = self._entries.get(bid)
            if e is None or bid in self._inflight:
                continue
            self._enqueue(e, Trigger.MANUAL, force, RANK_MANUAL, now)
            n += 1
        if n:
            self._wake.set()
        return n

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._dispatch_from = self._clock.monotonic() + self._settings().startup_delay_s
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self, grace_s: float = 10.0) -> None:
        """Stop dispatching, give in-flight checks ``grace_s`` seconds, then cancel them."""
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        tasks = list(self._inflight.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_s)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def wait_idle(self, max_wait_s: float = 30.0) -> None:
        """Test helper: wait until nothing is running and nothing can start right now."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        quiet = 0
        while quiet < 3:
            if loop.time() > deadline:
                raise TimeoutError(
                    f"scheduler not idle: in_flight={self.in_flight} queued={self.queue_length}"
                )
            await asyncio.sleep(0.003)
            if self._is_idle():
                quiet += 1
            else:
                quiet = 0

    def _is_idle(self) -> bool:
        """Nothing running and nothing startable now. Items blocked by host spacing or
        back-off may still be queued: they wait for the clock, not for us."""
        if self._inflight:
            return False
        scheduled_ok = not self._paused and self.online
        if self._queued and self._pick(scheduled_ok)[1] is not None:
            return False
        if not scheduled_ok:
            return True
        now = self._clock.now().timestamp()
        self._clean_heap_head()
        return (
            not self._heap
            or self._heap[0][0] > now
            or (self._clock.monotonic() < self._dispatch_from)
        )

    # -- the loop -----------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            wait = self._pump()
            if self._stopping:
                break
            timeout = MAX_IDLE_WAIT_S if wait is None else min(MAX_IDLE_WAIT_S, max(0.0, wait))
            sleeper = asyncio.ensure_future(self._clock.sleep(timeout))
            waker = asyncio.ensure_future(self._wake.wait())
            _, pending = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _clean_heap_head(self) -> None:
        while self._heap:
            due, seq, bid = self._heap[0]
            e = self._entries.get(bid)
            if e is None or e.seq != seq or e.due != due or not e.enabled:
                heapq.heappop(self._heap)
            else:
                return

    def _pump(self) -> float | None:
        """Move due bookmarks to the ready queues, dispatch what can run, and return the
        number of seconds until something else needs attention (``None`` = nothing)."""
        now = self._clock.now().timestamp()
        mono = self._clock.monotonic()
        if self._paused and self._paused_until is not None and now >= self._paused_until:
            self._paused, self._paused_until = False, None
            if self._on_autowatch_change:
                self._on_autowatch_change(False, None)
        wait: float | None = None
        scheduled_ok = not self._paused and self.online
        if scheduled_ok and mono < self._dispatch_from:
            wait = self._dispatch_from - mono
        elif scheduled_ok:
            while True:
                self._clean_heap_head()
                if not self._heap or self._heap[0][0] > now:
                    break
                due_ts, _, bid = heapq.heappop(self._heap)
                e = self._entries[bid]
                if bid in self._inflight or bid in self._queued:
                    continue
                rank = RANK_HOTSITE if e.priority else RANK_NORMAL
                e.due = None
                self._enqueue(e, e.next_trigger, False, rank, due_ts)
            self._clean_heap_head()
            if self._heap:
                wait = max(0.0, self._heap[0][0] - now)
        blocked = self._dispatch(scheduled_ok)
        if blocked is not None:
            wait = blocked if wait is None else min(wait, blocked)
        return wait

    def _enqueue(self, e: _Entry, trigger: Trigger, force: bool, rank: int, order: float) -> None:
        """``order`` is the due time (older first within a rank)."""
        seq = next(self._tick)
        item = _Ready(e.id, trigger, force, seq, (rank, order, seq))
        heapq.heappush(self._ready.setdefault(e.host, []), item)
        self._queued[e.id] = seq

    def _capacity(self, browser: bool) -> bool:
        s = self._settings()
        if browser:
            return self._browser_active < s.browser_pool
        return self._static_active < s.static_pool

    def _pick(self, scheduled_ok: bool) -> tuple[str | None, _Ready | None, float | None]:
        """The best ready item that may start right now (pools, host gate, pause/offline),
        and the shortest wait until a host-spacing/back-off delay expires for those that may
        not. Has no side effects beyond discarding stale queue heads."""
        best_host: str | None = None
        best: _Ready | None = None
        shortest: float | None = None
        for host, heap in list(self._ready.items()):
            while heap and self._queued.get(heap[0].id) != heap[0].seq:
                heapq.heappop(heap)  # stale (bumped or removed)
            if not heap:
                del self._ready[host]
                continue
            head = heap[0]
            # automatic checks respect pause/offline; manual ones always run
            if not scheduled_ok and head.trigger is not Trigger.MANUAL:
                continue
            e = self._entries.get(head.id)
            if e is None:
                heapq.heappop(heap)
                self._queued.pop(head.id, None)
                continue
            if not self._capacity(e.browser):
                continue
            ready_in = self._gate.ready_in(host)
            if ready_in is None:
                continue  # at the host's concurrency cap: wake when one finishes
            if ready_in > 0:
                shortest = ready_in if shortest is None else min(shortest, ready_in)
                continue
            if best is None or head < best:
                best, best_host = head, host
        return best_host, best, shortest

    def _dispatch(self, scheduled_ok: bool) -> float | None:
        """Start every ready check the pools and the host gate allow. Returns the shortest
        wait until a blocked item's host becomes available, if any."""
        while True:
            host, best, shortest = self._pick(scheduled_ok)
            if best is None or host is None:
                return shortest
            heapq.heappop(self._ready[host])
            if not self._ready[host]:
                del self._ready[host]
            self._queued.pop(best.id, None)
            self._start(self._entries[best.id], best.trigger, best.force)

    def _start(self, e: _Entry, trigger: Trigger, force: bool) -> None:
        self._gate.start(e.host)
        if e.browser:
            self._browser_active += 1
        else:
            self._static_active += 1
        self._inflight[e.id] = asyncio.create_task(
            self._run_one(e, trigger, force), name=f"check-{e.id}"
        )

    async def _run_one(self, e: _Entry, trigger: Trigger, force: bool) -> None:
        result: RunResult | None = None
        crashed = False
        try:
            result = await self._run(e.id, trigger, force)
        except asyncio.CancelledError:
            raise
        except Exception:
            crashed = True
            log.exception("check_crashed", bookmark_id=e.id)
        finally:
            self._gate.finish(e.host)
            if e.browser:
                self._browser_active -= 1
            else:
                self._static_active -= 1
            self._inflight.pop(e.id, None)
        self._reschedule(e, result, crashed)
        self._wake.set()

    def _reschedule(self, e: _Entry, result: RunResult | None, crashed: bool) -> None:
        if self._entries.get(e.id) is not e:
            return  # the bookmark was deleted while its check ran
        if result is not None and result.browser is not None:
            e.browser = result.browser
        override, e.override_due = e.override_due, None
        if override is not None:
            due: float | None = override
        elif crashed:
            due = self._clock.now().timestamp() + CRASH_RETRY_S
            e.next_trigger = Trigger.SCHEDULE
        elif result is not None:
            due = result.next_due.timestamp() if result.next_due else None
            e.next_trigger = result.next_trigger
        else:
            due = None
        e.seq = next(self._tick)
        e.due = due if e.enabled else None
        if e.due is not None and e.id not in self._queued:
            heapq.heappush(self._heap, (e.due, e.seq, e.id))

    def eta(self, seconds: float) -> datetime:
        return self._clock.now() + timedelta(seconds=seconds)
