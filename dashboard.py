"""
智巡哨兵 · 集成指挥中心 (dashboard_v2.py)
浏览器直接说话 → 模型理解 → 工具调用 → 结果展示 → 语音回复
启动后打开 http://localhost:5000 即可使用
"""

import os
import sys
import cv2
import json
import time
import wave
import base64
import struct
import asyncio
import threading
import numpy as np
from queue import Queue
from flask import Flask, Response, render_template_string, request
from flask_socketio import SocketIO, emit

# 添加当前目录到路径，确保能 import 本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helmet_detector import HelmetDetector
from pan_tilt_controller import PanTiltController

# ============================================================
# Flask + SocketIO
# ============================================================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ============================================================
# 配置
# ============================================================
try:
    from config import API_KEY
except ImportError:
    API_KEY = "sk-你的API_KEY填这里"
WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus"
OUTPUT_RATE = 24000

PAN_TILT_ANGLES = {"主入口": 30, "材料堆放区": 70, "脚手架区域": 110, "钢筋加工区": 150}
ZONES = ["主入口", "材料堆放区", "脚手架区域", "钢筋加工区"]

# ============================================================
# 全局状态（线程安全）
# ============================================================
_state_lock = threading.Lock()
_state = {
    "current_region": None,
    "camera_angle": 90,
    "total_persons": 0,
    "with_helmet": 0,
    "without_helmet": 0,
    "last_command": "",
    "last_transcript": "",
    "system_status": "就绪",
    "ai_online": False,
    "events": [],
    "history": [],  # [{safe: n, warn: n}]
}

# ============================================================
# 硬件模块
# ============================================================
detector = None
pan_tilt = None
camera = None
camera_lock = threading.Lock()


def get_detector():
    global detector
    if detector is None:
        detector = HelmetDetector()
    return detector


def get_pan_tilt():
    global pan_tilt
    if pan_tilt is None:
        pan_tilt = PanTiltController()
        pan_tilt.auto_connect()
    return pan_tilt


def get_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    return camera


def _add_event(kind, text):
    t = time.strftime("%H:%M:%S")
    with _state_lock:
        _state["events"].append({"time": t, "kind": kind, "text": text})
        if len(_state["events"]) > 50:
            _state["events"].pop(0)


def _broadcast_state():
    with _state_lock:
        socketio.emit("state", {
            "current_region": _state["current_region"],
            "camera_angle": _state["camera_angle"],
            "total_persons": _state["total_persons"],
            "with_helmet": _state["with_helmet"],
            "without_helmet": _state["without_helmet"],
            "last_command": _state["last_command"],
            "last_transcript": _state["last_transcript"],
            "system_status": _state["system_status"],
            "ai_online": _state["ai_online"],
            "events": _state["events"][-20:],
            "history": _state["history"][-12:],
        })


# ============================================================
# 工具函数
# ============================================================
def do_move_pan_tilt(region):
    angle = PAN_TILT_ANGLES.get(region, 90)
    pt = get_pan_tilt()
    pt.goto_region(angle)
    with _state_lock:
        _state["current_region"] = region
        _state["camera_angle"] = angle
        _state["system_status"] = f"摄像头转向 {region}"
    _add_event("pan", f"云台转向 {region} · {angle}°")
    _broadcast_state()
    return f"云台已转向{region}，摄像头就位"


def do_detect_helmet():
    cam = get_camera()
    if not cam.isOpened():
        return "摄像头不可用"

    for _ in range(3):
        cam.read()

    ret, frame = cam.read()
    if not ret or frame is None:
        return "摄像头读取失败"

    d = get_detector()
    result = d.detect(frame)
    total = result["total"]
    has = result["with_helmet"]
    no = result["without_helmet"]

    with _state_lock:
        _state["total_persons"] = total
        _state["with_helmet"] = has
        _state["without_helmet"] = no
        _state["history"].append({"safe": has, "warn": no})
        if len(_state["history"]) > 12:
            _state["history"].pop(0)

    if no > 0:
        _add_event("alert", f"检测: {total}人 · 佩戴{has} · 未佩戴{no} ⚠")
    else:
        _add_event("detect", f"检测: {total}人 · 佩戴{has} · 全部合规 ✓")

    _broadcast_state()

    if total == 0:
        return "当前画面中未检测到人员"
    if no == 0:
        return f"检测到{total}人，全部佩戴安全帽，安全"
    return f"检测到{total}人，其中{no}人未佩戴安全帽，需要提醒"


