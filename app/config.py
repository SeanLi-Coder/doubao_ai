from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_data_root() -> Path:
    override = os.environ.get("DOUBAO_VIDEO_DATA")
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "data"


DATA_ROOT = _default_data_root()
RUNTIME_ROOT = DATA_ROOT / "runtime"
CHROME_PROFILE_ROOT = DATA_ROOT / "chrome-profile"
CACHE_ROOT = DATA_ROOT / "cache"
LOG_ROOT = DATA_ROOT / "logs"
SETTINGS_PATH = DATA_ROOT / "settings.json"
TASKS_PATH = DATA_ROOT / "tasks.json"
DEFAULT_OUTPUT_DIR = Path.home() / "Movies" / "豆包视频生成"

DOUBAO_ORIGIN = "https://www.doubao.com"
CREATE_URL = f"{DOUBAO_ORIGIN}/chat/create-image"
CHAT_URL = f"{DOUBAO_ORIGIN}/chat/"

# Fixed generation parameters requested for every task.
FIXED_MODEL = "Seedance 2.5"
FIXED_RATIO = "9:16"
FIXED_DURATION_SECONDS = 30

MAX_REFERENCE_IMAGES = 10
UPLOADABLE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".apng"}
CONVERTIBLE_SUFFIXES = {".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
# Doubao rejects very large reference images; anything above this is re-encoded.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass
class Settings:
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    continue_text: str = "确认，直接生成"
    max_auto_continue: int = 3
    # One task at a time by default: submitting too often gets the account
    # logged out by Doubao.
    max_parallel: int = 1
    submit_gap_seconds: int = 30
    reply_timeout_minutes: int = 6
    generation_timeout_minutes: int = 120
    auto_retry: bool = True
    max_retries: int = 0  # 0 = keep retrying until the video is generated
    retry_delay_minutes: float = 2
    retry_max_delay_minutes: float = 30
    quota_wait_minutes: int = 60
    auto_import_login: bool = True
    chrome_profile: str = ""
    notify: bool = True

    def normalized(self) -> "Settings":
        self.continue_text = (self.continue_text or "").strip() or "确认，直接生成"
        # Doubao always asks for confirmation once, so at least one auto-reply is required.
        self.max_auto_continue = _clamp(self.max_auto_continue, 1, 10, int)
        self.max_parallel = _clamp(self.max_parallel, 1, 5, int)
        self.submit_gap_seconds = _clamp(self.submit_gap_seconds, 5, 3600, int)
        self.reply_timeout_minutes = _clamp(self.reply_timeout_minutes, 1, 60, int)
        self.generation_timeout_minutes = _clamp(self.generation_timeout_minutes, 10, 24 * 60, int)
        self.max_retries = _clamp(self.max_retries, 0, 1000, int)
        self.retry_delay_minutes = _clamp(self.retry_delay_minutes, 0.5, 240, float)
        self.retry_max_delay_minutes = _clamp(self.retry_max_delay_minutes, self.retry_delay_minutes, 24 * 60, float)
        self.quota_wait_minutes = _clamp(self.quota_wait_minutes, 5, 24 * 60, int)
        self.auto_retry = bool(self.auto_retry)
        self.auto_import_login = bool(self.auto_import_login)
        self.notify = bool(self.notify)
        self.chrome_profile = str(self.chrome_profile or "")
        self.output_dir = str(Path(self.output_dir or DEFAULT_OUTPUT_DIR).expanduser())
        return self


def _clamp(value: Any, low: float, high: float, kind: type) -> Any:
    try:
        number = kind(value)
    except (TypeError, ValueError):
        number = kind(low)
    return kind(max(low, min(number, high)))


_settings_lock = threading.Lock()


def ensure_dirs() -> None:
    for path in (DATA_ROOT, RUNTIME_ROOT, CHROME_PROFILE_ROOT, CACHE_ROOT, LOG_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> Settings:
    with _settings_lock:
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    known = {f.name for f in fields(Settings)}
    values = {k: v for k, v in raw.items() if k in known} if isinstance(raw, dict) else {}
    try:
        return Settings(**values).normalized()
    except (TypeError, ValueError):
        return Settings().normalized()


def save_settings(settings: Settings) -> Settings:
    settings = settings.normalized()
    with _settings_lock:
        atomic_write_json(SETTINGS_PATH, asdict(settings))
    return settings


def update_settings(patch: dict[str, Any]) -> Settings:
    current = asdict(load_settings())
    known = {f.name for f in fields(Settings)}
    for key, value in patch.items():
        if key in known and value is not None:
            current[key] = value
    return save_settings(Settings(**current))

