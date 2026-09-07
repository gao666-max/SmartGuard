# 智巡哨兵 · SmartGuard

面向工地安全的语音交互式具身智能巡检系统。

**2026 年天津市第九届大学生信息技术"新工科"工程实践创新技术竞赛** — 自主命题 A 类

> 工人对着系统说一句"去脚手架区域看看有没有人没戴安全帽"，系统就能理解意图、控制摄像头云台转向目标区域、用 YOLO 检测安全帽佩戴情况，再用语音把结果播报出来。

---

## ✨ 功能特性

- 🎙️ **语音交互** — 基于 Qwen-Audio-3.0-Realtime，支持 Function Call，一个模型完成语音识别 + 意图理解 + 语音合成
- 🦺 **安全帽检测** — YOLOv8（SafetyVision 13 类模型），CUDA 加速，区分"已佩戴 / 未佩戴 / 人员"
- 🤖 **具身控制** — 语音指令解析出空间方位词，通过串口控制 STM32 舵机云台转向目标区域
- 🚨 **主动报警** — 后台线程持续检测，连续 3 帧违规自动告警
- 📊 **实时面板** — Flask + SocketIO 监控面板，实时视频流、区域地图、统计数字、事件日志
- 🔌 **硬件可选** — STM32 不在线时自动降级为模拟模式，软件链路完整可跑

---

## 🏗️ 系统架构

```
┌─────────────┐     语音指令      ┌──────────────────────┐
│   用户(工人)  │ ───────────────▶ │  Qwen-Audio-3.0      │
│             │                   │  (语音识别+意图理解)   │
└─────────────┘                   └──────────┬───────────┘
                                             │ Function Call
                              ┌──────────────┼──────────────┐
                              ▼                             ▼
                    ┌──────────────────┐        ┌──────────────────┐
                    │  move_pan_tilt   │        │  detect_helmet   │
                    │  串口→STM32舵机   │        │  YOLOv8 安全帽    │
                    └──────────────────┘        └──────────────────┘
                              │                             │
                              └──────────────┬──────────────┘
                                             ▼
                              ┌──────────────────────────┐
                              │  Qwen-Audio 生成语音回复  │
                              │  播报检测结果             │
                              └──────────────────────────┘
                                             │
                                             ▼
                              ┌──────────────────────────┐
                              │  Flask + SocketIO 面板    │
                              │  视频流·区域地图·统计·日志 │
                              └──────────────────────────┘
```

感知 → 决策 → 执行 → 反馈，完整闭环。

---

## 🧰 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 语音交互 | Qwen-Audio-3.0-Realtime | WebSocket + Function Call |
| 视觉检测 | YOLOv8 + PyTorch | CUDA 加速 |
| 硬件控制 | STM32F103 + SG90 舵机 | 串口 115200 |
| Web 面板 | Flask + Flask-SocketIO | 实时推送 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install flask flask-socketio pyserial pyaudio websockets ultralytics opencv-python numpy
```

### 2. 配置 API Key

```bash
cp config.example.py config.py
```

编辑 `config.py`，填入你的阿里云百炼 API Key（模型：`qwen-audio-3.0-realtime-plus`）。

### 3. 启动

```bash
python dashboard.py
```

浏览器打开 `http://localhost:5000`，按住底部按钮说话即可。

> **模型说明**：默认加载 `safetyvision_model.pt`（安全帽检测专用），缺失时自动回退到 `yolov8n.pt`（通用检测，`ultralytics` 会自动下载）。安全帽模型可从 HuggingFace 获取 `ayushgupta7777/safetyvision-yolov8`。

---

## 📁 项目结构

```
├── dashboard.py            # 集成指挥中心（Flask + 语音 + 检测 + 面板）
├── helmet_detector.py      # YOLO 安全帽检测模块
├── pan_tilt_controller.py  # STM32 云台串口控制（含模拟模式）
├── voice_chat.py           # 独立语音助手（终端版）
├── test_servo.py           # 串口 + 舵机快速测试
├── stm32_firmware/         # STM32 固件（Arduino）
└── config.example.py       # 配置模板
```

---

## 🔌 硬件说明（可选）

软件链路不依赖硬件即可完整运行。STM32 云台是"具身"能力的载体，缺失时 `pan_tilt_controller.py` 会自动进入模拟模式（终端打印舵机角度）。

如需接入真实舵机：

- 舵机信号线 → STM32 `PA0`（水平）
- USB-TTL `RXD` → `A9`、`TXD` → `A10`（交叉）
- 固件烧录：`stm32_firmware/stm32_firmware.ino`

---

## 📄 License

MIT
