"""Task queue: submit prompts to Doubao, keep the round going, collect videos.

Lifecycle of one task::

    queued → submitting → waiting_reply ⇄ confirming → generating → downloading → success
                 │              │                          │             │
                 └──────────────┴────────── failure ───────┴─────────────┘
                                               │
                         retryable? ── yes ──→ retry_wait ──(timer)──→ queued (new conversation)
                                               │                    └→ recheck the same conversation
                                               no ──→ failed

Global gates stop new submissions while Doubao is logged out (the QR code is
shown in the web UI), while a captcha is on screen (the user solves it by
hand), during a quota / rate-limit cooldown, or when the user paused the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import classify, config, doubao, images, login
from .browser import ApiError, BrowserError, CaptchaRequired, ChromeController
from .notify import notify

log = logging.getLogger(__name__)

QUEUED = "queued"
SUBMITTING = "submitting"
WAITING_REPLY = "waiting_reply"
CONFIRMING = "confirming"
GENERATING = "generating"
DOWNLOADING = "downloading"
RETRY_WAIT = "retry_wait"
SUCCESS = "success"
FAILED = "failed"
CANCELED = "canceled"

ACTIVE = {SUBMITTING, WAITING_REPLY, CONFIRMING, GENERATING, DOWNLOADING}
MONITORED = {WAITING_REPLY, CONFIRMING, GENERATING, DOWNLOADING}
FINAL = {SUCCESS, FAILED, CANCELED}
SETTLE_SECONDS = 5
MAX_RECHECKS = 3  # recheck the same conversation this often before starting over

STATUS_LABELS = {
    QUEUED: "排队中",
    SUBMITTING: "提交中",
    WAITING_REPLY: "等待豆包回复",
    CONFIRMING: "自动确认继续",
    GENERATING: "生成中",
    DOWNLOADING: "下载中",
    RETRY_WAIT: "等待重试",
    SUCCESS: "成功",
    FAILED: "失败",
    CANCELED: "已取消",
}

# Failures that retrying cannot fix.
NON_RETRYABLE = {"images", "canceled"}
# Failures where the video may already exist: re-read the same conversation
# instead of spending quota on a new generation.
RECHECK_KINDS = {"download", "timeout", "monitor_transient", "continue"}
# Recheck kinds that must never fall back to a new generation.
RECHECK_ONLY = {"download"}
COOLDOWN_KINDS = {classify.FAIL_QUOTA, classify.FAIL_RATE_LIMIT}

FAIL_LABELS = {
    classify.FAIL_QUOTA: "豆包额度不足",
    classify.FAIL_RATE_LIMIT: "豆包提示操作太频繁",
    classify.FAIL_POLICY: "豆包拒绝生成（内容审核）",
    classify.FAIL_GENERATION: "豆包生成失败",
    "submit": "提交失败",
    "no_reply": "豆包没有回复",
    "no_start": "豆包没有开始生成",
    "continue": "自动回复失败",
    "timeout": "生成超时",
    "download": "下载失败",
    "monitor": "读取对话失败",
    "monitor_transient": "读取对话失败",
    "images": "参考图有问题",
    "internal": "内部错误",
}


class TaskCanceled(Exception):
    pass


def _now() -> float:
    return time.time()


def _fmt_wait(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"


@dataclass
class Task:
    id: str
    prompt: str
    folder: str
    title: str
    image_names: list[str] = field(default_factory=list)
    status: str = QUEUED
    stage: str = "排队中"
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    submitted_at: float | None = None
    generating_since: float | None = None
    recheck_started_at: float | None = None
    finished_at: float | None = None
    last_sent_at: float | None = None
    conversation_id: str = ""
    seen_ok: bool = False
    continues: int = 0
    attempts: int = 0
    retry_count: int = 0
    rechecks: int = 0
    next_retry_at: float | None = None
    retry_mode: str = ""
    eta_minutes: float | None = None
    replies: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False

    @property
    def conversation_url(self) -> str:
        return f"{config.DOUBAO_ORIGIN}/chat/{self.conversation_id}" if self.conversation_id else ""

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["status_label"] = STATUS_LABELS.get(self.status, self.status)
        data["conversation_url"] = self.conversation_url
        data.pop("cancel_requested", None)
        return data


class TaskManager:
    def __init__(self, chrome: ChromeController) -> None:
        self.chrome = chrome
        chrome.on_disconnect = self._on_browser_disconnect
        self.tasks: dict[str, Task] = {}
        self.order: list[str] = []
        self.paused = False
        self.pause_reason = ""
        self.captcha = False
        self.captcha_version = 0
        self.login_state = "unknown"
        self.login_reason = ""
        self.qr_png: bytes | None = None
        self.qr_text = ""
        self.qr_version = 0
        self.qr_error = ""
        self.browser_state = "idle"
        self.browser_error = ""
        self.cooldown_until = 0.0
        self.cooldown_kind = ""
        self.cooldown_reason = ""
        self.last_submit_at = 0.0
        self._rate_hits = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._workers: dict[str, asyncio.Task[Any]] = {}
        self._loops: list[asyncio.Task[Any]] = []
        self._login_task: asyncio.Task[Any] | None = None
        self._login_event = asyncio.Event()
        self._qr_refresh = asyncio.Event()
        self._save_handle: asyncio.TimerHandle | None = None
        self._captcha_notified = False
        self._submitting = False
        self._logout_strikes = 0
        self._last_login_probe = 0.0
        self._auto_import_tried = False

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            raw = json.loads(config.TASKS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        known = set(Task.__dataclass_fields__)
        for item in raw.get("tasks", []):
            if not isinstance(item, dict):
                continue
            try:
                task = Task(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            if task.cancel_requested and task.status not in FINAL:
                task.status, task.stage, task.finished_at = CANCELED, "已取消", _now()
            task.cancel_requested = False
            if task.status == SUBMITTING:
                if task.conversation_id:
                    task.status = WAITING_REPLY
                else:
                    task.status = QUEUED
                    task.stage = "上次提交被中断，重新排队"
            self.tasks[task.id] = task
            self.order.append(task.id)
        self.paused = bool(raw.get("paused"))
        self.pause_reason = str(raw.get("pause_reason") or "")
        with contextlib.suppress(TypeError, ValueError):
            self.cooldown_until = float(raw.get("cooldown_until") or raw.get("quota_until") or 0)
            self.cooldown_kind = str(raw.get("cooldown_kind") or (classify.FAIL_QUOTA if self.cooldown_until else ""))
            self.cooldown_reason = str(raw.get("cooldown_reason") or "")

    def _save_now(self) -> None:
        self._save_handle = None
        payload = {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "cooldown_until": self.cooldown_until,
            "cooldown_kind": self.cooldown_kind,
            "cooldown_reason": self.cooldown_reason,
            "tasks": [asdict(self.tasks[i]) for i in self.order if i in self.tasks],
        }
        try:
            config.atomic_write_json(config.TASKS_PATH, payload)
        except OSError as exc:
            log.error("Could not save tasks: %s", exc)

    def save(self) -> None:
        if self._save_handle is None:
            self._save_handle = asyncio.get_running_loop().call_later(0.5, self._save_now)

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The page stopped reading. Close its stream so the browser
                # reconnects and reloads a fresh snapshot.
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueEmpty):
                    while True:
                        queue.get_nowait()
                queue.put_nowait(None)

    def state(self) -> dict[str, Any]:
        settings = config.load_settings()
        cooling = self.cooldown_until > _now()
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "captcha": self.captcha,
            "captcha_version": self.captcha_version,
            "login": self.login_state,
            "login_reason": self.login_reason,
            "qr_version": self.qr_version if self.qr_png else 0,
            "qr_text": self.qr_text,
            "qr_error": self.qr_error,
            "browser": self.browser_state,
            "browser_error": self.browser_error,
            "cooldown_until": self.cooldown_until if cooling else 0,
            "cooldown_kind": self.cooldown_kind if cooling else "",
            "cooldown_reason": self.cooldown_reason if cooling else "",
            "max_parallel": settings.max_parallel,
            "fixed": {
                "model": config.FIXED_MODEL,
                "ratio": config.FIXED_RATIO,
                "duration": config.FIXED_DURATION_SECONDS,
            },
        }

    def publish_state(self) -> None:
        self._publish({"type": "state", "state": self.state()})

    def touch(self, task: Task) -> None:
        task.updated_at = _now()
        self._publish({"type": "task", "task": task.public()})
        self.save()

    def event(self, task: Task, text: str, level: str = "info") -> None:
        task.events.append({"t": _now(), "level": level, "text": text})
        if len(task.events) > 300:
            task.events = task.events[-300:]
        log.info("[%s] %s", task.id[:6], text)
        self.touch(task)

    def set_status(self, task: Task, status: str, stage: str | None = None) -> None:
        task.status = status
        task.stage = stage or STATUS_LABELS.get(status, status)
        if status in FINAL:
            task.finished_at = _now()
        self.touch(task)

    # ------------------------------------------------------------------ public API
    def list(self) -> list[dict[str, Any]]:
        return [self.tasks[i].public() for i in reversed(self.order) if i in self.tasks]

    def task_folders(self) -> set[Path]:
        return {Path(t.folder) for t in self.tasks.values() if t.folder}

    def result_files(self) -> dict[str, tuple[Task, int]]:
        out: dict[str, tuple[Task, int]] = {}
        for task in self.tasks.values():
            for index, item in enumerate((task.result or {}).get("files") or []):
                if item.get("path"):
                    out[str(Path(item["path"]).resolve())] = (task, index)
        return out

    def add(self, prompt: str, folder: str, title: str = "") -> Task:
        prompt = (prompt or "").strip()
        scan = images.scan_folder(folder) if (folder or "").strip() else None
        if not prompt and scan and scan.prompt_file:
            prompt = images.read_prompt_file(scan.prompt_file)
        if not prompt:
            raise ValueError("请填写提示词。")
        if scan and len(scan.images) > config.MAX_REFERENCE_IMAGES:
            raise ValueError(f"豆包最多支持 {config.MAX_REFERENCE_IMAGES} 张参考图，这个文件夹里有 {len(scan.images)} 张。")
        task = Task(
            id=uuid.uuid4().hex,
            prompt=prompt,
            folder=str(scan.folder) if scan else "",
            title=(title or "").strip() or _make_title(prompt, scan.folder.name if scan else ""),
            image_names=[p.name for p in scan.images] if scan else [],
        )
        self.tasks[task.id] = task
        self.order.append(task.id)
        self.event(task, f"已加入队列（{len(task.image_names)} 张参考图）")
        return task

    def cancel(self, task_id: str) -> Task:
        task = self._get(task_id)
        if task.status in FINAL:
            return task
        task.cancel_requested = True
        task.next_retry_at = None
        if task.status in (QUEUED, RETRY_WAIT) or task.id not in self._workers:
            self.set_status(task, CANCELED, "已取消")
        self.event(task, "已取消。已经提交给豆包的生成不会被撤回，只是不再跟踪。", "warn")
        return task

    def retry(self, task_id: str) -> Task:
        """Manual retry. For a waiting retry, run it now; otherwise start over."""

        task = self._get(task_id)
        if task.status == RETRY_WAIT:
            task.next_retry_at = _now()
            if task.error.get("kind") in COOLDOWN_KINDS:
                self.clear_cooldown()
            self.event(task, "立即重试")
            return task
        if task.status not in (FAILED, CANCELED):
            raise ValueError("只有失败、等待重试或已取消的任务可以重试。")
        task.cancel_requested = False
        task.retry_count = 0
        task.rechecks = 0
        self._reset_for_new_attempt(task)
        self.set_status(task, QUEUED, "重新排队")
        self.event(task, "重新排队，将新建一次豆包对话")
        return task

    def recheck(self, task_id: str) -> Task:
        task = self._get(task_id)
        if not task.conversation_id:
            raise ValueError("这个任务还没有豆包对话，无法重新检查。")
        if task.id in self._workers:
            return task
        task.cancel_requested = False
        self._start_recheck(task, "重新检查豆包对话结果")
        return task

    def remove(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.status in ACTIVE:
            raise ValueError("任务进行中，请先取消。")
        self.tasks.pop(task_id, None)
        with contextlib.suppress(ValueError):
            self.order.remove(task_id)
        images.cleanup_task_cache(task_id)
        self._publish({"type": "removed", "id": task_id})
        self.save()

    def clear_finished(self) -> int:
        removed = [i for i in self.order if self.tasks.get(i) and self.tasks[i].status in FINAL]
        for task_id in removed:
            self.tasks.pop(task_id, None)
            self.order.remove(task_id)
            images.cleanup_task_cache(task_id)
            self._publish({"type": "removed", "id": task_id})
        self.save()
        return len(removed)

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.paused = paused
        self.pause_reason = reason if paused else ""
        self.publish_state()
        self.save()

    def clear_cooldown(self) -> None:
        self.cooldown_until = 0
        self.cooldown_kind = ""
        self.cooldown_reason = ""
        for task in self.tasks.values():
            if task.status == RETRY_WAIT and task.error.get("kind") in COOLDOWN_KINDS:
                task.next_retry_at = _now()
        self.publish_state()
        self.save()

    def _get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _reset_for_new_attempt(self, task: Task) -> None:
        task.conversation_id = ""
        task.seen_ok = False
        task.continues = 0
        task.rechecks = 0
        task.eta_minutes = None
        task.generating_since = None
        task.recheck_started_at = None
        task.submitted_at = None
        task.last_sent_at = None
        task.finished_at = None
        task.next_retry_at = None
        task.retry_mode = ""
        task.error = {}
        task.result = {}

    def _start_recheck(self, task: Task, note: str) -> None:
        task.error = {}
        task.finished_at = None
        task.next_retry_at = None
        task.recheck_started_at = task.last_sent_at = _now()
        self.set_status(task, GENERATING if task.generating_since else WAITING_REPLY, "重新检查豆包对话")
        self.event(task, note)
        self._start_worker(task, self._monitor(task))

    # ------------------------------------------------------------------ loops
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        images.cleanup_orphan_caches(set(self.tasks))
        self._loops.append(loop.create_task(self._scheduler_loop()))
        self._loops.append(loop.create_task(self._watchdog_loop()))
        for task in self.tasks.values():
            if task.status in MONITORED and task.conversation_id:
                self.event(task, "程序重新启动，继续跟踪豆包对话")
                task.recheck_started_at = _now()
                self._start_worker(task, self._monitor(task))

    async def stop(self) -> None:
        jobs = list(self._loops) + list(self._workers.values())
        if self._login_task is not None:
            jobs.append(self._login_task)
        for job in jobs:
            job.cancel()
        for job in jobs:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(job, timeout=5)
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._save_now()

    def _start_worker(self, task: Task, coro: Any) -> None:
        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except TaskCanceled:
                if task.status not in FINAL:
                    self.set_status(task, CANCELED, "已取消")
            except Exception as exc:  # last-resort guard
                log.exception("Task %s crashed", task.id)
                if task.status not in FINAL and task.status != RETRY_WAIT:
                    # Keep an existing conversation: the video may still arrive there.
                    kind = "monitor_transient" if task.conversation_id else "internal"
                    self._fail(task, kind, f"出现意外错误：{exc}")
            finally:
                self._workers.pop(task.id, None)

        self._workers[task.id] = asyncio.get_running_loop().create_task(runner())

    def _gates_closed(self) -> str:
        if self.paused:
            return "paused"
        if self.captcha:
            return "captcha"
        if self.login_state == "logged_out":
            return "login"
        return ""

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self._tick()
            except Exception:
                log.exception("scheduler error")

    def _tick(self) -> None:
        now = _now()
        settings = config.load_settings()
        if self.cooldown_until and self.cooldown_until <= now:
            self.cooldown_until, self.cooldown_kind, self.cooldown_reason = 0, "", ""
            self.publish_state()
            self.save()
        gate = self._gates_closed()

        # Due retries: recheck the same conversation, or go back to the queue.
        for task in [self.tasks[i] for i in self.order if i in self.tasks]:
            if task.status != RETRY_WAIT or task.next_retry_at is None or task.next_retry_at > now:
                continue
            if task.cancel_requested:
                self.set_status(task, CANCELED, "已取消")
                continue
            if gate or task.id in self._workers:
                continue
            if task.retry_mode == "recheck" and task.conversation_id:
                self._start_recheck(task, f"第 {task.retry_count} 次重试：重新检查原对话")
            else:
                self._reset_for_new_attempt(task)
                self.set_status(task, QUEUED, f"第 {task.retry_count} 次重试，排队中")

        if gate or self._submitting or self.cooldown_until > now:
            return
        if now - self.last_submit_at < settings.submit_gap_seconds:
            return
        if sum(1 for t in self.tasks.values() if t.status in ACTIVE) >= settings.max_parallel:
            return
        for task_id in list(self.order):
            task = self.tasks.get(task_id)
            if task is None or task.status != QUEUED:
                continue
            if task.cancel_requested:
                self.set_status(task, CANCELED, "已取消")
                continue
            self._submitting = True
            self.last_submit_at = now
            self.set_status(task, SUBMITTING, "准备提交")
            self._start_worker(task, self._submit_and_monitor(task))
            return

    async def _watchdog_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(2)
            tick += 1
            if not self.chrome.running:
                if self.browser_state == "ready":
                    self.browser_state = "idle"
                    self.publish_state()
                if self.captcha:
                    await self._set_captcha(False)
                continue
            with contextlib.suppress(Exception):
                await self._set_captcha(await self.chrome.captcha_visible())
            if self.login_state == "logged_out":
                if tick % 3 == 0 and (self._login_task is None or self._login_task.done()):
                    self._login_task = asyncio.get_running_loop().create_task(self._login_flow())
            elif not self.captcha:
                with contextlib.suppress(Exception):
                    if tick % 5 == 0 and not await self.chrome.account_cookie_present():
                        await self._on_logged_out("豆包登录信息已被清除（可能因操作太频繁被下线）")
                    elif tick % 15 == 0:
                        await self._check_login_passively()

    async def _check_login_passively(self) -> None:
        ok = await self.chrome.verify_login()
        if ok is False:
            self._logout_strikes += 1
            if self._logout_strikes >= 2:
                await self._on_logged_out("检测到豆包已退出登录（可能因操作太频繁被下线）")
        elif ok is True:
            self._logout_strikes = 0
            if self.login_state != "logged_in":
                self._mark_logged_in()

    # ------------------------------------------------------------------ captcha
    def _on_browser_disconnect(self) -> None:
        if self.captcha:
            self.captcha = False
            self._captcha_notified = False
            self.publish_state()

    async def _set_captcha(self, visible: bool) -> None:
        if visible and self.captcha:
            self.captcha_version += 1
            self._publish({"type": "captcha", "version": self.captcha_version})
            return
        if visible == self.captcha:
            return
        self.captcha = visible
        if visible:
            self.captcha_version += 1
            log.warning("Captcha detected")
            await self.chrome.bring_to_front(await self.chrome.captcha_page())
            if not self._captcha_notified and config.load_settings().notify:
                notify("豆包需要安全验证", "请在弹出的 Chrome 窗口中手动完成验证，完成后会自动继续。", sound=True)
            self._captcha_notified = True
        else:
            self._captcha_notified = False
        self.publish_state()

    async def captcha_png(self) -> bytes | None:
        page = await self.chrome.captcha_page()
        if page is None:
            return None
        with contextlib.suppress(Exception):
            clip = await self.chrome.captcha_clip(page)
            if clip:
                return await page.screenshot(type="png", clip=clip, timeout=8000)
            return await page.screenshot(type="png", timeout=8000)
        return None

    async def focus_relevant_page(self) -> None:
        if self.captcha:
            page = await self.chrome.captcha_page()
        elif self.login_state == "logged_out":
            page = self.chrome.existing_login_page()
        else:
            page = None
        await self.chrome.bring_to_front(page)

    # ------------------------------------------------------------------ browser / login
    async def ensure_browser(self) -> None:
        if self.chrome.running:
            if self.browser_state != "ready":
                self.browser_state = "ready"
                self.publish_state()
            return
        self.browser_state = "starting"
        self.browser_error = ""
        self.publish_state()
        try:
            await self.chrome.ensure()
        except Exception as exc:
            self.browser_state = "error"
            self.browser_error = str(exc)
            self.publish_state()
            raise
        self.browser_state = "ready"
        self.publish_state()

    async def refresh_login(self) -> str:
        """Actively check the login state (authoritative API check)."""

        await self.ensure_browser()
        ok = await self.chrome.verify_login()
        if ok is True:
            self._mark_logged_in()
        elif ok is False:
            await self._on_logged_out("自动化 Chrome 没有登录豆包" if self.login_state != "logged_in" else "豆包登录已失效")
        return self.login_state

    def _mark_logged_in(self) -> None:
        changed = self.login_state != "logged_in"
        self.login_state = "logged_in"
        self.login_reason = ""
        self.qr_png = None
        self.qr_text = ""
        self.qr_error = ""
        self._auto_import_tried = False
        self._logout_strikes = 0
        self._login_event.set()
        login_task = self._login_task
        if login_task is not None and not login_task.done() and login_task is not asyncio.current_task():
            login_task.cancel()
        if self.chrome.existing_login_page() is not None:
            asyncio.get_running_loop().create_task(self.chrome.close_login_page())
        if changed:
            log.info("Logged in")
            self.publish_state()

    async def wait_logged_in(self, task: Task | None = None) -> None:
        while self.login_state == "logged_out":
            if task is not None and task.cancel_requested:
                raise TaskCanceled()
            self._login_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._login_event.wait(), timeout=5)

    async def _on_logged_out(self, reason: str) -> None:
        self._logout_strikes = 0
        if self.login_state != "logged_out":
            log.warning("Logged out: %s", reason)
            self.login_state = "logged_out"
            self.login_reason = reason
            self._login_event.clear()
            self.publish_state()
            if config.load_settings().notify:
                notify("豆包已下线", "请打开「豆包视频批量生成」页面，扫描页面上的二维码重新登录。", sound=True)
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.get_running_loop().create_task(self._login_flow())

    async def _login_flow(self) -> None:
        """Try the everyday-Chrome cookies once, then show the QR code until logged in."""

        backoff = 2.0
        while self.login_state == "logged_out":
            settings = config.load_settings()
            try:
                await self.ensure_browser()
                if settings.auto_import_login and not self._auto_import_tried:
                    self._auto_import_tried = True
                    await self._try_auto_import(settings)
                    if self.login_state != "logged_out":
                        return
                await self._qr_loop()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("login flow error: %s", exc)
                self.qr_png = None
                self.qr_error = f"打开登录二维码时出错，正在重试（{_short(exc)}）"
                self.publish_state()
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def _try_auto_import(self, settings: config.Settings) -> None:
        try:
            profile = settings.chrome_profile or await asyncio.to_thread(_best_chrome_profile)
            if not profile:
                self.qr_error = "日常 Chrome 里没有登录豆包，请扫码登录"
                return
            self.qr_error = "正在尝试从日常 Chrome 导入豆包登录…"
            self.publish_state()
            await self.import_chrome_login(profile, from_flow=True)
            if self.login_state == "logged_out":
                self.qr_error = "日常 Chrome 的豆包登录也已失效，请扫码登录"
        except Exception as exc:
            self.qr_error = f"从日常 Chrome 导入失败（{_short(exc)}），请扫码登录"
        finally:
            self.publish_state()

    async def _open_login_tab(self):
        page = await self.chrome.open_login_page()
        await page.goto(config.CHAT_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        return page

    async def _qr_loop(self) -> None:
        page = await self._open_login_tab()
        failures = 0
        checks = 0
        try:
            while self.login_state == "logged_out":
                if page.is_closed():
                    page = await self._open_login_tab()
                    continue
                if self._qr_refresh.is_set():
                    self._qr_refresh.clear()
                    self.qr_png = None
                    self.publish_state()
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)
                checks += 1
                if checks % 2 == 0 and await self.chrome.verify_login() is True:
                    break  # logged in (scanned here, in another tab, or by import)
                if self.captcha:
                    await asyncio.sleep(2)
                    continue
                if not await login.qr_dialog_visible(page):
                    if not await login.open_qr_dialog(page):
                        failures += 1
                        if failures % 8 == 0:
                            self.qr_error = "豆包登录框没有出现，正在重新加载…"
                            self.publish_state()
                            await page.reload(wait_until="domcontentloaded", timeout=60000)
                            await page.wait_for_timeout(2000)
                        await asyncio.sleep(1.5)
                        continue
                failures = 0
                try:
                    png, text = await login.qr_snapshot(page)
                except Exception:
                    await asyncio.sleep(1.5)
                    continue
                if png != self.qr_png or text != self.qr_text:
                    self.qr_png, self.qr_text = png, text
                    self.qr_version += 1
                    self.qr_error = ""
                    self.publish_state()
                if await login.refresh_qr_if_expired(page, text):
                    continue
                await asyncio.sleep(2)
        finally:
            await self.chrome.close_login_page()
        if self.login_state == "logged_out":
            self._mark_logged_in()
            log.info("Logged in again (detected while the QR code was shown)")
            if config.load_settings().notify:
                notify("豆包已重新登录", "任务会自动继续。")
        self.chrome.reset_api_page()
        await self._go_home_if_idle()

    async def _go_home_if_idle(self) -> None:
        work = self.chrome.existing_work_page()
        if work is None or self.chrome.ui_lock.locked():
            return
        async with self.chrome.ui_lock:
            with contextlib.suppress(Exception):
                await work.goto(config.CHAT_URL, wait_until="domcontentloaded", timeout=45000)

    async def start_qr_login(self) -> bool:
        """Show the QR code. Returns False when already logged in."""

        await self.ensure_browser()
        if self.login_state != "logged_out":
            if await self.chrome.verify_login() is True:
                self._mark_logged_in()
                return False
            self._auto_import_tried = True  # the user explicitly wants the QR code
            await self._on_logged_out(self.login_reason or "手动打开扫码登录")
            return True
        self._auto_import_tried = True
        self._qr_refresh.set()
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.get_running_loop().create_task(self._login_flow())
        return True

    async def import_chrome_login(self, profile_directory: str, from_flow: bool = False) -> int:
        from . import chrome_cookies

        cookies = await asyncio.to_thread(chrome_cookies.read_doubao_cookies, profile_directory)
        await self.ensure_browser()
        await self.chrome.add_cookies(cookies)
        self.chrome.reset_api_page()
        if await self.chrome.verify_login() is True:
            self._mark_logged_in()
            await self._go_home_if_idle()
        elif not from_flow:
            await self._on_logged_out("导入的 Chrome 登录已失效，请扫码登录")
        return len(cookies)

    async def _probe_logged_out(self, force: bool = False) -> bool:
        """Called when something failed: are we logged out? Never raises."""

        if self.login_state == "logged_out":
            return True
        if not force and _now() - self._last_login_probe < 20:
            return False
        self._last_login_probe = _now()
        try:
            ok = await self.chrome.verify_login()
        except Exception:
            return False
        if ok is False:
            await self._on_logged_out("豆包登录已失效（可能因操作太频繁被下线）")
            return True
        return False

    # ------------------------------------------------------------------ task flow
    async def _checkpoint(self, task: Task, stage: str) -> None:
        if task.cancel_requested:
            raise TaskCanceled()
        if not self.chrome.running:
            raise BrowserError("自动化 Chrome 已关闭")
        waited = False
        while await self.chrome.captcha_visible():
            await self._set_captcha(True)
            if not waited:
                task.stage = "等待你在 Chrome 中完成安全验证"
                self.event(task, "检测到豆包安全验证，请在 Chrome 窗口中手动完成，完成后自动继续", "warn")
                waited = True
            if task.cancel_requested:
                raise TaskCanceled()
            await asyncio.sleep(2)
            if not self.chrome.running:
                raise BrowserError("自动化 Chrome 已关闭")
        if waited:
            await self._set_captcha(False)
            self.event(task, "安全验证已完成，继续")
        task.stage = stage
        self.touch(task)

    async def _submit_and_monitor(self, task: Task) -> None:
        try:
            submitted = await self._submit(task)
        finally:
            self._submitting = False
        if submitted:
            await self._monitor(task)

    async def _requeue_for_login(self, task: Task, note: str) -> None:
        if task.cancel_requested:
            raise TaskCanceled()
        task.attempts = max(0, task.attempts - 1)
        self.set_status(task, QUEUED, "等待重新登录豆包")
        self.event(task, note, "warn")

    async def _submit(self, task: Task) -> bool:
        try:
            source = [Path(task.folder) / name for name in task.image_names] if task.folder else []
            ready = await asyncio.to_thread(images.prepare_for_upload, source, config.CACHE_ROOT / task.id)
        except images.ImageFolderError as exc:
            self._fail(task, "images", str(exc))
            return False

        task.attempts += 1
        try:
            await self.ensure_browser()
            async with self.chrome.ui_lock:
                page = await self.chrome.work_page()
                with contextlib.suppress(Exception):
                    await page.bring_to_front()
                cid = await doubao.submit_creation(page, task.prompt, ready, lambda s: self._checkpoint(task, s))
        except TaskCanceled:
            raise
        except doubao.LoginRequired as exc:
            await self._on_logged_out("提交时发现豆包未登录")
            await self._requeue_for_login(task, str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - UI automation is inherently flaky
            if task.cancel_requested:
                raise TaskCanceled() from exc
            kind = getattr(exc, "kind", "submit") or "submit"
            if kind == "submit" and await self._probe_logged_out(force=True):
                await self._requeue_for_login(task, f"提交中断（豆包已下线）：{exc}")
                return False
            self._fail(task, kind, f"提交到豆包失败：{exc}")
            return False

        task.conversation_id = cid
        task.seen_ok = False
        task.submitted_at = task.last_sent_at = _now()
        if self.login_state != "logged_in":
            self._mark_logged_in()
        self.set_status(task, WAITING_REPLY, "已提交，等待豆包回复")
        attempt = f"（第 {task.retry_count + 1} 次尝试）" if task.retry_count else ""
        self.event(task, f"已提交给豆包{attempt}：{config.FIXED_MODEL} · {config.FIXED_RATIO} · {config.FIXED_DURATION_SECONDS}s")
        return True

    def _generation_start(self, task: Task) -> float | None:
        starts = [t for t in (task.generating_since, task.recheck_started_at) if t]
        return max(starts) if starts else None

    async def _monitor(self, task: Task) -> None:
        settings = config.load_settings()
        seen_replies = {r.get("key") for r in task.replies}
        errors_in_row = 0
        missing_in_row = 0
        verify_in_row = 0
        last_state = ""
        # A reply must stay unchanged for a few seconds before we act on it:
        # Doubao streams blocks, so "我将为你生成…" can arrive before the
        # "视频生成已提交" block that follows it in the same message.
        settle_sig = ""
        settle_since = 0.0
        if task.last_sent_at is None:
            task.last_sent_at = _now()
        while True:
            if task.cancel_requested:
                raise TaskCanceled()
            if self.login_state == "logged_out":
                task.stage = "等待重新登录豆包"
                self.touch(task)
                await self.wait_logged_in(task)
                task.last_sent_at = max(task.last_sent_at or 0, _now() - 30)
                last_state = ""  # force a fresh status/stage once we can read again
                missing_in_row = 0
                continue
            try:
                await self.ensure_browser()
                messages = await self.chrome.pull_messages(task.conversation_id)
                errors_in_row = missing_in_row = verify_in_row = 0
                task.seen_ok = True
            except asyncio.CancelledError:
                raise
            except CaptchaRequired:
                verify_in_row += 1
                errors_in_row += 1
                if await self.chrome.captcha_page() is not None:
                    await self._set_captcha(True)
                elif verify_in_row in (1, 5):
                    # Let the page itself show Doubao's verification, if any.
                    await self.chrome.reload_api_page()
                if errors_in_row > 60:
                    self._fail(task, "monitor_transient", "豆包一直要求安全验证，暂时无法读取对话")
                    return
                await asyncio.sleep(min(30, 5 * verify_in_row))
                continue
            except Exception as exc:
                if await self._probe_logged_out(force=isinstance(exc, ApiError) and task.seen_ok):
                    continue
                errors_in_row += 1
                if isinstance(exc, ApiError) and exc.code == ApiError.NOT_FOUND:
                    missing_in_row += 1
                    if missing_in_row >= 5:
                        if task.seen_ok:
                            self._fail(task, "monitor_transient", "暂时读不到豆包对话（可能是网络或登录问题）")
                        else:
                            self._fail(task, "monitor", "豆包对话不存在（可能被删除）")
                        return
                if errors_in_row in (1, 10, 30):
                    self.event(task, f"读取豆包对话出错，稍后重试：{exc}", "warn")
                if errors_in_row > 60:
                    self._fail(task, "monitor_transient", f"多次无法读取豆包对话：{exc}")
                    return
                await asyncio.sleep(min(30, 3 * errors_in_row))
                continue

            assessment = classify.assess(messages)

            if assessment.state == classify.SUCCESS:
                self._record_replies(task, messages, assessment, seen_replies)
                await self._download(task, assessment)
                return

            if assessment.state in (classify.FAILED, classify.NEEDS_CONFIRM):
                signature = f"{assessment.state}:{assessment.last_assistant_index}:{len(assessment.reply_text)}"
                if signature != settle_sig:
                    settle_sig, settle_since = signature, _now()
                    await asyncio.sleep(2)
                    continue
                if _now() - settle_since < SETTLE_SECONDS:
                    await asyncio.sleep(2)
                    continue
            else:
                settle_sig = ""
            self._record_replies(task, messages, assessment, seen_replies)

            if assessment.state == classify.FAILED:
                self._handle_failure(task, assessment)
                return

            if assessment.state == classify.GENERATING:
                if task.generating_since is None:
                    task.generating_since = _now()
                if assessment.eta_minutes:
                    task.eta_minutes = assessment.eta_minutes
                if last_state != classify.GENERATING or task.status != GENERATING or task.stage != "视频生成中":
                    self.set_status(task, GENERATING, "视频生成中")
                    since = task.submitted_at or 0
                    if not any("开始生成视频" in e.get("text", "") and e.get("t", 0) >= since for e in task.events[-40:]):
                        eta = f"，豆包预计 {assessment.eta_minutes:g} 分钟" if assessment.eta_minutes else ""
                        self.event(task, f"豆包已开始生成视频{eta}")
                        self._rate_hits = 0
                last_state = classify.GENERATING
                if self._generation_timed_out(task, settings):
                    return
                await asyncio.sleep(10)
                continue

            if assessment.state == classify.NEEDS_CONFIRM:
                last_state = classify.NEEDS_CONFIRM
                if task.continues >= settings.max_auto_continue:
                    self._fail(
                        task,
                        "no_start",
                        f"已自动回复 {task.continues} 次，豆包仍没有开始生成。豆包最后说：{classify.strip_markdown(assessment.reply_text)[:200]}",
                    )
                    return
                if not await self._send_continue(task, assessment, settings.continue_text):
                    return
                settle_sig = ""
                continue

            # WAITING: no complete reply yet.
            if task.generating_since is None:
                if last_state == "" or task.status not in (WAITING_REPLY, CONFIRMING) or task.stage == "等待重新登录豆包":
                    self.set_status(task, WAITING_REPLY, "等待豆包回复")
            elif task.status != GENERATING or task.stage != "视频生成中":
                self.set_status(task, GENERATING, "视频生成中")
            last_state = classify.WAITING
            if task.generating_since is not None:
                if self._generation_timed_out(task, settings):
                    return
            elif _now() - (task.last_sent_at or _now()) > settings.reply_timeout_minutes * 60:
                self._fail(task, "no_reply", f"豆包 {settings.reply_timeout_minutes} 分钟没有回复")
                return
            await asyncio.sleep(3)

    def _generation_timed_out(self, task: Task, settings: config.Settings) -> bool:
        start = self._generation_start(task)
        limit = settings.generation_timeout_minutes * 60
        if start and _now() - start > limit:
            self._fail(task, "timeout", f"生成超过 {settings.generation_timeout_minutes} 分钟仍未完成")
            return True
        return False

    def _record_replies(self, task: Task, messages: list[dict[str, Any]], assessment: classify.Assessment, seen: set[Any]) -> None:
        if not assessment.finished:
            return
        for message in sorted(messages, key=classify.message_index):
            if classify.as_int(message.get("user_type")) != classify.ASSISTANT:
                continue
            key = f"{message.get('message_id')}:{len(str(message.get('content') or ''))}"
            if key in seen:
                continue
            texts = []
            for block in classify.parse_blocks(message):
                content = block.get("content") if isinstance(block.get("content"), dict) else {}
                text = ((content.get("text_block") or {}).get("text") or "").strip()
                if text:
                    texts.append(text)
                if "creation_block" in content:
                    texts.append("[视频]")
            if not texts:
                continue
            seen.add(key)
            text = classify.strip_markdown("\n".join(texts))
            task.replies.append({"key": key, "t": _now(), "text": text})
            if len(task.replies) > 50:
                task.replies = task.replies[-50:]
            short = re.sub(r"\s+", " ", text)
            self.event(task, f"豆包：{short[:160]}{'…' if len(short) > 160 else ''}")

    async def _latest_user_index(self, task: Task) -> int | None:
        try:
            return classify.assess(await self.chrome.pull_messages(task.conversation_id)).last_user_index
        except Exception:
            return None

    async def _wait_reply_landed(self, task: Task, before: int, timeout: float = 40) -> bool:
        """Wait until our reply shows up in the conversation (captcha time not counted)."""

        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2)
            if task.cancel_requested:
                raise TaskCanceled()
            if self.captcha or self.login_state == "logged_out":
                continue
            waited += 2
            latest = await self._latest_user_index(task)
            if latest is not None and latest > before:
                return True
        return False

    async def _send_continue(self, task: Task, assessment: classify.Assessment, text: str) -> bool:
        self.set_status(task, CONFIRMING, "豆包返回了内容，自动回复继续生成")
        self.event(task, f"豆包要求确认，自动回复「{text}」（第 {task.continues + 1} 次）")
        before = assessment.last_user_index
        for _ in range(3):
            if task.cancel_requested:
                raise TaskCanceled()
            await self.wait_logged_in(task)
            # An earlier attempt may have landed late: never send twice.
            latest = await self._latest_user_index(task)
            if latest is not None and latest > before:
                return self._continue_landed(task)
            try:
                async with self.chrome.ui_lock:
                    page = await self.chrome.work_page()
                    await doubao.send_reply(page, task.conversation_id, text, lambda s: self._checkpoint(task, s))
            except TaskCanceled:
                raise
            except doubao.LoginRequired:
                await self._on_logged_out("自动回复时发现豆包未登录")
                continue
            except Exception as exc:  # noqa: BLE001
                self.event(task, f"自动回复失败：{exc}", "warn")
                if await self._probe_logged_out(force=True):
                    continue
                await asyncio.sleep(3)
                continue
            if await self._wait_reply_landed(task, before):
                return self._continue_landed(task)
        self._fail(task, "continue", "自动回复没有发送成功")
        return False

    def _continue_landed(self, task: Task) -> bool:
        task.continues += 1
        task.last_sent_at = _now()
        self.set_status(task, WAITING_REPLY, "已自动确认，等待豆包开始生成")
        return True

    def _handle_failure(self, task: Task, assessment: classify.Assessment) -> None:
        kind = assessment.fail_kind or classify.FAIL_GENERATION
        detail = assessment.reason or classify.strip_markdown(assessment.reply_text)[:300]
        self._fail(task, kind, f"{FAIL_LABELS.get(kind, '生成失败')}：{detail}")

    def _start_cooldown(self, kind: str, message: str) -> None:
        settings = config.load_settings()
        if kind == classify.FAIL_QUOTA:
            seconds = settings.quota_wait_minutes * 60
        else:
            seconds = min(60, 10 * (2 ** min(self._rate_hits, 3))) * 60
            self._rate_hits += 1
        self.cooldown_until = max(self.cooldown_until, _now() + seconds)
        self.cooldown_kind = kind
        self.cooldown_reason = message[:200]
        self.publish_state()
        self.save()

    def _retry_delay(self, task: Task, kind: str, settings: config.Settings) -> float:
        if kind in COOLDOWN_KINDS:
            return max(30.0, self.cooldown_until - _now())
        if kind in RECHECK_KINDS:
            return min(1800, 30 * (2 ** min(task.rechecks, 6)))
        base = settings.retry_delay_minutes * 60
        return min(settings.retry_max_delay_minutes * 60, base * (2 ** min(task.retry_count, 6)))

    def _fail(self, task: Task, kind: str, message: str) -> None:
        settings = config.load_settings()
        now = _now()
        task.error = {"kind": kind, "message": message, "t": now}
        task.history.append(
            {"t": now, "attempt": task.retry_count + 1, "kind": kind, "message": message[:300], "conversation_id": task.conversation_id}
        )
        if len(task.history) > 100:
            task.history = task.history[-100:]
        if kind in COOLDOWN_KINDS:
            self._start_cooldown(kind, message)
        # Space the next submission from this failure, too.
        self.last_submit_at = max(self.last_submit_at, now)

        can_retry = (
            settings.auto_retry
            and kind not in NON_RETRYABLE
            and not task.cancel_requested
            and (settings.max_retries == 0 or task.retry_count < settings.max_retries)
        )
        if not can_retry:
            self.set_status(task, FAILED, "失败")
            self.event(task, message, "error")
            if settings.notify:
                notify("豆包视频生成失败", f"{task.title}：{message[:120]}")
            return

        recheck = kind in RECHECK_KINDS and bool(task.conversation_id)
        if recheck and kind not in RECHECK_ONLY and task.rechecks >= MAX_RECHECKS:
            recheck = False  # the old conversation is not coming back; start over
        delay = self._retry_delay(task, kind, settings)
        task.retry_count += 1
        task.rechecks = task.rechecks + 1 if recheck else 0
        task.next_retry_at = now + delay
        task.retry_mode = "recheck" if recheck else "new"
        what = "重新检查原对话" if recheck else "新建对话重试"
        limit = "" if settings.max_retries == 0 else f"/{settings.max_retries}"
        self.set_status(task, RETRY_WAIT, f"{FAIL_LABELS.get(kind, '失败')}，{_fmt_wait(delay)}后自动{what}")
        self.event(task, message, "warn")
        self.event(task, f"将在 {_fmt_wait(delay)}后自动{what}（第 {task.retry_count}{limit} 次重试）")
        if settings.notify and (task.retry_count == 1 or kind in COOLDOWN_KINDS):
            notify("豆包视频生成失败，将自动重试", f"{task.title}：{message[:100]}")

    async def _download(self, task: Task, assessment: classify.Assessment) -> None:
        self.set_status(task, DOWNLOADING, "视频已生成，正在下载")
        self.event(task, "豆包视频生成成功，开始下载")
        settings = config.load_settings()
        out_dir = Path(settings.output_dir).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / f".write-test-{uuid.uuid4().hex[:6]}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            self._fail(task, "download", f"视频已生成，但保存位置不可用（{out_dir}：{exc.strerror or exc}）。请在设置里换一个保存位置，之后会自动重新下载。")
            return
        vids = [v.vid for v in assessment.videos]
        clean: dict[str, str] = {}
        try:
            clean = await self.chrome.watermark_free_urls(vids)
        except Exception as exc:
            self.event(task, f"获取无水印地址失败，改用对话中的地址：{exc}", "warn")
        files = []
        stamp = datetime.fromtimestamp(task.created_at).strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}_{_safe_name(task.title)}"
        for index, video in enumerate(assessment.videos):
            urls = [u for u in (clean.get(video.vid), video.download_url) if u]
            if not urls:
                continue
            suffix = f"_{index + 1}" if len(assessment.videos) > 1 else ""
            dest = _unique(out_dir / f"{base}{suffix}.mp4")
            last_error: Exception | None = None
            for attempt in range(4):
                url = urls[min(attempt // 2, len(urls) - 1)]
                try:
                    size = await self.chrome.download(url, dest)
                    files.append(
                        {
                            "path": str(dest),
                            "name": dest.name,
                            "size": size,
                            "vid": video.vid,
                            "watermark_free": url == clean.get(video.vid),
                            "duration": video.duration,
                            "width": video.width,
                            "height": video.height,
                            "cover": video.cover_url,
                        }
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    await asyncio.sleep(3 * (attempt + 1))
            if last_error:
                self.event(task, f"下载第 {index + 1} 个视频失败：{last_error}", "warn")
            elif video.cover_url:
                with contextlib.suppress(Exception):
                    await self.chrome.download(video.cover_url, cover_path(task.id, len(files) - 1), min_bytes=256)
        if not files:
            self._fail(task, "download", "视频已生成，但下载失败")
            return
        _write_sidecar(files[0]["path"], task)
        task.result = {"files": files, "reply": classify.strip_markdown(assessment.reply_text)}
        task.next_retry_at = None
        task.error = {}
        self.set_status(task, SUCCESS, "成功")
        images.cleanup_task_cache(task.id)
        names = "、".join(f["name"] for f in files)
        tries = f"（共尝试 {task.retry_count + 1} 次）" if task.retry_count else ""
        self.event(task, f"已保存{tries}：{names}")
        if settings.notify:
            notify("豆包视频生成成功", f"{task.title} → {names}", sound=True)


def cover_path(task_id: str, index: int) -> Path:
    return config.CACHE_ROOT / "covers" / f"{task_id}_{index}.jpg"


def _best_chrome_profile() -> str:
    from . import chrome_cookies

    return chrome_cookies.best_profile()


def _short(exc: Exception) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text[:120] or exc.__class__.__name__


def _make_title(prompt: str, folder_name: str) -> str:
    text = re.sub(r"\s+", " ", prompt).strip()
    head = text[:18] + ("…" if len(text) > 18 else "")
    return f"{folder_name} · {head}" if folder_name else head


def _safe_name(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text).strip(" .·…")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "video")[:40]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{uuid.uuid4().hex[:6]}{path.suffix}")


def _write_sidecar(video_path: str, task: Task) -> None:
    path = Path(video_path).with_suffix(".txt")
    lines = [
        f"提示词：{task.prompt}",
        f"参考图文件夹：{task.folder or '（无）'}",
        f"参考图：{'、'.join(task.image_names) or '（无）'}",
        f"参数：{config.FIXED_MODEL} · {config.FIXED_RATIO} · {config.FIXED_DURATION_SECONDS}s",
        f"尝试次数：{task.retry_count + 1}",
        f"豆包对话：{task.conversation_url}",
        "",
        "豆包回复：",
        *[r.get("text", "") for r in task.replies],
    ]
    with contextlib.suppress(OSError):
        path.write_text("\n".join(lines), encoding="utf-8")
