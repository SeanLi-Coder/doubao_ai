from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config


class ImageFolderError(ValueError):
    pass


@dataclass
class FolderScan:
    folder: Path
    images: list[Path]
    skipped: list[str]
    prompt_file: Path | None

    def as_dict(self) -> dict:
        return {
            "folder": str(self.folder),
            "images": [p.name for p in self.images],
            "count": len(self.images),
            "skipped": self.skipped,
            "prompt": read_prompt_file(self.prompt_file) if self.prompt_file else "",
            "too_many": len(self.images) > config.MAX_REFERENCE_IMAGES,
            "limit": config.MAX_REFERENCE_IMAGES,
        }


PROMPT_FILE_NAMES = ("prompt.txt", "提示词.txt", "prompt.md", "提示词.md")
BROWSER_FRIENDLY = {".jpg", ".jpeg", ".png", ".webp", ".apng", ".gif"}
_PERMISSION_HINT = "无法读取这个文件夹。如果它在「桌面 / 文稿 / 下载」或外接盘里，请在「系统设置 → 隐私与安全性」中给终端（或本应用）开启访问权限。"


def _natural_key(path: Path) -> list:
    return [int(part) if part.isdecimal() and part.isascii() else part.lower() for part in re.split(r"(\d+)", path.name)]


def read_prompt_file(path: Path | None) -> str:
    if not path:
        return ""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _sorted_entries(folder: Path) -> list[Path]:
    try:
        return sorted(folder.iterdir(), key=_natural_key)
    except PermissionError as exc:
        raise ImageFolderError(_PERMISSION_HINT) from exc
    except OSError as exc:
        raise ImageFolderError(f"无法读取文件夹：{exc.strerror or exc}") from exc


def scan_folder(folder: str | Path) -> FolderScan:
    if not str(folder).strip():
        raise ImageFolderError("请选择图片文件夹。")
    path = Path(str(folder)).expanduser()
    if not path.is_dir():
        raise ImageFolderError(f"文件夹不存在：{path}")
    images: list[Path] = []
    skipped: list[str] = []
    prompt_file = None
    for entry in _sorted_entries(path):
        try:
            if entry.name.startswith(".") or not entry.is_file():
                continue
        except OSError:
            continue
        suffix = entry.suffix.lower()
        if entry.name.lower() in PROMPT_FILE_NAMES:
            prompt_file = entry
        elif suffix in config.UPLOADABLE_SUFFIXES or suffix in config.CONVERTIBLE_SUFFIXES:
            images.append(entry)
        elif suffix not in (".txt", ".md", ".json"):
            skipped.append(entry.name)
    return FolderScan(folder=path.resolve(), images=images, skipped=skipped, prompt_file=prompt_file)


def find_task_folders(parent: str | Path) -> list[FolderScan]:
    """Batch import: every sub-folder with images becomes one task."""

    root = Path(str(parent)).expanduser()
    if not str(parent).strip():
        raise ImageFolderError("请选择总文件夹。")
    if not root.is_dir():
        raise ImageFolderError(f"文件夹不存在：{root}")
    scans = []
    for child in _sorted_entries(root):
        if child.is_dir() and not child.name.startswith("."):
            scan = scan_folder(child)
            if scan.images or scan.prompt_file:
                scans.append(scan)
    return scans


def prepare_for_upload(images: list[Path], work_dir: Path) -> list[Path]:
    """Return paths Doubao accepts, converting HEIC/oversized files to JPEG."""

    if len(images) > config.MAX_REFERENCE_IMAGES:
        raise ImageFolderError(f"豆包最多支持 {config.MAX_REFERENCE_IMAGES} 张参考图，这个文件夹里有 {len(images)} 张。")
    ready: list[Path] = []
    for index, image in enumerate(images):
        try:
            size = image.stat().st_size
        except FileNotFoundError as exc:
            raise ImageFolderError(f"图片不存在（可能被移动或删除了）：{image}") from exc
        except OSError as exc:
            raise ImageFolderError(f"无法读取图片 {image.name}：{exc.strerror or exc}") from exc
        suffix = image.suffix.lower()
        too_big = size > config.MAX_UPLOAD_BYTES
        if suffix in config.UPLOADABLE_SUFFIXES and not too_big:
            ready.append(image)
            continue
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / f"{index + 1:02d}_{image.stem}.jpg"
        _convert_to_jpeg(image, target, max_side=4096 if not too_big else 3072)
        ready.append(target)
    return ready


def _sips() -> str:
    return shutil.which("sips") or "/usr/bin/sips"


def _dimensions(source: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run([_sips(), "-g", "pixelWidth", "-g", "pixelHeight", str(source)], capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    width = re.search(r"pixelWidth:\s*(\d+)", out)
    height = re.search(r"pixelHeight:\s*(\d+)", out)
    return (int(width.group(1)), int(height.group(1))) if width and height else None


def _convert_to_jpeg(source: Path, target: Path, max_side: int, quality: int = 90) -> None:
    command = [_sips(), "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    dims = _dimensions(source)
    if dims is None or max(dims) > max_side:
        command += ["-Z", str(max_side)]  # only shrink, never upscale
    command += [str(source), "--out", str(target)]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=180, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ImageFolderError(f"图片 {source.name} 太大，转换超时。请先缩小或换成 JPG/PNG。") from exc
    except OSError as exc:
        raise ImageFolderError(f"无法转换图片 {source.name}：{exc.strerror or exc}") from exc
    if completed.returncode != 0 or not target.exists():
        raise ImageFolderError(f"无法转换图片 {source.name}：{completed.stderr.decode(errors='replace')[:200]}")
    if target.stat().st_size > config.MAX_UPLOAD_BYTES and max_side > 1600:
        _convert_to_jpeg(source, target, max_side=max_side // 2, quality=85)


def thumbnail_for(source: Path, size: int = 360) -> Path:
    """Small JPEG preview for formats a browser cannot show (HEIC, TIFF…) or big files."""

    stat = source.stat()
    if source.suffix.lower() in BROWSER_FRIENDLY and stat.st_size <= 3 * 1024 * 1024:
        return source
    key = hashlib.sha1(f"{source}:{stat.st_mtime_ns}:{stat.st_size}:{size}".encode()).hexdigest()
    target = config.CACHE_ROOT / "thumbs" / f"{key}.jpg"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        _convert_to_jpeg(source, target, max_side=size, quality=80)
    return target


def cleanup_task_cache(task_id: str) -> None:
    shutil.rmtree(config.CACHE_ROOT / task_id, ignore_errors=True)


def cleanup_orphan_caches(known_task_ids: set[str]) -> None:
    root = config.CACHE_ROOT
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name not in known_task_ids and child.name not in ("thumbs", "covers"):
            shutil.rmtree(child, ignore_errors=True)
