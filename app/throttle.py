"""Login brute-force protection, the same design as cloud-resource-manager's."""

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

Clock = Callable[[], float]


class FailureWindow:
    """Counts recent failures per key and blocks a key once it reaches the limit.

    A sliding window: only failures from the last `window_seconds` count, so a blocked
    key unblocks on its own as its oldest failure ages out. State lives in this process's
    memory, which fits because logins go to the API, a single process (workers never see
    them). Several API instances would need a shared store such as Redis.
    """

    def __init__(self, limit: int, window_seconds: float, clock: Clock = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._last_sweep = clock()
        # Sync endpoints run on a thread pool, so requests can arrive at the same time.
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """Number of keys currently tracked."""
        return len(self._failures)

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may try again, or 0 if it isn't blocked."""
        with self._lock:
            now = self._clock()
            failures = self._failures.get(key)
            if failures is None:
                return 0
            self._expire(failures, now)
            if len(failures) < self._limit:
                return 0
            return math.ceil(failures[0] + self._window - now)

    def add(self, key: str) -> bool:
        """Record a failure for `key`. True if this failure is the one that blocks it."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            failures = self._failures.setdefault(key, deque())
            self._expire(failures, now)
            failures.append(now)
            return len(failures) == self._limit

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _expire(self, failures: deque[float], now: float) -> None:
        while failures and failures[0] <= now - self._window:
            failures.popleft()

    def _sweep(self, now: float) -> None:
        # Drop keys with no recent failures, at most once per window. Without this,
        # attempts against endless made-up usernames would grow memory forever.
        if now - self._last_sweep < self._window:
            return
        cutoff = now - self._window
        self._failures = {k: f for k, f in self._failures.items() if f and f[-1] > cutoff}
        self._last_sweep = now


class LoginGuard:
    """Brute-force protection for logins, with two independent limits.

    Per account: stops a slow attack on one account spread across many machines.
    Per client: stops one machine working through a list of accounts.
    """

    def __init__(
        self,
        per_account: int,
        per_client: int,
        window_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._accounts = FailureWindow(per_account, window_seconds, clock)
        self._clients = FailureWindow(per_client, window_seconds, clock)

    def retry_after(self, username: str, client: str) -> int:
        """Seconds until this login may be attempted, or 0 if it may be now."""
        return max(self._accounts.retry_after(username), self._clients.retry_after(client))

    def record_failure(self, username: str, client: str) -> None:
        # %r escapes newlines, so an attacker-chosen username can't forge log lines.
        if self._accounts.add(username):
            logger.warning("blocking logins to account %r after repeated failures", username)
        if self._clients.add(client):
            logger.warning("blocking logins from client %r after repeated failures", client)

    def clear_account(self, username: str) -> None:
        self._accounts.clear(username)
