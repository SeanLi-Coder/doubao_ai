"""Bootstrap a private virtualenv, start the local web UI and open it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS only
    fcntl = None

PROJECT_ROOT = Path(__file__).resolve().parent
MINIMUM_PYTHON = (3, 10)
DEFAULT_PORT = 8790
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
APP_MARKER = "豆包视频批量生成"


def data_root() -> Path:
    override = os.environ.get("DOUBAO_VIDEO_DATA")
    return Path(override).expanduser() if override else PROJECT_ROOT / "data"


def venv_root() -> Path:
    override = os.environ.get("DOUBAO_VIDEO_VENV")
    return Path(override).expanduser() if override else PROJECT_ROOT / ".venv"


RUNTIME = data_root() / "runtime"
SERVER_RECORD = RUNTIME / "server.json"
LAUNCH_LOCK = RUNTIME / "launcher.lock"


class LauncherError(RuntimeError):
    pass


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _venv_python() -> Path:
    return venv_root() / "bin" / "python"


def _python_version(python: Path) -> str | None:
    """'3.11' for a working interpreter, None when it is missing or broken."""

    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = completed.stdout.strip()
    return version if completed.returncode == 0 and version else None


def _supported(version: str | None) -> bool:
    if not version:
        return False
    major, minor = (int(x) for x in version.split("."))
    return (major, minor) >= MINIMUM_PYTHON


def prepare_environment() -> Path:
    python = _venv_python()
    version = _python_version(python)
    if not _supported(version):
        if venv_root().exists():
            print("重建 Python 运行环境（原环境已失效或版本过旧）…")
            shutil.rmtree(venv_root(), ignore_errors=True)
        print("首次运行：创建独立的 Python 环境（只需一次）…")
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(venv_root())], check=True, env=_clean_env())
        version = _python_version(python)
        if not _supported(version):
            raise LauncherError(f"创建 Python 环境失败：{python} 无法运行。请确认已安装 Python 3.10 或更高版本。")
    marker = venv_root() / ".requirements.sha256"
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes() + f"|{version}".encode()).hexdigest()
    if not marker.exists() or marker.read_text().strip() != wanted:
        print("安装依赖（首次需要联网，约 1–3 分钟）…")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"], check=False, env=_clean_env())
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQUIREMENTS)],
            check=False, env=_clean_env(),
        )
        if completed.returncode != 0:
            raise LauncherError("安装依赖失败，请检查网络后重新启动。")
        marker.write_text(wanted)
    return python


def _app_responds(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            return resp.status == 200 and APP_MARKER in resp.read(4096).decode("utf-8", "replace")
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _running_instance(default_port: int) -> int | None:
    candidates = []
    try:
        record = json.loads(SERVER_RECORD.read_text())
        candidates.append(int(record["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    candidates.append(default_port)
    for port in candidates:
        if _app_responds(port):
            return port
    return None


def _open(url: str, enabled: bool) -> None:
    print(f"已经在运行：{url}")
    if enabled:
        webbrowser.open(url)


def main() -> int:
    parser = argparse.ArgumentParser(description="豆包视频批量生成")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if sys.version_info < MINIMUM_PYTHON:
        print(f"需要 Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} 或更高版本，当前是 {sys.version.split()[0]}。")
        return 1
    if sys.platform != "darwin":
        print("目前只支持 macOS（需要 Google Chrome 与钥匙串）。")
        return 1

    RUNTIME.mkdir(parents=True, exist_ok=True)
    existing = _running_instance(args.port)
    if existing:
        _open(f"http://127.0.0.1:{existing}/", not args.no_browser)
        return 0

    # One launcher at a time (a double click during the first pip install
    # must not start a second server on the same data directory).
    lock = open(LAUNCH_LOCK, "w")
    if fcntl is not None:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个启动窗口正在准备运行环境，请稍候，它完成后会自动打开页面。")
            return 0

    try:
        python = prepare_environment()
    except (LauncherError, subprocess.CalledProcessError) as exc:
        print(f"启动失败：{exc}")
        return 1

    port = args.port
    while not _port_free(port):
        if _app_responds(port):
            _open(f"http://127.0.0.1:{port}/", not args.no_browser)
            return 0
        port += 1
        if port > args.port + 50:
            print("找不到可用端口。")
            return 1

    env = _clean_env()
    env["DOUBAO_VIDEO_DATA"] = str(data_root())
    command = [
        str(python), "-m", "uvicorn", "app.server:app",
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning", "--timeout-graceful-shutdown", "10",
    ]
    # Own session: terminal Ctrl+C reaches only this launcher, which forwards
    # exactly one SIGINT so the server can shut Chrome down cleanly.
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, start_new_session=True)
    SERVER_RECORD.write_text(json.dumps({"pid": process.pid, "port": port, "started": time.time()}))

    stopping = threading.Event()

    def escalate() -> None:
        for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                try:
                    process.send_signal(sig)
                except OSError:
                    return

    def stop(*_: object) -> None:
        if stopping.is_set() or process.poll() is not None:
            return
        stopping.set()
        try:
            process.send_signal(signal.SIGINT)
        except OSError:
            return
        threading.Thread(target=escalate, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)

    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        if _app_responds(port):
            break
        time.sleep(0.4)
    if process.poll() is not None:
        print("服务启动失败，请查看上面的错误信息。")
        return process.returncode or 1
    print(f"\n豆包视频批量生成已启动：{url}")
    print("使用期间请保持这个窗口打开；关闭窗口、按 Ctrl+C 或在网页里点「退出程序」即可退出。\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                stop()
    finally:
        try:
            record = json.loads(SERVER_RECORD.read_text())
            if int(record.get("pid", 0)) == process.pid:
                SERVER_RECORD.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    sys.exit(main())
