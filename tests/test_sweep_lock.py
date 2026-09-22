"""The lock that says whether a sweep is running.

Getting this wrong in the "yes" direction is not a harmless error: a True
answer disables the sweep button and every per-file Load button on the Data
page, so the page offers no way to load anything and nothing on it says why.
Clicking Load did nothing at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest


@pytest.fixture()
def app_module(tmp_path):
    import importlib

    import app as module

    importlib.reload(module)
    module.SWEEP_LOCK = tmp_path / "sweep.pid"
    return module


def test_no_lock_means_no_sweep(app_module):
    assert app_module._sweep_running() is False


def test_a_finished_child_is_not_a_running_sweep(app_module):
    """The bug: a subprocess nothing waits on becomes a zombie.

    A zombie keeps its pid, so `os.kill(pid, 0)` reported it alive for as
    long as the worker lived - and the Data page stayed wedged from the first
    sweep until the next deploy.
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    app_module.SWEEP_LOCK.write_text(str(process.pid))

    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(f"/proc/{process.pid}/stat"):
            state = (
                open(f"/proc/{process.pid}/stat")
                .read()
                .rsplit(") ", 1)[-1]
                .split()[0]
            )
            if state == "Z":
                break
        time.sleep(0.05)
    else:
        pytest.skip("the child never became a zombie on this platform")

    # This is the state the live service was in.
    assert app_module._sweep_running() is False
    assert not app_module.SWEEP_LOCK.exists(), "a dead lock must be cleared"


def test_a_running_child_is_a_running_sweep(app_module):
    """The check still has to say yes while a sweep is actually going."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        app_module.SWEEP_LOCK.write_text(str(process.pid))
        assert app_module._sweep_running() is True
        assert app_module.SWEEP_LOCK.exists()
    finally:
        process.kill()
        process.wait()


def test_a_lock_older_than_any_sweep_is_wreckage(app_module):
    """A restart leaves a pid behind that may belong to anything by now."""
    import datetime as dt

    app_module.SWEEP_LOCK.write_text(str(os.getpid()))  # certainly alive
    assert app_module._sweep_running() is True

    old = (
        dt.datetime.now() - app_module.SWEEP_LOCK_MAX_AGE - dt.timedelta(minutes=1)
    ).timestamp()
    os.utime(app_module.SWEEP_LOCK, (old, old))

    assert app_module._sweep_running() is False
    assert not app_module.SWEEP_LOCK.exists()


def test_a_junk_lock_is_not_a_running_sweep(app_module):
    app_module.SWEEP_LOCK.write_text("not a pid")
    assert app_module._sweep_running() is False
