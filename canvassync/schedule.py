"""Install/remove the background job that runs `canvassync sync` on a schedule.

macOS: launchd user agent. Windows: Task Scheduler. Other platforms: unsupported
(add a cron entry running `canvassync sync` instead).
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from .config import LOG_DIR

LABEL = "com.canvassync.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
TASK_NAME = "canvassync"


class ScheduleUnsupported(RuntimeError):
    pass


def _sync_command() -> list[str]:
    if sys.platform == "win32":
        # pythonw runs without flashing a console window every few hours
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return [str(pythonw), "-m", "canvassync", "sync"]
        return [sys.executable, "-m", "canvassync", "sync"]
    console_script = Path(sys.executable).with_name("canvassync")
    if console_script.exists():
        return [str(console_script), "sync"]
    return [sys.executable, "-m", "canvassync", "sync"]


def install(interval_hours: int) -> str:
    """Install (or replace) the scheduled job; returns a human-readable description."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        _install_launchd(interval_hours)
        return f"launchd agent installed — syncs every {interval_hours}h and at login"
    if sys.platform == "win32":
        _install_schtasks(interval_hours)
        return f"Task Scheduler task '{TASK_NAME}' installed — syncs every {interval_hours}h"
    raise ScheduleUnsupported(
        "automatic scheduling is only implemented for macOS and Windows; "
        "add a cron entry running 'canvassync sync' instead"
    )


def uninstall() -> bool:
    if sys.platform == "darwin":
        if not PLIST_PATH.exists():
            return False
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
        PLIST_PATH.unlink()
        return True
    if sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True
        )
        return result.returncode == 0
    return False


def status() -> str | None:
    """Return a short description of the loaded job, or None if not scheduled."""
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.endswith(LABEL):
                return line
        return None
    if sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True
        )
        return f"Task Scheduler task '{TASK_NAME}'" if result.returncode == 0 else None
    return None


def _install_launchd(interval_hours: int) -> None:
    plist = {
        "Label": LABEL,
        "ProgramArguments": _sync_command(),
        "StartInterval": interval_hours * 3600,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "sync.log"),
        "StandardErrorPath": str(LOG_DIR / "sync.log"),
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(PLIST_PATH)], check=True)


def _install_schtasks(interval_hours: int) -> None:
    command = subprocess.list2cmdline(_sync_command())
    subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
         "/SC", "HOURLY", "/MO", str(interval_hours), "/F"],
        check=True,
        capture_output=True,
        text=True,
    )
