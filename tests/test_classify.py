from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import classify  # noqa: E402


def user(index: int, text: str) -> dict:
    return {"message_id": f"u{index}", "user_type": 1, "content_type": 1, "index_in_conv": str(index), "content": json.dumps({"text": text}, ensure_ascii=False)}


def user_images(index: int) -> dict:
    return {"message_id": f"e{index}", "user_type": 1, "content_type": 20, "index_in_conv": str(index), "content": json.dumps({"entities": [{"entity_type": 2}]})}


def bot(index: int, *blocks: dict) -> dict:
    return {"message_id": f"a{index}", "user_type": 2, "content_type": 9999, "index_in_conv": str(index), "content": json.dumps(list(blocks), ensure_ascii=False)}


def text(t: str, finished: bool = True) -> dict:
    return {"block_type": 10000, "content": {"text_block": {"text": t}}, "is_finish": finished}


def thinking(finish: str = "视频生成已提交", streaming: str = "视频生成中", finished: bool = True) -> dict:
    return {"block_type": 10040, "content": {"thinking_block": {"finish_title": finish, "streaming_title": streaming}}, "is_finish": finished}


def creation(status: int = 3, vid: str = "v0269cg") -> dict:
    return {
        "block_type": 2074,
        "content": {"creation_block": {"creations": [{"type": 2, "video": {
            "vid": vid, "status": status, "duration": 30.049, "width": 384, "height": 682,
            "download_url": "https://example.invalid/video.mp4",
            "cover": {"image_preview": {"url": "https://example.invalid/cover.jpg"}},
        }}]}},
        "is_finish": True,
    }


def confirm(description: str = "镜头缓慢推近山谷。", extra: str = "") -> str:
    return (
        f"我将基于参考图生成一条竖屏视频：{description}\n\n**视频生成参数确认**\n\n"
        f"- **模型：** Seedance 2.5\n- **时长：** 30 秒\n- **比例：** 9:16 竖屏\n{extra}\n确认后我再开始生成视频。"
    )


SUBMITTED = "\n本次使用 **Seedance 2.5** 生成，预计等待 15 分钟。视频生成好后，我会主动发送给你。本次生成将消耗每日免费额度。"
BASE = [user_images(1), user(2, "生成视频：镜头缓慢推近山谷，9:16，30s")]
AFTER_CONFIRM = BASE + [bot(4, text(confirm())), user(7, "确认，直接生成")]


def state(msgs):
    a = classify.assess(msgs)
    return a.state, a.fail_kind


# ------------------------------------------------------------------ basics

def test_waiting_when_no_reply():
    assert classify.assess(BASE).state == classify.WAITING


def test_confirmation_needs_continue():
    a = classify.assess(BASE + [bot(4, text(confirm()))])
    assert a.state == classify.NEEDS_CONFIRM
    assert a.last_user_index == 2


def test_confirmation_streaming_is_waiting():
    assert classify.assess(BASE + [bot(4, text(confirm()[:20], finished=False))]).state == classify.WAITING


def test_generating_after_continue():
    a = classify.assess(AFTER_CONFIRM + [bot(9, text("我将为你生成一条 30 秒竖屏视频。"), thinking(), text(SUBMITTED))])
    assert a.state == classify.GENERATING
    assert a.eta_minutes == 15
    assert a.submitted


def test_generating_detected_from_thinking_block_while_streaming():
    msgs = AFTER_CONFIRM + [bot(9, text("我将为你生成一条视频。"), thinking(), text("本次使用", finished=False))]
    assert classify.assess(msgs).state == classify.GENERATING


def test_success_with_video():
    msgs = AFTER_CONFIRM + [
        bot(9, text("我将为你生成。"), thinking(), text("预计等待 15 分钟。")),
        bot(12, text("你的视频生成好了。"), creation()),
    ]
    a = classify.assess(msgs)
    assert a.state == classify.SUCCESS
    assert [v.vid for v in a.videos] == ["v0269cg"]
    assert a.videos[0].cover_url.endswith("cover.jpg")
    assert a.videos[0].duration == 30.049


def test_same_video_in_two_blocks_is_downloaded_once():
    msgs = AFTER_CONFIRM + [bot(9, thinking(), creation(status=3)), bot(12, text("你的视频生成好了。"), creation(status=3))]
    assert [v.vid for v in classify.assess(msgs).videos] == ["v0269cg"]


