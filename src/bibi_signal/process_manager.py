"""Process manager for the TUI launcher.

Tracks bots launched in the background by their mode name. PID files
live in `data/run/<mode>.pid`. On start, refuse if the PID file exists
and the process is still alive; on stop, send SIGTERM, then SIGKILL if
the process doesn't exit within a few seconds.

Stdlib only — works fine on Raspberry Pi.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RUN_DIR = Path("data/run")
LOG_DIR = Path("logs")


@dataclass(frozen=True)
class ProcStatus:
    mode: str
    pid: int | None
    alive: bool
    started_at: float | None  # epoch seconds
    log_path: Path | None


def _pid_file(mode: str) -> Path:
    return RUN_DIR / f"{mode}.pid"


def _log_file(mode: str) -> Path:
    return LOG_DIR / f"{mode}.log"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # exists but we don't own it
    return True


def status(mode: str) -> ProcStatus:
    pf = _pid_file(mode)
    if not pf.exists():
        return ProcStatus(mode=mode, pid=None, alive=False, started_at=None, log_path=None)
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return ProcStatus(mode=mode, pid=None, alive=False, started_at=None, log_path=None)
    alive = _process_alive(pid)
    started_at = pf.stat().st_mtime
    return ProcStatus(
        mode=mode, pid=pid, alive=alive, started_at=started_at,
        log_path=_log_file(mode) if _log_file(mode).exists() else None,
    )


def all_statuses() -> list[ProcStatus]:
    if not RUN_DIR.exists():
        return []
    out: list[ProcStatus] = []
    for pf in sorted(RUN_DIR.glob("*.pid")):
        out.append(status(pf.stem))
    return out


def start(mode: str, args: list[str]) -> ProcStatus:
    """Spawn `python -m bibi_signal.main --mode <mode> [args...]` detached.

    stdout/stderr go to logs/<mode>.log.
    """
    existing = status(mode)
    if existing.alive:
        raise RuntimeError(f"{mode} already running (pid {existing.pid})")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "bibi_signal.main", "--mode", mode, *args]
    log = _log_file(mode).open("ab", buffering=0)
    # start_new_session detaches from the parent's process group so the
    # bot survives the TUI exiting.
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=Path.cwd(),
    )
    _pid_file(mode).write_text(str(proc.pid))
    # Touch mtime so started_at is "now".
    os.utime(_pid_file(mode), None)
    # Brief settle so we can catch immediate failures.
    time.sleep(0.5)
    return status(mode)


def stop(mode: str, *, kill_after_seconds: int = 8) -> bool:
    """Returns True if a process was stopped, False if nothing was running."""
    s = status(mode)
    if not s.alive or s.pid is None:
        if _pid_file(mode).exists():
            _pid_file(mode).unlink()
        return False
    try:
        os.kill(s.pid, signal.SIGTERM)
    except ProcessLookupError:
        _pid_file(mode).unlink(missing_ok=True)
        return False

    deadline = time.time() + kill_after_seconds
    while time.time() < deadline:
        if not _process_alive(s.pid):
            _pid_file(mode).unlink(missing_ok=True)
            return True
        time.sleep(0.3)

    # Still alive — escalate.
    try:
        os.kill(s.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _pid_file(mode).unlink(missing_ok=True)
    return True


def tail_log(mode: str, lines: int = 40) -> str:
    p = _log_file(mode)
    if not p.exists():
        return f"(no log file at {p})"
    # Read last ~16KB to avoid loading huge log files into memory.
    with p.open("rb") as f:
        try:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return f"(could not read {p})"
    return "\n".join(data.splitlines()[-lines:])