# ============================================================
# Qwen-Audio WebSocket 后台任务
# ============================================================
audio_request_queue = Queue()  # 浏览器发来的音频排在这里


async def _qwen_send_text(ws, text):
    """通过 WebSocket 发送文本消息（用于简单对话测试）"""
    import uuid
    eid = "event_" + uuid.uuid4().hex
    await ws.send(json.dumps({
        "event_id": eid,
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}]
        }
    }))
    await ws.send(json.dumps({
        "event_id": "event_" + uuid.uuid4().hex,
        "type": "response.create",
        "response": {"modalities": ["text", "audio"]}
    }))


async def _qwen_send_audio(ws, audio_b64):
    """通过 WebSocket 发送音频消息"""
    import uuid
    await ws.send(json.dumps({
        "event_id": "event_" + uuid.uuid4().hex,
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio": audio_b64}]
        }
    }))
    await ws.send(json.dumps({
        "event_id": "event_" + uuid.uuid4().hex,
        "type": "response.create",
        "response": {"modalities": ["text", "audio"]}
    }))


async def _qwen_process_response(ws):
    """处理一轮 Qwen-Audio 回复（含 Function Call 循环）"""
    import uuid
    transcript = ""
    audio_chunks = []
    fn_queue = []
    current_fn = None

    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(msg)
        t = data.get("type", "")

        if t in ("response.audio_transcript.delta", "response.text.delta"):
            text = data.get("delta", "")
            transcript += text
            socketio.emit("transcript_delta", {"text": text})

        elif t == "response.audio.delta":
            chunk = data.get("delta", "")
            if chunk:
                audio_chunks.append(base64.b64decode(chunk))

        elif t == "response.output_item.added":
            item = data.get("item", {})
            if item.get("type") == "function_call":
                current_fn = {"call_id": item.get("call_id", ""),
                              "name": item.get("name", ""), "args": ""}
                print(f"[Qwen] Call: {current_fn['name']}")

        elif t == "response.function_call_arguments.delta":
            if current_fn:
                current_fn["args"] += data.get("delta", "")

        elif t == "response.function_call_arguments.done":
            if current_fn:
                if not current_fn["args"]:
                    current_fn["args"] = data.get("arguments", "")
                fn_queue.append(current_fn)
                print(f"[Qwen] Args: {current_fn['args']}")
                current_fn = None

        elif t == "response.done":
            if fn_queue:
                for fn in fn_queue:
                    args_dict = {}
                    try:
                        if fn["args"]:
                            args_dict = json.loads(fn["args"])
                    except json.JSONDecodeError:
                        pass

                    if fn["name"] == "move_pan_tilt":
                        region = args_dict.get("region", "主入口")
                        result = do_move_pan_tilt(region)
                    elif fn["name"] == "detect_helmet":
                        result = do_detect_helmet()
                    else:
                        result = "未知操作"

                    await ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": fn["call_id"],
                            "output": result
                        }
                    }))

                fn_queue = []
                await ws.send(json.dumps({
                    "event_id": "event_" + uuid.uuid4().hex,
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]}
                }))
            else:
                break

        elif t == "error":
            err = data.get("error", {})
            socketio.emit("error", {"message": err.get("message", str(err))})
            break

        elif t == "session.created":
            with _state_lock:
                _state["ai_online"] = True
            _broadcast_state()

    # 保存 + 发送音频
    if audio_chunks:
        audio_data = b"".join(audio_chunks)

        # 保存 wav
        wav_path = os.path.join(os.path.dirname(__file__), "response.wav")
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_RATE)
            wf.writeframes(audio_data)

        # 通过 SocketIO 发给浏览器播放
        socketio.emit("audio_response", {
            "audio": base64.b64encode(audio_data).decode(),
            "sample_rate": OUTPUT_RATE
        })

    if transcript:
        with _state_lock:
            _state["last_transcript"] = transcript
            _state["system_status"] = "就绪"
        _add_event("voice", f"AI: {transcript}")
        socketio.emit("transcript_done", {"text": transcript})
        _broadcast_state()


