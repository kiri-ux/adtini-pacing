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
import re
import threading
import time
import zlib
from collections import OrderedDict

from sqlalchemy import func, select

from models import IngestedFile

log = logging.getLogger(__name__)

# Rendered pages kept. The pacing table under a few filters, My Day, and the
# pilot buyers' running orders (page and Budgeting each); a page is 50-400KB.
MAX_PAGES = 600
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
# When each of those was built. A copy from before the newest delivery load
# shows yesterday's numbers, so it is never served as the stale copy.
_latest_built: dict[str, dt.datetime] = {}
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
    delivery = session.execute(
        select(func.max(IngestedFile.ingested_at)).where(IngestedFile.kind == "delivery")
    ).scalar()
    _stamp, _stamp_read = (newest, count, delivery), now
    return _stamp


# Saves made on one order. Its own pages and the pages that add up every
# order (pacing table, Home, My Day) are dropped; every other order's pages
# stay, so a refresh elsewhere after a save is still served from memory.
_order_edits: dict[int, int] = {}
_shared_edits = 0
_ORDER_PATH = re.compile(r"^/orders/(\d+)(?:[/?]|$)")


def _order_of(path: str) -> int | None:
    match = _ORDER_PATH.match(path)
    return int(match.group(1)) if match else None


def _scope_count(path: str) -> int:
    order_id = _order_of(path)
    return _order_edits.get(order_id, 0) if order_id is not None else _shared_edits


def edited(order_id: int | None = None) -> None:
    """Something was saved here.

    Saved on an order: that order's pages and the shared pages may now be
    wrong. Anywhere else: every cached page may be.
    """
    global _edits, _shared_edits
    with _lock:
        if order_id is None:
            _edits += 1
            _pages.clear()
            _latest.clear()
            _drop_snapshots(None)
            return
        _order_edits[order_id] = _order_edits.get(order_id, 0) + 1
        _shared_edits += 1
        for store in (_pages, _latest):
            for k in list(store):
                path = k[0] if isinstance(k, tuple) else k
                owner = _order_of(path)
                if owner is None or owner == order_id:
                    del store[k]
    _drop_snapshots(order_id)


def edit_count() -> int:
    """Moves on every save, scoped or not; what the warmer watches."""
    return _edits + _shared_edits


def key(path: str, stamp: tuple) -> tuple:
    # Today is in the key because pages default a date input to it.
    return (path, stamp, _edits, _scope_count(path), dt.date.today())


def get(page_key: tuple) -> bytes | None:
    with _lock:
        body = _pages.get(page_key)
        if body is not None:
            _pages.move_to_end(page_key)
        return body


def put(page_key: tuple, body: bytes) -> None:
    with _lock:
        if page_key[2] != _edits or page_key[3] != _scope_count(page_key[0]):
            return  # an edit landed while this was rendering
        _pages[page_key] = body
        _pages.move_to_end(page_key)
        while len(_pages) > MAX_PAGES:
            _pages.popitem(last=False)
        _latest[page_key[0]] = body
        _latest_built[page_key[0]] = dt.datetime.utcnow()
        _latest.move_to_end(page_key[0])
        while len(_latest) > MAX_PAGES:
            _latest.popitem(last=False)
    if persist_enabled:
        _to_save.put((page_key[0], body))


def stale(path: str, since: dt.datetime | None = None) -> bytes | None:
    """The last copy of this page built from older data, if there is one.
    After a deploy memory is empty, so the copy saved in the database.

    `since` is the newest delivery load: a copy built before it carries the
    day before's numbers, and is not offered."""
    with _lock:
        body = _latest.get(path)
        built = _latest_built.get(path)
    if body is not None:
        return body if since is None or (built and built >= since) else None
    if not persist_enabled:
        return None
    return _load_snapshot(path, since)


def clear() -> None:
    with _lock:
        _pages.clear()
        _latest.clear()


# --- copies kept across deploys ---------------------------------------------
# Off until the warmer starts (never under tests).
persist_enabled = False
SNAPSHOTS_KEPT = 400
_to_save: queue.Queue[tuple[str, bytes]] = queue.Queue()


def _load_snapshot(path: str, since: dt.datetime | None = None) -> bytes | None:
    from db import session_scope
    from models import PageSnapshot

    try:
        with session_scope() as session:
            row = session.get(PageSnapshot, path)
            if row is None or (since is not None and row.built_at < since):
                return None
            return zlib.decompress(row.body)
    except Exception:
        log.exception("reading snapshot %s", path)
        return None


def _drop_snapshots(order_id: int | None) -> None:
    """An edit makes the saved copies of its pages wrong too: the order's
    own and every shared page. None drops them all."""
    if not persist_enabled:
        return
    from sqlalchemy import delete, not_, or_
    from db import session_scope
    from models import PageSnapshot

    try:
        with session_scope() as session:
            stmt = delete(PageSnapshot)
            if order_id is not None:
                stmt = stmt.where(or_(
                    not_(PageSnapshot.path.like("/orders/%")),
                    PageSnapshot.path.like(f"/orders/{order_id}/%"),
                    PageSnapshot.path.like(f"/orders/{order_id}?%"),
                    PageSnapshot.path == f"/orders/{order_id}",
                ))
            session.execute(stmt)
    except Exception:
        log.exception("dropping snapshots")


def _save_snapshots() -> None:
    from sqlalchemy import select as _select
    from db import session_scope
    from models import PageSnapshot

    saved = 0
    while True:
        path, body = _to_save.get()
        try:
            with session_scope() as session:
                packed = zlib.compress(body, 6)
                row = session.get(PageSnapshot, path)
                now = dt.datetime.utcnow()
                if row is None:
                    session.add(PageSnapshot(path=path, body=packed, built_at=now))
                else:
                    row.body, row.built_at = packed, now
                saved += 1
                if saved % 50 == 0:
                    old = session.execute(
                        _select(PageSnapshot.path)
                        .order_by(PageSnapshot.built_at.desc())
                        .offset(SNAPSHOTS_KEPT)
                    ).scalars().all()
                    for p in old:
                        session.delete(session.get(PageSnapshot, p))
        except Exception:
            log.exception("saving snapshot %s", path)


_wanted: queue.Queue[str] = queue.Queue()


def prefetch(paths) -> None:
    """Build these pages in the background, ahead of the click.

    The order page asks for each of its product pages, so opening one from
    the Products table is served from memory.
    """
    for path in paths:
        _wanted.put(path)


def start_warmer(app, paths=("/",), every: float = 60.0) -> None:
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
                # The date is in every page's cache key, so a new day needs
                # warming as much as new data does.
                # Any save clears every cached page (`edited`), so the next
                # visitor to each would wait for a rebuild; edits re-warm too.
                with _lock:
                    edits = edit_count()
                if (stamp, dt.date.today(), edits) != seen:
                    full = seen is None or seen[:2] != (stamp, dt.date.today())
                    seen = (stamp, dt.date.today(), edits)
                    client = app.test_client()
                    with client.session_transaction() as sess:
                        sess["ok"] = True
                    with app.test_request_context():
                        todo = paths(full) if callable(paths) else paths
                    for path in todo:
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

    global persist_enabled
    persist_enabled = True
    threading.Thread(target=_save_snapshots, name="page-snapshots", daemon=True).start()
    threading.Thread(target=loop, name="page-warmer", daemon=True).start()
    threading.Thread(target=fetch, name="page-prefetch", daemon=True).start()
