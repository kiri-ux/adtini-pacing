"""Where a page's build time goes, so the slow part can be fixed rather than
guessed at. Each built page records its total and the main steps inside it;
/timings lists the slowest recent builds from both workers."""
from __future__ import annotations

import contextvars
import functools
import time
from collections import defaultdict

_steps: contextvars.ContextVar[dict | None] = contextvars.ContextVar("timing_steps", default=None)


def start() -> dict:
    steps: dict = defaultdict(float)
    _steps.set(steps)
    return steps


def stop() -> dict:
    steps = _steps.get() or {}
    _steps.set(None)
    return dict(steps)


def timed(name: str):
    """Add the wrapped call's time to the page being built, if any. Nested
    calls of the same step are counted once, at the outermost call."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            steps = _steps.get()
            if steps is None or steps.get("_in_" + name):
                return fn(*args, **kwargs)
            steps["_in_" + name] = 1
            began = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                steps[name] += time.monotonic() - began
                del steps["_in_" + name]
        return inner
    return wrap