def test_unfinished_video_keeps_generating():
    msgs = AFTER_CONFIRM + [bot(9, thinking(), text("预计等待 15 分钟。")), bot(12, creation(status=1))]
    assert classify.assess(msgs).state == classify.GENERATING


def test_only_replies_after_latest_user_message_count():
    msgs = BASE + [bot(4, text("当前操作过于频繁，请稍后再试。")), user(7, "确认，直接生成")]
    assert classify.assess(msgs).state == classify.WAITING


def test_plain_question_needs_confirm():
    assert classify.assess(BASE + [bot(4, text("需要我把镜头改成从左往右移动吗？"))]).state == classify.NEEDS_CONFIRM


def test_raw_string_content_is_text():
    msg = {"user_type": 2, "index_in_conv": "4", "content": "纯文本回复"}
    assert classify.parse_blocks(msg)[0]["content"]["text_block"]["text"] == "纯文本回复"


# ------------------------------------------------------------------ failures

def test_quota_failure():
    msgs = AFTER_CONFIRM + [bot(9, text("近 7 天额度已用完，预计明天恢复。或加购创作额度包，可按需使用，继续生成图片和视频。\n\n[购买创作额度包](https://www.doubao.com/member/quota-package?c=1)"))]
    a = classify.assess(msgs)
    assert (a.state, a.fail_kind) == (classify.FAILED, classify.FAIL_QUOTA)
    assert "购买创作额度包" in a.reason and "https://" not in a.reason


@pytest.mark.parametrize("reply", [
    "抱歉，你今日的视频生成额度已经用完了，请明日再来。",
    "你本周的视频生成次数已经用完了，下周一再来吧。",
    "抱歉，你已达到今日视频生成上限，请明天再试。",
    "你的创作额度已经耗尽，可以购买会员获得更多次数。",
    "今天的视频生成次数用光了，明天再来吧~",
])
def test_quota_phrasings(reply):
    assert state(AFTER_CONFIRM + [bot(9, text(reply))]) == (classify.FAILED, classify.FAIL_QUOTA)


@pytest.mark.parametrize("reply", [
    "很抱歉，你要求的这个剧情包含不雅的内容，不符合内容规范，我不能按照这个脚本生成视频，请更换其他创意。",
    "你的这个需求不符合内容规范，我没办法帮你生成这条视频，请更换健康积极的创作想法。",
    "抱歉，我暂时无法为您生成该视频，请换一个创意试试。",
    "抱歉，这个创意涉及真实人物，我不能直接生成，建议你修改描述。",
    "抱歉，这个内容我没法帮你生成，换个想法吧。",
    "抱歉，该内容存在风险，暂不支持生成。",
    "抱歉，我无法完成这个视频的生成，请更换其他创意。",
    "很抱歉，这个需求不太合适，我无法继续生成，请换一个主题。",
    "抱歉，这个请求可能包含不适宜的内容，我不能为您创作。",
])
def test_policy_refusals(reply):
    assert state(AFTER_CONFIRM + [bot(9, text(reply))]) == (classify.FAILED, classify.FAIL_POLICY)


def test_generation_failure_after_submit():
    msgs = AFTER_CONFIRM + [bot(9, thinking(), text("预计等待 15 分钟。")), bot(12, text("抱歉，视频生成失败了，请稍后再试。"))]
    assert state(msgs) == (classify.FAILED, classify.FAIL_GENERATION)


def test_failure_after_thinking_only_submission():
    msgs = AFTER_CONFIRM + [bot(9, thinking()), bot(12, text("抱歉，视频生成失败了，请稍后再试。"))]
    assert state(msgs) == (classify.FAILED, classify.FAIL_GENERATION)


def test_failed_thinking_title():
    msgs = AFTER_CONFIRM + [bot(9, thinking(finish="视频生成失败"))]
    assert state(msgs) == (classify.FAILED, classify.FAIL_GENERATION)


@pytest.mark.parametrize("reply", ["当前操作过于频繁，请稍后再试。", "当前排队人数过多，请稍后再试。", "视频生成排队的人太多了，服务繁忙，请稍后再试。"])
def test_rate_limit(reply):
    assert state(BASE + [bot(4, text(reply))]) == (classify.FAILED, classify.FAIL_RATE_LIMIT)
    assert state(AFTER_CONFIRM + [bot(9, text(reply))]) == (classify.FAILED, classify.FAIL_RATE_LIMIT)


