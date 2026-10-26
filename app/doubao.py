"""UI automation for Doubao "AI 创作 → 视频".

Every step verifies what the page shows afterwards, so a silent UI change
fails the task loudly instead of generating with the wrong settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import config, login
from .browser import BrowserError

Checkpoint = Callable[[str], Awaitable[None]]

SEL_VIDEO_TAB = "[data-testid=creation-skill-switch-tab-video]"
SEL_MODEL_BUTTON = '[data-input-engine-actionbar-control-key="video-model"]'
SEL_PARAMS_BUTTON = '[data-input-engine-actionbar-render-entry-key="creation-video-generation-params-panel"]'
SEL_PARAMS_MENU = "[role=menu][data-creation-params-panel-id]"
SEL_UPLOAD_INPUT = "[data-testid=upload-file-input]"
SEL_ATTACHMENT = "[data-testid=attachment-image-card]"
SEL_EDITOR = "[data-testid=chat_input_input] [contenteditable=true]"
SEL_SEND = "[data-testid=chat_input_send_button]"
CONVERSATION_RE = re.compile(r"/chat/(\d{6,})")


class AutomationError(BrowserError):
    """A UI step failed. ``kind`` lets the task manager pick the retry policy."""

    def __init__(self, message: str, kind: str = "submit") -> None:
        super().__init__(message)
        self.kind = kind


def _toast_kind(text: str) -> str:
    if any(k in text for k in ("频繁", "过多", "繁忙")):
        return "rate_limit"
    if any(k in text for k in ("额度", "上限", "次数")):
        return "quota"
    return "submit"


class LoginRequired(AutomationError):
    pass


async def visible_toasts(page: Page) -> list[str]:
    try:
        texts = await page.evaluate(
            """() => [...document.querySelectorAll('[class*=toast], [class*=Toast], [role=alert], [role=status]')]
                .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .map(e => (e.innerText || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []
    seen: list[str] = []
    for text in texts:
        if text not in seen and len(text) < 200:
            seen.append(text)
    return seen


LOGIN_MESSAGE = "豆包未登录或已下线，需要重新登录。"


async def _wait_for_editor(page: Page, timeout: float = 30000) -> None:
    try:
        await page.locator(SEL_EDITOR).first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeout as exc:
        if await login.login_button_visible(page) or await login.qr_dialog_visible(page):
            raise LoginRequired(LOGIN_MESSAGE) from exc
        raise AutomationError("豆包页面没有加载出输入框。") from exc
    # Logged-out pages still render the editor; the header login button tells.
    await page.wait_for_timeout(800)
    if await login.login_button_visible(page):
        raise LoginRequired(LOGIN_MESSAGE)
    await login.dismiss_popups(page)


async def open_video_creation(page: Page, checkpoint: Checkpoint) -> None:
    await checkpoint("打开豆包 AI 创作页面")
    await page.goto(config.CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    await checkpoint("等待页面加载")
    await _wait_for_editor(page)
    tab = page.locator(SEL_VIDEO_TAB).first
    try:
        await tab.wait_for(state="visible", timeout=20000)
    except PlaywrightTimeout as exc:
        raise AutomationError("没有找到「视频」切换按钮，豆包页面可能已改版。") from exc
    if await tab.get_attribute("aria-selected") != "true":
        await tab.click()
    for _ in range(20):
        if await tab.get_attribute("aria-selected") == "true":
            break
        await page.wait_for_timeout(250)
    else:
        raise AutomationError("切换到「视频」模式失败。")
    await page.locator(SEL_MODEL_BUTTON).first.wait_for(state="visible", timeout=15000)


async def select_model(page: Page, checkpoint: Checkpoint) -> None:
    await checkpoint(f"选择模型 {config.FIXED_MODEL}")
    await login.dismiss_popups(page)
    button = page.locator(SEL_MODEL_BUTTON).first
    if _model_matches(await button.inner_text()):
        return
    for attempt in range(2):
        await button.click()
        menu_item = page.get_by_role("menuitem").filter(
            has=page.locator("span", has_text=re.compile(rf"^\s*{re.escape(config.FIXED_MODEL)}\s*$"))
        ).first
        try:
            await menu_item.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeout:
            await page.keyboard.press("Escape")
            if attempt:
                raise AutomationError(f"模型列表里没有 {config.FIXED_MODEL}。")
            continue
        await menu_item.click()
        await page.wait_for_timeout(600)
        if _model_matches(await button.inner_text()):
            return
    raise AutomationError(f"切换模型到 {config.FIXED_MODEL} 失败。")


def _model_matches(label: str) -> bool:
    names = [line.strip() for line in (label or "").splitlines() if line.strip()]
    return config.FIXED_MODEL in names or (label or "").strip().endswith(config.FIXED_MODEL)


def _params_label_ok(label: str) -> bool:
    compact = re.sub(r"\s+", "", label or "")
    return config.FIXED_RATIO in compact and f"{config.FIXED_DURATION_SECONDS}s" in compact


async def set_ratio_and_duration(page: Page, checkpoint: Checkpoint) -> str:
    await checkpoint(f"设置比例 {config.FIXED_RATIO}、时长 {config.FIXED_DURATION_SECONDS}s")
    await login.dismiss_popups(page)
    button = page.locator(SEL_PARAMS_BUTTON).first
    await button.wait_for(state="visible", timeout=15000)
    for attempt in range(3):
        await button.click()
        menu = page.locator(SEL_PARAMS_MENU).first
        try:
            await menu.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeout:
            continue
        ratio = menu.get_by_role("button", name=config.FIXED_RATIO, exact=True).first
        await ratio.click()
        slider = menu.locator("[role=slider]").first
        await slider.wait_for(state="visible", timeout=5000)
        await slider.focus()
        await page.keyboard.press("End")
        await page.wait_for_timeout(300)
        value_now = await slider.get_attribute("aria-valuenow")
        value_max = await slider.get_attribute("aria-valuemax")
        duration_text = await menu.inner_text()
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
        label = (await button.inner_text()).strip()
        if _params_label_ok(label):
            return label
        if value_now != value_max or f"{config.FIXED_DURATION_SECONDS}s" not in duration_text:
            if attempt == 2:
                raise AutomationError(
                    f"当前模型的时长上限不是 {config.FIXED_DURATION_SECONDS}s（面板显示：{_one_line(duration_text)}）。"
                )
    raise AutomationError(f"比例/时长设置没有生效（当前显示：{_one_line(await button.inner_text())}）。")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:80]


async def upload_images(page: Page, images: list[Path], checkpoint: Checkpoint) -> None:
    attachments = page.locator(SEL_ATTACHMENT)
    existing = await attachments.count()
    if existing:
        # Leftovers from an earlier, interrupted attempt.
        for _ in range(existing):
            delete = page.locator("[data-testid=attachment-delete-btn]").first
            if await delete.count() == 0:
                break
            await delete.click(force=True)
            await page.wait_for_timeout(200)
    if not images:
        return
    await checkpoint(f"上传 {len(images)} 张参考图")
    await page.locator(SEL_UPLOAD_INPUT).first.set_input_files([str(p) for p in images])
    deadline = asyncio.get_running_loop().time() + 90
    while True:
        count = await attachments.count()
        toasts = [t for t in await visible_toasts(page) if any(k in t for k in ("不超过", "失败", "不支持", "过大", "格式"))]
        if toasts:
            raise AutomationError(f"上传参考图失败：{toasts[0]}")
        if count >= len(images) and not await _uploads_pending(page):
            break
        if asyncio.get_running_loop().time() > deadline:
            raise AutomationError(f"上传参考图超时（{count}/{len(images)} 张已显示）。")
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(800)


async def _uploads_pending(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => [...document.querySelectorAll('[data-testid=attachment-image-card]')].some(card =>
                    card.querySelector('[class*=loading], [class*=Loading], [class*=progress], [class*=uploading], [class*=spin], [role=progressbar]'))"""
            )
        )
    except Exception:
        return False


async def type_prompt(page: Page, text: str) -> None:
    editor = page.locator(SEL_EDITOR).first
    await editor.click()
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Backspace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if line:
            await page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            await page.keyboard.press("Shift+Enter")
    await page.wait_for_timeout(300)
    shown = re.sub(r"\s+", "", await editor.inner_text())
    expected = re.sub(r"\s+", "", text)
    if expected[:30] not in shown:
        raise AutomationError("提示词没有成功填入豆包输入框。")


async def click_send(page: Page) -> None:
    send = page.locator(SEL_SEND).first
    await send.wait_for(state="visible", timeout=15000)
    for _ in range(40):
        if await send.is_enabled() and (await send.get_attribute("data-disabled")) != "true":
            break
        await page.wait_for_timeout(250)
    await send.click()


async def submit_creation(page: Page, prompt: str, images: list[Path], checkpoint: Checkpoint) -> str:
    """Fill the AI 创作 video form and send it. Returns the conversation id."""

    await open_video_creation(page, checkpoint)
    await select_model(page, checkpoint)
    label = await set_ratio_and_duration(page, checkpoint)
    await upload_images(page, images, checkpoint)
    await checkpoint("填写提示词")
    await type_prompt(page, prompt)
    # Re-check the fixed parameters right before sending.
    if not _model_matches(await page.locator(SEL_MODEL_BUTTON).first.inner_text()):
        raise AutomationError(f"发送前模型不是 {config.FIXED_MODEL}。")
    if not _params_label_ok(await page.locator(SEL_PARAMS_BUTTON).first.inner_text()):
        raise AutomationError(f"发送前比例/时长被改动（{label}）。")
    await checkpoint("发送给豆包")
    await login.dismiss_popups(page)
    await click_send(page)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        match = CONVERSATION_RE.search(page.url or "")
        if match:
            return match.group(1)
        toasts = await visible_toasts(page)
        if toasts and any(k in toasts[0] for k in ("失败", "违规", "不支持", "额度", "上限", "错误", "频繁", "过多", "繁忙")):
            raise AutomationError(f"豆包拒绝了请求：{toasts[0]}", kind=_toast_kind(toasts[0]))
        if await login.qr_dialog_visible(page):
            raise LoginRequired(LOGIN_MESSAGE)
        # A captcha may pop up right after sending; time spent solving it
        # must not count against the deadline.
        paused_at = loop.time()
        await checkpoint("等待豆包响应")
        deadline += loop.time() - paused_at
        await page.wait_for_timeout(500)
    match = CONVERSATION_RE.search(page.url or "")
    if match:
        return match.group(1)
    raise AutomationError("发送后没有进入对话页面，无法确认豆包是否收到。")


def _on_conversation(page: Page, conversation_id: str) -> bool:
    match = CONVERSATION_RE.search(page.url or "")
    return bool(match) and match.group(1) == str(conversation_id)


async def send_reply(page: Page, conversation_id: str, text: str, checkpoint: Checkpoint) -> None:
    await checkpoint("打开对话")
    target = f"{config.DOUBAO_ORIGIN}/chat/{conversation_id}"
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    if not _on_conversation(page, conversation_id):
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    await _wait_for_editor(page)
    if not _on_conversation(page, conversation_id):
        raise AutomationError("没有停在目标对话页面，已取消这次自动回复。", kind="continue")
    await checkpoint(f"回复豆包：{text}")
    await type_prompt(page, text)
    await login.dismiss_popups(page)
    if not _on_conversation(page, conversation_id):
        raise AutomationError("页面在输入时跳转了，已取消这次自动回复。", kind="continue")
    await click_send(page)
    await page.wait_for_timeout(1500)
    if await login.qr_dialog_visible(page):
        raise LoginRequired(LOGIN_MESSAGE)
