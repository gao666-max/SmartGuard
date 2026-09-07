import os
import asyncio
import websockets
import json
import uuid
import base64
import urllib.request
import pyaudio
import wave
import cv2
import numpy as np
import threading
from pynput import keyboard
from helmet_detector import HelmetDetector
from pan_tilt_controller import PanTiltController

# 启动时自动尝试连接 STM32，失败则模拟
_pan_tilt = PanTiltController()
_pan_tilt.auto_connect()

try:
    from config import API_KEY
except ImportError:
    API_KEY = "sk-你的API_KEY填这里"
WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus"

CHUNK = 1024
INPUT_RATE = 16000
OUTPUT_RATE = 24000
FORMAT = pyaudio.paInt16
CHANNELS = 1


def make_event_id():
    return "event_" + uuid.uuid4().hex


# ========== 录音 ==========
recording = False
should_exit = False
interrupted = False
frames = []


def on_press(key):
    global recording, should_exit, interrupted, frames
    try:
        if key == keyboard.Key.esc:
            should_exit = True
            recording = False
            return False
        if key == keyboard.Key.space:
            if not recording:
                recording = True
                frames = []
                print("\n🎤 录音中...（松开空格发送，ESC 退出）")
            else:
                # 正在播放时按空格 = 打断
                interrupted = True
    except AttributeError:
        pass


def on_release(key):
    global recording
    try:
        if key == keyboard.Key.space and recording:
            recording = False
            return False
    except AttributeError:
        pass


def start_interrupt_listener():
    """启动一个后台键盘监听器，用于检测打断（空格键）"""
    global interrupted
    interrupted = False
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    return listener


def wait_for_record():
    """阻塞等待用户按住空格说话，返回 base64 音频。ESC 返回 None"""
    global recording, should_exit, frames
    recording = False
    should_exit = False
    frames = []

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    while not recording and not should_exit:
        import time
        time.sleep(0.02)

    if should_exit:
        listener.stop()
        return None

    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=INPUT_RATE,
                    input=True, frames_per_buffer=CHUNK)
    while recording:
        frames.append(stream.read(CHUNK, exception_on_overflow=False))

    stream.stop_stream()
    stream.close()
    p.terminate()
    listener.stop()

    if should_exit:
        return None

    raw = b"".join(frames)
    duration = len(raw) / (INPUT_RATE * 2)
    print(f"✅ 录音 {duration:.1f} 秒，{len(raw)} 字节")
    return base64.b64encode(raw).decode("utf-8")


# ========== 播放 ==========
def play_audio(pcm_data):
    """播放 PCM16 音频，失败不崩"""
    try:
        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=OUTPUT_RATE,
                        output=True)
        stream.write(pcm_data)
        stream.stop_stream()
        stream.close()
        p.terminate()
    except Exception as e:
        print(f"⚠ 播放失败: {e}，音频已保存到 response.wav")


# ========== Dashboard 事件推送 ==========
DASHBOARD_URL = "http://localhost:5000/api/event"


