"""Read doubao.com cookies from the user's everyday Google Chrome profile (macOS).

Only cookies whose host ends with ``doubao.com`` are read. Values are decrypted
locally with the "Chrome Safe Storage" key from the login Keychain (macOS may
ask the user to allow access) and are only ever handed to the dedicated
automation Chrome profile; they are never written to disk by this module.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import pbkdf2_hmac, sha256
from pathlib import Path
from typing import Any

CHROME_ROOT = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01
DOMAIN_SUFFIX = "doubao.com"
SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")
ACCOUNT_COOKIE = "multi_sids"  # guests also get a sessionid; only real accounts have this


class CookieImportError(RuntimeError):
    pass


@dataclass
class ChromeProfile:
    directory: str
    name: str
    has_doubao: bool
    has_session: bool
    last_used: bool


def _host_matches(host: str) -> bool:
    host = (host or "").lstrip(".").lower()
    return host == DOMAIN_SUFFIX or host.endswith("." + DOMAIN_SUFFIX)


_HOST_SQL = "(host_key IN ('doubao.com', '.doubao.com') OR host_key LIKE '%.doubao.com')"


def is_valid_profile(directory: str) -> bool:
    """Only accept profile directory names Chrome itself lists (no paths)."""

    if not directory or "/" in directory or "\\" in directory or directory.startswith("."):
        return False
    try:
        state = json.loads((CHROME_ROOT / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return directory in ((state.get("profile") or {}).get("info_cache") or {})


def _profile_cookie_db(directory: str) -> Path:
    base = CHROME_ROOT / directory
    for candidate in (base / "Network" / "Cookies", base / "Cookies"):
        if candidate.exists():
            return candidate
    return base / "Cookies"


def _query(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    # immutable=1 lets us read while Chrome holds the database open.
    uri = f"file:{db}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        conn = None
    if conn is None:
        return []
    try:
        return list(conn.execute(sql, params))
    except sqlite3.DatabaseError:
        # Fall back to a private copy (e.g. WAL-only changes).
        conn.close()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "Cookies"
            shutil.copy2(db, copy)
            for suffix in ("-wal", "-shm"):
                side = db.with_name(db.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
            conn2 = sqlite3.connect(str(copy))
            try:
                return list(conn2.execute(sql, params))
            finally:
                conn2.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_profiles() -> list[ChromeProfile]:
    try:
        state = json.loads((CHROME_ROOT / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    info = (state.get("profile") or {}).get("info_cache") or {}
    last_used = (state.get("profile") or {}).get("last_used") or ""
    profiles: list[ChromeProfile] = []
    for directory, meta in info.items():
        db = _profile_cookie_db(directory)
        names: set[str] = set()
        if db.exists():
            try:
                rows = _query(db, f"SELECT host_key, name FROM cookies WHERE {_HOST_SQL}")
                names = {r[1] for r in rows if _host_matches(r[0])}
            except Exception:
                names = set()
        profiles.append(
            ChromeProfile(
                directory=directory,
                name=str((meta or {}).get("name") or directory),
                has_doubao=bool(names),
                has_session=ACCOUNT_COOKIE in names and any(n in names for n in SESSION_COOKIES),
                last_used=directory == last_used,
            )
        )
    profiles.sort(key=lambda p: (not p.has_session, not p.last_used, p.directory))
    return profiles


def best_profile() -> str:
    for profile in list_profiles():
        if profile.has_session:
            return profile.directory
    return ""


def _safe_storage_password() -> bytes:
    for account in ("Chrome", "Google Chrome"):
        try:
            completed = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", account],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CookieImportError(f"无法读取钥匙串：{exc}") from exc
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    raise CookieImportError("没有拿到 Chrome 的钥匙串密钥。请在弹出的钥匙串对话框中点「允许」后重试。")


def _db_meta_version(db: Path) -> int:
    try:
        rows = _query(db, "SELECT value FROM meta WHERE key='version'")
        return int(rows[0][0]) if rows else 0
    except Exception:
        return 0


def _decrypt(encrypted: bytes, key: bytes, strip_domain_hash: bool) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted:
        return ""
    if encrypted[:3] not in (b"v10", b"v11"):
        return encrypted.decode("utf-8", "replace")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16:
        raise CookieImportError("Cookie 解密失败（填充无效）")
    plain = padded[:-pad]
    if strip_domain_hash and len(plain) >= 32:
        plain = plain[32:]
    return plain.decode("utf-8")


def _samesite(value: int) -> str | None:
    return {0: "None", 1: "Lax", 2: "Strict"}.get(int(value))


def read_doubao_cookies(profile_directory: str) -> list[dict[str, Any]]:
    if not profile_directory:
        raise CookieImportError("没有找到登录了豆包的 Chrome 用户。请先在日常 Chrome 中登录 www.doubao.com。")
    if not is_valid_profile(profile_directory):
        raise CookieImportError(f"Chrome 用户「{profile_directory}」不存在。")
    db = _profile_cookie_db(profile_directory)
    if not db.exists():
        raise CookieImportError(f"找不到 Chrome 用户「{profile_directory}」的 Cookie 数据库。")
    rows = _query(
        db,
        "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
        f"FROM cookies WHERE {_HOST_SQL}",
    )
    rows = [r for r in rows if _host_matches(str(r[0]))]
    if not rows:
        raise CookieImportError("这个 Chrome 用户里没有豆包的登录信息。请先在 Chrome 中登录 www.doubao.com。")

    key = pbkdf2_hmac("sha1", _safe_storage_password(), b"saltysalt", 1003, 16)
    strip_hash = _db_meta_version(db) >= 24
    cookies: list[dict[str, Any]] = []
    for host, name, value, encrypted, path, expires_utc, secure, httponly, samesite in rows:
        try:
            plain = value or _decrypt(bytes(encrypted or b""), key, strip_hash)
        except (CookieImportError, UnicodeDecodeError, ValueError):
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": plain,
            "domain": host,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(httponly),
        }
        expires = int(expires_utc or 0)
        if expires > 0:
            unix = expires / 1_000_000 - CHROME_EPOCH_OFFSET
            if unix > 0:
                cookie["expires"] = int(unix)
        same = _samesite(samesite) if samesite is not None else None
        if same and not (same == "None" and not cookie["secure"]):
            cookie["sameSite"] = same
        cookies.append(cookie)
    if not any(c["name"] in SESSION_COOKIES and c["value"] for c in cookies) or not any(c["name"] == ACCOUNT_COOKIE and c["value"] for c in cookies):
        raise CookieImportError("日常 Chrome 里的豆包没有登录（或只是游客身份）。请先在 Chrome 中登录豆包。")
    return cookies


def fingerprint(cookies: list[dict[str, Any]]) -> str:
    session = next((c["value"] for c in cookies if c["name"] == "sessionid"), "")
    return sha256(session.encode()).hexdigest()[:12] if session else ""
