# 智巡哨兵

面向工地安全的语音交互式具身智能巡检系统。

**2026年天津市第九届大学生信息技术"新工科"工程实践创新技术竞赛** — 自主命题A类

## 核心功能

对着麦克风说话 → 大模型理解意图 → 控制摄像头云台转向 → YOLO安全帽检测 → 语音播报结果

## 技术栈

- **语音交互**: Qwen-Audio-3.0-Realtime (WebSocket, Function Call)
- **视觉检测**: YOLOv8 (SafetyVision), CUDA 加速
- **硬件控制**: STM32F103 + SG90 舵机云台, 串口通信
- **可视化**: Flask + SocketIO Dashboard

## 快速开始

```bash
# 1. 安装依赖
pip install flask flask-socketio pyserial pyaudio websockets ultralytics opencv-python numpy

# 2. 配置 API Key
cp config.example.py config.py
# 编辑 config.py 填入你的百炼 API Key

# 3. 启动
python dashboard.py
# 浏览器打开 http://localhost:5000
```

## 项目结构

```
├── dashboard.py          # 集成指挥中心 (Flask + 语音 + 检测 + UI)
├── helmet_detector.py    # YOLO 安全帽检测模块
├── pan_tilt_controller.py # STM32 云台串口控制
├── voice_chat.py         # 独立语音助手 (终端版)
├── stm32_firmware/       # STM32 固件 (Keil)
└── config.example.py     # 配置模板
```
