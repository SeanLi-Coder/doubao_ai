"""Stop a running instance (and the automation Chrome it started)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ["DOUBAO_VIDEO_DATA"]).expanduser() if os.environ.get("DOUBAO_VIDEO_DATA") else PROJECT_ROOT / "data"
RECORD = DATA / "runtime" / "server.json"


def _command_line(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_exit(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.25)
    return not _alive(pid)


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def main() -> int:
    try:
        record = json.loads(RECORD.read_text())
        pid = int(record["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        print("没有正在运行的实例。")
        return 0
    command = _command_line(pid)
    if not _alive(pid) or "uvicorn" not in command or "app.server:app" not in command:
        # The record is stale (e.g. after a crash or reboot); the PID may now
        # belong to an unrelated process, so never signal it.
        RECORD.unlink(missing_ok=True)
        print("没有正在运行的实例。")
        return 0
    _signal(pid, signal.SIGINT)
    if not _wait_exit(pid, 25):
        _signal(pid, signal.SIGTERM)
        if not _wait_exit(pid, 8):
            print("服务没有在规定时间内退出，请在「活动监视器」中结束 python 进程。")
            return 1
    RECORD.unlink(missing_ok=True)
    print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
