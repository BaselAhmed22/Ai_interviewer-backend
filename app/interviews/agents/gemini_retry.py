"""Shared retry helper for transient Gemini server-side failures.

A 503 UNAVAILABLE (or any 5xx) from Gemini is usually transient —
an overloaded backend, a brief network hiccup — and worth one or two
quick retries before giving up. A 4xx (bad request, 429 quota-exceeded)
is different: it will fail identically on retry, so it's raised
immediately, unretried, letting the caller's own error-translation logic
(see app/interviews/api/pipeline.py) respond right away instead of
making the candidate wait through a pointless retry loop.
"""
import logging
import time
from typing import Callable, TypeVar

from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], *, max_retries: int = 2, base_delay: float = 1.0) -> T:
    """Calls fn() (a zero-arg callable wrapping one blocking Gemini
    request), retrying with a short linear backoff only on
    genai_errors.ServerError. Any genai_errors.ClientError or other
    exception propagates immediately on the first attempt."""
    last_exc: genai_errors.ServerError | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except genai_errors.ServerError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = base_delay * (attempt + 1)
            logger.warning(
                "Gemini returned a transient server error (attempt %d/%d): %s — retrying in %.1fs.",
                attempt + 1, max_retries + 1, exc, delay,
            )
            time.sleep(delay)
    raise last_exc
