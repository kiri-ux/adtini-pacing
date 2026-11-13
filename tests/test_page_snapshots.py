"""A deploy starts with an empty memory cache; the saved copy fills in."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time

import pytest


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/snap.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
    )
    os.environ["DATABASE_URL"] = url
    import config

    config.get_settings.cache_clear()
    import db as db_module

    importlib.reload(db_module)


def test_a_saved_copy_outlives_memory_and_an_edit_drops_it(site):
    import pagecache

    pagecache.persist_enabled = True
    try:
        pagecache._to_save.put(("/orders/5?", b"<p>five</p>"))
        pagecache._to_save.put(("/?", b"<p>table</p>"))
        pagecache._to_save.put(("/orders/6?", b"<p>six</p>"))
        threading.Thread(target=pagecache._save_snapshots, daemon=True).start()
        for _ in range(50):
            if pagecache._to_save.empty() and pagecache._load_snapshot("/orders/6?"):
                break
            time.sleep(0.05)
        pagecache.clear()
        assert pagecache.stale("/orders/5?") == b"<p>five</p>"
        pagecache.edited(5)
        assert pagecache.stale("/orders/5?") is None
        assert pagecache.stale("/?") is None
        assert pagecache.stale("/orders/6?") == b"<p>six</p>"
    finally:
        pagecache.persist_enabled = False


def test_a_copy_from_before_the_newest_delivery_is_not_served_stale():
    import datetime as dt
    import pagecache

    pagecache.clear()
    key = pagecache.key("/orders/1", (None, 0, None))
    pagecache.put(key, b"old")
    built = pagecache._latest_built["/orders/1"]
    assert pagecache.stale("/orders/1", built - dt.timedelta(minutes=1)) == b"old"
    assert pagecache.stale("/orders/1", built + dt.timedelta(minutes=1)) is None
    pagecache.clear()
