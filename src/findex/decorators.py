"""Small reusable decorators."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable

log = logging.getLogger("findex.timing")


def timed[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Log how long each call to ``func`` took, in milliseconds."""

    @functools.wraps(func)  # keeps __name__, __doc__, __wrapped__ of the original
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            log.info("%s took %.2f ms", func.__qualname__, (time.perf_counter() - t0) * 1000)

    return wrapper
