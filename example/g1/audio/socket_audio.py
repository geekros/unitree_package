#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import base64
import asyncio
import json
import numpy as np
import websockets
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from wav import play_stream_open, play_stream_write, play_stream_close

# ===== 可选：提高设备播放音量（0~100）=====
DEVICE_VOLUME_BOOT = 90

try:
    from scipy.signal import resample_poly
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False

WS_URL = "ws://127.0.0.1:20800/signaling/connect"

ORIG_SAMPLE_RATE   = 24000
TARGET_SAMPLE_RATE = 16000

FRAME_SAMPLES_16K = 320                      # 20ms@16k
FRAME_BYTES_16K   = FRAME_SAMPLES_16K * 2    # 640B

PLAY_CHUNK_FRAMES = 16
PLAY_CHUNK_BYTES  = PLAY_CHUNK_FRAMES * FRAME_BYTES_16K

MIN_ENQUEUE_FRAMES = 6
MIN_ENQUEUE_BYTES  = MIN_ENQUEUE_FRAMES * FRAME_BYTES_16K

RS_BLOCK_MS          = 80
RS_BLOCK_SAMPLES_24K = ORIG_SAMPLE_RATE * RS_BLOCK_MS // 1000   # 1920
RS_BLOCK_BYTES_24K   = RS_BLOCK_SAMPLES_24K * 2                 # 3840B

EOU_TIMEOUT_S      = 0.20     # 200ms 无新音频 → 认为一轮结束
EOU_SILENCE_FRAMES = 4        # 轮间短静音 ≈80ms
EOU_SILENCE_BYTES  = EOU_SILENCE_FRAMES * FRAME_BYTES_16K

TAIL_SILENCE_FRAMES = 24      # ≈480ms
TAIL_SILENCE_BYTES  = TAIL_SILENCE_FRAMES * FRAME_BYTES_16K
TAIL_SLEEP_S        = 0.35

DEVICE_WRITE_CHUNK_BYTES = 640 * 16

AGC_TARGET_RMS   = 8000.0   # 目标 RMS（推荐 7000~10000）
AGC_MIN_GAIN     = 0.5      # 最小增益
AGC_MAX_GAIN     = 8.0      # 最大增益（防过放大）
AGC_ATTACK_MS    = 10.0     # 增益上升速度（ms，小=更快）
AGC_RELEASE_MS   = 120.0    # 增益下降速度（ms，大=更稳）
LIMITER_THRESH   = 0.98     # 软限幅开始比例（相对 32767）
LIMITER_KNEE     = 0.5      # 软限幅曲线柔和度（0~1）

class AGCState:
    def __init__(self, sr=TARGET_SAMPLE_RATE):
        self.gain = 1.0
        attack_tc  = AGC_ATTACK_MS / 1000.0
        release_tc = AGC_RELEASE_MS / 1000.0
        self.attack_alpha  = np.exp(-0.02 / max(1e-6, attack_tc))
        self.release_alpha = np.exp(-0.02 / max(1e-6, release_tc))

    def process(self, x_int16: np.ndarray) -> np.ndarray:
        if x_int16.size == 0:
            return x_int16
        x = x_int16.astype(np.float32)

        rms = float(np.sqrt(np.mean(x * x)) + 1e-6)
        desired = AGC_TARGET_RMS / rms
        desired = float(np.clip(desired, AGC_MIN_GAIN, AGC_MAX_GAIN))

        if desired > self.gain:
            self.gain = self.attack_alpha * self.gain + (1 - self.attack_alpha) * desired
        else:
            self.gain = self.release_alpha * self.gain + (1 - self.release_alpha) * desired

        y = x * self.gain

        peak = np.max(np.abs(y)) + 1e-6
        limit = LIMITER_THRESH * 32767.0
        if peak > limit:
            over = peak / limit
            comp = 1.0 / (over ** (1.0 - LIMITER_KNEE))
            y *= comp

        y = np.clip(y, -32768.0, 32767.0).astype(np.int16)
        return y

_agc_state = AGCState()


# ---------- 重采样：24k → 16k ----------
def resample_24k_to_16k_int16(pcm24_int16: np.ndarray) -> np.ndarray:
    if pcm24_int16.size == 0:
        return pcm24_int16
    x = pcm24_int16.astype(np.float32)
    if HAS_SCIPY:
        y = resample_poly(x, 2, 3)  # 24k -> 16k
    else:
        ratio = TARGET_SAMPLE_RATE / ORIG_SAMPLE_RATE  # 2/3
        out_len = int(round(len(x) * ratio))
        if out_len <= 1:
            return np.zeros(0, dtype=np.int16)
        src_pos = np.linspace(0, len(x) - 1, out_len, dtype=np.float32)
        i0 = np.floor(src_pos).astype(np.int32)
        i1 = np.clip(i0 + 1, 0, len(x) - 1)
        f  = src_pos - i0
        y  = (1.0 - f) * x[i0] + f * x[i1]
    return np.clip(y, -32768, 32767).astype(np.int16)


