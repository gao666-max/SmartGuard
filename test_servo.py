"""
串口快速测试 — 发 P30 控制舵机
"""
import serial
import time

PORT = "COM13"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=1)
time.sleep(0.5)

# 读启动消息
resp = ser.readline().decode().strip()
print(f"收到: {resp}")

# 测试舵机
for angle in [30, 90, 150]:
    ser.write(f"P{angle}\n".encode())
    time.sleep(0.3)
    resp = ser.readline().decode().strip()
    print(f"P{angle} → {resp}")
    time.sleep(1)

ser.close()
print("测试完成")
