"""Protection against senders that flood the server: rate limits and a log that cannot be flooded.

Both work per key (a sender's address or IP) and forget keys that went quiet, so a stream of
forged sender addresses cannot make these tables grow without end either.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable
from dataclasses import dataclass

from s3lobby.constants import LOG_LINE_BURST, MAX_LOG_LINES_PER_SECOND


@dataclass
class TokenBucket:
    """Allows an average of refill_per_second events with bursts up to capacity."""

    capacity: float
    refill_per_second: float
    tokens: float
    last_refill_seconds: float

    def try_take(self, now_seconds: float) -> bool:
        """Refill the tokens for the time since the last call, then use one up; False when none is left."""
        # A clock that steps back must not drain the bucket with a negative refill.
        elapsed_seconds = max(0.0, now_seconds - self.last_refill_seconds)
        self.tokens = min(self.capacity, self.tokens + elapsed_seconds * self.refill_per_second)
        self.last_refill_seconds = now_seconds
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def has_token_at(self, now_seconds: float) -> bool:
        """True when a token would be available at now_seconds; neither takes it nor changes the bucket."""
        elapsed_seconds = max(0.0, now_seconds - self.last_refill_seconds)
        return self.tokens + elapsed_seconds * self.refill_per_second >= 1.0

    def is_full_at(self, now_seconds: float) -> bool:
        """True when the bucket would be full again: the key has been quiet long enough to forget."""
        elapsed_seconds = now_seconds - self.last_refill_seconds
        return self.tokens + elapsed_seconds * self.refill_per_second >= self.capacity


class RateLimiter:
    """One token bucket per key."""

    def __init__(self, events_per_second: float, burst: float) -> None:
        """Allow each key events_per_second events on average, with bursts of up to burst events."""
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
        """Use up one event of the key without refusing anything; is_exhausted later tells whether the
        key went over its limit."""
        self._bucket_for(key, now_seconds).try_take(now_seconds)

    def _bucket_for(self, key: Hashable, now_seconds: float) -> TokenBucket:
        """The key's bucket, created full on first use so that a new key starts with its whole burst."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._burst, self._events_per_second, self._burst, now_seconds)
            self._buckets[key] = bucket
        return bucket

    def forget_quiet_keys(self, now_seconds: float) -> None:
        """Drop the buckets that have refilled completely.

        A full bucket behaves exactly like a new one, so forgetting it changes nothing for that key but
        keeps a stream of forged sender addresses from growing the table."""
        quiet_keys = [key for key, bucket in self._buckets.items() if bucket.is_full_at(now_seconds)]
        for key in quiet_keys:
            del self._buckets[key]

    @property
    def tracked_key_count(self) -> int:
        """How many keys currently have a bucket."""
        return len(self._buckets)


TOTAL_BUDGET_KEY = "all"


class ThrottledLogger:
    """Logs a message about a key at most once per interval, and says how many it held back.

    Warnings and errors about one key are throttled apart, so a bug's traceback is never held back
    behind a warning. On top, all keys share a budget of lines per second: every forged sender
    address is a key of its own, and without the budget a flood of them would fill the disk."""

    def __init__(self, logger: logging.Logger, interval_seconds: float) -> None:
        """Log about each key at most once per interval_seconds and per level."""
        self._logger = logger
        self._interval_seconds = interval_seconds
        self._last_logged_seconds: dict[Hashable, float] = {}
        self._suppressed_counts: dict[Hashable, int] = {}
        self._total_lines = RateLimiter(MAX_LOG_LINES_PER_SECOND, LOG_LINE_BURST)
        self._lines_over_budget = 0

    def warning(self, key: Hashable, now_seconds: float, message: str, *arguments: object) -> None:
        """Log a warning about the key, unless one about it went out within the interval or the shared
        line budget is used up; either way it is counted as held back."""
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
        """Log one line at level unless throttled.

        The interval is kept per level and key, the line budget is shared by all keys. The first line
        that gets through after a quiet spell says how many similar ones were held back before it."""
        throttle_key = (level, key)
        last_logged = self._last_logged_seconds.get(throttle_key)
        is_within_interval = last_logged is not None and now_seconds - last_logged < self._interval_seconds
        if is_within_interval:
            self._suppressed_counts[throttle_key] = self._suppressed_counts.get(throttle_key, 0) + 1
            return
        # Lines over the shared budget are only counted in total, not per key, and the key's interval
        # does not start: the next line about it may get through once the budget allows.
        if not self._total_lines.allows(TOTAL_BUDGET_KEY, now_seconds):
            self._lines_over_budget += 1
            return
        suppressed_count = self._suppressed_counts.pop(throttle_key, 0)
        if suppressed_count:
            message += f" ({suppressed_count} similar messages held back)"
        self._logger.log(level, message, *arguments, exc_info=include_traceback)
        self._last_logged_seconds[throttle_key] = now_seconds

    def forget_quiet_keys(self, now_seconds: float) -> None:
        """Drop keys whose interval has passed, after reporting the lines (warnings or errors) still
        held back for them."""
        quiet_keys = [
            key
            for key, last_logged in self._last_logged_seconds.items()
            if now_seconds - last_logged >= self._interval_seconds
        ]
        # These reports skip the line budget, but each belongs to a line that did go out within it,
        # so there are never more of them than lines logged.
        for throttle_key in quiet_keys:
            suppressed_count = self._suppressed_counts.pop(throttle_key, 0)
            if suppressed_count:
                # Throttle keys are (level, key); the report names only the key.
                _, key = throttle_key
                self._logger.warning("%s: %d more similar messages held back", key, suppressed_count)
            del self._last_logged_seconds[throttle_key]
        if self._lines_over_budget:
            self._logger.warning(
                "%d log messages held back: more than the log takes per second", self._lines_over_budget
            )
            self._lines_over_budget = 0
        self._total_lines.forget_quiet_keys(now_seconds)
