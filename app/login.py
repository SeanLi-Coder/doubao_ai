"""Doubao login helpers: popups, login detection and the QR-code dialog."""

from __future__ import annotations

import contextlib

from playwright.async_api import Page

SEL_LOGIN_BUTTON = "[data-testid=to_login_button]"
SEL_QR = "[data-testid=qrcode_image]"
SEL_OPEN_DIALOG = "[role=dialog][data-state=open]"
QR_EXPIRED_HINTS = ("失效", "过期", "刷新", "重新获取")


async def dismiss_popups(page: Page) -> int:
    """Close promotional dialogs (e.g. "下载豆包电脑版") that block clicks.

    The login dialog (it contains the QR code) is left alone.
    """

    closed = 0
    for _ in range(3):
        try:
            dialogs = page.locator(SEL_OPEN_DIALOG)
            count = await dialogs.count()
        except Exception:
            return closed
        target = None
        for i in range(count):
            dialog = dialogs.nth(i)
            with contextlib.suppress(Exception):
                if await dialog.locator(SEL_QR).count():
                    continue
                if not await dialog.is_visible():
                    continue
                target = dialog
                break
        if target is None:
            return closed
        close = target.locator('button[aria-label="关闭"], button[aria-label="Close"], [data-testid*=close]').first
        try:
            if await close.count():
                await close.click(timeout=3000)
            else:
                await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            closed += 1
        except Exception:
            return closed
    return closed


async def login_button_visible(page: Page) -> bool:
    try:
        button = page.locator(SEL_LOGIN_BUTTON).first
        return bool(await button.count()) and await button.is_visible()
    except Exception:
        return False


async def qr_dialog_visible(page: Page) -> bool:
    try:
        qr = page.locator(SEL_QR).first
        return bool(await qr.count()) and await qr.is_visible()
    except Exception:
        return False


async def open_qr_dialog(page: Page) -> bool:
    """Make sure the login dialog with the QR code is showing on ``page``."""

    if await qr_dialog_visible(page):
        return True
    await dismiss_popups(page)
    if await qr_dialog_visible(page):
        return True
    if not await login_button_visible(page):
        return False
    with contextlib.suppress(Exception):
        await page.locator(SEL_LOGIN_BUTTON).first.click(timeout=8000)
    for _ in range(20):
        if await qr_dialog_visible(page):
            return True
        await page.wait_for_timeout(500)
    return False


async def qr_snapshot(page: Page) -> tuple[bytes, str]:
    """PNG of the QR code area plus its overlay text (e.g. "二维码已失效")."""

    qr = page.locator(SEL_QR).first
    # The container two levels up also holds the expired / scanned overlays.
    container = qr.locator("xpath=../..")
    text = ""
    with contextlib.suppress(Exception):
        text = (await container.inner_text()).strip()
    target = container if await container.count() else qr
    png = await target.screenshot(animations="disabled", timeout=8000)
    return png, text


async def refresh_qr_if_expired(page: Page, status_text: str) -> bool:
    if not any(h in (status_text or "") for h in QR_EXPIRED_HINTS):
        return False
    container = page.locator(SEL_QR).first.locator("xpath=../..")
    with contextlib.suppress(Exception):
        refresh = container.get_by_text("刷新").first
        if await refresh.count():
            await refresh.click(timeout=3000)
        else:
            await container.click(timeout=3000)
        await page.wait_for_timeout(1200)
        return True
    return False
