"""Per-address rate limits, so nobody can guess room codes or hammer the server from one machine."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

# bucket: (how many, per how many seconds). The admin page's Rate limits setting scales the counts.
# Generous on purpose: a whole party can share one address on a venue's wifi.
LIMITS = {
    "miss": (30, 600),     # looking up a room code that doesn't exist
    "create": (20, 3600),  # starting a room
    "join": (60, 600),     # joining a room as a singer
    "search": (60, 60),    # YouTube searches
    "pair": (20, 600),     # a screen asking the host to let it in
    "account": (30, 3600),  # making a guest account
    "link": (60, 600),     # "this is me" codes to sign another phone in
}  # wrong passwords have their own fixed caps, in guards.Logins
SOCKETS = 100  # open connections from one address at once, also scaled


class Limiter:
    def __init__(self):
        self.hits: dict = {}
        self.lock = threading.RLock()

    def _recent(self, key: tuple, window: float, now: float) -> deque:
        times = self.hits.get(key)
        if times is None:
            # ponytail: one deque per address and bucket, dropped once it empties. Bounded by how
            # many addresses show up in a window; a big public server would want this in Redis.
            if len(self.hits) > 50000:
                self.prune(now)
            times = self.hits[key] = deque()
        while times and times[0] <= now - window:
            times.popleft()
        return times

    def prune(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        longest = max(window for _, window in LIMITS.values())
        for key in [k for k, t in self.hits.items() if not t or t[-1] <= now - longest]:
            del self.hits[key]

    def full(self, bucket: str, address: str, scale: float, now: Optional[float] = None) -> bool:
        """True when this address has used up the bucket. A scale of 0 means no limits."""
        if scale <= 0:
            return False
        count, window = LIMITS[bucket]
        with self.lock:
            return len(self._recent((bucket, address), window, now or time.monotonic())) >= max(1, round(count * scale))

    def add(self, bucket: str, address: str, now: Optional[float] = None, item: Optional[str] = None) -> None:
        """Uses one from the bucket. With an `item` (say, the wrong room code), the same item
        from the same address counts once per window, so one page retrying a typo costs one."""
        with self.lock:
            now = now or time.monotonic()
            window = LIMITS[bucket][1]
            if item is not None:
                seen = self._recent((bucket, address, item), window, now)
                if seen:
                    return
                seen.append(now)
            self._recent((bucket, address), window, now).append(now)

    def take(self, bucket: str, address: str, scale: float, now: Optional[float] = None) -> bool:
        """Uses one from the bucket. False, and nothing used, when it's already full."""
        with self.lock:
            if self.full(bucket, address, scale, now):
                return False
            self.add(bucket, address, now)
            return True
