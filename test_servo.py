"""
串口快速测试 — 自动搜索串口，发 P 命令控制舵机
"""
import serial
import serial.tools.list_ports
import time

BAUD = 115200

# 自动搜索 STM32 串口
port = None
for p in serial.tools.list_ports.comports():
    desc = str(p.description)
    if "STM" in desc or "USB" in desc or "Serial" in desc or "CH340" in desc or "COM" in desc:
        port = p.device
        print(f"找到串口: {port} ({desc})")
        break

if port is None:
    print("没找到串口，检查 USB-TTL 是否插好、驱动是否装了")
    print("当前电脑上的串口列表：")
    for p in serial.tools.list_ports.comports():
        print(f"  {p.device} - {p.description}")
    exit(1)

ser = serial.Serial(port, BAUD, timeout=1)
time.sleep(1)

# 读启动消息（固件烧录成功的话，STM32 上电会打印 READY）
resp = ser.readline().decode().strip()
print(f"启动消息: {repr(resp)}")

# 测试舵机
for angle in [30, 90, 150]:
    ser.write(f"P{angle}\n".encode())
    time.sleep(0.3)
    resp = ser.readline().decode().strip()
    print(f"P{angle} → {repr(resp)}")
    time.sleep(1)

ser.close()
print("测试完成")