def _post_event(event_type, **data):
    """向 Dashboard 推送事件（非关键路径，失败不报错）"""
    try:
        payload = json.dumps({"type": event_type, **data}).encode()
        req = urllib.request.Request(DASHBOARD_URL, data=payload,
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass  # Dashboard 没开也不影响语音链路


# ========== 工具函数 ==========

PAN_TILT_ANGLES = {
    "1号区域": 30,
    "2号区域": 70,
    "3号区域": 110,
    "4号区域": 150,
}


def move_pan_tilt(region: str) -> str:
    """控制云台转向指定区域"""
    angle = PAN_TILT_ANGLES.get(region, 90)
    print(f"\n🤖 [执行] 云台转向 {region} (角度: {angle}°)")
    _pan_tilt.goto_region(angle)
    _post_event("pan_tilt", region=region, angle=angle)
    _post_event("voice_command", text=f"巡查{region}")
    return f"云台已转向{region}，摄像头就位"


# ---- 真实 YOLO 检测 ----
_detector = None


def _get_frame():
    """获取一帧图像，优先从 Dashboard 取（避免抢摄像头）"""
    # 方案1：从 Dashboard 截图接口拉
    try:
        req = urllib.request.Request("http://localhost:5000/api/snapshot")
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        img_b64 = data.get("image", "")
        if img_b64:
            jpeg_bytes = base64.b64decode(img_b64)
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        pass

    # 方案2：自己开摄像头
    cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if cam.isOpened():
        for _ in range(5):
            cam.read()
        ret, frame = cam.read()
        cam.release()
        if ret and frame is not None:
            return frame

    return None


def detect_helmet() -> str:
    """抓一帧画面，YOLO 安全帽检测"""
    global _detector
    if _detector is None:
        _detector = HelmetDetector()

    frame = _get_frame()
    if frame is None:
        return "摄像头不可用，无法检测"

    result = _detector.detect(frame)

    total = result["total"]
    has = result["with_helmet"]
    no = result["without_helmet"]

    print(f"\n🤖 [YOLO] {total}人, 佩戴{has}人, 未佩戴{no}人")

    # 推给 Dashboard
    _post_event("detection", result={
        "total": total, "with_helmet": has, "without_helmet": no
    })

    if total == 0:
        return "当前画面中未检测到人员"
    if no == 0:
        return f"检测到{total}人，全部佩戴安全帽，安全"
    else:
        return f"检测到{total}人，其中{no}人未佩戴安全帽，需要提醒"


# ========== 主动报警线程 ==========
_alert_enabled = True
_alert_last_triggered = 0  # 防止反复报警


def alert_loop():
    """后台线程：持续 YOLO 检测，连续 3 帧违规 → 报警"""
    global _detector, _alert_enabled, _alert_last_triggered
    if _detector is None:
        _detector = HelmetDetector()

    consecutive_violations = 0
    print("🛡 主动报警线程已启动（后台运行）")

    while _alert_enabled:
        import time
        time.sleep(0.8)  # 每秒检测一次

        frame = _get_frame()
        if frame is None:
            consecutive_violations = 0
            continue

        try:
            result = _detector.detect(frame)
        except Exception:
            continue

        if result["without_helmet"] > 0:
            consecutive_violations += 1
            if consecutive_violations >= 3 and time.time() - _alert_last_triggered > 5:
                msg = f"⚠ 报警！{result['without_helmet']}人未佩戴安全帽"
                print(f"\n🚨 {msg}")
                _post_event("detection", result={
                    "total": result["total"],
                    "with_helmet": result["with_helmet"],
                    "without_helmet": result["without_helmet"],
                    "alert": True
                })
                _alert_last_triggered = time.time()
                consecutive_violations = 0
        else:
            consecutive_violations = 0


def start_alert_thread():
    t = threading.Thread(target=alert_loop, daemon=True)
    t.start()
    return t


def execute_tool(call_id: str, name: str, arguments: dict) -> dict:
    """执行工具并返回结果消息"""
    if name == "move_pan_tilt":
        result = move_pan_tilt(**arguments)
    elif name == "detect_helmet":
        result = detect_helmet()
    else:
        result = f"未知工具: {name}"
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": result
        }
    }


# ========== 工具定义 ==========
TOOLS = [
    {
        "type": "function",
        "name": "move_pan_tilt",
        "description": "控制摄像头云台转向工地指定区域，传入区域编号如'1号区域'、'2号区域'、'3号区域'、'4号区域'",
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "目标区域编号",
                    "enum": ["1号区域", "2号区域", "3号区域", "4号区域"]
                }
            },
            "required": ["region"]
        }
    },
    {
        "type": "function",
        "name": "detect_helmet",
        "description": "对当前摄像头画面进行安全帽佩戴检测，返回佩戴和未佩戴人数",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
]

# 系统提示词
INSTRUCTIONS = """你是智巡哨兵——工地安全巡检语音助手。

## 工作流程
当用户让你去某个区域检查安全帽时，按以下步骤操作：
1. 先调用 move_pan_tilt 把摄像头转到对应区域
2. 再调用 detect_helmet 检测安全帽
3. 根据检测结果用中文播报

## 区域对照
- 1号区域 = 主入口
- 2号区域 = 材料堆放区
- 3号区域 = 脚手架区域
- 4号区域 = 钢筋加工区

## 规则
- 用户说"看XX区域"就是让你去巡查
- 始终按 移动→检测→播报 的顺序
- 播报要简洁：几号区域，几个人，几个没戴安全帽"""


