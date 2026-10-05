"""Per-host politeness: concurrency cap, minimum spacing between request starts, and
host-wide back-off after ``Retry-After`` (429 / 503)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pagewatch.engine.clock import Clock
from pagewatch.models import Settings


def host_of(url: str) -> str:
    """Host key used for limiting. Local files and anything unparsable share one bucket."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    return (host or "local").lower()


@dataclass(slots=True)
class _State:
    in_flight: int = 0
    next_start: float = 0.0  # monotonic
    backoff_until: float = 0.0


class HostGate:
    def __init__(self, clock: Clock, settings: Callable[[], Settings]) -> None:
        self._clock = clock
        self._settings = settings
        self._hosts: dict[str, _State] = {}

    def limits(self, host: str) -> tuple[int, float]:
        s = self._settings()
        ov = s.host_overrides.get(host)
        conc = ov.concurrency if ov and ov.concurrency is not None else s.per_host_concurrency
        gap = ov.min_gap_s if ov and ov.min_gap_s is not None else s.per_host_min_gap_s
        return conc, gap

    def ready_in(self, host: str) -> float | None:
        """Seconds until a request to ``host`` may start; ``None`` while at the concurrency
        cap (it becomes ready when a request finishes)."""
        st = self._hosts.get(host)
        if st is None:
            return 0.0
        conc, _ = self.limits(host)
        if st.in_flight >= conc:
            return None
        now = self._clock.monotonic()
        return max(0.0, st.next_start - now, st.backoff_until - now)

    def start(self, host: str) -> None:
        st = self._hosts.setdefault(host, _State())
        _, gap = self.limits(host)
        st.in_flight += 1
        st.next_start = self._clock.monotonic() + gap

    def finish(self, host: str) -> None:
        st = self._hosts.get(host)
        if st is None:
            return
        st.in_flight = max(0, st.in_flight - 1)
        if st.in_flight == 0 and not self._active(st):
            del self._hosts[host]  # keep the table small

    def _active(self, st: _State) -> bool:
        now = self._clock.monotonic()
        return st.next_start > now or st.backoff_until > now

    def backoff(self, host: str, seconds: float) -> None:
        st = self._hosts.setdefault(host, _State())
        st.backoff_until = max(st.backoff_until, self._clock.monotonic() + seconds)

    def in_flight(self, host: str) -> int:
        st = self._hosts.get(host)
        return st.in_flight if st else 0

    @property
    def tracked_hosts(self) -> int:
        return len(self._hosts)
