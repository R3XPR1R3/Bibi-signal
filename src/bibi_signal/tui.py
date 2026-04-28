"""Console launcher TUI for Raspberry Pi (and any terminal).

Plain stdlib — no rich/textual deps so it boots fine on a low-end Pi.
Run with: bibi-tui

Lets the user:
    - configure .env (Telegram, Robinhood Crypto)
    - view current cash balances
    - start/stop modes (paper, live) in the background
    - run one-shot modes (backtest, optimize) in the foreground
    - tail logs and check process status
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import database as db
from . import env_io, process_manager
from .config import AppSettings, StrategyConfig
from .database import Environment, init_db


ENV_PATH = Path(".env")
CONFIG_PATH = Path("config.yaml")


# ---------- output helpers (stdlib only, ANSI colors optional) ----------

USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if USE_COLOR else text


def bold(s: str) -> str:
    return _c("1", s)


def green(s: str) -> str:
    return _c("32", s)


def yellow(s: str) -> str:
    return _c("33", s)


def red(s: str) -> str:
    return _c("31", s)


def dim(s: str) -> str:
    return _c("2", s)


def clear_screen() -> None:
    if not USE_COLOR:
        print("\n" * 2)
        return
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def hr() -> None:
    print("─" * 56)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "д", "да")


def pause() -> None:
    input(dim("\n[enter to continue] "))


# ---------- header ----------

def _read_balances() -> tuple[str, str]:
    """Return (live_cash, paper_cash) as strings, or 'n/a' if DB missing."""
    try:
        settings = AppSettings()
    except Exception:
        return "n/a", "n/a"
    try:
        sf = init_db(settings.database_url)
        with sf() as session:
            live = db.get_cash(session, Environment.LIVE)
            paper = db.get_cash(session, Environment.PAPER)
        return f"${float(live):.2f}", f"${float(paper):.2f}"
    except Exception:
        return "n/a", "n/a"


def _config_snippet() -> str:
    if not CONFIG_PATH.exists():
        return red("config.yaml: missing")
    try:
        sc = StrategyConfig.from_yaml(CONFIG_PATH)
        tickers = ", ".join(sc.tickers.keys())
        bits = [
            f"tickers: {tickers}",
            f"interval: {sc.check_frequency_minutes}m",
            f"crypto: {'on' if sc.crypto.enabled else 'off'}",
            f"dividends: {'on' if sc.dividends.enabled else 'off'}",
        ]
        return " · ".join(bits)
    except Exception as e:
        return red(f"config.yaml: invalid ({e})")


def header() -> None:
    clear_screen()
    live_cash, paper_cash = _read_balances()
    print(bold("╔══ Bibi-Signal Launcher " + "═" * 31 + "╗"))
    print(f"  cwd:     {dim(str(Path.cwd()))}")
    print(f"  config:  {_config_snippet()}")
    print(f"  cash:    LIVE {green(live_cash)}   PAPER {yellow(paper_cash)}")
    statuses = process_manager.all_statuses()
    if statuses:
        for s in statuses:
            if s.alive:
                age = (
                    datetime.fromtimestamp(s.started_at).strftime("%H:%M")
                    if s.started_at else "?"
                )
                print(f"  running: {green('●')} {s.mode} pid {s.pid} since {age}")
            else:
                print(f"  stale:   {red('×')} {s.mode} pid {s.pid} (dead)")
    else:
        print(f"  running: {dim('nothing in background')}")
    print(bold("╚" + "═" * 55 + "╝"))


# ---------- configure submenu ----------

ENV_FIELDS = [
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token (from @BotFather)"),
    ("TELEGRAM_ALLOWED_CHAT_IDS", "Allowed Telegram chat IDs (comma-separated)"),
    ("ROBINHOOD_CRYPTO_API_KEY", "Robinhood Crypto API key (optional)"),
    ("ROBINHOOD_CRYPTO_PRIVATE_KEY_B64", "Robinhood Crypto Ed25519 private seed (base64)"),
    ("LOG_LEVEL", "Log level (DEBUG|INFO|WARNING|ERROR)"),
]


def _mask(value: str) -> str:
    if not value:
        return dim("<empty>")
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "***" + value[-4:]


def _menu_configure() -> None:
    while True:
        header()
        print(bold("\n  Configure"))
        env = env_io.read_env(ENV_PATH)
        for i, (key, desc) in enumerate(ENV_FIELDS, 1):
            shown = _mask(env.get(key, ""))
            print(f"   [{i}] {desc}\n       {dim(key)} = {shown}")
        editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi")
        print(f"\n   [c] Open config.yaml in {editor or '$EDITOR'}")
        print(f"   [v] View current config.yaml")
        print(f"   [0] Back")
        choice = input("\n  > ").strip().lower()
        if choice == "0" or choice == "":
            return
        if choice == "c":
            if editor:
                subprocess.call([editor, str(CONFIG_PATH)])
            else:
                print(red("\n  No editor found. Set $EDITOR."))
                pause()
            continue
        if choice == "v":
            if CONFIG_PATH.exists():
                print()
                print(CONFIG_PATH.read_text())
            else:
                print(red("\n  config.yaml does not exist."))
            pause()
            continue
        try:
            idx = int(choice) - 1
            if not 0 <= idx < len(ENV_FIELDS):
                continue
        except ValueError:
            continue
        key, desc = ENV_FIELDS[idx]
        current = env.get(key, "")
        print(f"\n  {desc}")
        print(f"  current: {_mask(current)}")
        new = input("  new value (empty to keep, '-' to clear): ").strip()
        if not new:
            continue
        if new == "-":
            new = ""
        env_io.update_env(ENV_PATH, {key: new})
        print(green(f"  ✓ saved {key}"))
        time.sleep(0.6)


# ---------- run helpers ----------

def _run_foreground(mode: str, extra_args: list[str]) -> None:
    cmd = [sys.executable, "-m", "bibi_signal.main", "--mode", mode, *extra_args]
    print(dim(f"\n$ {' '.join(cmd)}\n"))
    try:
        subprocess.call(cmd)
    except KeyboardInterrupt:
        print(yellow("\n  interrupted"))
    pause()


def _menu_background(mode: str, extra_args: list[str] | None = None) -> None:
    """Start/stop/status for paper or live."""
    extra_args = extra_args or []
    while True:
        header()
        s = process_manager.status(mode)
        if s.alive:
            age = (
                datetime.fromtimestamp(s.started_at).strftime("%Y-%m-%d %H:%M")
                if s.started_at else "?"
            )
            print(bold(f"\n  {mode}") + f"  {green('● running')}  pid {s.pid}  since {age}")
        else:
            print(bold(f"\n  {mode}") + f"  {dim('○ stopped')}")
        print()
        print(f"   [1] Start in background")
        print(f"   [2] Run in foreground (Ctrl+C to stop)")
        print(f"   [3] Stop")
        print(f"   [4] Tail log (last 40 lines)")
        print(f"   [0] Back")
        choice = input("\n  > ").strip()
        if choice == "0" or choice == "":
            return
        if choice == "1":
            try:
                process_manager.start(mode, extra_args)
                print(green(f"  ✓ {mode} started"))
            except RuntimeError as e:
                print(red(f"  {e}"))
            pause()
        elif choice == "2":
            _run_foreground(mode, extra_args)
        elif choice == "3":
            stopped = process_manager.stop(mode)
            print(green(f"  ✓ stopped") if stopped else dim(f"  nothing was running"))
            pause()
        elif choice == "4":
            print()
            print(process_manager.tail_log(mode))
            pause()


# ---------- one-shot menus (backtest, optimize) ----------

def _menu_backtest() -> None:
    header()
    try:
        sc = StrategyConfig.from_yaml(CONFIG_PATH)
    except Exception as e:
        print(red(f"  config error: {e}"))
        pause()
        return
    tickers = list(sc.tickers)
    print(bold("\n  Backtest"))
    print(f"  tickers in config: {', '.join(tickers)}")
    ticker = ask("  ticker", default=tickers[0] if tickers else "QQQ")
    years = ask("  years of history", default="5")
    cash = ask("  starting cash (blank = config default)", default="")
    args = ["--ticker", ticker, "--years", years]
    if cash:
        args += ["--starting-cash", cash]
    _run_foreground("backtest", args)


def _menu_optimize() -> None:
    header()
    try:
        sc = StrategyConfig.from_yaml(CONFIG_PATH)
    except Exception as e:
        print(red(f"  config error: {e}"))
        pause()
        return
    tickers = list(sc.tickers)
    print(bold("\n  Optimize (parallel grid search)"))
    print(f"  tickers in config: {', '.join(tickers)}")
    ticker = ask("  ticker", default=tickers[0] if tickers else "QQQ")
    years = ask("  years of history", default="5")
    top = ask("  show top N combos", default="10")
    args = ["--ticker", ticker, "--years", years, "--top", top]
    _run_foreground("optimize", args)


def _menu_paper() -> None:
    header()
    print(bold("\n  Paper trading (virtual money on live prices)"))
    no_hours = confirm("  ignore market hours? (useful on weekends)", default=False)
    extra = ["--no-market-hours"] if no_hours else []
    _menu_background("paper", extra)


def _menu_live() -> None:
    env = env_io.read_env(ENV_PATH)
    if not env.get("TELEGRAM_BOT_TOKEN"):
        header()
        print(red("\n  Live mode requires TELEGRAM_BOT_TOKEN."))
        print(dim("  Configure it from the main menu first."))
        pause()
        return
    _menu_background("live")


def _menu_status() -> None:
    header()
    print(bold("\n  Process status"))
    statuses = process_manager.all_statuses()
    if not statuses:
        print(dim("  no PID files in data/run/"))
    for s in statuses:
        flag = green("● running") if s.alive else red("× dead   ")
        age = (
            datetime.fromtimestamp(s.started_at).strftime("%Y-%m-%d %H:%M:%S")
            if s.started_at else "?"
        )
        print(f"  {flag}  {s.mode:<10} pid {s.pid}  since {age}")
        if s.log_path:
            print(f"             log: {s.log_path}")
    pause()


def _menu_logs() -> None:
    header()
    print(bold("\n  Tail logs"))
    statuses = process_manager.all_statuses()
    if not statuses:
        print(dim("  no logs yet"))
        pause()
        return
    for i, s in enumerate(statuses, 1):
        print(f"   [{i}] {s.mode}")
    print("   [0] Back")
    choice = input("\n  > ").strip()
    if not choice or choice == "0":
        return
    try:
        s = statuses[int(choice) - 1]
    except (ValueError, IndexError):
        return
    print()
    print(process_manager.tail_log(s.mode, lines=80))
    pause()


# ---------- main loop ----------

def main_menu() -> None:
    while True:
        header()
        print(bold("\n  Main menu"))
        print(f"   [1] Configure       — API keys, strategy YAML")
        print(f"   [2] Backtest        — historical replay")
        print(f"   [3] Optimize        — parallel grid search")
        print(f"   [4] Paper           — virtual money on live prices")
        print(f"   [5] Live (all-in-1) — Telegram signals + paper mirror + crypto + dividends")
        print(f"   [6] Status          — running processes")
        print(f"   [7] Logs            — tail recent")
        print(f"   [0] Quit")
        choice = input("\n  > ").strip().lower()
        if choice == "0" or choice in ("q", "quit", "exit"):
            return
        if choice == "1":
            _menu_configure()
        elif choice == "2":
            _menu_backtest()
        elif choice == "3":
            _menu_optimize()
        elif choice == "4":
            _menu_paper()
        elif choice == "5":
            _menu_live()
        elif choice == "6":
            _menu_status()
        elif choice == "7":
            _menu_logs()


def run() -> None:
    try:
        main_menu()
    except (KeyboardInterrupt, EOFError):
        print()
        return


if __name__ == "__main__":
    run()
