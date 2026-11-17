"""The server process: pid file, stop and status (`./arb ui --stop | --status`)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from arbkit.chain.spec import ROOT

PID_FILE = ROOT / "data" / "ui.pid"
SERVER_COMMAND = re.compile(r"^\S*python[\d.]*\s+-m\s+arbkit\s+ui(\s|$)")


def write_pidfile(path: Path, pid: int, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid} {port}\n")


def read_pidfile(path: Path) -> tuple[int, int] | None:
    """(pid, port) of the server that wrote the file, or None when there is no file or it is unreadable."""
    try:
        pid, port = path.read_text().split()
        return int(pid), int(port)
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_servers() -> list[int]:
    """PIDs of every `arbkit ui` process of this user (for a server started without the pid file, or a stale file)."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        # only the interpreter running `-m arbkit ui`: never a shell whose command text merely mentions it
        if len(parts) == 2 and SERVER_COMMAND.match(parts[1]) and int(parts[0]) != os.getpid():
            pids.append(int(parts[0]))
    return pids


def stop(console=None, path: Path = PID_FILE, wait_s: float = 5.0) -> int:
    """Stop the dashboard started by `./arb ui` (SIGTERM, then SIGKILL after `wait_s`). Returns 0 when nothing is left running."""
    targets = []
    info = read_pidfile(path)
    if info and process_alive(info[0]):
        targets.append(info[0])
    targets += [pid for pid in running_servers() if pid not in targets]
    if not targets:
        if console:
            console.print("No dashboard server is running.", style="dim")
        path.unlink(missing_ok=True)
        return 0
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and any(process_alive(pid) for pid in targets):
        time.sleep(0.1)
    for pid in targets:
        if process_alive(pid):
            os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    if console:
        console.print(f"Stopped dashboard server (pid {', '.join(map(str, targets))}).", style="bold cyan")
    return 0


def status(console, path: Path = PID_FILE) -> int:
    info = read_pidfile(path)
    pids = running_servers()
    if info and process_alive(info[0]):
        rss = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(info[0])], capture_output=True, text=True, check=False
        ).stdout.strip()
        mem = f", {int(rss) // 1024} MB resident" if rss.isdigit() else ""
        console.print(
            f"Dashboard running: pid {info[0]}, http://127.0.0.1:{info[1]}/{mem}. Stop it with: ./arb ui --stop",
            style="bold cyan",
        )
        return 0
    if pids:
        console.print(
            f"Dashboard process(es) running without a pid file: {pids}. Stop with: ./arb ui --stop",
            style="yellow",
        )
        return 0
    console.print("No dashboard server is running.", style="dim")
    return 1