def align_frames_16k(pcm16_bytes: bytes, pad: bool = False):
    usable = (len(pcm16_bytes) // FRAME_BYTES_16K) * FRAME_BYTES_16K
    head = pcm16_bytes[:usable]
    tail = pcm16_bytes[usable:]
    if pad and tail:
        pad_len = FRAME_BYTES_16K - len(tail)
        head += tail + b"\x00" * pad_len
        tail = b""
    return head, tail


async def audio_receiver(net_interface: str):
    ChannelFactoryInitialize(0, net_interface)
    audioClient = AudioClient()
    audioClient.SetTimeout(10.0)
    audioClient.Init()
    if DEVICE_VOLUME_BOOT is not None:
        try:
            audioClient.SetVolume(int(DEVICE_VOLUME_BOOT))
            print(f"[INFO] 🔊 Set device volume to {DEVICE_VOLUME_BOOT}")
        except Exception as e:
            print(f"[WARN] SetVolume failed: {e}")
    print("[INFO] ✅ AudioClient 初始化完成，等待音频流...")

    play_stream_open(audioClient, "stream")

    in24_buf  = bytearray()
    out16_buf = bytearray()

    async def enqueue_16k(bytes_blob: bytes):
        if not bytes_blob:
            return
        mv = memoryview(bytes_blob)
        while len(mv) > 0:
            send_bytes = min(len(mv), max(PLAY_CHUNK_BYTES, DEVICE_WRITE_CHUNK_BYTES))
            usable = (send_bytes // FRAME_BYTES_16K) * FRAME_BYTES_16K
            if usable == 0:
                break
            chunk = mv[:usable].tobytes()
            mv = mv[usable:]
            play_stream_write(
                audioClient,
                chunk,
                stream_name="stream",
                chunk_size=DEVICE_WRITE_CHUNK_BYTES,
                pace=False,
                sample_rate=16000,
            )

    async def flush_utterance_tail():
        nonlocal out16_buf
        if out16_buf:
            playable, _ = align_frames_16k(bytes(out16_buf), pad=True)
            out16_buf.clear()
            if playable:
                await enqueue_16k(playable)
        if EOU_SILENCE_BYTES > 0:
            await enqueue_16k(b"\x00" * EOU_SILENCE_BYTES)

    async def handle_interrupt():
        nonlocal in24_buf, out16_buf
        in24_buf.clear()
        out16_buf.clear()
        play_stream_close(audioClient, "stream")
        await asyncio.sleep(0.02)
        play_stream_open(audioClient, "stream")
        print("[INFO] ⏹️ 已打断并清空缓冲，重新开流等待下一轮")

    async with websockets.connect(WS_URL) as ws:
        print(f"[INFO] ✅ 已连接 WebSocket：{WS_URL}")

        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=EOU_TIMEOUT_S)
                data = json.loads(message)

                if "control" in data:
                    if data["control"] == "utterance_end":
                        await flush_utterance_tail()
                        continue
                    if data["control"] == "interrupt":
                        await handle_interrupt()
                        continue

                if "audio" not in data:
                    continue

                chunk24 = base64.b64decode(data["audio"])
                in24_buf.extend(chunk24)

                while len(in24_buf) >= RS_BLOCK_BYTES_24K:
                    block_bytes = in24_buf[:RS_BLOCK_BYTES_24K]
                    del in24_buf[:RS_BLOCK_BYTES_24K]
                    block24 = np.frombuffer(block_bytes, dtype=np.int16)

                    block16 = resample_24k_to_16k_int16(block24)
                    block16 = _agc_state.process(block16)

                    out16_buf.extend(block16.tobytes())

                while len(out16_buf) >= MIN_ENQUEUE_BYTES:
                    send_bytes = min(len(out16_buf), PLAY_CHUNK_BYTES)
                    usable = (send_bytes // FRAME_BYTES_16K) * FRAME_BYTES_16K
                    if usable == 0:
                        break
                    out = bytes(out16_buf[:usable])
                    del out16_buf[:usable]
                    await enqueue_16k(out)

            except asyncio.TimeoutError:
                await flush_utterance_tail()
                continue
            except websockets.ConnectionClosed:
                if len(in24_buf) >= 2:
                    tail24 = np.frombuffer(bytes(in24_buf), dtype=np.int16)
                    if tail24.size > 0:
                        tail16 = resample_24k_to_16k_int16(tail24)
                        tail16 = _agc_state.process(tail16)
                        out16_buf.extend(tail16.tobytes())
                    in24_buf.clear()
                await flush_utterance_tail()
                break
            except Exception as e:
                print(f"[ERROR] ❌ 数据处理出错: {e}")

    if TAIL_SILENCE_BYTES > 0:
        play_stream_write(
            audioClient,
            b"\x00" * TAIL_SILENCE_BYTES,
            stream_name="stream",
            chunk_size=DEVICE_WRITE_CHUNK_BYTES,
            pace=False,
            sample_rate=16000,
        )
        await asyncio.sleep(TAIL_SLEEP_S)

    play_stream_close(audioClient, "stream")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <network_interface>")
        sys.exit(1)
    net_interface = sys.argv[1]
    asyncio.run(audio_receiver(net_interface))


if __name__ == "__main__":
    main()
