"""
Exponential backoff retry decorators for external API calls.

Usage (async):
    @async_retry(max_attempts=5, base_delay=1.0)
    async def call_api():
        ...

Usage (sync):
    @sync_retry(max_attempts=3)
    def call_api():
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Callable, Optional, Tuple, Type

logger = logging.getLogger(__name__)

# Exceptions that indicate rate limiting — try to respect Retry-After header
RATE_LIMIT_EXCEPTIONS: Tuple[Type[Exception], ...] = ()
try:
    import anthropic
    RATE_LIMIT_EXCEPTIONS = (anthropic.RateLimitError, anthropic.APIStatusError)
except ImportError:
    pass


def async_retry(
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    reraise_on: Optional[Tuple[Type[Exception], ...]] = None,
) -> Callable:
    """
    Decorator for async functions. Retries with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including first try).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Cap on delay between retries.
        exceptions: Tuple of exception types to catch and retry on.
        reraise_on: Tuple of exception types to never retry — always re-raise immediately.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    # Never retry these
                    if reraise_on and isinstance(exc, reraise_on):
                        raise

                    if not isinstance(exc, exceptions):
                        raise

                    last_exc = exc

                    if attempt == max_attempts:
                        break

                    # Try to respect Retry-After header for rate limit errors
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    if RATE_LIMIT_EXCEPTIONS and isinstance(exc, RATE_LIMIT_EXCEPTIONS):
                        retry_after = _get_retry_after(exc)
                        if retry_after:
                            delay = max(delay, retry_after)

                    logger.warning(
                        f"{func.__qualname__} attempt {attempt}/{max_attempts} failed: "
                        f"{type(exc).__name__}: {exc}. Retrying in {delay:.1f}s."
                    )
                    await asyncio.sleep(delay)

            logger.error(
                f"{func.__qualname__} failed after {max_attempts} attempts. "
                f"Last error: {type(last_exc).__name__}: {last_exc}"
            )
            raise last_exc  # type: ignore[misc]

        return wrapper
    return decorator


def sync_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Decorator for synchronous functions. Retries with exponential backoff.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if not isinstance(exc, exceptions):
                        raise
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        f"{func.__qualname__} attempt {attempt}/{max_attempts} failed: "
                        f"{type(exc).__name__}: {exc}. Retrying in {delay:.1f}s."
                    )
                    time.sleep(delay)

            raise last_exc  # type: ignore[misc]

        return wrapper
    return decorator


def _get_retry_after(exc: Exception) -> Optional[float]:
    """Extract Retry-After header value from rate limit exceptions."""
    # anthropic SDK stores headers on the response attribute
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                return float(retry_after)
    except (AttributeError, ValueError):
        pass
    return None