def test_creation_failure_reason():
    block = creation(status=4)
    block["content"]["creation_block"]["creations"][0]["fail_reason"] = "视频生成失败"
    a = classify.assess(AFTER_CONFIRM + [bot(9, block)])
    assert a.state == classify.FAILED
    assert a.reason == "视频生成失败"


# ------------------------------------------------------------------ prompt echoes must not flip the state

@pytest.mark.parametrize("description", [
    "交警拦下一辆违规停车的汽车。",
    "一位穿着不雅的小丑在街头表演。",
    "厨师正在制作披萨，面粉飞扬。",
    "清晨的奶茶店门口，年轻人在排队买奶茶。",
    "画家正在渲染夜景。",
    "AI 机器人正在生成一幅画。",
    "特工潜入大楼，任务失败后从楼顶撤离。",
    "屏幕上弹出“网络错误”的提示，程序员抓狂。",
    "演员说：服务异常，请稍后再试。",
    "花店里，店员说今天的花卖完了，明天再恢复供应。",
    "游戏里显示金币额度不足，主角很沮丧。",
    "主角说自己操作太频繁被系统提醒。",
])
def test_confirmation_with_echoed_prompt(description):
    assert classify.assess(BASE + [bot(4, text(confirm(description)))]).state == classify.NEEDS_CONFIRM


@pytest.mark.parametrize("extra", ["- 预计耗时：约 15 分钟\n", "- 消耗：创作额度包 1 次\n", "- 时长：30 秒（已达上限）\n"])
def test_confirmation_template_lines(extra):
    assert classify.assess(BASE + [bot(4, text(confirm(extra=extra)))]).state == classify.NEEDS_CONFIRM


def test_confirmation_mentioning_delivery():
    reply = "我将为你生成。\n\n**视频生成参数确认**\n\n确认后我再开始生成视频，生成好后我会主动发送给你。"
    assert classify.assess(BASE + [bot(4, text(reply))]).state == classify.NEEDS_CONFIRM


def test_planning_thinking_block_is_not_submission():
    msgs = BASE + [bot(4, thinking(finish="已完成视频脚本", streaming="视频脚本生成中"), text(confirm()))]
    assert classify.assess(msgs).state == classify.NEEDS_CONFIRM


@pytest.mark.parametrize("tail", [
    "如果效果不符合你的要求，可以随时告诉我调整。",
    "当前使用人数较多，生成时间可能会延长，请耐心等待。",
    "如果生成失败，不会扣除额度。",
])
def test_submitted_round_ignores_conditional_or_benign_notes(tail):
    msgs = AFTER_CONFIRM + [bot(9, text("我将为你生成一条 30 秒竖屏视频：交警拦下一辆违规停车的汽车。"), thinking(), text("预计等待 15 分钟。视频生成好后，我会主动发送给你。" + tail))]
    assert classify.assess(msgs).state == classify.GENERATING


def test_progress_note_after_submission_keeps_generating():
    msgs = AFTER_CONFIRM + [bot(9, thinking(), text("预计等待 15 分钟。")), bot(12, text("当前使用人数较多，你的视频还在生成中，请耐心等待。"))]
    assert classify.assess(msgs).state == classify.GENERATING


# ------------------------------------------------------------------ ETA

@pytest.mark.parametrize("sentence, minutes", [
    ("预计等待 15 分钟", 15),
    ("预计等待 1 小时 30 分钟", 90),
    ("预计等待 15~20 分钟", 20),
    ("预计等待 15-20 分钟", 20),
    ("预计耗时：约 15 分钟", 15),
    ("预计需要大约 20 分钟", 20),
    ("预计等待时间约 15 分钟", 15),
    ("预计还需 15 分钟", 15),
    ("预计等待 1 小时", 60),
])
def test_parse_eta(sentence, minutes):
    assert classify.parse_eta_minutes(sentence) == minutes


def test_parse_eta_ignores_video_length_in_seconds():
    assert classify.parse_eta_minutes("马上就好") is None
    assert classify.parse_eta_minutes("预计 30 秒的视频将在 15 分钟后生成好") != 0.5


def test_real_resolution_from_video_model():
    block = creation()
    model = {"video_list": {"video_1": {"vwidth": 720, "vheight": 1280}, "video_2": {"vwidth": 360, "vheight": 640}}}
    block["content"]["creation_block"]["creations"][0]["video"]["video_model"] = json.dumps(model)
    v = classify.assess(AFTER_CONFIRM + [bot(9, thinking(), block)]).videos[0]
    assert (v.width, v.height) == (720, 1280)
