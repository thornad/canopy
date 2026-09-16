"""Tests for the server entry point's parent watchdog."""

import os
import subprocess
import sys
import threading

from canopy.__main__ import watch_parent


def test_fires_when_parent_is_not_ours():
    orphaned = threading.Event()
    # A pid that is not our parent behaves like a parent that already exited.
    watch_parent(os.getppid() + 1_000_000, orphaned.set, interval=0.01)
    assert orphaned.wait(timeout=2)


def test_stays_quiet_while_parent_lives():
    orphaned = threading.Event()
    watch_parent(os.getppid(), orphaned.set, interval=0.01)
    assert not orphaned.wait(timeout=0.2)


def test_child_exits_after_parent_is_killed(tmp_path):
    # Parent spawns a watcher child in its own session (like the app does),
    # prints the child's pid, then gets SIGKILLed. The child must notice.
    child_code = (
        "import os, sys, time;"
        "from canopy.__main__ import watch_parent;"
        "watch_parent(int(sys.argv[1]), lambda: os._exit(0), interval=0.05);"
        "time.sleep(30); os._exit(1)"
    )
    parent_code = (
        "import os, subprocess, sys;"
        f"p = subprocess.Popen([sys.executable, '-c', {child_code!r}, str(os.getpid())], start_new_session=True);"
        "print(p.pid, flush=True);"
        "import time; time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait()
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        threading.Event().wait(0.05)
    os.kill(child_pid, 9)
    raise AssertionError("watcher child outlived its killed parent")
