"""Protection against senders that flood the server: rate limits and a log that cannot be flooded.

Both work per key (a sender's address or IP) and forget keys that went quiet, so a stream of
forged sender addresses cannot make these tables grow without end either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Hashable


@dataclass
class TokenBucket:
    """Allows an average of refill_per_second events with bursts up to capacity."""

    capacity: float
    refill_per_second: float
    tokens: float
    last_refill_seconds: float

    def try_take(self, now_seconds: float) -> bool:
        elapsed_seconds = max(0.0, now_seconds - self.last_refill_seconds)
        self.tokens = min(self.capacity, self.tokens + elapsed_seconds * self.refill_per_second)
        self.last_refill_seconds = now_seconds
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def has_token_at(self, now_seconds: float) -> bool:
        elapsed_seconds = max(0.0, now_seconds - self.last_refill_seconds)
        return self.tokens + elapsed_seconds * self.refill_per_second >= 1.0

    def is_full_at(self, now_seconds: float) -> bool:
        """True when the bucket would be full again: the key has been quiet long enough to forget."""
        elapsed_seconds = now_seconds - self.last_refill_seconds
        return self.tokens + elapsed_seconds * self.refill_per_second >= self.capacity


class RateLimiter:
    """One token bucket per key."""

    def __init__(self, events_per_second: float, burst: float) -> None:
        self._events_per_second = events_per_second
        self._burst = burst
        self._buckets: dict[Hashable, TokenBucket] = {}

    def allows(self, key: Hashable, now_seconds: float) -> bool:
        """Take one event from the key's bucket; False when the bucket is empty."""
        return self._bucket_for(key, now_seconds).try_take(now_seconds)

    def is_exhausted(self, key: Hashable, now_seconds: float) -> bool:
        """True when the key has no event left, without using one up. Together with
        record_event this limits only some events, for example failed logons."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return False
        return not bucket.has_token_at(now_seconds)

    def record_event(self, key: Hashable, now_seconds: float) -> None:
        self._bucket_for(key, now_seconds).try_take(now_seconds)

    def _bucket_for(self, key: Hashable, now_seconds: float) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._burst, self._events_per_second, self._burst, now_seconds)
            self._buckets[key] = bucket
        return bucket

    def forget_quiet_keys(self, now_seconds: float) -> None:
        quiet_keys = [key for key, bucket in self._buckets.items() if bucket.is_full_at(now_seconds)]
        for key in quiet_keys:
            del self._buckets[key]

    @property
    def tracked_key_count(self) -> int:
        return len(self._buckets)


class ThrottledLogger:
    """Logs a warning about a key at most once per interval, and says how many it held back."""

    def __init__(self, logger: logging.Logger, interval_seconds: float) -> None:
        self._logger = logger
        self._interval_seconds = interval_seconds
        self._last_logged_seconds: dict[Hashable, float] = {}
        self._suppressed_counts: dict[Hashable, int] = {}

    def warning(self, key: Hashable, now_seconds: float, message: str, *arguments: object) -> None:
        self._log(logging.WARNING, key, now_seconds, message, arguments, include_traceback=False)

    def error_with_traceback(self, key: Hashable, now_seconds: float, message: str, *arguments: object) -> None:
        """For use inside an except block: logs the exception's traceback too."""
        self._log(logging.ERROR, key, now_seconds, message, arguments, include_traceback=True)

    def _log(
        self,
        level: int,
        key: Hashable,
        now_seconds: float,
        message: str,
        arguments: tuple[object, ...],
        include_traceback: bool,
    ) -> None:
        last_logged = self._last_logged_seconds.get(key)
        is_within_interval = last_logged is not None and now_seconds - last_logged < self._interval_seconds
        if is_within_interval:
            self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
            return
        suppressed_count = self._suppressed_counts.pop(key, 0)
        if suppressed_count:
            message += f" ({suppressed_count} similar messages held back)"
        self._logger.log(level, message, *arguments, exc_info=include_traceback)
        self._last_logged_seconds[key] = now_seconds

    def forget_quiet_keys(self, now_seconds: float) -> None:
        """Drop keys whose interval has passed, after reporting warnings still held back for them."""
        quiet_keys = [
            key
            for key, last_logged in self._last_logged_seconds.items()
            if now_seconds - last_logged >= self._interval_seconds
        ]
        for key in quiet_keys:
            suppressed_count = self._suppressed_counts.pop(key, 0)
            if suppressed_count:
                self._logger.warning("%s: %d more similar messages held back", key, suppressed_count)
            del self._last_logged_seconds[key]
