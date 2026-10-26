"""Turn raw Doubao conversation messages into a generation state.

The Doubao web client stores every assistant reply as a JSON list of blocks:

* ``10000`` text block   -> ``{"text_block": {"text": "..."}}``
* ``10040`` thinking     -> ``{"thinking_block": {"finish_title": "视频生成已提交", ...}}``
* ``2074``  creation     -> ``{"creation_block": {"creations": [{"video": {...}}]}}``

A video round normally looks like::

    user:      生成视频：<prompt>，9:16，30s
    assistant: <plan> 视频生成参数确认 ... 确认后我再开始生成视频。      -> NEEDS_CONFIRM
    user:      确认，直接生成
    assistant: <thinking: 视频生成已提交> 预计等待 15 分钟 ...           -> GENERATING
    assistant: 你的视频生成好了。 <creation_block video status=3>       -> SUCCESS

or ends with a refusal / quota / rate-limit message instead.

Doubao echoes the user's prompt in its replies, so a prompt that mentions
"排队", "失败" or "额度不足" must not flip the state. The rules therefore:

1. Blocks are read in order and split at the submission marker (a finished
   thinking block titled "…已提交", or a creation block).
2. Before submission, a parameter-confirmation message always wins.
3. After submission, only explicit, non-conditional failure sentences that
   come *after* the marker count; anything else means "still generating".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

USER = 1
ASSISTANT = 2

BLOCK_TEXT = 10000
BLOCK_THINKING = 10040
BLOCK_CREATION = 2074

VIDEO_STATUS_SUCCESS = 3

WAITING = "waiting"
NEEDS_CONFIRM = "needs_confirm"
GENERATING = "generating"
SUCCESS = "success"
FAILED = "failed"

FAIL_QUOTA = "quota"
FAIL_RATE_LIMIT = "rate_limit"
FAIL_POLICY = "policy"
FAIL_GENERATION = "generation"

_STOP = r"[^。！!？?\n]"

_QUOTA_RE = re.compile(
    rf"(?:额度|次数|配额|生成机会){_STOP}{{0,8}}?(?:已经?|都)?(?:用完|用尽|耗尽|用光|不足|不够)|"
    rf"已(?:经)?达(?:到)?{_STOP}{{0,10}}?(?:上限|限额)|"
    r"明[天日](?:再)?(?:恢复|再来|再试)|下周[一二三四五六日天]?再来|"
    rf"(?:购买|加购){_STOP}{{0,6}}?额度"
)
_RATE_RE = re.compile(
    r"(?:操作|请求|访问|提交|使用|发送)(?:过于|太过|太|较为|过度)?频繁|请求(?:过多|太多)|"
    r"排队(?:的)?人数(?:过多|太多)|人数过多|(?:系统|服务|服务器)繁忙"
)
_POLICY_RE = re.compile(
    rf"不符合{_STOP}{{0,12}}?(?:规范|规定|政策|准则|社区)|违反{_STOP}{{0,10}}?(?:规范|规定|政策|法律|准则)|"
    r"审核未通过|未通过审核|"
    r"(?:无法|没法|没办法|不能|不可以|暂不支持|不支持)[^。！!？?\n，,]{0,10}?(?:生成|创作|制作)(?!好)|生成不了"
)
_GEN_FAIL_RE = re.compile(
    r"生成失败|未能(?:成功)?生成|生成(?:出现|遇到)(?:了)?(?:异常|错误|问题)|生成出错|任务失败|"
    r"网络(?:异常|错误)|生成(?:已)?(?:被)?(?:中断|取消)"
)
_GENERATING_RE = re.compile(
    r"预计(?:等待|需要|耗时|还需)|生成好后|生成完成后|会主动(?:发送|通知)|"
    r"已(?:经)?(?:提交|开始生成)|正在为你生成|开始为你生成|视频生成中"
)
_CONDITIONAL_RE = re.compile(r"如果|若是|若|假如|万一|如遇|如有|如出现|一旦")
_ETA_RE = re.compile(
    r"(?:预计|大约|大概|约|还需|还要|需要)[^。！!？?\n\d]{0,8}?"
    r"(\d+(?:\.\d+)?)(?:\s*[-~～至到]\s*(\d+(?:\.\d+)?))?\s*(小时|分钟|分)"
    r"(?:\s*(\d+)\s*(?:分钟|分))?"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！!？?\n；;])")


@dataclass
class VideoResult:
    vid: str
    status: int | None
    download_url: str = ""
    cover_url: str = ""
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass
class Assessment:
    state: str
    texts: list[str] = field(default_factory=list)
    videos: list[VideoResult] = field(default_factory=list)
    fail_kind: str = ""
    reason: str = ""
    eta_minutes: float | None = None
    finished: bool = True
    submitted: bool = False
    last_user_index: int = -1
    last_assistant_index: int = -1

    @property
    def reply_text(self) -> str:
        return "\n\n".join(t for t in self.texts if t.strip()).strip()


def as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def message_index(message: dict[str, Any]) -> int:
    return as_int(message.get("index_in_conv"))


def parse_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("content")
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    if not isinstance(raw, str) or not raw:
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return [{"block_type": BLOCK_TEXT, "content": {"text_block": {"text": raw}}, "is_finish": True}]
    if isinstance(decoded, list):
        return [b for b in decoded if isinstance(b, dict)]
    if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
        return [{"block_type": BLOCK_TEXT, "content": {"text_block": {"text": decoded["text"]}}, "is_finish": True}]
    return []


def user_text(message: dict[str, Any]) -> str:
    raw = message.get("content")
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw or "")
    if isinstance(decoded, dict):
        return str(decoded.get("text") or "")
    return ""


def _video_from_creation(creation: dict[str, Any]) -> VideoResult | None:
    video = creation.get("video")
    if not isinstance(video, dict) or not video.get("vid"):
        return None
    cover = video.get("cover") or {}
    preview = (cover.get("image_preview") or cover.get("image_ori") or cover.get("image_thumb") or {}) if isinstance(cover, dict) else {}
    duration = video.get("duration")
    width, height = _real_resolution(video)
    return VideoResult(
        vid=str(video.get("vid")),
        status=as_int(video.get("status"), default=-1) if video.get("status") is not None else None,
        download_url=str(video.get("download_url") or ""),
        cover_url=str(preview.get("url") or "") if isinstance(preview, dict) else "",
        width=width or as_int(video.get("width"), default=0) or None,
        height=height or as_int(video.get("height"), default=0) or None,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
    )


def _real_resolution(video: dict[str, Any]) -> tuple[int, int]:
    """``video.width/height`` describe the thumbnail; the stream sizes live in ``video_model``."""

    model = video.get("video_model")
    try:
        model = json.loads(model) if isinstance(model, str) else model
    except ValueError:
        return 0, 0
    best = (0, 0)
    for item in ((model or {}).get("video_list") or {}).values() if isinstance(model, dict) else []:
        if isinstance(item, dict):
            w, h = as_int(item.get("vwidth"), 0), as_int(item.get("vheight"), 0)
            if w * h > best[0] * best[1]:
                best = (w, h)
    return best


def _creation_failed(creation: dict[str, Any]) -> str:
    for source in (creation, creation.get("video") if isinstance(creation.get("video"), dict) else {}):
        for key in ("fail_reason", "fail_msg", "error_msg", "err_msg", "failed_reason"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def parse_eta_minutes(text: str) -> float | None:
    for match in _ETA_RE.finditer(text or ""):
        low, high, unit, extra = match.groups()
        amount = float(high or low)
        minutes = amount * 60 if unit == "小时" else amount
        if extra:
            minutes += float(extra)
        if minutes > 0:
            return minutes
    return None


def looks_like_confirmation(text: str) -> bool:
    if "参数确认" in text:
        return True
    asks = ("确认后" in text) or ("是否确认" in text) or ("请确认" in text) or ("确认一下" in text) or ("是否开始" in text)
    return asks and "生成" in text


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _explicit(regex: re.Pattern[str], text: str) -> str:
    """First non-conditional sentence matching ``regex`` (\"\" if none)."""

    for sentence in _sentences(text):
        match = regex.search(sentence)
        if match and not _CONDITIONAL_RE.search(sentence[: match.start()]):
            return sentence.strip()
    return ""


def _dedupe_videos(videos: list[VideoResult]) -> list[VideoResult]:
    best: dict[str, VideoResult] = {}
    order: list[str] = []
    for video in videos:
        if video.vid not in best:
            order.append(video.vid)
            best[video.vid] = video
        elif video.status == VIDEO_STATUS_SUCCESS or best[video.vid].status != VIDEO_STATUS_SUCCESS:
            best[video.vid] = video
    return [best[v] for v in order]


def assess(messages: Iterable[dict[str, Any]], after_index: int | None = None) -> Assessment:
    """Assess the assistant replies that follow the newest user message."""

    ordered = sorted((m for m in messages if isinstance(m, dict)), key=message_index)
    last_user = -1
    for message in ordered:
        if as_int(message.get("user_type")) == USER:
            last_user = max(last_user, message_index(message))
    anchor = last_user if after_index is None else max(after_index, last_user)

    replies = [m for m in ordered if as_int(m.get("user_type")) == ASSISTANT and message_index(m) > anchor]
    result = Assessment(state=WAITING, last_user_index=last_user)
    if not replies:
        return result
    result.last_assistant_index = message_index(replies[-1])

    # Flatten blocks in order: ("text", str) | ("submit", "") | ("fail", reason) | ("video", VideoResult)
    seq: list[tuple[str, Any]] = []
    finished = True
    for message in replies:
        for block in parse_blocks(message):
            done = block.get("is_finish") is not False
            if not done:
                finished = False
            block_type = as_int(block.get("block_type"))
            content = block.get("content") if isinstance(block.get("content"), dict) else {}
            if block_type == BLOCK_TEXT or "text_block" in content:
                text = str((content.get("text_block") or {}).get("text") or "").strip()
                if text:
                    seq.append(("text", text))
            elif block_type == BLOCK_THINKING or "thinking_block" in content:
                thinking = content.get("thinking_block") or {}
                title = str(thinking.get("finish_title") or "")
                if done and "失败" in title:
                    seq.append(("fail", title))
                elif done and "已提交" in title:
                    seq.append(("submit", ""))
            elif block_type == BLOCK_CREATION or "creation_block" in content:
                for creation in (content.get("creation_block") or {}).get("creations") or []:
                    if not isinstance(creation, dict):
                        continue
                    failure = _creation_failed(creation)
                    if failure:
                        seq.append(("fail", failure))
                    video = _video_from_creation(creation)
                    if video:
                        seq.append(("video", video))

    result.finished = finished
    result.texts = [v for k, v in seq if k == "text"]
    videos = _dedupe_videos([v for k, v in seq if k == "video"])
    result.videos = videos

    submit_pos = next((i for i, (k, _) in enumerate(seq) if k in ("submit", "video")), None)
    result.submitted = submit_pos is not None
    pre = "\n".join(v for k, v in (seq[:submit_pos] if submit_pos is not None else seq) if k == "text")
    post = "\n".join(v for k, v in seq[submit_pos + 1:] if k == "text") if submit_pos is not None else ""
    joined = "\n".join(result.texts)
    result.eta_minutes = parse_eta_minutes(post) or parse_eta_minutes(pre if result.submitted else joined)

    done_videos = [v for v in videos if v.status == VIDEO_STATUS_SUCCESS]
    if done_videos:
        result.state = SUCCESS
        result.videos = done_videos
        return result

    failure = next((v for k, v in seq if k == "fail"), "")
    if failure:
        return _failed(result, FAIL_GENERATION, failure)

    if result.submitted:
        # Only explicit failures reported after the submission marker count.
        for kind, regex in ((FAIL_QUOTA, _QUOTA_RE), (FAIL_RATE_LIMIT, _RATE_RE), (FAIL_GENERATION, _GEN_FAIL_RE)):
            sentence = _explicit(regex, post)
            if sentence and finished:
                return _failed(result, kind, _summary([post]))
        result.state = GENERATING
        return result

    if not finished:
        return result  # still streaming, nothing decided yet

    if looks_like_confirmation(joined):
        result.state = NEEDS_CONFIRM
        return result

    for kind, regex in ((FAIL_QUOTA, _QUOTA_RE), (FAIL_RATE_LIMIT, _RATE_RE), (FAIL_POLICY, _POLICY_RE), (FAIL_GENERATION, _GEN_FAIL_RE)):
        if _explicit(regex, joined):
            return _failed(result, kind, _summary(result.texts))

    if _explicit(_GENERATING_RE, joined):
        result.state = GENERATING
        return result

    if joined.strip():
        result.state = NEEDS_CONFIRM
    return result


def _failed(result: Assessment, kind: str, reason: str) -> Assessment:
    result.state = FAILED
    result.fail_kind = kind
    result.reason = reason
    return result


def _summary(texts: list[str], limit: int = 300) -> str:
    text = "\n".join(t for t in texts if t).strip()
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strip_markdown(text: str) -> str:
    return _summary([text], limit=100000)
