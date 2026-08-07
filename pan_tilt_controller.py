"""
云台串口控制模块 — pan_tilt_controller.py
协议：
    发 "P<角度>" → 水平舵机转到指定角度 (0-180)
    发 "T<角度>" → 俯仰舵机转到指定角度 (0-90)
    STM32 回 "OK\r\n" 表示执行完成

如果 STM32 没连接，自动使用模拟模式（控制台打印）。
"""

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    serial = None
    HAS_SERIAL = False

import time


class PanTiltController:
    def __init__(self, port=None, baudrate=115200):
        self.ser = None
        self.simulated = True
        if port:
            self.connect(port, baudrate)

    def connect(self, port, baudrate=115200):
        """连接指定串口"""
        if not HAS_SERIAL:
            print("⚠ pyserial 未安装，使用模拟模式")
            return
        try:
            self.ser = serial.Serial(port, baudrate, timeout=1)
            time.sleep(0.5)
            self.simulated = False
            print(f"✅ 云台已连接: {port} ({baudrate})")
        except Exception as e:
            print(f"⚠ 云台连接失败({e})，使用模拟模式")

    def auto_connect(self):
        """自动搜索 STM32 串口（通过USB描述符匹配）"""
        if not HAS_SERIAL:
            print("⚠ pyserial 未安装，使用模拟模式")
            return False
        ports = serial.tools.list_ports.comports()
        for p in ports:
            # STM32 的 USB 描述符通常包含 "STMicroelectronics" 或 "USB Serial"
            if "STM" in str(p.description) or "USB" in str(p.description) or "Serial" in str(p.description):
                try:
                    self.connect(p.device, 115200)
                    return True
                except Exception:
                    continue
        print("⚠ 未找到 STM32 设备，使用模拟模式")
        return False

    def _send(self, cmd):
        if self.simulated:
            return True
        try:
            self.ser.write(f"{cmd}\n".encode())
            resp = self.ser.readline().decode().strip()
            return resp == "OK"
        except Exception as e:
            print(f"⚠ 串口通信错误: {e}")
            self.simulated = True
            return True  # 不回退仍继续

    def set_pan(self, angle):
        """水平旋转 (0-180度)"""
        angle = max(0, min(180, int(angle)))
        ok = self._send(f"P{angle}")
        if self.simulated:
            print(f"  [模拟] 舵机 → {angle}°")
        return ok

    def goto_region(self, angle):
        """转向指定区域（水平角度）"""
        self.set_pan(angle)

    def reset(self):
        """复位到初始位置"""
        self.set_pan(90)

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()


# ========== 测试 ==========
if __name__ == "__main__":
    p = PanTiltController()
    p.auto_connect()
    for angle in [30, 70, 110, 150]:
        print(f"转到 {angle}°")
        p.set_pan(angle)
        time.sleep(1)
    print("复位")
    p.reset()
    p.close()
