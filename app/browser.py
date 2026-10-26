"""Dedicated, visible Google Chrome instance driven over CDP.

We launch the user's installed Google Chrome with its own ``--user-data-dir``
(Chrome refuses remote debugging on the everyday profile) and attach
Playwright to it. The window stays visible so the user can log in or finish a
security check (captcha) by hand whenever Doubao asks for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from . import config

log = logging.getLogger(__name__)

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
RUNTIME_FILE = config.RUNTIME_ROOT / "chrome.json"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

# Query string the Doubao web client sends with every API call.
API_QUERY = {
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "aid": "497858",
    "real_aid": "497858",
    "pkg_type": "release_version",
    "samantha_web": "1",
    "web_platform": "browser",
}

CAPTCHA_SELECTORS = (
    "#captcha_container",
    "#captcha-verify-image",
    "#verify-bar-box",
    ".captcha_verify_container",
    ".captcha-verify-container",
    ".captcha_verify_img_slide",
    ".secsdk-captcha-drag-icon",
    ".vc-captcha-verify",
    "[id^=captcha]",
    "[class*=captcha_verify]",
    "[class*=captcha-verify]",
)
CAPTCHA_TEXTS = ("请完成下列验证后继续", "请完成安全验证", "安全验证", "拖动滑块", "按住左边按钮拖动", "请依次点击", "向右拖动")
CAPTCHA_URL_HINTS = ("captcha", "verifycenter", "verify.zijieapi", "verify.snssdk", "rmc.bytedance.com/verify")

_VISIBLE_JS = """(el) => {
  const r = el.getBoundingClientRect();
  if (r.width < 40 || r.height < 40) return false;
  if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) < 0.05) return false;
  }
  return true;
}"""


class BrowserError(RuntimeError):
    pass


class CaptchaRequired(BrowserError):
    pass


class ApiError(BrowserError):
    NOT_FOUND = 712017001  # "数据不存在": conversation missing, or not logged in

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        try:
            self.code = int(code) if code is not None else None
        except (TypeError, ValueError):
            self.code = None


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise BrowserError("没有找到 Google Chrome。请先安装 Chrome（https://www.google.com/chrome/）。")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _devtools_alive(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_doubao(url: str) -> bool:
    return (url or "").startswith(config.DOUBAO_ORIGIN)


async def _close_quietly(page: Page | None) -> None:
    if page is not None and not page.is_closed():
        with contextlib.suppress(Exception):
            await page.close()


class ChromeController:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pid: int | None = None
        self._lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._api_page: Page | None = None
        self._work_page: Page | None = None
        self._login_page: Page | None = None
        self.ui_lock = asyncio.Lock()
        self.on_disconnect: Callable[[], None] | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def ensure(self) -> BrowserContext:
        async with self._lock:
            if self.running and self._context is not None:
                return self._context
            await self._teardown_connection()
            port = await asyncio.to_thread(self._reuse_or_launch)
            if self._pw is None:
                self._pw = await async_playwright().start()
            deadline = time.monotonic() + 30
            last_error: Exception | None = None
            browser: Browser | None = None
            while time.monotonic() < deadline:
                try:
                    browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
                    break
                except Exception as exc:  # Chrome still starting
                    last_error = exc
                    await asyncio.sleep(0.5)
            if browser is None:
                raise BrowserError(f"无法连接到 Chrome：{last_error}")
            self._browser = browser
            browser.on("disconnected", lambda *_: self._on_disconnect())
            self._context = browser.contexts[0] if browser.contexts else await browser.new_context()
            return self._context

    def _on_disconnect(self) -> None:
        log.warning("Chrome disconnected")
        self._browser = None
        self._context = None
        self._api_page = None
        self._work_page = None
        self._login_page = None
        if self.on_disconnect is not None:
            with contextlib.suppress(Exception):
                self.on_disconnect()

    def _reuse_or_launch(self) -> int:
        """Blocking: reuse our Chrome from a previous run, or start a new one."""

        try:
            saved = json.loads(RUNTIME_FILE.read_text())
            pid, port = int(saved["pid"]), int(saved["port"])
            if _pid_alive(pid) and _devtools_alive(port):
                self._pid = pid
                return port
        except (OSError, ValueError, KeyError, TypeError):
            pass
        chrome = find_chrome()
        config.ensure_dirs()
        port = _free_port()
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={config.CHROME_PROFILE_ROOT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,ChromeWhatsNewUI",
            "--window-size=1360,960",
            config.CHAT_URL,
        ]
        log.info("Launching Chrome on port %s", port)
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self._pid = process.pid
        RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_FILE.write_text(json.dumps({"pid": process.pid, "port": port}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _devtools_alive(port):
                return port
            if process.poll() is not None:
                raise BrowserError("Chrome 启动后立即退出了。可能还有一个用本工具数据目录启动的 Chrome 没关干净，请稍等几秒再试。")
            time.sleep(0.3)
        raise BrowserError("等待 Chrome 启动超时。")

    async def _teardown_connection(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=5)
        self._context = None
        self._api_page = None
        self._work_page = None
        self._login_page = None

    async def shutdown(self, close_chrome: bool = True) -> None:
        async with self._lock:
            await self._teardown_connection()
            if self._pw is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._pw.stop(), timeout=5)
                self._pw = None
            if close_chrome and self._pid and _pid_alive(self._pid):
                with contextlib.suppress(OSError):
                    os.kill(self._pid, signal.SIGTERM)
                with contextlib.suppress(OSError):
                    RUNTIME_FILE.unlink()

    # ------------------------------------------------------------------ pages
    def _helper_pages(self) -> tuple[Page | None, ...]:
        return (self._api_page, self._login_page)

    async def work_page(self) -> Page:
        """The tab used for UI automation (submitting prompts, replying)."""
        context = await self.ensure()
        if self._work_page is None or self._work_page.is_closed():
            helpers = self._helper_pages()
            pages = [p for p in context.pages if not p.is_closed() and p not in helpers]
            doubao = [p for p in pages if _is_doubao(p.url)]
            self._work_page = doubao[0] if doubao else (pages[0] if pages else await context.new_page())
        return self._work_page

    def existing_work_page(self) -> Page | None:
        page = self._work_page
        return page if page is not None and not page.is_closed() else None

    async def open_login_page(self) -> Page:
        """A dedicated tab that shows Doubao's QR-code login dialog."""
        context = await self.ensure()
        if self._login_page is None or self._login_page.is_closed():
            self._login_page = await context.new_page()
        with contextlib.suppress(Exception):
            await self._login_page.set_viewport_size({"width": 1280, "height": 860})
        return self._login_page

    def existing_login_page(self) -> Page | None:
        page = self._login_page
        return page if page is not None and not page.is_closed() else None

    async def close_login_page(self) -> None:
        page, self._login_page = self._login_page, None
        await _close_quietly(page)

    async def api_page(self) -> Page:
        """A quiet background tab on doubao.com used for same-origin API calls."""
        async with self._api_lock:
            await self.ensure()
            page = self._api_page
            if page is not None and not page.is_closed() and _is_doubao(page.url):
                return page
            await _close_quietly(page)
            context = await self.ensure()
            page = await context.new_page()
            self._api_page = page
            try:
                await page.goto(f"{config.DOUBAO_ORIGIN}/chat/create-image", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                self._api_page = None
                await _close_quietly(page)
                raise
            work = self.existing_work_page()
            if work is not None:
                with contextlib.suppress(Exception):
                    await work.bring_to_front()
            return page

    def reset_api_page(self) -> None:
        """Forget the API tab so the next call reopens it (e.g. after re-login)."""
        page, self._api_page = self._api_page, None
        if page is not None:
            asyncio.get_running_loop().create_task(_close_quietly(page))

    async def reload_api_page(self) -> Page | None:
        try:
            page = await self.api_page()
            async with self._api_lock:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            return page
        except Exception:
            self.reset_api_page()
            return None

    # ------------------------------------------------------------------ API calls
    async def api_call(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 30) -> dict[str, Any]:
        page = await self.api_page()
        script = """async ({path, query, body, headers, timeoutMs}) => {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), timeoutMs);
            try {
              const resp = await fetch(path + '?' + new URLSearchParams(query), {
                method: 'POST', credentials: 'include', signal: ctrl.signal,
                headers: Object.assign({'content-type': 'application/json'}, headers || {}),
                body: JSON.stringify(body),
              });
              const text = await resp.text();
              return {status: resp.status, text};
            } catch (e) {
              return {status: 0, text: String(e)};
            } finally { clearTimeout(t); }
        }"""
        try:
            result = await page.evaluate(
                script,
                {"path": path, "query": API_QUERY, "body": body, "headers": headers or {}, "timeoutMs": int(timeout * 1000)},
            )
        except Exception as exc:
            # The tab navigated or crashed; recreate it next time.
            self.reset_api_page()
            raise BrowserError(f"调用豆包接口失败：{exc}") from exc
        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        if status == 0:
            raise BrowserError(f"调用豆包接口失败：{text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"_raw": text[:500]}
        if not isinstance(payload, dict):
            payload = {"_raw": str(payload)[:500]}
        payload["_http_status"] = status
        return payload

    async def pull_messages(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        body = {
            "cmd": 3100,
            "uplink_body": {
                "pull_singe_chain_uplink_body": {
                    "conversation_id": str(conversation_id),
                    "anchor_index": 9007199254740991,
                    "conversation_type": 3,
                    "direction": 1,
                    "limit": limit,
                    "ext": {},
                    "filter": {"index_list": []},
                    "evaluate_ab_params": "",
                    "evaluate_common_params": "",
                    "option": {"lazy_load_strategy": 0},
                }
            },
            "sequence_id": str(uuid.uuid4()),
            "channel": 2,
            "version": "1",
        }
        payload = await self.api_call(
            "/im/chain/single",
            body,
            headers={"content-type": "application/json; encoding=utf-8", "agw-js-conv": "str"},
        )
        downlink = (payload.get("downlink_body") or {}).get("pull_singe_chain_downlink_body") or {}
        messages = downlink.get("messages")
        if not isinstance(messages, list):
            if payload.get("_http_status") in (401, 403) or _looks_like_verify(payload):
                raise CaptchaRequired("豆包要求安全验证")
            code = payload.get("status_code")
            desc = payload.get("status_desc") or payload.get("_raw") or ""
            raise ApiError(f"读取对话失败（{code} {desc}）".strip(), code=code)
        return messages

    async def verify_login(self) -> bool | None:
        """Authoritative login check: True / False, or None when it cannot tell."""

        context = await self.ensure()
        cookies = {c.get("name"): c.get("value") for c in await context.cookies(config.DOUBAO_ORIGIN)}
        uid = _account_uid(cookies)
        if not cookies.get("sessionid") or not uid:
            # Doubao hands logged-out visitors a guest sessionid; only a real
            # account carries ``multi_sids`` ("<uid>:<sid>").
            return False
        try:
            payload = await self.api_call("/alice/profile/self", {"visit_id": uid, "avatar_format": "png"}, timeout=15)
        except Exception:
            return None
        if payload.get("code") == 0 and isinstance(payload.get("data"), dict):
            return True
        if payload.get("_http_status") == 200 and payload.get("code") not in (None, 0):
            return False
        return None

    async def watermark_free_urls(self, vids: list[str]) -> dict[str, str]:
        payload = await self.api_call("/creativity/resource/get_without_watermark", {"vid": vids})
        data = payload.get("data") or {}
        out: dict[str, str] = {}
        for vid, item in (data.get("download_video") or {}).items():
            url = (item or {}).get("download_url")
            if url:
                out[str(vid)] = str(url)
        return out

    async def download(self, url: str, dest: Path, min_bytes: int = 1024) -> int:
        """Download ``url`` to ``dest`` (streamed to disk, then renamed into place)."""

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            size = await asyncio.to_thread(_stream_to_file, url, tmp)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            # Signed CDN links normally need no cookies; fall back to the
            # browser context (cookies + proxy) if the plain request fails.
            log.info("Direct download failed (%s), retrying through the browser context", exc)
            size = await self._download_via_context(url, tmp)
        if size < min_bytes:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise BrowserError("下载到的文件太小，可能链接已失效。")
        os.replace(tmp, dest)
        return size

    async def _download_via_context(self, url: str, tmp: Path) -> int:
        context = await self.ensure()
        response = await context.request.get(url, timeout=600000, max_redirects=5)
        try:
            if not response.ok:
                raise BrowserError(f"下载失败：HTTP {response.status}")
            body = await response.body()
        finally:
            with contextlib.suppress(Exception):
                await response.dispose()
        tmp.write_bytes(body)
        return len(body)

    # ------------------------------------------------------------------ state checks
    def _scan_pages(self) -> list[Page]:
        if not self.running or self._context is None:
            return []
        pages = []
        for page in self._context.pages:
            if page.is_closed():
                continue
            url = (page.url or "").lower()
            if _is_doubao(page.url) or any(h in url for h in CAPTCHA_URL_HINTS):
                pages.append(page)
        return pages

    async def captcha_page(self) -> Page | None:
        for page in self._scan_pages():
            try:
                if await _page_has_captcha(page):
                    return page
            except Exception:
                continue
        return None

    async def captcha_clip(self, page: Page) -> dict[str, float] | None:
        """Bounding box (with margin) of the visible captcha on ``page``."""

        boxes: list[dict[str, float]] = []
        for frame in page.frames:
            if frame is page.main_frame or not any(h in (frame.url or "").lower() for h in CAPTCHA_URL_HINTS):
                continue
            with contextlib.suppress(Exception):
                element = await frame.frame_element()
                try:
                    box = await element.bounding_box()
                finally:
                    await element.dispose()
                if box:
                    boxes.append(box)
        if not boxes:
            with contextlib.suppress(Exception):
                found = await page.evaluate(
                    """(selectors) => {
                        for (const sel of selectors) for (const el of document.querySelectorAll(sel)) {
                          const r = el.getBoundingClientRect();
                          if (r.width > 40 && r.height > 40) return {x: r.x, y: r.y, width: r.width, height: r.height};
                        }
                        return null;
                    }""",
                    list(CAPTCHA_SELECTORS),
                )
                if found:
                    boxes.append(found)
        if not boxes:
            return None
        box = max(boxes, key=lambda b: b["width"] * b["height"])
        viewport = page.viewport_size or await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        margin = 48
        x = max(0.0, box["x"] - margin)
        y = max(0.0, box["y"] - margin)
        width = min(viewport["width"] - x, box["width"] + 2 * margin)
        height = min(viewport["height"] - y, box["height"] + 2 * margin)
        return {"x": x, "y": y, "width": width, "height": height} if width > 0 and height > 0 else None

    async def captcha_visible(self) -> bool:
        return await self.captcha_page() is not None

    async def bring_to_front(self, page: Page | None = None) -> None:
        try:
            target = page or await self.work_page()
            await target.bring_to_front()
        except Exception:
            pass
        if not self._pid:
            return
        script = f'tell application "System Events" to set frontmost of (first process whose unix id is {int(self._pid)}) to true'
        with contextlib.suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=5)

    async def account_cookie_present(self) -> bool:
        """Cheap check: does the browser still hold a logged-in (non-guest) account?"""
        context = await self.ensure()
        cookies = {c.get("name"): c.get("value") for c in await context.cookies(config.DOUBAO_ORIGIN)}
        return bool(cookies.get("sessionid")) and bool(_account_uid(cookies))

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        context = await self.ensure()
        await context.add_cookies(cookies)


def _account_uid(cookies: dict[str, Any]) -> str:
    uid = urllib.parse.unquote(str(cookies.get("multi_sids") or "")).split(":")[0].strip()
    return uid if uid.isdigit() else ""


def _stream_to_file(url: str, tmp: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": f"{config.DOUBAO_ORIGIN}/"})
    size = 0
    with urllib.request.urlopen(request, timeout=60) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            size += len(chunk)
    return size


def _looks_like_verify(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)[:4000].lower()
    return any(h in text for h in ("verify_center", "captcha", "need_verify", "\"verify\""))


async def _page_has_captcha(page: Page) -> bool:
    if any(h in (page.url or "").lower() for h in CAPTCHA_URL_HINTS):
        return True
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        url = (frame.url or "").lower()
        if not any(h in url for h in CAPTCHA_URL_HINTS):
            continue
        try:
            element = await frame.frame_element()
            try:
                if await element.evaluate(_VISIBLE_JS):
                    return True
            finally:
                await element.dispose()
        except Exception:
            continue  # frame detached mid-check
    return bool(
        await page.evaluate(
            """({selectors, texts}) => {
                const visible = """ + _VISIBLE_JS + """;
                for (const sel of selectors) {
                  for (const el of document.querySelectorAll(sel)) { if (visible(el)) return true; }
                }
                const dialogs = document.querySelectorAll('[role=dialog], [class*=modal], [class*=Modal], [class*=verify]');
                for (const d of dialogs) {
                  if (!visible(d)) continue;
                  const t = d.innerText || '';
                  if (texts.some(x => t.includes(x))) return true;
                }
                return false;
            }""",
            {"selectors": list(CAPTCHA_SELECTORS), "texts": list(CAPTCHA_TEXTS)},
        )
    )
