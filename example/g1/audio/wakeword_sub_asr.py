#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import re
import json
import argparse
from typing import List, Optional, Union, Tuple

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_ as DDS_String

DEFAULT_CONF_THRESHOLD_FINAL   = 0.20   # final 置信度阈值
DEFAULT_CONF_THRESHOLD_PARTIAL = 0.15   # partial 置信度阈值（稍低，提升灵敏度）
DEFAULT_SESSION_TIMEOUT        = 5.0    # 会话超时回睡
DEFAULT_DEBOUNCE_COOLDOWN      = 0.8    # 去抖（两次触发最小间隔）
DEFAULT_REQUIRE_FINAL          = True   # 默认等待 final，除非 --accept-partials
DEFAULT_IGNORE_WHEN_PLAYING    = True   # 播放时屏蔽（防自激活）

DEFAULT_WINDOW_MS        = 1200        # 滑窗时长：把最近 1.2s 的文本合并
DEFAULT_NEED_PARTIAL_HITS = 2          # 至少 N 次 partial 命中才触发
DEFAULT_ACCEPT_PARTIALS   = False       # 不接受 partial 触发，除非指定 --accept-partials

DEFAULT_FRONT_CENTER = 90
DEFAULT_FRONT_TOLER  = 60              # 90±60 视为前方
DEFAULT_FRONT_BOOST  = 0.05            # 前方命中时阈值降低 0.05

ENABLE_LED_FEEDBACK   = True
ENABLE_TTS_FEEDBACK   = True
WAKE_TTS_TEXT         = "我在，请说。"

ASR_TOPIC  = "rt/audio_msg"
PLAY_TOPIC = "rt/audio_play_state"

DEBUG      = True
LOG_PREFIX = "[WAKE]"

HOMOPHONE_SET = "元远圆员原园缘源羲玄宣轩旋渲"
WAKE_BASES = [
    "小",  # 前缀
]
WAKE_SUFFIXES = [
    "你好", "", "在吗", "在不在"
]
# 预编译的正则（精确/宽松）
REGEX_PATTERNS = [
    re.compile(r"(你好|喂|哈喽)?小[" + HOMOPHONE_SET + r"]", re.IGNORECASE),
    re.compile(r"小[" + HOMOPHONE_SET + r"]你好", re.IGNORECASE),
    re.compile(r"你好小[" + HOMOPHONE_SET + r"]", re.IGNORECASE),
    re.compile(r"唤醒", re.IGNORECASE),
]

FUZZY_TARGETS: List[str] = []
for h in HOMOPHONE_SET:
    base = "小" + h
    FUZZY_TARGETS.append(base)
    for suf in WAKE_SUFFIXES:
        if suf:
            FUZZY_TARGETS.append(base + suf)


def _normalize_text(s: str) -> str:
    s = (s or "").strip()
    return re.sub(r"[\s，。、“”‘’！？,.!?；;：:（）()\[\]{}]", "", s)