async def qwen_main_loop():
    """后台主循环：维持 WebSocket → 等待音频 → 处理 → 循环"""
    import websockets as ws_lib
    import uuid

    while True:
        try:
            async with ws_lib.connect(
                WS_URL,
                additional_headers={"Authorization": f"Bearer {API_KEY}"},
                ping_interval=20
            ) as ws:
                print("[Qwen] WebSocket 已连接")

                # 配置会话
                await ws.send(json.dumps({
                    "event_id": "event_" + uuid.uuid4().hex,
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": """你是智巡哨兵——工地安全巡检语音助手。

## 工作流程
你只做巡查这一件事：
1. 用户让你去某个区域检查 → 调用 move_pan_tilt 转到对应区域
2. 调用 detect_helmet 对当前画面进行安全帽检测
3. 用中文播报检测结果

## 区域名称（必须严格按这些名称传参）
- "主入口"
- "材料堆放区"
- "脚手架区域"
- "钢筋加工区"

## 规则
- 始终按 移动→检测→播报 的顺序操作
- 播报简洁：区域名，人数，几人未佩戴
- 如果只是问你是谁或闲聊，直接文本回复不需要调工具""",
                        "tools": [
                            {
                                "type": "function",
                                "name": "move_pan_tilt",
                                "description": "控制摄像头云台转向工地指定区域",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "region": {
                                            "type": "string",
                                            "description": "区域名称",
                                            "enum": ZONES
                                        }
                                    },
                                    "required": ["region"]
                                }
                            },
                            {
                                "type": "function",
                                "name": "detect_helmet",
                                "description": "对当前摄像头画面进行安全帽佩戴检测",
                                "parameters": {"type": "object", "properties": {}}
                            }
                        ],
                        "tool_choice": "auto",
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                        "voice": "longanlingxi",
                        "turn_detection": None
                    }
                }))

                # 主循环：等待音频请求
                while True:
                    req = audio_request_queue.get()  # 阻塞等待
                    if req is None:
                        break

                    audio_b64 = req.get("audio")
                    if audio_b64:
                        await _qwen_send_audio(ws, audio_b64)
                        print("[Qwen] 处理音频...")
                        await _qwen_process_response(ws)

                    text = req.get("text")
                    if text:
                        await _qwen_send_text(ws, text)
                        print("[Qwen] 处理文本...")
                        await _qwen_process_response(ws)

        except Exception as e:
            print(f"[Qwen] 连接断开，5秒后重连: {e}")
            with _state_lock:
                _state["ai_online"] = False
            _broadcast_state()
            await asyncio.sleep(5)


def start_qwen_thread():
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(qwen_main_loop())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ============================================================
# 主动报警线程
# ============================================================
def alert_thread():
    consecutive = 0
    last_alert = 0
    print("[告警] 后台检测线程启动")

    while True:
        time.sleep(0.8)
        cam = get_camera()
        if not cam.isOpened():
            consecutive = 0
            continue

        ret, frame = cam.read()
        if not ret or frame is None:
            continue

        try:
            d = get_detector()
            result = d.detect(frame)
        except Exception:
            continue

        if result["without_helmet"] > 0:
            consecutive += 1
            if consecutive >= 3 and time.time() - last_alert > 5:
                with _state_lock:
                    _state["total_persons"] = result["total"]
                    _state["with_helmet"] = result["with_helmet"]
                    _state["without_helmet"] = result["without_helmet"]
                _add_event("alert", f"⚠ 自动报警！{result['without_helmet']}人未佩戴安全帽")
                _broadcast_state()
                last_alert = time.time()
                consecutive = 0
        else:
            consecutive = 0


def start_alert_thread():
    t = threading.Thread(target=alert_thread, daemon=True)
    t.start()
    return t


# ============================================================
# Flask 路由
# ============================================================
@app.route("/video_feed")
def video_feed():
    def generate():
        while True:
            cam = get_camera()
            with camera_lock:
                ret, frame = cam.read()
            if not ret:
                time.sleep(0.1)
                continue
            _, jpeg = cv2.imencode(".jpg", frame)
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" +
                   jpeg.tobytes() + b"\r\n")
            time.sleep(0.05)
    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/snapshot")
def api_snapshot():
    cam = get_camera()
    with camera_lock:
        ret, frame = cam.read()
    if not ret:
        return {"error": "camera failed"}, 500
    _, jpeg = cv2.imencode(".jpg", frame)
    return {"image": base64.b64encode(jpeg.tobytes()).decode()}


# ============================================================
# SocketIO 事件
# ============================================================
@socketio.on("connect")
def on_connect():
    _broadcast_state()


@socketio.on("voice_audio")
def on_voice_audio(data):
    """浏览器发来录音（base64 PCM16 16kHz mono）"""
    audio_b64 = data.get("audio", "")
    if not audio_b64:
        emit("error", {"message": "音频数据为空"})
        return

    with _state_lock:
        _state["system_status"] = "正在处理语音..."
    _broadcast_state()

    audio_request_queue.put({"audio": audio_b64})


@socketio.on("voice_text")
def on_voice_text(data):
    """备用：文本指令"""
    text = data.get("text", "")
    if not text:
        return

    with _state_lock:
        _state["last_command"] = text
        _state["system_status"] = f"收到指令: {text}"
    _add_event("voice", f"指令: {text}")
    _broadcast_state()

    audio_request_queue.put({"text": text})


# ============================================================
# Dashboard HTML
# ============================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智巡哨兵</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:#0e1620;color:#cdd6e0;font-family:'Microsoft YaHei',sans-serif;
  height:100vh;display:flex;flex-direction:column;overflow:hidden;
}

/* === 顶栏 === */
.top{
  height:44px;background:#131e2a;border-bottom:1px solid #1e3045;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;flex-shrink:0;
}
.top-l{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:700;color:#e8f0f8}
.top-l .icon{font-size:18px}
.top-l .accent{color:#38bdf8}
.top-r{display:flex;align-items:center;gap:16px;font-size:12px}
.top-r .clock{font-family:'Consolas',monospace;font-size:20px;font-weight:700;color:#38bdf8}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.g{background:#22c55e}
.dot.r{background:#ef4444}
.dot.b{background:#38bdf8}

/* === 主体 === */
.main{
  flex:1;display:flex;min-height:0;overflow:hidden;
}

/* === 左侧：摄像头 === */
.cam-panel{
  flex:1;display:flex;flex-direction:column;padding:8px;min-width:0;
}
.cam-box{
  flex:1;position:relative;background:#000;border-radius:6px;
  border:1px solid #1e3045;overflow:hidden;min-height:0;
}
#cam-feed{width:100%;height:100%;object-fit:contain;display:block}
.cam-info{
  position:absolute;bottom:12px;left:16px;
  font-family:'Consolas',monospace;font-size:11px;color:rgba(255,255,255,0.4);pointer-events:none
}
.cam-stats{
  position:absolute;bottom:12px;right:16px;display:flex;gap:20px;pointer-events:none;
}
.cs-n{font-family:'Consolas',monospace;font-size:26px;font-weight:700}
.cs-l{font-size:10px;opacity:0.6}
.cs-item{text-align:center}
.green{color:#22c55e}
.red{color:#ef4444}

/* === 右侧面板 === */
.sidebar{
  width:340px;display:flex;flex-direction:column;padding:8px 8px 8px 0;gap:8px;overflow-y:auto;flex-shrink:0;
}
.sidebar::-webkit-scrollbar{width:4px}
.sidebar::-webkit-scrollbar-thumb{background:#1e3045;border-radius:2px}

.panel{
  background:#131e2a;border:1px solid #1e3045;border-radius:6px;overflow:hidden;
}
.panel-title{
  padding:7px 12px;font-size:11px;color:#5a7d9a;letter-spacing:1px;
  border-bottom:1px solid #1e3045;text-transform:uppercase;
}
.panel-body{padding:10px}

/* === 区域地图 === */
.zone-grid{
  display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:6px;height:200px;
}
.zone-cell{
  border:2px solid #1e3045;border-radius:5px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  font-size:13px;font-weight:500;color:#4e6d8a;cursor:default;
  transition:all 0.3s;text-align:center;
}
.zone-cell .zn{font-size:11px;opacity:0.6;margin-top:2px}
.zone-cell.active{
  border-color:#38bdf8;color:#38bdf8;
  background:rgba(56,189,248,0.06);box-shadow:0 0 12px rgba(56,189,248,0.12);
}
.zone-cell .marker{
  width:8px;height:8px;border-radius:50%;background:#ef4444;margin-top:4px;
  display:none;
}
.zone-cell.active .marker{display:block}

/* === 数字卡 === */
.num-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.num-card{
  background:#131e2a;border:1px solid #1e3045;border-radius:6px;
  padding:12px 8px;text-align:center;
}
.num-card .n{font-family:'Consolas',monospace;font-size:34px;font-weight:700}
.num-card .l{font-size:10px;color:#5a7d9a;margin-top:4px}
.nc-blue .n{color:#38bdf8}
.nc-green .n{color:#22c55e}
.nc-red .n{color:#ef4444}
.nc-red.alert{animation:flash 0.5s infinite alternate}
@keyframes flash{from{box-shadow:0 0 10px rgba(239,68,68,0.2)}to{box-shadow:0 0 24px rgba(239,68,68,0.6)}}

/* === 日志 === */
.log{
  font-family:'Consolas',monospace;font-size:10px;max-height:180px;overflow-y:auto;
}
.log::-webkit-scrollbar{width:3px}
.log::-webkit-scrollbar-thumb{background:#1e3045}
.log .e{display:flex;gap:6px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.03)}
.log .t{color:#4e6d8a;white-space:nowrap}
.log .e.warn .m{color:#fbbf24}
.log .e.danger .m{color:#f87171}
.log .e.info .m{color:#38bdf8}

/* === 底部语音栏 === */
.voice-bar{
  height:72px;background:#131e2a;border-top:2px solid #1e3045;
  display:flex;align-items:center;justify-content:center;
  padding:0 16px;flex-shrink:0;gap:12px;
}
.btn-talk{
  width:320px;height:52px;border:none;border-radius:28px;
  background:linear-gradient(180deg,#1d3a50,#152a3c);
  border:2px solid #38bdf8;color:#e8f0f8;
  font-size:16px;font-weight:700;font-family:inherit;
  cursor:pointer;letter-spacing:2px;user-select:none;
  display:flex;align-items:center;justify-content:center;gap:10px;
  transition:all 0.15s;
}
.btn-talk:hover{background:linear-gradient(180deg,#234a64,#1a3548);box-shadow:0 0 20px rgba(56,189,248,0.2)}
.btn-talk:active{transform:scale(0.97)}
.btn-talk.recording{
  border-color:#ef4444!important;background:linear-gradient(180deg,#4a1a1a,#301212)!important;
  animation:flash 0.7s infinite alternate;
}
.btn-talk .mic{font-size:24px}
.btn-talk .txt{font-size:14px}
.hint{font-size:10px;color:#4e6d8a;text-align:center;margin-top:2px}

/* === 状态文字 === */
#st-text{font-size:12px;color:#5a7d9a}

@media(max-width:800px){
  .sidebar{display:none}
  .btn-talk{width:100%}
}
</style>
</head>
<body>

<!-- 顶栏 -->
<div class="top">
  <div class="top-l">
    <span class="icon">&#x1F6E1;</span>
    智巡<span class="accent">·</span>哨兵
    <span style="font-size:10px;color:#4e6d8a;font-weight:400;margin-left:4px">V1.0</span>
  </div>
  <div class="top-r">
    <span style="color:#5a7d9a">AI</span><span class="dot b" id="d-ai"></span>
    <span style="color:#5a7d9a">检测</span><span class="dot g" id="d-yolo"></span>
    <span id="st-text">就绪</span>
    <span class="clock" id="clock">--:--:--</span>
  </div>
</div>

<!-- 主体 -->
<div class="main">
  <!-- 摄像头 -->
  <div class="cam-panel">
    <div class="cam-box">
      <img id="cam-feed" src="/video_feed">
      <div class="cam-info">CAM-01 · LIVE</div>
      <div class="cam-stats">
        <div class="cs-item"><div class="cs-n green" id="cs-safe">0</div><div class="cs-l">佩戴</div></div>
        <div class="cs-item"><div class="cs-n red" id="cs-warn">0</div><div class="cs-l">未佩戴</div></div>
      </div>
    </div>
  </div>

  <!-- 右侧面板 -->
  <div class="sidebar">
    <!-- 区域地图 -->
    <div class="panel">
      <div class="panel-title">📍 四向云台定位 — 摄像头当前指向（高亮 = 正在巡查）</div>
      <div class="panel-body" style="padding:6px">
        <div class="zone-grid">
          <div class="zone-cell" id="z0">
            &#x1F3E2; 主入口
            <span class="zn">大门 · 车辆通道</span>
            <div class="marker"></div>
          </div>
          <div class="zone-cell" id="z1">
            &#x1F4E6; 材料堆放区
            <span class="zn">钢筋 · 模板 · 管材</span>
            <div class="marker"></div>
          </div>
          <div class="zone-cell" id="z2">
            &#x1F3D7; 脚手架区域
            <span class="zn">高空作业 · 主体结构</span>
            <div class="marker"></div>
          </div>
          <div class="zone-cell" id="z3">
            &#x1F529; 钢筋加工区
            <span class="zn">切割 · 弯曲 · 焊接</span>
            <div class="marker"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 统计 -->
    <div class="num-row">
      <div class="num-card nc-blue">
        <div class="n" id="nc-total">0</div>
        <div class="l">检测人数</div>
      </div>
      <div class="num-card nc-green">
        <div class="n" id="nc-safe">0</div>
        <div class="l">已佩戴</div>
      </div>
      <div class="num-card nc-red" id="nc-warn-card">
        <div class="n" id="nc-warn">0</div>
        <div class="l">未佩戴 ⚠</div>
      </div>
    </div>

    <!-- 日志 -->
    <div class="panel" style="flex:1">
      <div class="panel-title">事件记录</div>
      <div class="panel-body log" id="log" style="padding:4px 10px;">
        <div class="e info"><span class="t">--:--:--</span><span class="m">系统就绪，等待语音指令</span></div>
      </div>
    </div>
  </div>
</div>

<!-- 语音栏 -->
<div class="voice-bar">
  <button class="btn-talk" id="btn-talk">
    <span class="mic">&#x1F399;</span>
    <span class="txt" id="btn-txt">点击或按住 开始说话</span>
  </button>
</div>
<div class="hint">按住按钮说话，松开发送 · 例如："去脚手架区域看看有没有人没戴安全帽"</div>

<script>
// === 时钟 ===
(function tick(){
  var d=new Date();document.getElementById('clock').textContent=d.toTimeString().slice(0,8);setTimeout(tick,1000);
})();

// === SocketIO ===
var socket=io();

socket.on('state',function(s){
  document.getElementById('nc-total').textContent=s.total_persons||0;
  document.getElementById('nc-safe').textContent=s.with_helmet||0;
  document.getElementById('nc-warn').textContent=s.without_helmet||0;
  document.getElementById('cs-safe').textContent=s.with_helmet||0;
  document.getElementById('cs-warn').textContent=s.without_helmet||0;

  // 报警闪烁
  var wc=document.getElementById('nc-warn-card');
  if(s.without_helmet>0)wc.classList.add('alert');
  else wc.classList.remove('alert');

  // 区域高亮
  var zi=['z0','z1','z2','z3'];
  var names=['主入口','材料堆放区','脚手架区域','钢筋加工区'];
  zi.forEach(function(id,i){
    document.getElementById(id).classList.toggle('active',s.current_region===names[i]);
  });

  // 状态灯
  document.getElementById('d-ai').className='dot '+(s.ai_online?'b':'r');
  document.getElementById('d-yolo').className='dot g';
  document.getElementById('st-text').textContent=s.system_status||'就绪';

  // 日志
  if(s.events){
    var log=document.getElementById('log');
    log.innerHTML='';
    s.events.slice(-18).forEach(function(ev){
      var cls=ev.kind==='alert'?'danger':(ev.kind==='pan'?'info':(ev.kind==='detect'?'warn':'info'));
      log.innerHTML+='<div class="e '+cls+'"><span class="t">'+ev.time+'</span><span class="m">'+ev.text+'</span></div>';
    });
    log.scrollTop=log.scrollHeight;
  }
});

socket.on('transcript_delta',function(d){
  document.getElementById('st-text').textContent='AI: '+d.text;
});

socket.on('audio_response',function(d){
  playAudio(d.audio,d.sample_rate);
  document.getElementById('st-text').textContent='就绪';
});

// === 语音录制（全局持久节点，避免 GC 回收） ===
var stream=null,audioCtx=null,rec=false,chunks=[];
var procNode=null,sourceNode=null;
var btn=document.getElementById('btn-talk');
var label=document.getElementById('btn-txt');

navigator.mediaDevices.getUserMedia({audio:{sampleRate:16000,channelCount:1}}).then(function(s){
  stream=s;
  audioCtx=new AudioContext({sampleRate:16000});

  // 创建一次，持久使用
  sourceNode=audioCtx.createMediaStreamSource(stream);
  procNode=audioCtx.createScriptProcessor(4096,1,1);
  procNode.onaudioprocess=function(e){
    if(!rec)return;
    var inp=e.inputBuffer.getChannelData(0);
    var pcm=new Int16Array(inp.length);
    for(var i=0;i<inp.length;i++)pcm[i]=Math.max(-32768,Math.min(32767,Math.round(inp[i]*32767)));
    chunks.push(new Uint8Array(pcm.buffer));
  };
  sourceNode.connect(procNode);
  procNode.connect(audioCtx.destination);

  btn.disabled=false;
  label.textContent='点击或按住 开始说话';
}).catch(function(e){
  btn.disabled=true;
  label.textContent='麦克风被拒绝，请允许浏览器访问麦克风';
});

function startRec(){
  if(!audioCtx||rec)return;
  audioCtx.resume();
  rec=true;chunks=[];
  btn.classList.add('recording');
  label.textContent='正在聆听...';
}

function stopRec(){
  if(!rec)return;
  rec=false;
  btn.classList.remove('recording');
  label.textContent='处理中...';

  if(chunks.length===0){label.textContent='未检测到声音，请重试';return;}

  var total=0;chunks.forEach(function(c){total+=c.length;});
  var merged=new Uint8Array(total),off=0;
  chunks.forEach(function(c){merged.set(c,off);off+=c.length;});

  // 高效 base64 编码
  var bytes=new Uint8Array(merged);
  var binary='';
  for(var i=0;i<bytes.length;i+=4096){
    binary+=String.fromCharCode.apply(null,bytes.subarray(i,Math.min(i+4096,bytes.length)));
  }
  var b64=btoa(binary);

  socket.emit('voice_audio',{audio:b64});
  setTimeout(function(){if(!rec){label.textContent='点击或按住 开始说话';}},2000);
}

btn.addEventListener('mousedown',startRec);
btn.addEventListener('mouseup',stopRec);
btn.addEventListener('mouseleave',stopRec);
btn.addEventListener('touchstart',function(e){e.preventDefault();startRec();});
btn.addEventListener('touchend',stopRec);

// === 播放音频（打断之前的音频） ===
var sharedAudioCtx=null,currentAudioSrc=null;

function playAudio(b64,rate){
  // 立即停止正在播放的音频
  if(currentAudioSrc){
    try{currentAudioSrc.stop();}catch(e){}
    currentAudioSrc=null;
  }
  if(!sharedAudioCtx||sharedAudioCtx.state==='closed'){
    sharedAudioCtx=new AudioContext({sampleRate:rate});
  }

  var bin=atob(b64);var buf=new ArrayBuffer(bin.length);var v=new Uint8Array(buf);
  for(var i=0;i<bin.length;i++)v[i]=bin.charCodeAt(i);
  var pcm=new Int16Array(buf);
  var f=new Float32Array(pcm.length);
  for(var i=0;i<pcm.length;i++)f[i]=pcm[i]/32768;
  var ab=sharedAudioCtx.createBuffer(1,f.length,rate);ab.getChannelData(0).set(f);
  var src=sharedAudioCtx.createBufferSource();src.buffer=ab;src.connect(sharedAudioCtx.destination);
  src.start();
  currentAudioSrc=src;
  src.onended=function(){
    if(currentAudioSrc===src)currentAudioSrc=null;
  };
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("  智巡哨兵 · 集成指挥中心")
    print("  http://localhost:5000")
    print("=" * 50)

    start_qwen_thread()
    start_alert_thread()

    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
