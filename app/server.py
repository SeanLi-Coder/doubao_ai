from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import signal
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, chrome_cookies, config, images
from .browser import ChromeController
from .tasks import TaskManager, cover_path

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS only tool
    fcntl = None  # type: ignore[assignment]

STATIC_ROOT = Path(__file__).resolve().parent / "static"
BOOT_ID = uuid.uuid4().hex[:8]
log = logging.getLogger("doubao_video")


def _setup_logging() -> None:
    config.ensure_dirs()
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(config.LOG_ROOT / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


chrome = ChromeController()
manager = TaskManager(chrome)
_scanned_folders: dict[str, None] = {}  # folders the UI scanned (insertion-ordered, capped)
_instance_lock: Any = None


def _acquire_instance_lock() -> None:
    """One server per data directory: two would submit the same queue twice."""

    global _instance_lock
    if fcntl is None:
        return
    config.ensure_dirs()
    handle = open(config.RUNTIME_ROOT / "server.lock", "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("已经有一个「豆包视频批量生成」在运行（同一个数据目录）。请先关闭它。")
    handle.write(str(os.getpid()))
    handle.flush()
    _instance_lock = handle


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_logging()
    _acquire_instance_lock()
    manager.load()
    manager.start()
    log.info("Doubao video generator %s ready", __version__)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(manager.stop(), timeout=15)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(chrome.shutdown(close_chrome=True), timeout=15)


app = FastAPI(title="豆包视频批量生成", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # The API drives a logged-in browser: refuse anything that is not this page.
    host_header = request.headers.get("host") or ""
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") <= 1 else host_header
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return JSONResponse({"detail": "forbidden cross-site request"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != f"http://{host_header}":
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


class NoCacheStatic(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class TaskIn(BaseModel):
    prompt: str = ""
    folder: str = ""
    title: str = ""


class BatchIn(BaseModel):
    parent: str
    default_prompt: str = ""


class FolderIn(BaseModel):
    folder: str


class ImportIn(BaseModel):
    profile: str = ""


class PauseIn(BaseModel):
    paused: bool


class OpenIn(BaseModel):
    path: str
    reveal: bool = False


def _error(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


def _remember_folder(folder: str | Path) -> None:
    key = str(Path(folder).resolve())
    _scanned_folders.pop(key, None)
    _scanned_folders[key] = None
    while len(_scanned_folders) > 200:
        _scanned_folders.pop(next(iter(_scanned_folders)))


@app.get("/")
async def index() -> HTMLResponse:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8").replace("__VERSION__", f"{__version__}-{BOOT_ID}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", NoCacheStatic(directory=STATIC_ROOT), name="static")


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    return {
        "version": __version__,
        "state": manager.state(),
        "settings": asdict(config.load_settings()),
        "tasks": manager.list(),
    }


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = manager.subscribe()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'state': manager.state()}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:  # dropped for falling behind: let the browser reconnect
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.post("/api/tasks")
async def add_task(body: TaskIn) -> dict[str, Any]:
    try:
        task = manager.add(body.prompt, body.folder, body.title)
    except (ValueError, images.ImageFolderError) as exc:
        raise _error(exc)
    return task.public()


@app.post("/api/tasks/batch")
async def add_batch(body: BatchIn) -> dict[str, Any]:
    try:
        scans = await asyncio.to_thread(images.find_task_folders, body.parent)
    except images.ImageFolderError as exc:
        raise _error(exc)
    added, skipped = [], []
    for scan in scans:
        prompt = images.read_prompt_file(scan.prompt_file) if scan.prompt_file else body.default_prompt
        try:
            added.append(manager.add(prompt, str(scan.folder), scan.folder.name).public())
        except (ValueError, images.ImageFolderError) as exc:
            skipped.append({"folder": scan.folder.name, "reason": str(exc)})
    if not added and not skipped:
        raise HTTPException(400, "这个文件夹下没有包含图片或 prompt.txt 的子文件夹。")
    return {"added": added, "skipped": skipped}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, Any]:
    try:
        return manager.cancel(task_id).public()
    except KeyError:
        raise HTTPException(404, "任务不存在")


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str) -> dict[str, Any]:
    try:
        return manager.retry(task_id).public()
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise _error(exc)


@app.post("/api/tasks/{task_id}/recheck")
async def recheck_task(task_id: str) -> dict[str, Any]:
    try:
        return manager.recheck(task_id).public()
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise _error(exc)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    try:
        manager.remove(task_id)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise _error(exc)
    return {"ok": True}


@app.post("/api/tasks/clear-finished")
async def clear_finished() -> dict[str, Any]:
    return {"removed": manager.clear_finished()}


@app.post("/api/queue/pause")
async def pause_queue(body: PauseIn) -> dict[str, Any]:
    manager.set_paused(body.paused, "已手动暂停" if body.paused else "")
    return manager.state()


def _task_file(task_id: str, index: int) -> dict[str, Any]:
    task = manager.tasks.get(task_id)
    files = (task.result or {}).get("files") if task else None
    if not files or not 0 <= index < len(files):
        raise HTTPException(404, "没有视频")
    return files[index]


@app.get("/api/tasks/{task_id}/video/{index}")
async def task_video(task_id: str, index: int) -> FileResponse:
    path = Path(_task_file(task_id, index)["path"])
    if not path.exists():
        raise HTTPException(404, "视频文件已被移动或删除")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "video/mp4")


@app.get("/api/tasks/{task_id}/cover/{index}")
async def task_cover(task_id: str, index: int) -> FileResponse:
    _task_file(task_id, index)
    path = cover_path(task_id, index)
    if not path.exists():
        raise HTTPException(404, "没有封面")
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/folder/scan")
async def scan_folder(body: FolderIn) -> dict[str, Any]:
    try:
        scan = await asyncio.to_thread(images.scan_folder, body.folder)
    except images.ImageFolderError as exc:
        raise _error(exc)
    _remember_folder(scan.folder)
    return scan.as_dict()


@app.get("/api/folder/thumb")
async def folder_thumb(folder: str, name: str) -> FileResponse:
    base = Path(folder).expanduser().resolve()
    allowed = {str(p.resolve()) for p in manager.task_folders()} | set(_scanned_folders)
    if str(base) not in allowed:
        raise HTTPException(403, "只能预览已选择的文件夹")
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(404, "not found")
    suffix = path.suffix.lower()
    if suffix not in config.UPLOADABLE_SUFFIXES and suffix not in config.CONVERTIBLE_SUFFIXES:
        raise HTTPException(404, "not an image")
    try:
        thumb = await asyncio.to_thread(images.thumbnail_for, path)
    except (images.ImageFolderError, OSError):
        raise HTTPException(404, "无法生成预览")
    return FileResponse(thumb, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/folder/pick")
async def pick_folder() -> dict[str, Any]:
    script = 'POSIX path of (choose folder with prompt "选择图片文件夹")'
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return {"folder": ""}
    folder = out.decode().strip().rstrip("/") or "/"
    _remember_folder(folder)
    return {"folder": folder}


@app.post("/api/open")
async def open_path(body: OpenIn) -> dict[str, Any]:
    resolved = Path(body.path).expanduser().resolve()
    if str(resolved) not in manager.result_files():
        raise HTTPException(403, "只能打开本工具生成的视频")
    if not resolved.exists():
        raise HTTPException(404, "文件已被移动或删除")
    args = ["open", "-R", str(resolved)] if body.reveal else ["open", str(resolved)]
    subprocess.Popen(args)
    return {"ok": True}


@app.post("/api/open-output")
async def open_output() -> dict[str, Any]:
    out = Path(config.load_settings().output_dir).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _error(Exception(f"无法创建保存文件夹：{exc.strerror or exc}"))
    subprocess.Popen(["open", str(out)])
    return {"ok": True}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return asdict(config.load_settings())


@app.put("/api/settings")
async def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
    if "output_dir" in patch:
        out = Path(str(patch["output_dir"] or "")).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / f".write-test-{uuid.uuid4().hex[:6]}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise _error(Exception(f"视频保存位置不可用：{out}（{exc.strerror or exc}）"))
    if patch.get("chrome_profile") and not chrome_cookies.is_valid_profile(str(patch["chrome_profile"])):
        raise _error(Exception("Chrome 用户不存在"))
    try:
        settings = config.update_settings(patch)
    except (TypeError, ValueError) as exc:
        raise _error(exc)
    manager.publish_state()
    return asdict(settings)


@app.get("/api/chrome/profiles")
async def chrome_profiles() -> dict[str, Any]:
    profiles = await asyncio.to_thread(chrome_cookies.list_profiles)
    return {"profiles": [asdict(p) for p in profiles], "selected": config.load_settings().chrome_profile}


@app.post("/api/browser/show")
async def browser_show() -> dict[str, Any]:
    try:
        await manager.ensure_browser()
        await manager.focus_relevant_page()
    except Exception as exc:
        raise _error(exc, 500)
    return manager.state()


@app.post("/api/login/import")
async def login_import(body: ImportIn) -> dict[str, Any]:
    profile = body.profile or config.load_settings().chrome_profile or await asyncio.to_thread(chrome_cookies.best_profile)
    if profile and not chrome_cookies.is_valid_profile(profile):
        raise HTTPException(400, "Chrome 用户不存在")
    try:
        count = await manager.import_chrome_login(profile)
    except chrome_cookies.CookieImportError as exc:
        raise _error(exc)
    except Exception as exc:
        raise _error(exc, 500)
    if body.profile:
        config.update_settings({"chrome_profile": body.profile})
    return {"imported": count, "state": manager.state()}


@app.post("/api/login/check")
async def login_check() -> dict[str, Any]:
    try:
        await manager.refresh_login()
    except Exception as exc:
        raise _error(exc, 500)
    return manager.state()


@app.post("/api/login/qr")
async def login_qr_start() -> dict[str, Any]:
    try:
        started = await manager.start_qr_login()
    except Exception as exc:
        raise _error(exc, 500)
    return {**manager.state(), "already_logged_in": not started}


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/login/qr.png")
async def login_qr_png() -> Response:
    if not manager.qr_png:
        raise HTTPException(404, "暂无二维码")
    return Response(manager.qr_png, media_type="image/png", headers=NO_STORE)


@app.get("/api/captcha.png")
async def captcha_png() -> Response:
    png = await manager.captcha_png()
    if not png:
        raise HTTPException(404, "当前没有验证码")
    return Response(png, media_type="image/png", headers=NO_STORE)


@app.post("/api/quota/clear")
async def quota_clear() -> dict[str, Any]:
    manager.clear_cooldown()
    return manager.state()


@app.post("/api/shutdown")
async def shutdown() -> dict[str, Any]:
    log.info("Shutdown requested from the web UI")
    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGINT)
    return {"ok": True}