def levenshtein_distance(a: str, b: str, max_dist: int = 1) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(
                prev[j] + 1,       # 删除
                cur[j - 1] + 1,    # 插入
                prev[j - 1] + cost # 替换
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return row_min
        prev = cur
    return prev[-1]


def fuzzy_contains(text: str, targets: List[str], max_dist: int = 1) -> bool:
    if not text:
        return False
    text = _normalize_text(text)
    for t in targets:
        t = _normalize_text(t)
        lt = len(t)
        if lt == 0 or lt > len(text) + max_dist:
            continue
        if t in text:
            return True
        for i in range(0, max(1, len(text) - lt + 1)):
            seg = text[i:i + lt]
            if levenshtein_distance(seg, t, max_dist) <= max_dist:
                return True
    return False


class WakeDialogManager:
    def __init__(self,
                 audio_client: AudioClient,
                 loco_client: Optional[LocoClient] = None,
                 conf_final: float = DEFAULT_CONF_THRESHOLD_FINAL,
                 conf_partial: float = DEFAULT_CONF_THRESHOLD_PARTIAL,
                 session_timeout: float = DEFAULT_SESSION_TIMEOUT,
                 debounce_cooldown: float = DEFAULT_DEBOUNCE_COOLDOWN,
                 require_final: bool = DEFAULT_REQUIRE_FINAL,
                 ignore_when_playing: bool = DEFAULT_IGNORE_WHEN_PLAYING,
                 accept_partials: bool = DEFAULT_ACCEPT_PARTIALS,
                 window_ms: int = DEFAULT_WINDOW_MS,
                 need_partial_hits: int = DEFAULT_NEED_PARTIAL_HITS,
                 front_center: int = DEFAULT_FRONT_CENTER,
                 front_toler: int = DEFAULT_FRONT_TOLER,
                 front_boost: float = DEFAULT_FRONT_BOOST):

        self.audio_client = audio_client
        self.loco_client  = loco_client

        self.awake = False
        self.last_heard_time = 0.0
        self.last_trigger_ts = 0.0
        self.play_state = 0

        self.conf_final = conf_final
        self.conf_partial = conf_partial
        self.session_timeout = session_timeout
        self.debounce_cooldown = debounce_cooldown
        self.require_final = require_final
        self.ignore_when_playing = ignore_when_playing
        self.accept_partials = accept_partials

        self.window_ms = window_ms
        self.need_partial_hits = max(1, need_partial_hits)
        self.partial_buffer: List[Tuple[float, str]] = []  # [(ts, text)]
        self.partial_hits = 0

        self.front_center = front_center
        self.front_toler  = front_toler
        self.front_boost  = front_boost

        self._patterns = REGEX_PATTERNS

    def set_play_state(self, playing: Union[int, bool]):
        self.play_state = 1 if playing else 0
        if DEBUG:
            print(f"{LOG_PREFIX} play_state={self.play_state}")

    def _threshold_for(self, is_final: bool, angle: Optional[int]) -> float:
        base = self.conf_final if is_final else self.conf_partial
        if angle is not None:
            try:
                angle = int(angle)
                if abs(angle - self.front_center) <= self.front_toler:
                    base = max(0.0, base - self.front_boost)
            except Exception:
                pass
        return base

    def _matches_wake(self, text: str, confidence: float) -> bool:
        if not text:
            return False
        norm = _normalize_text(text)
        for pat in self._patterns:
            if pat.search(norm):
                return True
        return fuzzy_contains(norm, FUZZY_TARGETS, max_dist=1)

    def _feed_partial(self, text: str):
        now = time.time()
        self.partial_buffer.append((now, text))
        cutoff = now - self.window_ms / 1000.0
        self.partial_buffer = [(ts, t) for ts, t in self.partial_buffer if ts >= cutoff]

    def _merged_recent_text(self) -> str:
        return "".join(t for _, t in self.partial_buffer)

    def _reset_partial_state(self):
        self.partial_buffer.clear()
        self.partial_hits = 0

    def on_asr_json(self, js: dict):
        text = js.get("text", "") or ""
        conf = float(js.get("confidence", 0.0) or 0.0)
        is_final = bool(js.get("is_final", True))
        angle = js.get("angle")
        spkid = js.get("speaker_id")

        if DEBUG:
            print(f"{LOG_PREFIX} ASR text='{text}' conf={conf:.2f} final={is_final} angle={angle} spk={spkid}")

        if self.ignore_when_playing and self.play_state == 1:
            if DEBUG:
                print(f"{LOG_PREFIX} ignore because playing")
            return

        need_conf = self._threshold_for(is_final, angle)
        if conf < need_conf:
            if not is_final and self.accept_partials:
                self._feed_partial(text)
            return

        now = time.time()

        matched = self._matches_wake(text, conf)

        if is_final:
            if self.require_final or not self.accept_partials:
                if not matched:
                    return
                if now - self.last_trigger_ts < self.debounce_cooldown:
                    return
                self._reset_partial_state()
                self._trigger_wake(text, conf, angle, spkid)
                return
            else:
                if matched and (now - self.last_trigger_ts >= self.debounce_cooldown):
                    self._reset_partial_state()
                    self._trigger_wake(text, conf, angle, spkid)
                return
        else:
            if not self.accept_partials:
                self._feed_partial(text)
                return

            self._feed_partial(text)
            merged = self._merged_recent_text()
            if self._matches_wake(merged, conf):
                self.partial_hits += 1
                if DEBUG:
                    print(f"{LOG_PREFIX} partial hit {self.partial_hits}/{self.need_partial_hits} (window {self.window_ms}ms)")
                if self.partial_hits >= self.need_partial_hits and (now - self.last_trigger_ts >= self.debounce_cooldown):
                    self._reset_partial_state()
                    self._trigger_wake(merged, conf, angle, spkid)

    def _trigger_wake(self, text: str, conf: float, angle, spkid):
        self.last_trigger_ts = time.time()
        self.awake = True
        self.last_heard_time = self.last_trigger_ts

        try:
            if ENABLE_LED_FEEDBACK:
                self.audio_client.LedControl(0, 255, 0)
            if ENABLE_TTS_FEEDBACK:
                self.audio_client.TtsMaker(WAKE_TTS_TEXT, 0)
        except Exception as e:
            print(f"{LOG_PREFIX} feedback fail: {e}")

        print(f"🟢 唤醒！text='{text}' conf={conf:.2f} angle={angle} spk={spkid}")

    def tick(self):
        if self.awake and (time.time() - self.last_heard_time) > self.session_timeout:
            self._sleep()

    def _sleep(self):
        self.awake = False
        try:
            self.audio_client.LedControl(0, 0, 0)
        except Exception:
            pass
        print("[SLEEP] session timeout; go to sleep.")


def _get_dds_string_raw(msg: DDS_String) -> str:
    try:
        if hasattr(msg, "data"):
            attr = getattr(msg, "data")
            val = attr() if callable(attr) else attr
        elif hasattr(msg, "value"):
            val = getattr(msg, "value")
        elif hasattr(msg, "string"):
            val = getattr(msg, "string")
        else:
            return ""
        if isinstance(val, bytes):
            return val.decode("utf-8", "ignore")
        if isinstance(val, str):
            return val
        return str(val)
    except Exception:
        return ""


def _safe_parse_dds_string(msg: DDS_String) -> dict:
    raw = _get_dds_string_raw(msg)
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        if DEBUG:
            head = (raw[:160] + "...") if len(raw) > 160 else raw
            print(f"{LOG_PREFIX} JSON parse error: {e}; raw={repr(head)}")
        return {}


def main():
    ap = argparse.ArgumentParser(description="Unitree ASR Wakeword (High-Sensitivity)")
    ap.add_argument("net_if", help="network interface, e.g., eth0")

    ap.add_argument("--conf-final", type=float, default=DEFAULT_CONF_THRESHOLD_FINAL,
                    help="confidence threshold for final segments (default: 0.20)")
    ap.add_argument("--conf-partial", type=float, default=DEFAULT_CONF_THRESHOLD_PARTIAL,
                    help="confidence threshold for partial segments (default: 0.15)")
    ap.add_argument("--accept-partials", action="store_true", default=DEFAULT_ACCEPT_PARTIALS,
                    help="allow wake on partial segments")
    ap.add_argument("--need-partial-hits", type=int, default=DEFAULT_NEED_PARTIAL_HITS,
                    help="min partial hits in window to trigger (default: 2)")
    ap.add_argument("--window-ms", type=int, default=DEFAULT_WINDOW_MS,
                    help="merge window for partial text (ms)")

    ap.add_argument("--front-center", type=int, default=DEFAULT_FRONT_CENTER,
                    help="front center angle (default: 90)")
    ap.add_argument("--front-toler", type=int, default=DEFAULT_FRONT_TOLER,
                    help="front tolerance (+/-) (default: 60)")
    ap.add_argument("--front-boost", type=float, default=DEFAULT_FRONT_BOOST,
                    help="threshold relaxation when speaking from front (default: 0.05)")

    ap.add_argument("--session-timeout", type=float, default=DEFAULT_SESSION_TIMEOUT,
                    help="session timeout seconds (default: 5.0)")
    ap.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_COOLDOWN,
                    help="debounce cooldown seconds (default: 0.8)")

    ap.add_argument("--no-play-state", action="store_true",
                    help="do not subscribe rt/audio_play_state")
    ap.add_argument("--require-final", action="store_true", default=DEFAULT_REQUIRE_FINAL,
                    help="require final segments to trigger (default: True)")

    args = ap.parse_args()

    ChannelFactoryInitialize(0, args.net_if)

    audio_client = AudioClient();  audio_client.SetTimeout(10.0); audio_client.Init()
    loco_client  = LocoClient();   loco_client.SetTimeout(10.0);  loco_client.Init()

    mgr = WakeDialogManager(
        audio_client, loco_client,
        conf_final=args.conf_final,
        conf_partial=args.conf_partial,
        session_timeout=args.session_timeout,
        debounce_cooldown=args.debounce,
        require_final=args.require_final,
        ignore_when_playing=DEFAULT_IGNORE_WHEN_PLAYING,
        accept_partials=args.accept_partials,
        window_ms=args.window_ms,
        need_partial_hits=args.need_partial_hits,
        front_center=args.front_center,
        front_toler=args.front_toler,
        front_boost=args.front_boost
    )

    sub_asr = ChannelSubscriber(ASR_TOPIC, DDS_String)
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

    print(f"{LOG_PREFIX} ✅ Wakeword service started. "
          f"(accept_partials={mgr.accept_partials}, partial_hits={mgr.need_partial_hits}, "
          f"conf_final={mgr.conf_final}, conf_partial={mgr.conf_partial})")

    try:
        while True:
            mgr.tick()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print(f"{LOG_PREFIX} exit.")


if __name__ == "__main__":
    main()