# ========== 单轮对话 ==========
async def handle_one_turn(ws, audio_b64, cancel_listener):
    """单轮对话。cancel_listener 是后台键盘监听器，按空格设置 interrupted"""
    global interrupted
    interrupted = False

    await ws.send(json.dumps({
        "event_id": make_event_id(),
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio": audio_b64}]
        }
    }))
    await ws.send(json.dumps({
        "event_id": make_event_id(),
        "type": "response.create",
        "response": {"modalities": ["text", "audio"]}
    }))

    transcript = ""
    audio_chunks = []
    fn_queue = []
    current_fn = None

    while True:
        if interrupted:
            interrupted = False
            try:
                await ws.send(json.dumps({"event_id": make_event_id(), "type": "response.cancel"}))
            except Exception:
                pass
            print("\n⏹ 已打断")
            return None

        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
        except asyncio.TimeoutError:
            continue

        data = json.loads(msg)
        t = data.get("type", "")

        if t in ("response.audio_transcript.delta", "response.text.delta"):
            text = data.get("delta", "")
            print(text, end="", flush=True)
            transcript += text

        elif t == "response.audio.delta":
            chunk = data.get("delta", "")
            if chunk:
                audio_chunks.append(base64.b64decode(chunk))

        elif t == "response.output_item.added":
            item = data.get("item", {})
            if item.get("type") == "function_call":
                current_fn = {"call_id": item.get("call_id", ""),
                              "name": item.get("name", ""), "args": ""}
                print(f"\n\U0001f4de 模型调用: {current_fn['name']}")

        elif t == "response.function_call_arguments.delta":
            if current_fn:
                current_fn["args"] += data.get("delta", "")

        elif t == "response.function_call_arguments.done":
            if current_fn:
                if not current_fn["args"]:
                    current_fn["args"] = data.get("arguments", "")
                fn_queue.append(current_fn)
                print(f"   参数: {current_fn['args']}")
                current_fn = None

        elif t == "response.done":
            if fn_queue:
                for fn in fn_queue:
                    try:
                        args_dict = json.loads(fn["args"]) if fn["args"] else {}
                    except json.JSONDecodeError:
                        args_dict = {}
                    result_msg = execute_tool(fn["call_id"], fn["name"], args_dict)
                    await ws.send(json.dumps(result_msg))
                fn_queue = []
                await ws.send(json.dumps({
                    "event_id": make_event_id(),
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]}
                }))
            else:
                break

        elif t == "error":
            err = data.get("error", {})
            print(f"\n� 错误: {err.get('message', data)}")
            break

    if audio_chunks:
        audio_data = b"".join(audio_chunks)
        wav_path = os.path.join(os.path.dirname(__file__), "response.wav")
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_RATE)
            wf.writeframes(audio_data)

        if not interrupted:
            print("\n\U0001f50a 播放中...")
            play_audio(audio_data)
        else:
            interrupted = False
            print("\n⏹ 播放已打断")

    if transcript:
        print(f"\U0001f4dd {transcript}")
        _post_event("transcript", text=transcript)

    return transcript


# ========== 主入口 ==========


async def main():
    print("🚀 智巡哨兵语音助手启动")
    print("=" * 40)
    print("按住 空格键 说话，松开后自动发送")
    print("按 ESC 退出")
    print("=" * 40)

    async with websockets.connect(
        WS_URL,
        additional_headers={"Authorization": f"Bearer {API_KEY}"}
    ) as ws:
        # 一次连接，只发一次 session.update
        await ws.send(json.dumps({
            "event_id": make_event_id(),
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": INSTRUCTIONS,
                "tools": TOOLS,
                "tool_choice": "auto",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "voice": "longanlingxi",
                "turn_detection": None
            }
        }))
        await ws.recv()
        print("✅ 已连接，等待语音指令...\n")

        # 启动后台报警线程
        start_alert_thread()

        # 后台监听器：持续检测打断（空格键）/ 退出（ESC）
        listener = start_interrupt_listener()

        turn_count = 0
        while True:
            if should_exit:
                break

            audio_b64 = wait_for_record()
            if audio_b64 is None:
                print("\n👋 退出")
                break

            turn_count += 1
            print(f"\n--- 第 {turn_count} 轮 ---")
            await handle_one_turn(ws, audio_b64, listener)

            if should_exit:
                print("\n👋 退出")
                break
            print("\n等待下一条指令...\n")

        listener.stop()


if __name__ == "__main__":
    asyncio.run(main())
