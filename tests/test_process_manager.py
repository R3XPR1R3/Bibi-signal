"""Tests for the process manager.

We don't actually launch bibi-signal subprocesses (they'd need yfinance);
instead we use a tiny `python -c` sleep process to verify start/stop/status.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bibi_signal import process_manager as pm


@pytest.fixture(autouse=True)
def isolate_run_dir(tmp_path: Path, monkeypatch):
    """Redirect RUN_DIR/LOG_DIR to a temp directory for every test."""
    run_dir = tmp_path / "run"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(pm, "RUN_DIR", run_dir)
    monkeypatch.setattr(pm, "LOG_DIR", log_dir)
    yield


def test_status_for_unknown_mode_is_dead(tmp_path):
    s = pm.status("nothing")
    assert not s.alive
    assert s.pid is None


def _spawn_sleeper(seconds: int = 30) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_status_reports_running_pid(tmp_path):
    proc = _spawn_sleeper(30)
    try:
        pm.RUN_DIR.mkdir(parents=True, exist_ok=True)
        pid_file = pm.RUN_DIR / "fake.pid"
        pid_file.write_text(str(proc.pid))
        s = pm.status("fake")
        assert s.alive
        assert s.pid == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_status_reports_dead_when_pid_gone(tmp_path):
    proc = _spawn_sleeper(0)  # exits immediately
    proc.wait(timeout=5)
    pm.RUN_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = pm.RUN_DIR / "fake.pid"
    pid_file.write_text(str(proc.pid))
    s = pm.status("fake")
    assert not s.alive
    assert s.pid == proc.pid


def test_stop_sends_sigterm_and_cleans_up(tmp_path):
    proc = _spawn_sleeper(30)
    pm.RUN_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = pm.RUN_DIR / "fake.pid"
    pid_file.write_text(str(proc.pid))

    stopped = pm.stop("fake")
    assert stopped is True
    # Process should be reaped within a few seconds
    proc.wait(timeout=5)
    assert not pid_file.exists()


def test_stop_returns_false_when_nothing_running(tmp_path):
    assert pm.stop("nothing") is False


def test_all_statuses_lists_pid_files(tmp_path):
    pm.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (pm.RUN_DIR / "alpha.pid").write_text(str(os.getpid()))
    (pm.RUN_DIR / "beta.pid").write_text("99999999")
    statuses = pm.all_statuses()
    modes = {s.mode for s in statuses}
    assert modes == {"alpha", "beta"}


def test_tail_log_when_missing(tmp_path):
    out = pm.tail_log("nothing")
    assert "no log file" in out


def test_tail_log_returns_last_lines(tmp_path):
    pm.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = pm.LOG_DIR / "fake.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)))
    out = pm.tail_log("fake", lines=5)
    assert "line 99" in out
    assert "line 0" not in out
