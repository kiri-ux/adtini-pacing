"""Rendered pages, kept until the data under them changes.

The pacing table works out every order on the book to show fifty of them,
and the order pages add up every day of every line item. On the Starter
instance that was seconds a page, for numbers that only change when a drop
is loaded or a buyer saves something.

So a page is rendered once and served from memory until either happens:

* a file is loaded - by the nightly cron, the Data page, or an upload. Each
  writes a row to `ingested_files`, so its newest timestamp and row count
  say whether the data moved. The cron runs in its own process, which is
  why this is read from the database rather than kept here.
* anything is posted to this app - a save, a note, a link. Every POST bumps
  a counter, which drops everything cached.

One gunicorn worker, so one cache. A page carrying a flash message is never
stored: the message is for that one load.
"""
from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
import time
from collections import OrderedDict

from sqlalchemy import func, select

from models import IngestedFile

log = logging.getLogger(__name__)

# Rendered pages kept. Most are the pacing table under a few filters and the
# orders being worked that day; a page is 50-400KB.
MAX_PAGES = 120
# How long the data stamp is trusted before the database is asked again.
STAMP_TTL = 30.0

_lock = threading.Lock()
# One lock per path, held while that page is built. The pacing table under
# every filter is one path, so it is never worked out twice at once; a
# product page has its own and never waits behind it.
_render_locks: dict[str, threading.Lock] = {}


def render_lock(path: str) -> threading.Lock:
    with _lock:
        return _render_locks.setdefault(path, threading.Lock())
_pages: OrderedDict[tuple, bytes] = OrderedDict()
# The last good copy of each page, whatever data it was built from. Served
# while a fresh one is built behind it, so a new drop or a deploy never
# makes anyone wait on the build. Dropped on any edit: after a save the
# page has to show the save.
_latest: OrderedDict[str, bytes] = OrderedDict()
# Sent by the background builder: build this page, never answer it stale.
REFRESH_HEADER = "X-Pacing-Refresh"
_edits = 0
_stamp: tuple | None = None
_stamp_read = 0.0


def data_stamp(session, fresh: bool = False) -> tuple:
    """What the loaded data looks like, cheaply: newest load and file count."""
    global _stamp, _stamp_read
    now = time.monotonic()
    if not fresh and _stamp is not None and now - _stamp_read < STAMP_TTL:
        return _stamp
    newest, count = session.execute(
        select(func.max(IngestedFile.ingested_at), func.count(IngestedFile.id))
    ).one()
    _stamp, _stamp_read = (newest, count), now
    return _stamp


def edited() -> None:
    """Something was saved here. Every cached page may now be wrong."""
    global _edits
    with _lock:
        _edits += 1
        _pages.clear()
        _latest.clear()


def key(path: str, stamp: tuple) -> tuple:
    # Today is in the key because pages default a date input to it.
    return (path, stamp, _edits, dt.date.today())


def get(page_key: tuple) -> bytes | None:
    with _lock:
        body = _pages.get(page_key)
        if body is not None:
            _pages.move_to_end(page_key)
        return body


def put(page_key: tuple, body: bytes) -> None:
    with _lock:
        if page_key[2] != _edits:
            return  # an edit landed while this was rendering
        _pages[page_key] = body
        _pages.move_to_end(page_key)
        while len(_pages) > MAX_PAGES:
            _pages.popitem(last=False)
        _latest[page_key[0]] = body
        _latest.move_to_end(page_key[0])
        while len(_latest) > MAX_PAGES:
            _latest.popitem(last=False)


def stale(path: str) -> bytes | None:
    """The last copy of this page built from older data, if there is one."""
    with _lock:
        return _latest.get(path)


def clear() -> None:
    with _lock:
        _pages.clear()
        _latest.clear()


_wanted: queue.Queue[str] = queue.Queue()


def prefetch(paths) -> None:
    """Build these pages in the background, ahead of the click.

    The order page asks for each of its product pages, so opening one from
    the Products table is served from memory.
    """
    for path in paths:
        _wanted.put(path)


def start_warmer(app, paths: tuple[str, ...] = ("/",), every: float = 60.0) -> None:
    """Re-render the pacing table whenever new data lands.

    The first person to open it after the nightly load would otherwise wait
    for the whole book to be worked out.
    """
    from db import session_scope

    def loop() -> None:
        seen = None
        time.sleep(every)  # let the instance finish starting first
        while True:
            try:
                with session_scope() as session:
                    stamp = data_stamp(session, fresh=True)
                if stamp != seen:
                    seen = stamp
                    client = app.test_client()
                    with client.session_transaction() as sess:
                        sess["ok"] = True
                    for path in paths:
                        started = time.monotonic()
                        client.get(path)
                        log.info(
                            "warmed %s in %.1fs", path, time.monotonic() - started
                        )
            except Exception:  # never let the warmer take the worker down
                log.exception("page warmer")
            time.sleep(every)

    def fetch() -> None:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["ok"] = True
        while True:
            path = _wanted.get()
            try:
                client.get(path, headers={REFRESH_HEADER: "1"})
            except Exception:
                log.exception("prefetch %s", path)

    threading.Thread(target=loop, name="page-warmer", daemon=True).start()
    threading.Thread(target=fetch, name="page-prefetch", daemon=True).start()
