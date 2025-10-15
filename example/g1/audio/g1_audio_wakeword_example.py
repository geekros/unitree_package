#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wakeword via DDS (Unitree)
- 订阅 rt/audio_msg（std_msgs/String 等价）读取内置 ASR 的 JSON
- 正则唤醒词、置信度阈值、会话去抖/超时
- 可选订阅 rt/audio_play_state，播放中屏蔽识别以防 TTS 自激活
- 不依赖本机麦克风
"""

import sys
import time
import re
import json
import argparse
from typing import List, Optional

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

# Unitree SDK 自带 IDL 类型（无需 ROS2）
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_ as DDS_String

# ========= 可编辑配置 =========
WAKE_WORDS: List[str] = [
    r"(你好|喂|哈喽)?小(元|远|羲)",
    r"小(元|远|羲)你好",
    r"你好小(元|远|羲)",
    r"唤醒",
]

CONFIDENCE_THRESHOLD = 0.20    # 置信度阈值
SESSION_TIMEOUT       = 5.0    # 唤醒后无语音多久回睡（秒）
DEBOUNCE_COOLDOWN     = 1.2    # 连续命中最小间隔（秒）
REQUIRE_FINAL         = True   # 只在 is_final=True 时触发
IGNORE_WHEN_PLAYING   = True   # 播放中屏蔽（需要订阅 rt/audio_play_state）

# 可选联动
ENABLE_LED_FEEDBACK   = True
ENABLE_TTS_FEEDBACK   = True
WAKE_TTS_TEXT         = "我在，请说。"

# 话题名
ASR_TOPIC   = "rt/audio_msg"
PLAY_TOPIC  = "rt/audio_play_state"   # {"play_state": 0|1}

# 调试输出
DEBUG        = True
LOG_PREFIX   = "[WAKE]"


def _normalize_text(s: str) -> str:
    """去除空白和常见标点，利于正则匹配"""
    s = (s or "").strip()
    return re.sub(r"[\s，。、“”‘’！？,.!?；;：:（）()\[\]{}]", "", s)


class WakeDialogManager:
    def __init__(self, audio_client: AudioClient, loco_client: Optional[LocoClient] = None):
        self.audio_client = audio_client
        self.loco_client  = loco_client

        self.awake = False
        self.last_heard_time = 0.0
        self.last_trigger_ts = 0.0
        self.play_state = 0  # 0:停止 1:播放

        # 预编译正则
        self._patterns = []
        for p in WAKE_WORDS:
            try:
                self._patterns.append(re.compile(p, re.IGNORECASE))
            except re.error:
                # 防止非法正则，fallback 精确匹配
                self._patterns.append(re.compile(re.escape(p), re.IGNORECASE))

    # ---------- 播放状态 ----------
    def set_play_state(self, playing: int or bool):
        self.play_state = 1 if playing else 0
        if DEBUG:
            print(f"{LOG_PREFIX} play_state={self.play_state}")

    # ---------- 唤醒匹配 ----------
    def _matches_wake(self, text: str, confidence: float) -> bool:
        if not text:
            return False
        if confidence < CONFIDENCE_THRESHOLD:
            return False
        norm = _normalize_text(text)
        for pat in self._patterns:
            if pat.search(norm):
                return True
        return False

    # ---------- 收到一条 ASR JSON ----------
    def on_asr_json(self, js: dict):
        """
        js = {
            "index": 1,
            "timestamp": 29319303490,
            "text": "你好",
            "angle": 90,
            "speaker_id": 0,
            "sense": "unknown",
            "confidence": 0.95,
            "language": "zh-CN",
            "is_final": true
        }
        """
        text = js.get("text", "") or ""
        conf = float(js.get("confidence", 0.0) or 0.0)
        is_final = bool(js.get("is_final", True))
        angle = js.get("angle")
        spkid = js.get("speaker_id")

        if DEBUG:
            print(f"{LOG_PREFIX} ASR text='{text}' conf={conf:.2f} final={is_final} angle={angle} spk={spkid}")

        # 播放中屏蔽（防自激活）
        if IGNORE_WHEN_PLAYING and self.play_state == 1:
            if DEBUG:
                print(f"{LOG_PREFIX} ignore because playing")
            return

        # 流式时只在 final 上判断
        if REQUIRE_FINAL and not is_final:
            return

        now = time.time()

        if self._matches_wake(text, conf):
            # 去抖：短期内只触发一次
            if now - self.last_trigger_ts < DEBOUNCE_COOLDOWN:
                if DEBUG:
                    print(f"{LOG_PREFIX} debounce, ignore")
                return
            self.last_trigger_ts = now

            self.awake = True
            self.last_heard_time = now

            # 反馈
            try:
                if ENABLE_LED_FEEDBACK:
                    self.audio_client.LedControl(0, 255, 0)  # 绿色
                if ENABLE_TTS_FEEDBACK:
                    self.audio_client.TtsMaker(WAKE_TTS_TEXT, 0)
            except Exception as e:
                print(f"{LOG_PREFIX} feedback fail: {e}")

            print(f"🟢 唤醒！text='{text}' conf={conf:.2f} angle={angle} spk={spkid}")
            return

        if self.awake:
            # 进入对话态的简单示例（可替换为外部 NLU/LLM）
            self.last_heard_time = now
            reply = None
            try:
                if "时间" in text or "几点" in text:
                    reply = "现在是北京时间 " + time.strftime("%H点%M分")
                elif "你的名字" in text or "你是谁" in text:
                    reply = "我是 Unitree 机器人。"
                elif "再见" in text or "退出" in text:
                    reply = "再见。"
                    self._sleep()
                else:
                    # 回声
                    reply = "你说的是：" + text
            finally:
                if reply:
                    try:
                        self.audio_client.TtsMaker(reply, 0)
                    except Exception as e:
                        print(f"{LOG_PREFIX} TTS fail: {e}")

    # ---------- 会话心跳（主循环定期调用） ----------
    def tick(self):
        if self.awake and (time.time() - self.last_heard_time) > SESSION_TIMEOUT:
            self._sleep()

    def _sleep(self):
        self.awake = False
        try:
            self.audio_client.LedControl(0, 0, 0)  # 熄灯
        except Exception:
            pass
        print("[SLEEP] session timeout; go to sleep.")


def _get_dds_string_raw(msg: DDS_String) -> str:
    """
    统一拿出 String 的内容，兼容：
    - msg.data 是属性（字符串或字节串）
    - msg.data() 是方法（个别平台）
    """
    raw = ""
    try:
        # 优先拿属性
        if hasattr(msg, "data"):
            attr = getattr(msg, "data")
            if callable(attr):
                # 某些实现提供 data() 方法
                val = attr()
            else:
                val = attr
        else:
            # 极端兜底：有些类型可能用 .value/.string
            for name in ("value", "string"):
                if hasattr(msg, name):
                    val = getattr(msg, name)
                    break
            else:
                val = ""

        if isinstance(val, bytes):
            raw = val.decode("utf-8", "ignore")
        elif isinstance(val, str):
            raw = val
        else:
            raw = str(val)
    except Exception:
        raw = ""
    return raw


def _safe_parse_dds_string(msg: DDS_String) -> dict:
    """把 DDS String 的内容解析为 dict，容错打印片段；空串直接忽略"""
    raw = _get_dds_string_raw(msg)
    if not raw or not raw.strip():
        # 直接忽略空消息，避免刷屏
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        if DEBUG:
            head = (raw[:160] + "...") if len(raw) > 160 else raw
            print(f"{LOG_PREFIX} JSON parse error: {e}; raw={repr(head)}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Unitree ASR Wakeword (DDS)")
    parser.add_argument("net_if", help="network interface, e.g., eth0")
    parser.add_argument("--no-play-state", action="store_true",
                        help="do not subscribe rt/audio_play_state")
    args = parser.parse_args()

    # 初始化 DDS 通道
    ChannelFactoryInitialize(0, args.net_if)

    # 控制客户端（联动用）
    audio_client = AudioClient();  audio_client.SetTimeout(10.0); audio_client.Init()
    loco_client  = LocoClient();   loco_client.SetTimeout(10.0);  loco_client.Init()

    mgr = WakeDialogManager(audio_client, loco_client)

    # 订阅 ASR 话题
    sub_asr = ChannelSubscriber(ASR_TOPIC, DDS_String)
    # Unitree 的 ChannelSubscriber 支持 Init(callback, queue_depth)
    def _asr_cb(msg: DDS_String):
        js = _safe_parse_dds_string(msg)
        if js:
            if isinstance(js, list):
                for item in js:
                    if isinstance(item, dict):
                        mgr.on_asr_json(item)
            elif isinstance(js, dict):
                mgr.on_asr_json(js)

    sub_asr.Init(_asr_cb, 10)
    print(f"{LOG_PREFIX} ✅ Subscribed: {ASR_TOPIC}")

    # 可选订阅：播放状态
    if not args.no_play_state:
        try:
            sub_play = ChannelSubscriber(PLAY_TOPIC, DDS_String)
            def _play_cb(msg: DDS_String):
                js = _safe_parse_dds_string(msg)
                if isinstance(js, dict) and "play_state" in js:
                    mgr.set_play_state(int(js.get("play_state", 0)))
            sub_play.Init(_play_cb, 10)
            print(f"{LOG_PREFIX} ✅ Subscribed: {PLAY_TOPIC}")
        except Exception as e:
            print(f"{LOG_PREFIX} ⚠️ subscribe {PLAY_TOPIC} failed (ignored): {e}")

    print(f"{LOG_PREFIX} ✅ Wakeword service started. Waiting ASR...")

    try:
        while True:
            mgr.tick()
            time.sleep(0.1)  # 主循环心跳
    except KeyboardInterrupt:
        print(f"{LOG_PREFIX} exit.")


if __name__ == "__main__":
    main()
